r"""Window and monitor handles at or above 2**31 do not break the Win32 calls.

ctypes.windll functions without prototypes take a bare Python int as a C
int. A handle at or above 2**31 then raises ArgumentError - and the calls
that did that sat inside EnumWindows / EnumDisplayMonitors callbacks, where
the exception is swallowed and the callback answers FALSE: the enumeration
simply stopped, and the window list or the monitor list came back short
with nothing in the log. The sites: winapi._is_taskbar_window
(DwmGetWindowAttribute), channels.enable_wgc (IsWindow), the four capture
monitor helpers and diagnostics._windows_displays (GetMonitorInfoW).

Checked with the real helpers and the real Win32 calls; only the
enumerators are replaced, by ones that hand the callback a handle above
2**31 first and the real primary monitor after it, and stop on FALSE the
way Windows does:
* no ArgumentError reaches the caller or the unraisable hook;
* the enumeration goes on past the large handle - the real monitor is still
  found.

Run:  runtime\python.exe tests\test_handle_width.py
"""
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import capture  # noqa: E402
import channels  # noqa: E402
import diagnostics  # noqa: E402
import winapi  # noqa: E402

BIG = 0x1_8000_0000


def _primary():
    """The primary monitor: its handle, rectangle and device name."""
    user32 = ctypes.WinDLL("user32")
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    hmon = user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # PRIMARY
    info = capture._MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(info)
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
    user32.GetMonitorInfoW(hmon, ctypes.byref(info))
    name = "".join(info.szDevice).rstrip("\x00")
    return hmon, info.rcMonitor, name


def main() -> int:
    failures = []
    unraisable = []
    real_hook = sys.unraisablehook
    sys.unraisablehook = lambda u: unraisable.append(repr(u.exc_value))
    hmon, rect, name = _primary()

    def enum(_hdc, _clip, callback, _data):
        """A large handle first, the real primary monitor second."""
        for handle in (BIG, hmon):
            r = wintypes.RECT(rect.left, rect.top, rect.right, rect.bottom)
            if not callback(handle, 0, ctypes.pointer(r), 0):
                return False
        return True

    u32 = ctypes.windll.user32
    real_enum = u32.EnumDisplayMonitors
    u32.EnumDisplayMonitors = enum
    try:
        for label, call, found in (
                ("capture.monitor_origin", lambda: capture.monitor_origin(name),
                 lambda r: r is not None),
                ("capture.monitor_work_size",
                 lambda: capture.monitor_work_size(name), lambda r: r is not None),
                ("capture.monitor_size", lambda: capture.monitor_size(name),
                 lambda r: r is not None),
                ("capture.list_monitors", capture.list_monitors,
                 lambda r: name in [m[3] for m in r]),
                ("diagnostics._windows_displays", diagnostics._windows_displays,
                 lambda r: name in [d["name"] for d in r])):
            before = len(unraisable)
            result = call()
            if len(unraisable) > before:
                failures.append(f"{label}: the callback raised on a handle "
                                f"above 2**31 ({unraisable[-1]}) and the "
                                f"enumeration was cut")
            if not found(result):
                failures.append(f"{label} lost the real monitor behind a "
                                f"large handle: {result!r}")
    finally:
        u32.EnumDisplayMonitors = real_enum

    try:
        winapi._is_taskbar_window(BIG)
    except Exception as exc:
        failures.append(f"winapi._is_taskbar_window({BIG:#x}) raised {exc!r}")

    st = SimpleNamespace(dda_attempted=False, window_hwnd=BIG)
    try:
        if channels.enable_wgc(st) is not False:
            failures.append("channels.enable_wgc took a handle that is no "
                            "window for a live one")
    except Exception as exc:
        failures.append(f"channels.enable_wgc with hwnd {BIG:#x} raised "
                        f"{exc!r}")

    sys.unraisablehook = real_hook
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: handles above 2**31 pass through every enumeration and check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
