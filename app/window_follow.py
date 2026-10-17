"""Window mode follows the focused window (#157, v2.6.0).

In window mode the capture stays on the window the user picked. With "Follow
the focused window" on, a window that keeps the focus for DWELL_S becomes the
capture source, at most once per MIN_GAP_S - the live switch of #148 part 2,
well under a second, instead of a rebuild.

Only a switch that the live path can take: another window on the monitor the
processed one is on, while NR captures and nothing records. Anything else
(another monitor, a recording) would be a full rebuild for a focus change, so
it is not followed.

The exclusions come from the reporter of #157, who ran this daily:

* cloaked, owned and tool windows - they report as visible and give nothing
  or a black frame (the taskbar rule of the window list);
* LockApp.exe - alive after logon with a full-screen window, a black screen;
* explorer.exe - while the shell restarts its desktop window spans every
  monitor and GetShellWindow does not know it yet;
* dwm.exe's "Ghost" windows - put in place of a hung program, same size and
  focused, and gone when it recovers;
* screenshot tools selecting a region - a window over the whole virtual
  desktop; caught by the size rule as well as by name.

Every switch and every skipped window is logged with its process and class:
that is what made the Explorer and ghost cases findable for the reporter.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

import winapi

#: How long a window has to keep the focus before the capture goes to it.
DWELL_S = 2.5
#: The shortest time between two switches the follower makes.
MIN_GAP_S = 8.0
#: How often the focus is looked at.
CHECK_S = 0.25
#: Below this the window is not worth processing (the switch's own floor).
MIN_SIDE = 64
#: How far past its monitor a window may reach and still count as on it.
SPAN_SLACK = 128

NEVER_PROCESSES = frozenset({
    "lockapp.exe", "explorer.exe", "dwm.exe", "snippingtool.exe",
    "screenclippinghost.exe", "sharex.exe", "lightshot.exe", "greenshot.exe",
    "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
})
NEVER_CLASSES = frozenset({
    "Ghost", "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow", "ForegroundStaging", "LockScreenBackstopFrame",
})


@dataclass
class WindowInfo:
    """What the follower needs to know about one window."""
    hwnd: int
    process: str = ""           # the image's file name, lower case
    cls: str = ""
    visible: bool = True
    iconic: bool = False
    ours: bool = False
    taskbar: bool = True        # top-level, not owned, not a tool window, not cloaked
    rect: tuple = (0, 0, 0, 0)  # x, y, w, h - the DWM frame
    monitor: tuple = (0, 0, 0, 0)
    same_monitor: bool = True   # as the processed window


def why_not(info: WindowInfo) -> str | None:
    """None when the window can be followed, otherwise why not."""
    if info.ours:
        return "one of ours"
    if not info.visible:
        return "hidden"
    if info.iconic:
        return "minimised"
    if info.process in NEVER_PROCESSES:
        return f"{info.process} is never followed"
    if info.cls in NEVER_CLASSES:
        return f"class {info.cls} is never followed"
    if not info.taskbar:
        return "not a taskbar window (owned, tool or cloaked)"
    _x, _y, w, h = info.rect
    if w < MIN_SIDE or h < MIN_SIDE:
        return f"too small ({w}x{h})"
    _mx, _my, mw, mh = info.monitor
    if mw and mh and (w > mw + SPAN_SLACK or h > mh + SPAN_SLACK):
        return f"wider than its monitor ({w}x{h} on {mw}x{mh})"
    if not info.same_monitor:
        return "on another monitor (that would be a rebuild, not a switch)"
    return None


class FocusFollower:
    """When the focus has stayed on one followable window long enough.

    update() is given the followable focused window (0 for none) and the
    processed one, and answers the window to switch to, or None."""

    def __init__(self) -> None:
        self.last_switch = float("-inf")
        self.reset()

    def reset(self) -> None:
        self.candidate = 0
        self.since = 0.0

    def update(self, now: float, focused: int, current: int) -> int | None:
        if not focused or focused == current:
            self.reset()
            return None
        if focused != self.candidate:
            self.candidate, self.since = int(focused), now
            return None
        if now - self.since < DWELL_S or now - self.last_switch < MIN_GAP_S:
            return None
        self.last_switch = now
        self.reset()
        return int(focused)


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _process_name(pid: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(handle)


def _monitor_of(hwnd: int):
    user32 = ctypes.windll.user32
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
    mon = user32.MonitorFromWindow(ctypes.c_void_p(int(hwnd)), 2)  # NEAREST
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return mon, (0, 0, 0, 0)
    r = info.rcMonitor
    return mon, (r.left, r.top, r.right - r.left, r.bottom - r.top)


def window_info(hwnd: int, current: int) -> WindowInfo:
    """Ask Windows about hwnd; `current` is the processed window."""
    u = winapi._user32
    mon, mon_rect = _monitor_of(hwnd)
    cur_mon = _monitor_of(current)[0] if current else mon
    return WindowInfo(
        hwnd=int(hwnd),
        process=_process_name(winapi._window_pid(hwnd)),
        cls=winapi._window_class_name(hwnd),
        visible=bool(u.IsWindowVisible(hwnd)),
        iconic=bool(u.IsIconic(hwnd)),
        ours=winapi._is_our_window(hwnd),
        taskbar=winapi._is_taskbar_window(hwnd),
        rect=winapi.window_frame_rect(hwnd) or (0, 0, 0, 0),
        monitor=mon_rect,
        same_monitor=bool(mon) and mon == cur_mon)


def focused_window() -> int:
    """The foreground window as it is, 0 for none (the filtering is why_not's)."""
    return int(winapi._user32.GetForegroundWindow() or 0)
