"""The FIRST click on the taskbar button must open the menu.

Reported: "the first click does nothing, the second expands it". The live message
trace showed the whole bug in two lines:

    msg=0x0086 wp=0x1 cursorTb=True fgOurs=True was=False   -> menu stayed shut
    msg=0x0112 wp=0xF020  (SC_MINIMIZE, unconditional)      -> only THEN it opened

A click on a NOT-yet-active button arrives as WM_NCACTIVATE(1), and the guard
required `_was_active` - which is still False at that moment, because the message
IS the activation. So the first click was dropped and the second one worked by a
different route (SC_MINIMIZE, which emits unconditionally).

The old `_was_active` test existed to keep #96 shut: when another window is
minimised, Windows activates us as a fallback with the SAME message, and the menu
opened by itself. `_was_active` cannot separate those two - both arrive with the
flag False - so the discriminator is the PREVIOUS foreground window: a real click
leaves it alive, the fallback leaves it minimised. WM_NCACTIVATE carries 0 in
lParam, so that state is sampled continuously instead of read from the message.

Everything here is a real Win32 window: the taskbar window is the real
TaskbarWindow class, and the "other program" is a real visible top-level window
with its own message pump (a window without a pump makes SW_MINIMIZE block, and
a separate thread mirrors reality).

Three cases, and all three matter:

  * [1] first click - our window not active, previous window ALIVE -> the menu
    opens (the reported bug);
  * [2] the #96 fallback - previous window MINIMISED -> nothing happens;
  * [3] SC_MINIMIZE from the taskbar -> still opens the menu (it always did;
    a fix that broke it would be a regression).

Run:  runtime\\python.exe tests\\test_taskbar_first_click.py
"""
import ctypes
import ctypes.wintypes as wt
import queue
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import taskbar  # noqa: E402

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SW_MINIMIZE, SW_RESTORE = 6, 9
VK_MENU = 0x12
EMIT_DEDUP_S = 0.55


def _make_foreign(name: str):
    """A real visible top-level window with its own message pump."""
    cls = "ProbeFirstClickForeign"
    ready = threading.Event()
    box: dict = {}

    def run():
        hinst = kernel32.GetModuleHandleW(None)
        wc = taskbar.WNDCLASSW()
        proc = taskbar.WNDPROC(
            lambda h, m, w, l: taskbar.user32.DefWindowProcW(h, m, w, l))
        wc.lpfnWndProc = proc           # keep the reference: a collected
        wc.hInstance = hinst            # callback crashes the process
        wc.lpszClassName = cls
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(
            0, cls, name,
            taskbar.WS_POPUP | taskbar.WS_VISIBLE | taskbar.WS_CAPTION
            | taskbar.WS_SYSMENU | taskbar.WS_MINIMIZEBOX,
            60, 60, 500, 300, None, None, hinst, None)
        user32.ShowWindow(hwnd, 5)
        box["hwnd"] = hwnd
        box["proc"] = proc
        ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.DestroyWindow(hwnd)

    thread = threading.Thread(target=run, daemon=True, name="foreign")
    thread.start()
    ready.wait(5.0)
    return box.get("hwnd", 0), thread


