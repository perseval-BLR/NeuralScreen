"""A captured window made borderless and stretched over its monitor (#176).

Older games without a borderless mode run in a framed window, or in
exclusive full screen where nothing can sit over them. Run framed, one-window
mode captures them, but the picture keeps the title bar and the game a
corner of the screen. This takes the frame off the window and sizes it to
the monitor it is on - the window itself, not a scaled copy - and puts back
exactly what it took when asked, or when NeuralScreen exits.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_LONG_PTR = ctypes.c_ssize_t
for _name, _args, _res in (
        ("GetWindowLongPtrW", [wintypes.HWND, ctypes.c_int], _LONG_PTR),
        ("SetWindowLongPtrW", [wintypes.HWND, ctypes.c_int, _LONG_PTR], _LONG_PTR),
        ("GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        ("SetWindowPos", [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
        ("IsWindow", [wintypes.HWND], wintypes.BOOL),
        ("IsZoomed", [wintypes.HWND], wintypes.BOOL),
        ("ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        ("MonitorFromWindow", [wintypes.HWND, wintypes.DWORD], wintypes.HMONITOR)):
    _fn = getattr(_user32, _name)
    _fn.argtypes = _args
    _fn.restype = _res

GWL_STYLE, GWL_EXSTYLE = -16, -20
#: What makes a frame: the caption, the sizing border, the system menu and
#: its boxes, and the edges drawn around the client area.
FRAME_STYLE = 0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000
FRAME_EXSTYLE = 0x00000001 | 0x00000200 | 0x00020000 | 0x00000100
SWP_FRAMECHANGED, SWP_NOZORDER, SWP_NOOWNERZORDER, SWP_NOACTIVATE = 0x20, 0x4, 0x200, 0x10
SW_MAXIMIZE, SW_RESTORE = 3, 9


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL


@dataclass(frozen=True)
class Saved:
    """What the window was, to be put back exactly."""
    style: int
    exstyle: int
    rect: tuple          # left, top, right, bottom
    maximized: bool


def _rect(hwnd: int) -> tuple:
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def monitor_rect(hwnd: int) -> tuple:
    """The whole monitor the window is (mostly) on: left, top, right, bottom."""
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    mon = _user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if not mon or not _user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return _rect(hwnd)
    r = info.rcMonitor
    return (r.left, r.top, r.right, r.bottom)


def make_borderless(hwnd: int) -> Saved | None:
    """Take the frame off and cover the monitor; None if the window is gone."""
    if not hwnd or not _user32.IsWindow(hwnd):
        return None
    saved = Saved(style=int(_user32.GetWindowLongPtrW(hwnd, GWL_STYLE)),
                  exstyle=int(_user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)),
                  rect=_rect(hwnd), maximized=bool(_user32.IsZoomed(hwnd)))
    if saved.maximized:
        # A maximised window keeps its frame metrics; restore it first so the
        # new size is the one it takes.
        _user32.ShowWindow(hwnd, SW_RESTORE)
    _user32.SetWindowLongPtrW(hwnd, GWL_STYLE, saved.style & ~FRAME_STYLE)
    _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, saved.exstyle & ~FRAME_EXSTYLE)
    left, top, right, bottom = monitor_rect(hwnd)
    _user32.SetWindowPos(hwnd, None, left, top, right - left, bottom - top,
                         SWP_FRAMECHANGED | SWP_NOZORDER | SWP_NOOWNERZORDER
                         | SWP_NOACTIVATE)
    return saved


def restore(hwnd: int, saved: Saved) -> bool:
    """Put the frame, the size and the place back. False if the window is gone."""
    if not hwnd or not _user32.IsWindow(hwnd):
        return False
    _user32.SetWindowLongPtrW(hwnd, GWL_STYLE, saved.style)
    _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, saved.exstyle)
    left, top, right, bottom = saved.rect
    _user32.SetWindowPos(hwnd, None, left, top, right - left, bottom - top,
                         SWP_FRAMECHANGED | SWP_NOZORDER | SWP_NOOWNERZORDER
                         | SWP_NOACTIVATE)
    if saved.maximized:
        _user32.ShowWindow(hwnd, SW_MAXIMIZE)
    return True


def is_framed(hwnd: int) -> bool:
    """Whether the window still has a caption or a sizing border."""
    return bool(int(_user32.GetWindowLongPtrW(hwnd, GWL_STYLE)) & (0x00C00000 | 0x00040000))
