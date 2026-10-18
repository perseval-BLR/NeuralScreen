"""Key presses that waited out a stall are dropped, not replayed (#171).

A rebuild holds the main loop for as long as the worker takes to come back -
seconds, 30-70 s with a cold NGX - and nothing reads the command queue
meanwhile. Every press made in that time ran at once afterwards: the
reporter's twenty Num2 presses and one Num1 opened and closed the menu twenty
times and switched NR on and off within 1.5 s.

Checked on the real hotkey thread's type and the real drain:

1. a press older than HOTKEY_STALE_S when the queue is read is dropped, and
   the drop is logged once with how many and what;
2. a fresh press runs;
3. the tray's commands (plain strings, no press time) run whatever their
   age, and so does a stale quit - a quit asked for during a freeze is meant;
4. a press is still the command it names (it compares equal to the string).

Fails before the change: presses carried no time and all of them ran.

Run:  runtime\\python.exe tests\\test_hotkey_stale.py
"""
import contextlib
import io
import os
import queue
import sys
import time
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))


def main() -> int:
    failures = []
    import commands
    from hotkeys import HOTKEY_STALE_S, HotkeyPress

    toggled = []
    real = commands.apply_menu_action
    commands.apply_menu_action = lambda st, action: toggled.append(action)
    alerts = []
    try:
        now = time.monotonic()
        st = types.SimpleNamespace(
            tray_commands=queue.Queue(), running=True, lang="en", cfg={},
            frame_index=0, display=types.SimpleNamespace(alert=alerts.append))
        old = now - HOTKEY_STALE_S - 20.0          # pressed during a long rebuild
        for _ in range(20):
            st.tray_commands.put(HotkeyPress("lens", old))
        st.tray_commands.put("lens")               # the tray's own, untimed
        st.tray_commands.put(HotkeyPress("lens", time.monotonic()))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            running = commands.drain_commands(st)
        if len(toggled) != 2:
            failures.append(f"{len(toggled)} camera toggles ran, not 2 (the tray's "
                            f"and the fresh press)")
        log = out.getvalue()
        if log.count("hotkey press(es) dropped") != 1 or "20 hotkey" not in log \
                or "lens" not in log:
            failures.append(f"the drop was not logged once with its count: {log!r}")
        if not running:
            failures.append("the drain asked to quit")

        st.tray_commands.put(HotkeyPress("quit", old))
        with contextlib.redirect_stdout(io.StringIO()):
            running = commands.drain_commands(st)
        if running or st.running:
            failures.append("a quit pressed during a freeze was dropped")
    finally:
        commands.apply_menu_action = real

    press = HotkeyPress("settings", 1.0)
    if press != "settings" or press.at != 1.0 or not isinstance(press, str):
        failures.append(f"a press is not the command it names: {press!r}")

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
