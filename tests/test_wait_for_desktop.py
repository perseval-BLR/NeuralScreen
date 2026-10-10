r"""A start before the user's desktop is up waits for it (#158).

The Run key starts the program while the session is still coming up. A
reporter's log from that moment: Windows reported one 1524x3264 display
instead of three, Desktop Duplication was refused with E_ACCESSDENIED (the
input still belonged to Winlogon), and DXGI listed the adapters in another
order than an hour later - the pipeline was built for a display that did not
exist, on the card with no monitors. Startup now waits, before anything reads
the monitors or the cards, until the input desktop is "Default" and the
monitor layout reads the same twice; at most three minutes.

Checked on startup.wait_for_desktop with the desktop, the layout, the clock
and the sleep stubbed - no real waiting:
* on the user's desktop (an ordinary launch) it returns at once, no sleep;
* at logon it waits for "Default", then for two equal layout readings, and
  the layout it settles on is the final one, not the one seen at logon;
* a desktop that never comes up is given up on at the limit;
* the real probes answer on this session ("Default", a sane layout).

Run:  runtime\python.exe tests\test_wait_for_desktop.py
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import startup  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = 0

    def sleep(self, s):
        self.now += s
        self.sleeps += 1

    def __call__(self):
        return self.now


def _seq(values):
    it = iter(values)
    last = [None]

    def get():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]
    return get


def main() -> int:
    failures = []
    clock = Clock()
    waited = startup.wait_for_desktop(desktop=lambda: "Default",
                                      layout=lambda: (0, 0, 1, 1, 1),
                                      sleep=clock.sleep, clock=clock)
    if waited or clock.sleeps:
        failures.append(f"an ordinary launch waited ({clock.sleeps} sleeps)")

    clock = Clock()
    logon = (0, 0, 1524, 3264, 1)
    real = (0, 0, 5760, 1080, 3)
    seen = []
    layouts = _seq([logon, real, real])
    waited = startup.wait_for_desktop(
        desktop=_seq([None, None, "Winlogon", "Default"]),
        layout=lambda: seen.append(layouts()) or seen[-1],
        sleep=clock.sleep, clock=clock)
    print(f"    logon start: waited={waited}, {clock.now:.0f} s, layouts seen {seen}")
    if not waited:
        failures.append("a start at logon did not wait")
    if not seen or seen[-1] != real:
        failures.append(f"it settled on {seen[-1] if seen else None}, not the real layout")
    if clock.now > 10:
        failures.append(f"it waited {clock.now:.0f} s for a desktop that was up in a few")

    clock = Clock()
    waited = startup.wait_for_desktop(desktop=lambda: None,
                                      layout=lambda: (0, 0, 1, 1, 1),
                                      sleep=clock.sleep, clock=clock, limit=30.0)
    if not waited or not 30.0 <= clock.now <= 31.0:
        failures.append(f"a desktop that never came up was waited {clock.now:.0f} s, "
                        "not the 30 s limit")

    name = startup.input_desktop_name()
    layout = startup.screen_layout()
    print(f"    this session: desktop {name!r}, layout {layout}")
    if name != "Default":
        failures.append(f"the real probe read the desktop as {name!r}")
    if layout[2] <= 0 or layout[3] <= 0 or layout[4] < 1:
        failures.append(f"the real layout probe read {layout}")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a start before the desktop waits for it; an ordinary launch does not")
    return 0


if __name__ == "__main__":
    sys.exit(main())
