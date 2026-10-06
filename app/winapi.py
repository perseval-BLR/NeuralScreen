"""Win32 window helpers: geometry, the window under the cursor, the list.

Moved out of main.py unchanged. Everything here is pure Windows plumbing -
it takes a window handle and asks the system about it - which is why it was
the first thing that did not belong in a 2200-line main().

The rules these encode are hard-won and the comments carry them:
  * a window's visible bounds are the DWM frame bounds, not GetWindowRect;
  * the desktop (Progman/WorkerW) is not a window you can capture;
  * a window that does not show in the taskbar (owned, tool, DWM-cloaked)
    is a background helper, not something a user means to pick.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


# A private user32, prototypes stated: handles are pointer-sized, and without
# a restype ctypes reads them as a signed C int - the window list handed out
# unsigned handles (EnumWindows' HWND) while window_under_cursor handed out
# signed ones (GetAncestor's default int), and one window could compare
# unequal to itself. Private because windll.user32's function objects are
# shared process-wide and other modules (pystray) declare their own on them.
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
for _name, _args, _res in (
        ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
         wintypes.DWORD),
        ("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        ("GetForegroundWindow", [], wintypes.HWND),
        ("SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
        ("IsWindow", [wintypes.HWND], wintypes.BOOL),
        ("IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
        ("IsIconic", [wintypes.HWND], wintypes.BOOL),
        ("GetCursorPos", [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
        ("WindowFromPoint", [wintypes.POINT], wintypes.HWND),
        ("GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
        ("GetWindow", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
        ("GetWindowLongW", [wintypes.HWND, ctypes.c_int], wintypes.LONG),
        ("GetWindowTextLengthW", [wintypes.HWND], ctypes.c_int),
        ("GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        ("EnumWindows", [_WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL)):
    _fn = getattr(_user32, _name)
    _fn.argtypes = _args
    _fn.restype = _res
# GetWindowRect keeps a free argument list: callers pass this module's _RECT.
_user32.GetWindowRect.restype = wintypes.BOOL


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


DWMWA_EXTENDED_FRAME_BOUNDS = 9
OUR_NATIVE_WINDOW_CLASSES = frozenset({"NeuralScreenPresent"})


def _window_pid(hwnd: int) -> int:
    pid = ctypes.c_ulong(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _window_class_name(hwnd: int) -> str:
    cls = ctypes.create_unicode_buffer(128)
    if not _user32.GetClassNameW(hwnd, cls, len(cls)):
        return ""
    return cls.value


def _is_our_window(hwnd: int) -> bool:
    """True for either Python-owned UI or the native presenter process.

    The presenter deliberately lives in the worker process, so comparing its
    PID with ``os.getpid()`` is not enough.  ``WindowFromPoint`` can still
    return that layered click-through window; treating it as a foreign target
    made window mode capture NeuralScreen's own output instead of the game.
    """
    return (_window_pid(hwnd) == os.getpid() or
            _window_class_name(hwnd) in OUR_NATIVE_WINDOW_CLASSES)


def window_frame_rect(hwnd: int):
    """The window's visible bounds as the compositor sees them: (x, y, w, h).

    Not GetWindowRect: that one includes the invisible resize border - several
    pixels of nothing on each side - so an overlay placed by it sits visibly
    off the window. The DWM answer is also the rectangle Windows Graphics
    Capture hands over, which is what the picture has to line up with.
    Returns None when the window is gone.
    """
    r = _RECT()
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        ctypes.c_void_p(int(hwnd)), ctypes.c_uint(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(r), ctypes.sizeof(r))
    if hr != 0:
        if not _user32.GetWindowRect(ctypes.c_void_p(int(hwnd)),
                                                  ctypes.byref(r)):
            return None
    return (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))


def foreign_foreground() -> int:
    """The focused window, unless it is one of ours. 0 when there is none.

    "Ours" matters because the hotkey may be pressed while the menu has the
    focus, and capturing our own overlay is exactly the loop this mode exists
    to avoid.
    """
    user32 = _user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return 0
    if _is_our_window(hwnd):
        return 0
    if _is_desktop_window(hwnd):
        return 0
    return int(hwnd)


def give_back_foreground(ours: int, target: int) -> bool:
    """Hand the foreground back to `target` if our window `ours` holds it.

    Closing the menu makes the layer click-through again, but a window that
    is foreground stays foreground: the panel took the focus on the user's
    click and kept it, so the next keys the user typed - meant for the game
    or the editor under the overlay - landed in our queue instead. True when
    the foreground was moved.
    """
    if not ours or not target:
        return False
    user32 = _user32
    if int(user32.GetForegroundWindow() or 0) != int(ours):
        return False            # the user already went elsewhere: leave it
    if not user32.IsWindow(target):
        return False
    return bool(user32.SetForegroundWindow(target))


def _is_desktop_window(hwnd: int) -> bool:
    """Progman / WorkerW - the desktop itself, not a window to capture.

    Clicking the desktop before Num5 made the capture target the wallpaper
    (Progman): the overlay then showed the desktop with every real window
    transparent above it. The desktop is not a window - exclude it (user
    report: "only the desktop is shown, the windows are transparent").
    """
    user32 = _user32
    cls = ctypes.create_unicode_buffer(64)
    if not user32.GetClassNameW(hwnd, cls, 64):
        return False
    return cls.value in ("Progman", "WorkerW")


def window_under_cursor() -> int:
    """The topmost real window under the mouse, 0 when none/ours/desktop.

    The Num5 alternative to the focused window: point at the window you want
    and press. Works on the desktop too - the focused window there is
    Progman, which is not capturable.
    """
    user32 = _user32
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return 0
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return 0
    # WindowFromPoint can return a child (a button inside Chrome); walk up
    # to the top-level owner.
    top = user32.GetAncestor(hwnd, 2)  # GA_ROOT
    if not top:
        top = hwnd
    if not user32.IsWindowVisible(top) or user32.IsIconic(top):
        return 0
    if _is_our_window(top):
        return 0
    if _is_desktop_window(top):
        return 0
    return int(top)


def _is_taskbar_window(hwnd: int) -> bool:
    """A window that shows in the taskbar: top-level, not owned, not a tool
    window.

    Background processes keep hidden helper windows (owned or tool
    windows) that EnumWindows still reports - they are not real desktop
    apps and must not appear in the window list (user report: "сторонние
    процессы попадают в список").
    """
    user32 = _user32
    if user32.GetWindow(hwnd, 4):  # GW_OWNER
        return False
    ex = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
    if ex & 0x00000080:  # WS_EX_TOOLWINDOW
        return False
    cloaked = ctypes.c_int(0)
    # c_void_p: dwmapi has no prototypes here, and a bare int handle goes in
    # as a C int - one at or above 2**31 raised ArgumentError, which inside
    # the EnumWindows callback ended the enumeration without a word.
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(int(hwnd)), 14, ctypes.byref(cloaked), 4) == 0             and cloaked.value:
        return False  # DWM_CLOAKED: hidden from the taskbar (TextInputHost,
        # the Settings helper windows, ...)
    return True


def list_capturable_windows() -> list[tuple[int, str]]:
    """Visible top-level windows that can be captured: (hwnd, title).

    The window list for the menu. Excludes our own windows, the desktop
    (Progman/WorkerW), windows without a title and windows that do not
    show in the taskbar (owned/tool windows of background processes).

    Minimised windows ARE listed. They used to be skipped - a minimised
    window has nothing to capture - but the list is what the user reads to
    find their game, and a menu showing one entry out of five open programs
    reads as broken (user report, 11.09). Picking one restores it first,
    see pipeline.switch_window.
    """
    user32 = _user32
    out: list[tuple[int, str]] = []

    @_WNDENUMPROC
    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if _is_our_window(hwnd):
            return True
        if _is_desktop_window(hwnd) or not _is_taskbar_window(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if title:
            out.append((int(hwnd), title))
        return True

    user32.EnumWindows(cb, 0)
    return out
