"""One press of a hotkey must produce exactly one command.

There are two paths to the same command: RegisterHotKey (WM_HOTKEY) and the
polling fallback that exists for games which grab the keyboard. Both ran for
every press, and nothing told the poller that a WM_HOTKEY had already been
delivered - so on the desktop every hotkey fired TWICE. A toggle looked stuck
("Num1 only ever switches NR on"), the menu opened and closed on one press,
and inside a game it all worked, because a game that swallows WM_HOTKEY leaves
the poller as the only path.

The second half of the same bug: a key held down re-fired the command every
cooldown. A press is an edge, not a state.

Third case, found while this test kept failing in the suite: the baseline that
stops a stuck key from firing was taken on the POLLER'S FIRST SAMPLE, so in a
game - where WM_HOTKEY never arrives and the poller is the only path - the
first press after launch was recorded as "already down" and swallowed. The
baseline is taken at registration now, which is before the poller's first tick.

F13 is the test key: nothing else on the machine uses it, and no physical
keyboard sends it by accident.

Run:  runtime\\python.exe test_hotkey_once.py
"""
import ctypes
import queue
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # the project root
sys.path.insert(0, str(BASE))  # the project modules (main.py, display.py, ...)
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)

import hotkeys  # noqa: E402  (the module: the game case patches its user32)
from hotkeys import HotkeyController, MOD_NOREPEAT  # noqa: E402

VK_F13 = 0x7C
KEYEVENTF_KEYUP = 0x0002
COLLECT_S = 1.2


def tap(hold: float = 0.12) -> None:
    """Press and release, holding as long as a person does.

    Not an instant press: while a game that grabs the keyboard has the focus,
    WM_HOTKEY never arrives and the poller - which samples every 30 ms - is
    the only path left. A zero-length synthetic tap falls between two samples
    and is lost, which is a property of the machine, not what this test is
    about. A human press lasts 50-100 ms.
    """
    u = ctypes.windll.user32
    u.keybd_event(VK_F13, 0, 0, 0)
    time.sleep(hold)
    u.keybd_event(VK_F13, 0, KEYEVENTF_KEYUP, 0)


def _collect(q: queue.Queue, seconds: float) -> list:
    got = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            got.append(q.get(timeout=0.05))
        except queue.Empty:
            pass
    return got


# `collect` above is the test's own; this name is what the poller-only case
# below uses so the two read the same.
collect = _collect


def _first_press_in_game() -> str | None:
    """The first press after launch must work where the poller is the only path.

    This is the case the polling fallback exists for: a game holds the
    keyboard, WM_HOTKEY never arrives, and GetAsyncKeyState is all we have.
    The baseline that stops a stuck key from firing used to be taken on the
    poller's FIRST SAMPLE (`was_down = self._poll_down.get(vk, down)`), so a
    press whose whole lifetime fell before that sample was recorded as
    "already down" and swallowed - the very first hotkey press after launch
    did nothing. Measured before the fix: 6 of 6 presses lost this way.

    The baseline now comes from _register(), which runs before the poller's
    first tick, so the same press is a fresh edge.

    The game is stood in for by a registration that SUCCEEDS but never
    delivers - which is what a swallowed keyboard means for us: the key is
    ours, the game just keeps WM_HOTKEY from arriving. (A registration that
    fails is another program holding the key, and the poller leaves those
    alone.) RegisterHotKey is replaced rather than _register: the REAL
    _register must run, otherwise this would test the test's own copy of the
    baseline instead of the product's.

    Returns a failure string, or None when it behaved.
    """
    real_register_hotkey = hotkeys.user32.RegisterHotKey

    def swallowed(*args, **kwargs):
        """Accepted, but no WM_HOTKEY will ever arrive: the poller is the
        only path."""
        return 1

    hotkeys.user32.RegisterHotKey = swallowed
    ctl = None
    try:
        commands: queue.Queue = queue.Queue()
        bindings = {1: (MOD_NOREPEAT, VK_F13, "toggle", "F13")}
        ctl = HotkeyController(commands, bindings)
        ctl.start()
        tap()                       # immediately, as a player would
        got = _collect(commands, COLLECT_S)
        if got != ["toggle"]:
            return (f"the first press in a game produced {len(got)} commands: "
                    f"{got} - the poller took it as the baseline")
        return None
    finally:
        hotkeys.user32.RegisterHotKey = real_register_hotkey
        if ctl is not None:
            ctl.stop()


def main() -> int:
    commands: queue.Queue = queue.Queue()
    bindings = {1: (MOD_NOREPEAT, VK_F13, "toggle", "F13")}
    ctl = HotkeyController(commands, bindings)
    ctl.start()
    if "F13" not in ctl.registered:
        print(f"SKIP: F13 could not be registered (taken by {ctl.failed})")
        ctl.stop()
        return 0

    failures = []
    try:
        # A plain tap.
        tap()
        got = collect(commands, COLLECT_S)
        print(f"one tap        -> {got}")
        if got != ["toggle"]:
            failures.append(f"one tap produced {len(got)} commands: {got}")

        # Held down for longer than the poller's cooldown.
        tap(hold=0.7)
        got = collect(commands, COLLECT_S)
        print(f"held for 0.7 s -> {got}")
        if got != ["toggle"]:
            failures.append(f"holding the key produced {len(got)} commands: {got}")

        # Two deliberate taps are two commands - the fix must not swallow real
        # presses.
        tap()
        time.sleep(0.45)
        tap()
        got = collect(commands, COLLECT_S)
        print(f"two taps       -> {got}")
        if got != ["toggle", "toggle"]:
            failures.append(f"two taps produced {len(got)} commands: {got}")
    finally:
        ctl.stop()

    # The case the poller exists for, and where its baseline used to eat the
    # very first press: a game holds the keyboard, WM_HOTKEY never arrives.
    game = _first_press_in_game()
    print(f"first press in a game -> {'ok' if game is None else game}")
    if game is not None:
        failures.append(game)

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: one press, one command - and a held key does not repeat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