def _park_cursor_on_taskbar() -> bool:
    """Put the cursor inside a taskbar rect - the guard reads it live."""
    for cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        tb = user32.FindWindowW(cls, None)
        if not tb:
            continue
        rect = wt.RECT()
        if not user32.GetWindowRect(tb, ctypes.byref(rect)):
            continue
        user32.SetCursorPos(rect.left + (rect.right - rect.left) // 2,
                            rect.top + (rect.bottom - rect.top) // 2)
        return True
    return False


def _force_foreground(hwnd: int) -> bool:
    """ALT-trick focus, verified to have stuck (Windows refuses otherwise)."""
    for _ in range(8):
        user32.keybd_event(VK_MENU, 0, 0, 0)
        time.sleep(0.05)
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        user32.keybd_event(VK_MENU, 0, 0x2, 0)
        time.sleep(0.3)
        if user32.GetForegroundWindow() == hwnd:
            time.sleep(0.4)
            if user32.GetForegroundWindow() == hwnd:
                return True
    return False


def _drain(commands: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(commands.get_nowait())
        except queue.Empty:
            return out


def _arm(commands: queue.Queue) -> None:
    """Wait past the emit dedup and drop what earlier steps emitted."""
    time.sleep(EMIT_DEDUP_S)
    _drain(commands)


def main() -> int:
    failures: list[str] = []
    commands: queue.Queue = queue.Queue()
    win = taskbar.TaskbarWindow(commands, "NeuralScreenFirstClickTest")
    win.start()
    deadline = time.monotonic() + 5.0
    while win.hwnd is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if not win.hwnd:
        print("FAIL: the taskbar window was not created")
        return 1
    print(f"taskbar hwnd: {hex(win.hwnd)}")

    foreign, _thread = _make_foreign("Foreign window - first click test")
    if not foreign:
        print("FAIL: could not create the foreign test window")
        win.stop()
        return 1
    print(f"foreign hwnd: {hex(foreign)}")

    # Let the sampler record a non-ours foreground at least once.
    _force_foreground(foreign)
    time.sleep(0.5)

    # --- 1. THE BUG: click on our NOT-yet-active button -------------------
    print("\n[1] the first click: our window not active, previous window ALIVE")
    user32.ShowWindow(foreign, SW_RESTORE)
    time.sleep(0.3)
    if not _force_foreground(foreign):
        failures.append("could not give the foreign window the foreground")
    time.sleep(0.4)
    if not _force_foreground(win.hwnd):
        failures.append("could not take the foreground for the first click")
    time.sleep(0.3)
    _arm(commands)
    # A real click makes us the foreground AS the activation, so at the moment
    # WM_NCACTIVATE(1) arrives the flag is still False. Taking the foreground
    # above fired our own WM_ACTIVATE and set it, so it is cleared here to
    # reproduce the measured state (`was=False`) rather than the second click.
    win._was_active = False
    if not _park_cursor_on_taskbar():
        failures.append("could not park the cursor over the taskbar")
    else:
        alive = bool(user32.IsWindow(foreign))
        iconic = bool(user32.IsIconic(foreign))
        print(f"    prev={hex(foreign)} alive={alive} minimised={iconic} "
              f"ours_fg={user32.GetForegroundWindow() == win.hwnd} "
              f"was_active={win._was_active}")
        if not alive or iconic:
            failures.append("the previous window is not in the 'alive' state "
                            "this step is about")
        user32.SendMessageW(win.hwnd, taskbar.WM_NCACTIVATE, 1, 0)
        time.sleep(0.6)
        got = _drain(commands)
        print(f"    commands: {got}")
        if "show_settings" not in got:
            failures.append(
                "the first click on a not-yet-active button was dropped - the "
                f"reported symptom: {got}")

    # --- 2. the #96 fallback: the previous window is MINIMISED ------------
    print("\n[2] the #96 fallback: previous window MINIMISED")
    user32.ShowWindow(foreign, SW_RESTORE)
    time.sleep(0.3)
    _force_foreground(foreign)
    time.sleep(0.4)
    user32.ShowWindow(foreign, SW_MINIMIZE)
    time.sleep(0.5)
    if not _force_foreground(win.hwnd):
        failures.append("could not take the foreground for the #96 check")
    time.sleep(0.3)
    _arm(commands)
    if not _park_cursor_on_taskbar():
        failures.append("could not park the cursor for the #96 check")
    else:
        alive = bool(user32.IsWindow(foreign))
        iconic = bool(user32.IsIconic(foreign))
        print(f"    prev={hex(foreign)} alive={alive} minimised={iconic} "
              f"ours_fg={user32.GetForegroundWindow() == win.hwnd}")
        if not iconic:
            failures.append("the previous window is not minimised - this step "
                            "would not exercise the #96 path")
        user32.SendMessageW(win.hwnd, taskbar.WM_NCACTIVATE, 1, 0)
        time.sleep(0.6)
        got = _drain(commands)
        print(f"    commands: {got}")
        if got:
            failures.append("a minimised previous window opened the menu "
                            f"(#96 regression): {got}")

    # --- 3. SC_MINIMIZE from the taskbar button still works ---------------
    print("\n[3] SC_MINIMIZE from the taskbar button")
    _arm(commands)
    user32.SendMessageW(win.hwnd, taskbar.WM_SYSCOMMAND, taskbar.SC_MINIMIZE, 0)
    time.sleep(0.5)
    got = _drain(commands)
    print(f"    commands: {got}")
    if "show_settings" not in got:
        failures.append(f"SC_MINIMIZE no longer shows the menu: {got}")

    user32.ShowWindow(foreign, SW_RESTORE)
    time.sleep(0.2)
    user32.DestroyWindow(foreign)
    win.stop()
    time.sleep(0.2)

    print("=" * 60)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: the first click opens the menu; a minimised previous window "
          "still does not")
    return 0


if __name__ == "__main__":
    sys.exit(main())
