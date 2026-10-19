"""A captured window made borderless over its monitor, and back (#176).

Older games without a borderless mode keep their frame in one-window mode,
or run exclusive full screen, where nothing can sit over them. The Source
block gains a button that takes the frame off the processed window and sizes
it to its monitor; a second click, or NeuralScreen's exit, puts back exactly
what was taken.

Checked on a real (hidden) framed window:

1. made borderless: no caption, no sizing border, the window covers its
   monitor;
2. restored: the same style, extended style and rectangle as before;
3. the command layer: the button toggles it for the processed window, the
   menu state follows, and restore_borderless (the exit) puts every window
   back; a window that is gone is reported, not followed;
4. the button is in the Source block in window mode, and says which way it
   goes.

Fails before the change: there was no button and no module.

Run:  runtime\\python.exe tests\\test_borderless.py
"""
import contextlib
import ctypes
import io
import os
import sys
import types
from ctypes import wintypes

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, wintypes.LPVOID]
user32.DestroyWindow.argtypes = [wintypes.HWND]
WS_OVERLAPPEDWINDOW = 0x00CF0000


def _framed_window() -> int:
    """A framed top-level window, never shown."""
    return int(user32.CreateWindowExW(0, "STATIC", "NsBorderlessTest", WS_OVERLAPPEDWINDOW,
                                      120, 140, 640, 360, None, None, None, None) or 0)


def main() -> int:
    failures = []
    import borderless as bl

    hwnd = _framed_window()
    if not hwnd:
        print("SKIP: no window could be created")
        return 0
    try:
        before = (int(bl._user32.GetWindowLongPtrW(hwnd, bl.GWL_STYLE)),
                  int(bl._user32.GetWindowLongPtrW(hwnd, bl.GWL_EXSTYLE)), bl._rect(hwnd))
        if not bl.is_framed(hwnd):
            failures.append("the test window has no frame to take")
        saved = bl.make_borderless(hwnd)
        if saved is None or bl.is_framed(hwnd):
            failures.append("the window still has its frame")
        if bl._rect(hwnd) != bl.monitor_rect(hwnd):
            failures.append(f"the window is {bl._rect(hwnd)}, not its monitor "
                            f"{bl.monitor_rect(hwnd)}")
        bl.restore(hwnd, saved)
        after = (int(bl._user32.GetWindowLongPtrW(hwnd, bl.GWL_STYLE)),
                 int(bl._user32.GetWindowLongPtrW(hwnd, bl.GWL_EXSTYLE)), bl._rect(hwnd))
        if after != before:
            failures.append(f"restored as {after}, was {before}")

        # The command layer.
        import commands
        states = []
        st = types.SimpleNamespace(window_hwnd=hwnd, display=types.SimpleNamespace(
            menu=types.SimpleNamespace(set_state=states.append)))
        with contextlib.redirect_stdout(io.StringIO()):
            commands.apply_menu_action(st, ("button", "borderless"))
            first = (bl.is_framed(hwnd), dict(states[-1]))
            commands.apply_menu_action(st, ("button", "borderless"))
            second = (bl.is_framed(hwnd), dict(states[-1]))
            commands.apply_menu_action(st, ("button", "borderless"))
            commands.restore_borderless(st)
        if first != (False, {"borderless": True}) or second != (True, {"borderless": False}):
            failures.append(f"the button gave {first}, then {second}")
        if bl.is_framed(hwnd) is not True or st.borderless_saved:
            failures.append("the exit did not put the frame back")
    finally:
        user32.DestroyWindow(hwnd)
    st = types.SimpleNamespace(window_hwnd=hwnd, display=None)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        commands.toggle_borderless(st)
    if "gone" not in err.getvalue() or st.borderless_saved:
        failures.append("a window that is gone was not reported")

    # The menu.
    import pygame
    pygame.init()
    pygame.display.set_mode((64, 64))
    import fonts
    import overlay_ui as ui
    from i18n import STRINGS
    menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
    menu.visible = True
    menu.page = "main"
    menu.folds = {"source"}
    for on, key in ((False, "borderless_on"), (True, "borderless_off")):
        menu.set_state({"nr": True, "gpu_ok": True, "profile": "Natural",
                        "profiles": ["Natural"], "window_mode": True,
                        "window_current": (0x4D5E, "Game"), "borderless": on})
        menu.layout(1920, 1080)
        btn = next((i for i in menu.items if i.kind == "button" and i.key == "borderless"), None)
        if btn is None or btn.extra.get("label") != STRINGS["en"][key]:
            failures.append(f"borderless={on}: the button is {btn and btn.extra}")
    menu.set_state({"window_mode": False})
    menu.layout(1920, 1080)
    if any(i.key == "borderless" for i in menu.items):
        failures.append("the borderless button shows in full-screen mode")
    pygame.quit()

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
