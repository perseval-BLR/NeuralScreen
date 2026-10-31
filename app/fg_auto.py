"""Frame Generation matched to the display: the multiplier that fills the refresh rate.

With a fixed multiplier a 30 fps video on a 144 Hz screen gets x2 (60 fps)
unless the user picks x4 by hand, and the same x4 on a 60 fps game asks for
240 frames a second the screen cannot show - the extra generated frames cost
GPU time and only add uneven pacing. The neighbours do this on their own
(Lossless Scaling's adaptive FG, Skynizz's auto multiplier, MAKO's "prefer the
lower multiplier"): the step is the largest one whose output still fits the
refresh rate.

The choice moves only after it has held for a while: a multiplier change
recreates DLSS-G (a short hitch), so a source that wobbles around a boundary
must not flip it back and forth. The user's own multiplier stays the ceiling.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

#: How long a new choice has to hold before it is taken, in seconds.
HOLD_S = 2.0
#: The first choice of a run holds longer: the program's own start (the panel,
#: the "NR on" notice) changes the desktop and reads as a faster source for a
#: few seconds - 42.7 for a 36 fps source (bench 31.10).
FIRST_HOLD_S = 4.0
#: Output may run this far over the refresh rate and still count as fitting:
#: 59.94 fps x2 on a 119.88 Hz screen is the case it is there for.
FIT_MARGIN = 1.03
#: The step in force is kept while its output stays within this much over the
#: refresh rate: a step goes up only when it fits (FIT_MARGIN) and comes down
#: only past this, so a rate read noisily around a boundary does not flip it -
#: the whole desktop's new pictures, which is what fullscreen mode counts, read
#: 33-42 a second for a 36 fps source in the first seconds (bench 31.10).
KEEP_MARGIN = 1.10
#: Below this source rate the measurement is not worth acting on (a still
#: picture held, a paused video): the current step stays.
MIN_SOURCE_FPS = 5.0


def best_multiplier(source_fps: float, refresh_hz: float, ceiling: int, floor: int = 2) -> int:
    """The largest step in [floor, ceiling] whose output fits the refresh rate.

    Never below `floor`: FG on means at least one generated frame, even when
    the source alone already fills the screen.
    """
    ceiling = max(floor, int(ceiling))
    if source_fps <= 0.0 or refresh_hz <= 0.0:
        return ceiling
    for step in range(ceiling, floor - 1, -1):
        if source_fps * step <= refresh_hz * FIT_MARGIN:
            return step
    return floor


class Chooser:
    """Holds the step in force and moves it only after a new choice has held."""

    def __init__(self, hold_s: float = HOLD_S, first_hold_s: float = FIRST_HOLD_S) -> None:
        self.hold_s = float(hold_s)
        self.first_hold_s = float(first_hold_s)
        self.current: int | None = None
        self._pending: int | None = None
        self._since = 0.0

    def update(self, now: float, source_fps: float, refresh_hz: float, ceiling: int) -> int:
        ceiling = max(2, int(ceiling))
        if self.current is not None and self.current > ceiling:
            # The user lowered the ceiling: that is an instruction, not a reading.
            self.current = ceiling
            self._pending = None
            return self.current
        if source_fps < MIN_SOURCE_FPS:
            self._pending = None
            return self.current if self.current is not None else 2
        want = best_multiplier(source_fps, refresh_hz, ceiling)
        if (self.current is not None and want < self.current
                and source_fps * self.current <= refresh_hz * KEEP_MARGIN):
            want = self.current
        if want == self.current:
            self._pending = None
        elif want != self._pending:
            self._pending, self._since = want, now
        elif now - self._since >= (self.hold_s if self.current is not None else self.first_hold_s):
            self.current, self._pending = want, None
        # Until a reading has held, the cheapest step: the first measured rates
        # of a start are noisy, and every change recreates FG - one change from
        # x2 to the held step, instead of three on the way there (bench 31.10).
        return self.current if self.current is not None else 2

    def reset(self) -> None:
        self.current = None
        self._pending = None


class _DEVMODEW(ctypes.Structure):
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
                ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
                ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
                ("dmDisplayFlags", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
                ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
                ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


def refresh_hz(hwnd: int | None = None) -> float:
    """The refresh rate of the monitor `hwnd` is on (the primary one without it), or 0."""
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        device = None
        if hwnd:
            monitor = user32.MonitorFromWindow(ctypes.c_void_p(int(hwnd)), 2)
            info = _MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(info)
            if monitor and user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
                device = info.szDevice
        mode = _DEVMODEW()
        mode.dmSize = ctypes.sizeof(mode)
        if user32.EnumDisplaySettingsW(device, -1 & 0xFFFFFFFF, ctypes.byref(mode)):
            return float(mode.dmDisplayFrequency) if mode.dmDisplayFrequency > 1 else 0.0
    except Exception:
        pass
    return 0.0
