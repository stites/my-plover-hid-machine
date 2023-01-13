'''
A plover machine plugin for supporting the Plover HID protocol.

This protocol is a simple HID-based protocol that sends the current state
of the steno machine every time that state changes.

See the README for more details on the protocol.

The order of the buttons (from left to right) is the same as in `KEYS_LAYOUT`.
Most buttons have the same names as in GeminiPR, except for the extra buttons
which are called X1-X26.
'''
from plover.machine.base import ThreadedStenotypeBase
from plover import log

from bitstring import BitString
import hid
import platform
from threading import Timer

from plover.system import english_stenotype

# This is a hack to not open the hid device in exclusive mode on
# darwin, if the version of hidapi installed is current enough
if platform.system() == "Darwin":
    import ctypes
    try:
        hid.hidapi.hid_darwin_set_open_exclusive.argtypes = (ctypes.c_int, )
        hid.hidapi.hid_darwin_set_open_exclusive.restype = None
        hid.hidapi.hid_darwin_set_open_exclusive(0)
    except AttributeError as e:
        log.error("hidapi < 0.12 in use, plover-hid will not work correctly")

USAGE_PAGE: int = 0xFF50
USAGE: int = 0x4C56

N_LEVERS: int = 64

# A simple report contains the report id 1 and one bit
# for each of the 64 buttons in the report.
SIMPLE_REPORT_TYPE: int = 0x01
SIMPLE_REPORT_LEN: int = N_LEVERS // 8

class InvalidReport(Exception):
    pass

# let the insanity begin!
MY_ACTIONS = ("-!", "-@", "-$", "-%", "-&", "-+", "-=", "-|")

english_stenotype.KEYS = (*english_stenotype.KEYS, *MY_ACTIONS)

STENO_KEY_CHART = ("S-", "T-", "K-", "P-", "W-", "H-",
                   "R-", "A-", "O-", "*", "-E", "-U",
                   "-F", "-R", "-P", "-B", "-L", "-G",
                   "-T", "-S", "-D", "-Z", "#",
                   "X1", "X2", "X3", "X4", "X5", "X6",
                   "X7", "X8", "X9", "X10", "X11", "X12",
                   "X13", "X14", "X15", "X16", "X17", "X18",
                   "X19", "X20", "X21", "X22", "X23", "X24",
                   "X25", "X26", "X27", "X28", "X29", "X30",
                   "X31", "X32", "X33", "X34", "X35", "X36",
                   "X37", "X38", "X39", "X40", "X41")


class HidMachine(ThreadedStenotypeBase):
    KEYS_LAYOUT: str = '''
        #  #  #  #  #  #  #  #  #  #
        S- T- P- H- *  -F -P -L -T -D
        S- K- W- R- *  -R -B -G -S -Z
              A- O-    -E -U
     X1  X2  X3  X4  X5  X6  X7  X8  X9  X10
     X11 X12 X13 X14 X15 X16 X17 X18 X19 X20
     X21 X22 X23 X24 X25 X26 X27 X28 X29 X30
     X31 X32 X33 X34 X35 X36 X37 X38 X39 X40
     X41
    '''

    def __init__(self, params):
        super().__init__()
        self._params = params
        self._hid = None


    def _parse(self, report):
        # The first byte is the report id, and due to idiosynchrasies
        # in how HID-apis work on different operating system we can't
        # map the report id to the contents in a good way, so we force
        # compliant devices to always use a report id of 0x50 ('P').
        if len(report) > SIMPLE_REPORT_LEN and report[0] == 0x50:
            return BitString(report[1:SIMPLE_REPORT_LEN+1])
        else:
            raise InvalidReport()

    def run(self):
        global MS_DEBOUNCE
        self._ready()
        keystate = BitString(N_LEVERS)

        # NOTE: Here I use a WPM upper bound to compute a debounce. I find that machine-level precision
        # of keystrokes is unforgiving for a beginner, so I compute an appropriate debounce based on an upper bound of
        # how much of a beginner I am. <X> WPM means that I expect not to hit a speed of <X> between strokes -- this is
        # a bound computed between strokes and is only an approximation because there is a mismatch between
        # "strokes-per-minute" and "words-per-minute". Assuming I will not hit 800wpm as a beginner fails to work in the
        # typey-typey one-sylablle multi-stroke lesson, where a 2000 WPM bound seems more natural.
        #
        # Everyone else uses machine precision, so maybe I'm just a clumsy typer -- but as my
        # debounce level is going down, I suspect that this is actually a helpful feature and not a hurtful one.
        #
        # On the whole, WPM is a silly metric (how long is a word?) but corresponds closely to "strokes-per-minute" and
        # it's the most common metric, so you can get a better sense of what this threshold means ("at every moment you
        # never pass <X> WPM").
        #
        # On tuning this number:
        # - if you feel your chords are getting split up, at no fault to you, lower your WPM bound (increase the debounce)
        # - if you feel two chords are "clobbering" into one chord, increase your WPM bound (lower the debounce)
        #
        # Grainular notes:
        # - 800wpm (75ms) works to get from 0-15 WPM on one-syllable words (measured in typey-typey lessons)
        # - 2000wpm (30ms -- current) seems to be important for the one-syllable multi-stroke words (untested on single strokes)
        wpm = 2000
        sec_per_word = 1 / (wpm / 60) # ie: 30 ms
        print(f"[hid-machine] debounce upper-bound:\t{wpm} wpm")
        print(f"[hid-machine] debounce debounce:\t{sec_per_word} s/word")
        print(f"[hid-machine] computed debounce:\t{sec_per_word*1000} ms/word")
        debouncer = None

        def send_to_plover():
            nonlocal keystate, debouncer
            steno_actions = self.keymap.keys_to_actions(
                [STENO_KEY_CHART[i] for (i, x) in enumerate(keystate) if x]
            )
            if steno_actions:
                self._notify(steno_actions)
            keystate = BitString(N_LEVERS)
            debouncer = None

        while not self.finished.wait(0):
            try:
                report = self._hid.read(65536, timeout=1000)
            except hid.HIDException:
                self._error()
                return
            if not report:
                continue
            try:
                report = self._parse(report)
            except InvalidReport:
                continue

            keystate |= report
            if not report:
                debouncer = Timer(sec_per_word, send_to_plover)
                debouncer.start()

    def start_capture(self):
        self.finished.clear()
        self._initializing()
        # Enumerate all hid devices on the machine and if we find one with our
        # usage page and usage we try to connect to it.
        try:
            devices = [
                device["path"]
                for device in hid.enumerate()
                if device["usage_page"] == USAGE_PAGE and device["usage"] == USAGE
            ]
            if not devices:
                self._error()
                return
            # FIXME: if multiple compatible devices are found we should either
            # let the end user configure which one they want, or support reading
            # from all connected plover hid devices at the same time.
            self._hid = hid.Device(path=devices[0])
        except hid.HIDException:
            self._error()
            return
        self.start()

    def stop_capture(self):
        super().stop_capture()
        if self._hid:
            self._hid.close()
            self._hid = None

    @classmethod
    def get_option_info(cls):
        return {}


print("Initialized Plover-HID Machine")
