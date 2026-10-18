"""With "Minimise to tray" on, the program starts in the tray (#171).

The reporter's note: hiding the taskbar button right after taskbar.start()
does nothing - the window does not exist yet, and the taskbar thread shows
it a moment later while the log already says it is hidden. So the window is
created hidden instead, and only when the tray icon is up.

Checked on a real taskbar window (1x1, never shown):

1. start_hidden: the window exists, carries its taskbar style, and is NOT
   visible - and set_visible(True) brings the button back (the way out of the
   tray still works);
2. the decision: the setting off - no; on with a live tray icon - yes; on
   with no tray icon - no (no way back), said in the log.

Fails before the change: there was no start_hidden and every start showed
the button.

Run:  runtime\\python.exe tests\\test_start_in_tray.py
"""
import contextlib
import ctypes
import io
import os
import queue
import sys
import time
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))

user32 = ctypes.windll.user32


def main() -> int:
    failures = []
    import taskbar

    win = taskbar.TaskbarWindow(queue.Queue(), "NeuralScreenTrayStartTest")
    win.start_hidden = True
    win.start()
    deadline = time.monotonic() + 5.0
    while win.hwnd is None and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        hwnd = win.hwnd
        if not hwnd:
            failures.append("the hidden taskbar window was not created")
        else:
            time.sleep(0.3)                      # past where a late show would land
            if user32.IsWindowVisible(hwnd):
                failures.append("the window started in the tray is visible")
            if not (user32.GetWindowLongW(hwnd, -20) & taskbar.WS_EX_APPWINDOW):
                failures.append("the hidden window lost WS_EX_APPWINDOW")
            win.set_visible(True)
            time.sleep(0.1)
            if not user32.IsWindowVisible(hwnd):
                failures.append("set_visible(True) did not bring the button back")
            win.set_visible(False)
    finally:
        win.stop()

    import startup
    for cfg, alive, want in (({}, True, False), ({"tray_on_minimise": True}, True, True),
                             ({"tray_on_minimise": True}, False, False)):
        st = types.SimpleNamespace(cfg=cfg, tray=types.SimpleNamespace(alive=lambda a=alive: a))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = startup._starts_in_tray(st, wait=0.1)
        if got is not want:
            failures.append(f"start in the tray with {cfg}, tray alive {alive}: {got}")
        if cfg and not alive and "tray icon is not up" not in err.getvalue():
            failures.append("a start that could not go to the tray did not say why")

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
