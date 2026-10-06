"""Display module for the DLSS 5 Desktop NR prototype.

Borderless fullscreen window for the frame + the in-overlay settings
menu. The window is hidden at creation and revealed on the first real
frame (no blank flash at launch); it is excluded from screen capture
via SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) so that
screen-capture tools (dxcam, OBS) do not see it. Mode switches (Num5,
monitor change) are covered by a semi-transparent veil (a blur of the
last frame) with an assembling mark (enter_switch_mode /
exit_switch_mode).

Usage:
    disp = Display(2560, 1440)
    disp.set_hud({"fps": 60, "status": "NR ON", "resolution": "2560x1440",
                  "params": {"intensity": 0.5, "local_tone": 0.3,
                             "local_structure": 0.7}, "frames": 1234})
    while True:
        disp.show(frame_bgra)
        for ev in disp.poll_events():
            if ev == "quit":
                break
    disp.close()
"""

from __future__ import annotations

import ctypes
import math
import os
import struct
import sys
import time
from ctypes import wintypes
from typing import Dict, List

# --- argtypes for user32: WITHOUT them ctypes passes ints as a 32-bit c_int.
# HWND_TOPMOST=-1 turns into 0xFFFFFFFF instead of 0xFFFFFFFFFFFFFFFF and
# SetWindowPos silently FAILS (ret=0) on 64-bit Windows - topmost is not
# applied. hwnd (64-bit) would also be truncated at values >= 2^31.
user32 = ctypes.windll.user32
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.SetWindowLongW.restype = wintypes.LONG
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF,
                                              wintypes.BYTE, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
#: How far the z-order walk may go before it gives up. The old bound was 16
#: and it was the bug behind #96: helper windows are skipped but still spend
#: steps, and on a busy desktop our own windows sit deeper than that. 512 is
#: far past any real stack and still microseconds - the walk stops at the
#: first window that matters, which is normally one of ours.
WALK_LIMIT = 512

# The z-order walkers used by raise_topmost's guard (flicker audit 15.09).
user32.GetTopWindow.argtypes = [wintypes.HWND]
user32.GetTopWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


CURSOR_SHOWING = 0x00000001


def _cloaked(hwnd) -> int:
    """DWMWA_CLOAKED for a window: nonzero when DWM draws none of it."""
    try:
        value = wintypes.DWORD(0)
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(int(hwnd)), 14, ctypes.byref(value),
            ctypes.sizeof(value))
        return int(value.value) if hr == 0 else 0
    except Exception:
        return 0


def window_can_cover(rect, virtual: tuple[int, int, int, int],
                     min_px: int = 16) -> bool:
    """Whether a window's rect could be covering our layer on the desktop.

    The z-order guard (`Display._top_real_window`) walks the stack and needs
    to skip helper windows: invisible 0x0 IME/MSCTFIME entries, a 1x1 dwm
    thumbnail helper, an off-screen Narrator helper at -40000,-40000. A
    window counts only when it is big enough in both dimensions AND its rect
    intersects the VIRTUAL desktop - every monitor, not the primary one.

    The virtual bounds matter on more than one monitor: on a second screen
    to the LEFT of the primary a full-screen window is (-2560, 0, 0, 1440),
    and a primary-only test (`rect.right > 0`) rejected it, so the guard
    found no real window and the HUD was never re-asserted above the picture
    - the panel stayed hidden while still being baked into screenshots
    (issue #89).

    Extracted from the walk so the rule can be tested without a second
    monitor: `virtual` is a plain (x, y, w, h) tuple.
    """
    left, top, right, bottom = (rect.left, rect.top, rect.right, rect.bottom)
    if right - left < min_px or bottom - top < min_px:
        return False
    vx, vy, vw, vh = virtual
    return right > vx and left < vx + vw and bottom > vy and top < vy + vh


def describe_window(hwnd) -> str:
    """Name a window for the log: class, title, process id and rect.

    The z-order guard decides whether to re-assert the HUD above the worker
    picture, and until now it decided silently. A user reporting "the panel
    is invisible over the picture" sent a log in which nothing said what the
    guard saw or which branch it took, so the only next step was to guess
    (issue #96/#89). This is the line that answers it.

    Failure is not fatal: a window can die between the walk and this call, so
    every field degrades to a placeholder and the function never raises.
    """
    try:
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                    ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    except Exception:
        pass
    cls, title, pid, rect_s = "", "", 0, "?"
    try:
        hwnd = int(hwnd)
    except (TypeError, ValueError):
        # A null or bogus handle - the callers log whatever they have, and a
        # diagnostic must never raise inside the render path.
        return "hwnd=invalid"
    try:
        buf = ctypes.create_unicode_buffer(256)
        if user32.GetClassNameW(hwnd, buf, 256):
            cls = buf.value
        if user32.GetWindowTextW(hwnd, buf, 256):
            title = buf.value
    except Exception:
        pass
    try:
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        pid = int(p.value)
    except Exception:
        pass
    try:
        r = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(r)):
            rect_s = f"{r.left},{r.top},{r.right},{r.bottom}"
    except Exception:
        pass
    if len(title) > 40:
        title = title[:37] + "..."
    return (f"hwnd=0x{int(hwnd):X} pid={pid} class={cls!r} "
            f"title={title!r} rect=({rect_s})")


def system_cursor_visible() -> bool:
    """Is there a mouse pointer on screen right now? (Nothing uses this yet.)

    Kept for the open problem it belongs to: a fullscreen game hides the
    cursor and our menu is then unusable. Drawing our own pointer was tried
    and reverted - it produced a SECOND pointer in real use, which is worse
    than none. See "The menu pointer is missing or frozen" in the README.

    A fullscreen game hides it (ShowCursor(FALSE) on its own input queue) and
    it stays hidden while our menu is up. When it is gone the overlay draws
    its own; when it is there, drawing one would mean two pointers.

    Unknown counts as visible: a missing answer must not put a second pointer
    on a normal desktop.
    """
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    try:
        if not user32.GetCursorInfo(ctypes.byref(ci)):
            return True
    except Exception:
        return True
    return bool(ci.flags & CURSOR_SHOWING)

# DPI-aware BEFORE import pygame: on load SDL freezes the process awareness,
# and a later SetProcessDpiAwarenessContext no longer takes effect (it returns
# an error). Without this Windows scales the window: 125% -> 3072x1728 instead
# of 3840x2160 and the frame no longer matches the screen. Verified by
# diagnostics.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import numpy as np
import pygame

import fonts
import vdesk
from overlay_ui import (OverlayMenu, palette as ui_palette,
                        status_readings)

from i18n import STRINGS

# --- Brand palette (DLSS5-Video-Converter) -------------------------------
BG_ALPHA = 235                     # HUD panel translucency (nearly opaque, so text stays readable)
SWITCH_ALPHA = 170                 # mode-switch overlay: the live desktop shows through it
# The veil eases in and out instead of cutting to full strength: a hard
# cut from the picture to the dim freeze reads as a flash (user: the
# switch must be smooth). The entrance is a short SYNCHRONOUS ramp drawn
# inside enter_switch_mode (the caller blocks the loop right after, so a
# time-based ramp would never get a frame); the exit is time-based and
# animated by the loop. Seconds.
SWITCH_FADE_IN = 0.21
SWITCH_FADE_OUT = 0.26
# A veil that has been up this long is no longer a transition: the frame
# that normally takes it down never came. That is the NR-OFF rebuild path -
# the loop takes the low-cost branch, no frame is ever received, and while
# the veil is up draw_overlay refuses to paint the menu, so a stuck veil
# hides the whole interface (issues #89/#96: "the window becomes
# invisible"). This is a BACKSTOP, not the main way down: main's idle
# branch takes the veil down itself. It must stay behind the worker
# watchdog (a 5 s silent-recv timeout plus a restart) so a slow-but-alive
# worker still gets its veil. Seconds.
SWITCH_HOLD_MAX = 8.0
WDA_EXCLUDEFROMCAPTURE = 0x00000011

# Chroma key for HUD mode: pixels of exactly this colour are not drawn at all
# by the layered window, and the worker's overlay shows through them. Picked so
# it cannot occur in the HUD palette (#0D1117 / #FFBF00 / #E6EDF3 / #8B949E).
CHROMA_KEY = (0xFF, 0x00, 0xFF)
LWA_COLORKEY = 0x1
LWA_ALPHA = 0x2

# The layer's appearance - see Display.set_layer_state. Exactly one of the
# three is in force. The mode-switch veil borrows the layer and writes its
# own alpha for the duration of a switch; everything else goes through the
# one writer (_write_layer_attrs).
LAYER_OPAQUE = "opaque"                # alpha 255, no key: Python shows a full-layer picture
LAYER_HUD = "hud"                      # key + BG_ALPHA: the worker shows the frame, only the HUD here
LAYER_PICTURE_KEYED = "picture_keyed"  # key + alpha 255: Python shows a window-sized picture, surround cut out

# Which faces the program draws with lives in fonts.py - including the
# per-script CJK families, re-exported here because the docs renderer and
# the offscreen menu renderer import them from this module.
CJK_FONTS = fonts.CJK_FONTS
# The base layout sizes are set for 1440p. On taller screens the interface is
# scaled up, on shorter ones it stays as is: there is nowhere left to shrink to,
# the text would become unreadable. Hence the "up only" rule (see ui_scale).
FONT_SIZE = 18
UI_BASE_HEIGHT = 1800
ALERT_FONT_SIZE = 28
# How far below the top of the SCREEN an alert sits, in unscaled pixels.
# A fixed number, not a share of the height: 4.5% is 65 px on a 1440-tall
# screen and 13 px on a 300-tall window, and neither of those is the same
# margin (user, 13.09).
ALERT_TOP_MARGIN = 28


def ui_scale_for(height: int) -> float:
    """Interface multiplier derived from the screen height.

    Up only: 1.0 at 1800p and below, 1.2 at 4K. Scaling proportionally down at
    1080p would give a nine-pixel font, so we never scale down. The base was
    raised from 1440 to 1800 - at 1.5 the interface came out too large.
    """
    return max(1.0, float(height) / UI_BASE_HEIGHT)


# The mode-switch mark: five tiles fly into a tight cluster, hold, then
# scatter - the shape the switch overlay shows while the worker warms up.
# Geometry only (no pygame), so the phases can be pinned by a test.
SWITCH_MARK_PERIOD = 3.0        # one assemble-hold-scatter cycle, seconds
SWITCH_MARK_ASSEMBLE = 0.6      # flight in
SWITCH_MARK_HOLD_END = 2.35     # the cluster holds until here
SWITCH_MARK_SCATTER_END = 2.95  # flight out
SWITCH_MARK_TILE = 0.018        # tile size, of min(w, h)
SWITCH_MARK_ORBIT = 0.075       # scatter orbit radius, of min(w, h)
SWITCH_MARK_GAP = 1.35          # cluster spacing, in tile sizes


def _ease_out(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def _ease_in_out(t: float) -> float:
    if t < 0.5:
        return 4.0 * t * t * t
    return 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


def _assemble_tiles(t: float, w: int, h: int) -> list:
    """Tile centres for the mode-switch mark at time t.

    Returns (cx, cy, size, accent) per tile: the middle tile carries the
    accent colour, the other four are neutral. Tiles fly in from a loose
    orbit (assemble), hold as a tight cluster with a slight breathing,
    then scatter back out; the loop repeats every SWITCH_MARK_PERIOD.
    """
    cx, cy = w / 2.0, h / 2.0
    m = float(min(w, h))
    tile = m * SWITCH_MARK_TILE
    gap = tile * SWITCH_MARK_GAP
    orbit = m * SWITCH_MARK_ORBIT
    offsets = ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))
    t = max(0.0, t)
    cycle = t % SWITCH_MARK_PERIOD
    if cycle < SWITCH_MARK_ASSEMBLE:
        f = _ease_out(cycle / SWITCH_MARK_ASSEMBLE)
        breathe = 1.0
    elif cycle < SWITCH_MARK_HOLD_END:
        f = 1.0
        breathe = 1.0 + 0.05 * math.sin(t * 2.5)
    else:
        q = min(1.0, (cycle - SWITCH_MARK_HOLD_END)
                / (SWITCH_MARK_SCATTER_END - SWITCH_MARK_HOLD_END))
        f = 1.0 - _ease_in_out(q)
        breathe = 1.0
    tiles = []
    for i, (ox, oy) in enumerate(offsets):
        tx, ty = cx + ox * gap, cy + oy * gap
        # The orbit rocks gently and returns to its start every cycle -
        # a continuous drift would make the mark jump on the wrap.
        ang = i * (2.0 * math.pi / 5.0) + \
            0.35 * math.sin(cycle * (2.0 * math.pi / SWITCH_MARK_PERIOD))
        sx, sy = cx + math.cos(ang) * orbit, cy + math.sin(ang) * orbit
        x = tx + (sx - tx) * (1.0 - f)
        y = ty + (sy - ty) * (1.0 - f)
        size = tile * (0.62 + 0.38 * f) * breathe
        tiles.append((x, y, size, i == 0))
    return tiles


class Display:
    """Fullscreen borderless window that renders frames + branded HUD."""

    def __init__(self, width: int, height: int, fullscreen: bool = True, click_through: bool = True):
        # DPI awareness is already set at module level (before import pygame).
        # Calling it again here has no effect - it is kept as a fallback for
        # cases where the module is imported without the top block.
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        # SDL hints BEFORE pygame.init():
        # SDL_WINDOWS_DPI_AWARENESS=permonitorv2 - without it Windows scales
        # the window (125% -> 3072x1728 instead of 3840x2160).
        # SDL_MOUSE_FOCUS_CLICKTHROUGH=1 - a click on the window is not
        # intercepted by SDL (the window is not activated, focus is not taken
        # away), so clicks reach the applications underneath the overlay.
        try:
            os.environ["SDL_WINDOWS_DPI_AWARENESS"] = "permonitorv2"
        except Exception:
            pass
        try:
            os.environ["SDL_MOUSE_FOCUS_CLICKTHROUGH"] = "1"
        except Exception:
            pass
        pygame.init()
        pygame.display.set_caption("NeuralScreen")
        # Borderless windowed instead of FULLSCREEN: a pygame fullscreen window
        # loses its rendering on click/focus (the screen freezes while the loop
        # keeps spinning). A window the size of the monitor at position (0,0)
        # looks the same but is stable, and click-through works.
        flags = pygame.NOFRAME
        self._flags = flags
        # pygame.HIDDEN (128, SDL_WINDOW_HIDDEN): create the window invisible.
        # NOTE: the raw SDL flag 0x8 is IGNORED by pygame 2.6 (verified
        # experimentally) - the window comes up visible. pygame.HIDDEN works;
        # the explicit SW_HIDE below is a belt-and-suspenders fallback. Shown
        # only once the first real frame arrives - otherwise a blank window
        # sits over the desktop during the NGX warm-up (user: screen flashes
        # on startup / on mode switches because a new window pops up empty).
        hidden = pygame.NOFRAME | pygame.HIDDEN
        self.screen = pygame.display.set_mode((width, height), hidden)
        self.width, self.height = self.screen.get_size()
        # The window starts hidden; reveal() shows it after the first real
        # frame. set_visible/is_visible interplay: is_visible() is consulted
        # by _follow_window before any show/hide decision, so the initial
        # state must match the real (hidden) window.
        self._visible = False
        self._reveal_pending = True
        # pygame 2.6 ignores SDL_WINDOW_HIDDEN (verified experimentally: the
        # window is VISIBLE right after set_mode with the 0x8 flag) - hide it
        # explicitly or a blank window flashes over the desktop during the
        # NGX warm-up (user: translucent/blank flash on startup).
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            pass
        # Where this monitor's top-left corner is on the virtual desktop.
        # (0,0) is the primary monitor; a second one can sit anywhere. Set
        # through set_origin() once the monitor is known (main owns that).
        self._origin = (0, 0)
        # Cached once: the pid of the shell's desktop window and our own pid.
        # The z-order walk asks "is this window ours or the shell's" for every
        # entry it passes, and both answers are process-wide constants.
        self._shell_pid: int | None = None
        self._own_pid: int | None = None
        self._move_to_origin()
        # Force the physical window size: even if DPI awareness did not apply
        # (a 3072x1728 window instead of 3840x2160), we stretch the window to
        # the requested size so the pygame surface matches.
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, width, height, 0x0004)  # SWP_NOZORDER
        except Exception:
            pass
        self._set_topmost()
        self.clock = pygame.time.Clock()
        self._hud: Dict = {}
        self._alerts: List[tuple[str, float]] = []  # (text, expires_at)
        # Window mode: where the layer belongs when no alert is up,
        # and whether an alert is currently holding it open.
        # One-window mode: where the captured window is (the frame blit
        # needs it) and how big the frame is. The LAYER is always the screen.
        self._window_layer: tuple[int, int, int, int] | None = None
        #: Where the last shown frame sits on the layer (None when it covered
        #: the layer). The fade-out clips the veil's dim to it while the
        #: surround of a keyed picture must stay pure key - see _draw_veil.
        self._frame_layer_rect: "pygame.Rect | None" = None
        self._frame_size: tuple[int, int] | None = None
        # Interface scale and the layout sizes derived from it.
        self.ui_scale = ui_scale_for(self.height)
        self.font_size = max(8, int(round(FONT_SIZE * self.ui_scale)))
        # HUD language (NR ON/NR OFF), see set_lang(). Must exist before the
        # menu: the loader picks the face by the language, and OverlayMenu
        # builds its fonts inside its constructor - with this assignment
        # below the menu, that first build fell through to pygame's default
        # face instead of ours.
        self._lang = "ru"
        # The settings menu lives in this same layer. A separate window on top
        # of the game would steal focus and fight for topmost, whereas here we
        # are already above the frame and already transparent by key.
        self.menu = OverlayMenu(self.ui_scale, self._load_font)
        # The HUD is nothing but readings - fps, resolution, frame counter -
        # so it takes the monospaced face whole; the menu picks per element.
        self._font = self._load_font(size=self.font_size, mono=True)
        self._alert_font = self._load_font(
            size=max(10, int(round(ALERT_FONT_SIZE * self.ui_scale))))
        # Disable vsync: flip() must not wait for vblank (otherwise the FPS is
        # tied to the monitor refresh rate and frames are lost on a slow
        # pipeline).
        try:
            pygame.display.set_swap_interval(0)
        except Exception:
            pass
        self._excluded = self._exclude_from_capture()
        self._click_through = False
        self._menu_input = False
        if click_through:
            self._set_click_through()
        # NOTE: _visible/_reveal_pending are set right after set_mode - the
        # window starts hidden and reveal() shows it after the first frame.
        # The layer's appearance: ONE state variable and ONE writer. Every
        # pipeline SetLayeredWindowAttributes call goes through
        # _write_layer_attrs(); show() only REQUESTS a state. Two writers
        # overwriting each other was the blink of d10cfd4/e46a24b; a keyed
        # fill on an unkeyed layer was the magenta screen of ca1801d.
        self._layer_state = LAYER_OPAQUE
        self._menu_opaque = False
        self._last_overlay = 0.0
        self._last_alert_count = 0
        # The z-order guard's decision log: signature of the last reported
        # state and when it was written. The guard runs every frame while the
        # menu is open, so an unthrottled line would drown the log; only a
        # CHANGE of the picture (or a re-assert after a quiet spell) is worth
        # a line. See raise_topmost.
        self._zlog_sig = None
        self._zlog_t = 0.0
        # The mode-switch veil (blur + assembling mark): shown while the
        # pipeline is rebuilt and the new worker warms up, so the screen
        # does not sit bare for a second on every Num5 (user: mode-switch
        # flashes). The veil owns the layer for the whole switch - follow
        # and shrink paths must not touch its geometry.
        self._switch_active = False
        self._switch_phase = "off"  # off | in | on | out
        # The sharp copy the dim fades in over (plain mode; the HUD path
        # fades the window's own alpha instead and leaves this None).
        self._switch_base: "pygame.Surface | None" = None
        # A single pre-made copy of the dim that carries the per-frame
        # alpha - copying a 4K surface every frame of the fade would cost
        # more than the fade is worth.
        self._switch_dim_soft: "pygame.Surface | None" = None
        # The flat veil for the no-frame case (WNDO before the first
        # pixels): also cached, also faded by alpha.
        self._switch_fill: "pygame.Surface | None" = None
        self._switch_mark_t0 = 0.0   # when the mark's cycle started
        self._switch_alpha = 0.0     # current veil alpha, 0..SWITCH_ALPHA
        self._switch_alpha0 = 0.0    # the alpha the fade-out started from
        self._switch_ramp_t0 = 0.0   # when the fade-out started
        # The veil must never outlive an honest transition: past this
        # monotonic deadline it is dropped outright, whatever phase it is
        # in. Armed by enter_switch_mode, cleared by the teardown.
        self._switch_deadline = 0.0
        self._switch_ret = (0, 0)  # layer size to restore on exit
        self._switch_pending = None  # deferred resize (w, h) while active

    def _sync_cursor(self) -> None:
        """Cursor over the menu area: move and resize arrows.

        Set only on change - calling set_cursor every frame makes the cursor
        flicker noticeably.
        """
        want = (self.menu.desired_cursor if self.menu.visible
                else pygame.SYSTEM_CURSOR_ARROW)
        if want == getattr(self, "_cursor", None):
            return
        self._cursor = want
        try:
            pygame.mouse.set_cursor(want)
        except Exception:
            pass

    @property
    def theme(self) -> dict:
        """The menu theme palette - alerts are kept in the same look."""
        return ui_palette(self.menu.state.get("theme", "light"))

    def get_hwnd(self) -> int:
        """HWND of the overlay window - the parent for native dialogs."""
        try:
            return int(pygame.display.get_wm_info()["window"])
        except Exception:
            return 0

    @staticmethod
    def _rgb(color: str) -> tuple[int, int, int]:
        c = color.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)

    def set_lang(self, lang: str) -> None:
        """Switch the HUD status language (en/ru/fr/...)."""
        if lang in STRINGS and lang != self._lang:
            self._lang = lang
            # The HUD/alert fonts follow the language: CJK scripts have no
            # glyphs in the default font (tofu boxes).
            self._font = self._load_font(size=self.font_size, mono=True)
            # Scaled, the way __init__ builds it. Without the ui_scale the
            # alert shrank the first time the language changed and stayed
            # small - on a 125% display 45 px became 37 (audit).
            self._alert_font = self._load_font(
                size=max(10, int(round(ALERT_FONT_SIZE * self.ui_scale))))

    def set_visible(self, visible: bool) -> None:
        """Show/hide the window (SW_SHOW/SW_HIDE).

        With NR OFF the window is hidden completely so the desktop does not
        slow down (no capture/blit/flip). With NR ON it is shown again.
        """
        if visible and self._reveal_pending:
            # The first frame has not arrived yet (the window is hidden on
            # purpose - SDL_WINDOW_HIDDEN at creation to avoid the blank
            # flash). _follow_window would happily show it early; only
            # reveal() may show the window for the first time.
            return
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            # SW_SHOWNA, not SW_SHOW: showing must not activate. Right after a
            # hotkey Windows lets this process take the foreground, and a
            # plain show then took the keyboard from the game (Num1 back on,
            # the veil after Num5) - the rule set_menu_input writes down.
            user32.ShowWindow(hwnd, 8 if visible else 0)  # SW_SHOWNA=8, SW_HIDE=0
            self._visible = visible
        except Exception:
            pass

    def reveal(self) -> None:
        """Show the window once a real frame has arrived.

        The window is created hidden (SDL_WINDOW_HIDDEN) to avoid a blank
        flash over the desktop during the NGX warm-up. Called after the
        first successful frame exchange in main; idempotent - after the
        first call the window is simply visible and the mode switches go
        through set_visible() (which hides it for minimised windows etc).
        """
        if self._reveal_pending:
            try:
                hwnd = pygame.display.get_wm_info()["window"]
                user32.ShowWindow(hwnd, 8)  # SW_SHOWNA - see set_visible
                self._visible = True
            finally:
                self._reveal_pending = False

    def is_visible(self) -> bool:
        return getattr(self, "_visible", True)

    def _set_topmost(self) -> None:
        """HWND_TOPMOST - the window always sits above the rest (overlay)."""
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            # SWP_NOACTIVATE too: a z-order change is not a reason to take
            # the keyboard (set_fullscreen_layer and the veil call this).
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0,
                                              0x0001 | 0x0002 | 0x0010)  # NOSIZE|NOMOVE|NOACTIVATE
        except Exception:
            pass

    def move_to(self, x: int, y: int) -> None:
        """Put the overlay's top-left corner at (x, y) on the desktop.

        The one-window mode needs it: the overlay is the size of the window
        being processed and has to sit exactly on it. Topmost is reasserted on
        the way (the insert-after argument), so a game raising itself does not
        end up above the HUD.
        """
        # The layer covers the screen, so it does not travel with the window
        # any anymore - it only has to remember where the window is, for the
        # frame blit when the worker is not presenting. This used to be a
        # SetWindowPos on every frame the target moved.
        self._window_layer = (int(x), int(y),
                              self._frame_size[0] if self._frame_size else 0,
                              self._frame_size[1] if self._frame_size else 0)
        screen = self._screen_rect()
        if screen is not None and (self.width, self.height) == (screen[2], screen[3]):
            return
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            # SWP_NOSIZE | SWP_NOACTIVATE - move only, never take the focus.
            user32.SetWindowPos(hwnd, -1, int(x), int(y), 0, 0, 0x0001 | 0x0010)
        except Exception as exc:
            print(f"Display: WARNING could not move the overlay: {exc}")

    def set_origin(self, x: int, y: int) -> None:
        """Where this monitor's top-left corner sits on the virtual desktop.

        The overlay is the size of ONE monitor; only the primary has its
        corner at (0,0). A window created at (0,0) while the capture runs
        on a second monitor covers the PRIMARY screen - the user sees
        nothing where they are looking (issues #28, #33).
        """
        self._origin = (int(x), int(y))
        self._move_to_origin()

    def _move_to_origin(self) -> None:
        """Move the window to the monitor's top-left corner (self._origin)."""
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            x, y = getattr(self, "_origin", (0, 0))
            # NO SWP_SHOWWINDOW here: the window is created hidden
            # (SDL_WINDOW_HIDDEN) and revealed only after the first real
            # frame - otherwise the blank window flashes over the desktop
            # during the NGX warm-up.
            flags = 0x0001 | 0x0010  # SWP_NOSIZE | SWP_NOACTIVATE
            if (x, y) == (0, 0):
                # The primary monitor: SDL already placed it there, and the
                # pre-multi-monitor behaviour (no move at all) stays intact.
                flags |= 0x0002  # SWP_NOMOVE
            ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, 0, 0, flags)
            # The worker needs this handle, and this is the one place that
            # runs after every creation AND every recreation of the window
            # (__init__, the layer resize, the fullscreen layer all end
            # here). Published through the environment because the worker is
            # a child process started later - and re-published on every move,
            # because a set_mode can hand us a different window and a stale
            # handle would silently put the picture back on top.
            #
            # Why the worker wants it: it reveals its picture window on the
            # first real frame, and ShowWindow puts a topmost window ABOVE
            # this one. Measured on a reporter's machine (#89): nine reveals,
            # nine losses of the top, each corrected 10-55 ms later - one to
            # three refreshes with no panel on screen. With the handle it
            # inserts the picture directly below this window instead, in one
            # operation, and there is no moment to catch.
            os.environ["NS_HUD_HWND"] = str(int(hwnd))
        except Exception:
            pass

    # -- window plumbing --------------------------------------------------

    def _load_font(self, size: int = FONT_SIZE, mono: bool = False,
                   bold: bool = False):
        """The loader handed to the menu - see fonts.py for the faces."""
        return fonts.load(size, mono=mono, bold=bold,
                          lang=getattr(self, "_lang", "en"))

    def _set_click_through(self) -> bool:
        """Click-through: WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_LAYERED.

        Verified by diagnostics (_work/diag_click.py, Win11 4K): WS_EX_TRANSPARENT
        WITHOUT WS_EX_LAYERED does NOT work - WindowFromPoint still returns the
        overlay and the click goes to the overlay. The working combination (per
        MSDN "Layered Windows"): a layered window + WS_EX_TRANSPARENT -> hit
        testing ignores the window shape and clicks go to the window under the
        cursor.

        The order is mandatory:
          1. SetWindowLongW(GWL_EXSTYLE, ... | WS_EX_LAYERED | WS_EX_TRANSPARENT
             | WS_EX_NOACTIVATE)
          2. SetLayeredWindowAttributes(alpha=255, LWA_ALPHA) - activates
             layered mode (without the call the window stays ordinary: the
             LAYERED flag is in exstyle but hit testing does not change)
          3. SetWindowPos(SWP_FRAMECHANGED) - drops the window style cache
             (required by the SetWindowLongW documentation)
        WS_EX_NOACTIVATE: the window does not take focus, the keyboard stays
        with the active application. Control is via global hotkeys
        (GetAsyncKeyState in main.py).
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_LAYERED = 0x00080000
            WS_EX_TOOLWINDOW = 0x00000080
            LWA_ALPHA = 0x2
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                  style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
                                  | WS_EX_LAYERED | WS_EX_TOOLWINDOW)
            # Activate layered mode: alpha=255 (an opaque window, only hit
            # testing changes, visually we touch nothing)
            user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
            # Drop the style cache - without SWP_FRAMECHANGED the
            # SetWindowLongW changes may not take effect
            SWP_NOACTIVATE = 0x0010
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                                | SWP_FRAMECHANGED | SWP_NOACTIVATE)
            self._click_through = True
            return True
        except Exception as exc:
            print(f"Display: WARNING click-through failed: {exc}")
            return False

    def _exclude_from_capture(self) -> bool:
        """SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) on the window.

        Makes the window invisible to screen capture (dxcam, OBS, etc.).
        Best-effort: warn on failure, never crash.
        """
        return self.set_excluded_from_capture(True)

    def set_excluded_from_capture(self, hide: bool) -> bool:
        """Hide the overlay from screen capture, or stop hiding it.

        Hiding is mandatory while the input is Desktop Duplication of the whole
        screen: without it the pipeline would capture its own output. With one
        window as the input there is no such loop, and then hiding is pure
        loss - it is what stops OBS from seeing the overlay and stops the
        NVIDIA App from recording at all.
        """
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            user32 = ctypes.windll.user32
            want = WDA_EXCLUDEFROMCAPTURE if hide else 0  # 0 = WDA_NONE
            ok = user32.SetWindowDisplayAffinity(hwnd, want)
            if not ok:
                print(f"Display: WARNING SetWindowDisplayAffinity({want}) failed "
                      f"(the overlay may be visible to screen capture)")
                return False
            self._excluded = bool(hide)
            return True
        except Exception as exc:
            print(f"Display: WARNING cannot change the capture affinity: {exc}")
            return False

    # -- public API -------------------------------------------------------

    def set_menu_input(self, enabled: bool) -> None:
        """Whether input should reach our layer (while the menu is open).

        Normally the window is click-through: WS_EX_TRANSPARENT gives clicks to
        whatever is underneath. While the menu is up the flag is removed - the
        clicks are ours and the game under the overlay does not get them.
        Exactly the ReShade behaviour. WS_EX_NOACTIVATE is removed too,
        otherwise there is no keyboard.
        """
        opening = bool(enabled) and not self._menu_input
        self._menu_input = bool(enabled)
        if opening:
            # Events are read only while the menu is open, and the closed
            # paths pump() without reading - so every key typed while the
            # menu was closed and our layer still had the focus stayed
            # queued, and the next open replayed it into the panel: a Space
            # toggled NR on the focused switch, an Esc closed the menu that
            # had just opened. Input from before the open was never meant
            # for the panel; window events stay queued.
            try:
                pygame.event.clear((pygame.KEYDOWN, pygame.KEYUP,
                                    pygame.TEXTINPUT, pygame.MOUSEBUTTONDOWN,
                                    pygame.MOUSEBUTTONUP, pygame.MOUSEWHEEL))
            except Exception:
                pass
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception as exc:
            print(f"Display: WARNING no hwnd for menu input: {exc}")
            return
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x2, 0x1, 0x4, 0x20
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enabled:
            style &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        else:
            style |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        # WS_EX_TOOLWINDOW stays on in both states: the overlay is an
        # instrument window, not an application - it must never create a
        # second taskbar button / Alt+Tab entry next to the 1x1 APPWINDOW
        # button (user: two thumbnails in the taskbar, a narrow settings one
        # and the big overlay one).
        style |= WS_EX_TOOLWINDOW
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        # SWP_NOACTIVATE: the frame change on every open and close must not be
        # the activation this method promises never to make (R2 below).
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED
                            | 0x0010)
        # R2: no focus is taken here - ever. The old code attached our thread
        # to the foreground thread and called SetForegroundWindow, which
        # re-stole the keyboard focus within 0.5 s of every Alt+Tab, monitor
        # change and window-mode rebuild that re-opened the menu (flicker
        # audit F2; the user-visible bug: the NR window "stops working"
        # while another window takes focus). The model now:
        #   - the global hotkeys are RegisterHotKey(NULL, ...) - they work
        #     without focus, so Num1..Num6 close/switch regardless;
        #   - the mouse works without focus (WS_EX_TRANSPARENT removal is
        #     what makes the clicks land on the panel);
        #   - the panel activates NATURALLY on the user's first click on it
        #     - a click is the user gesture Windows requires, no theft
        #     needed - and SDL's KEYDOWN (Esc, Enter, the remap capture)
        #     flows from that focus.
        # The NOACTIVATE style is still removed while the menu is open (the
        # click must be able to activate); the activation itself is left to
        # the user's click, never to our code.
        self._click_through = not enabled

    def resize(self, w: int, h: int) -> None:
        """Resize the HUD layer WITHOUT destroying the SDL window.

        pygame.display.set_mode() on the same display reuses the existing
        window (a soft resize), which is what a one-window mode switch needs:
        the old code went through display.close() -> pygame.quit() and rebuilt
        the whole window from scratch - the screen went black for a moment on
        every Num5 (user: screen flashes on mode switches).

        set_mode returns a NEW surface - it must become self.screen,
        otherwise main keeps drawing on the old (window-sized) surface and
        the layer never actually resizes. set_mode alone does NOT resize the
        physical window in SDL2, so the caller forces it with SetWindowPos.
        The recreated window loses EVERYTHING (layered attributes, capture
        affinity, input styles) - restore them here, once.

        While the mode-switch veil is up the resize is DEFERRED: the
        veil spans the full monitor on purpose (no bare desktop around
        the mark), and the teardown applies the pipeline size when it
        comes down.
        """
        # The pipeline's size is the FRAME's size, which is the monitor only
        # in full-screen mode. The layer itself stays the size of the screen
        # in either mode: shrinking it onto a window put the HUD, the menu
        # and every alert inside somebody else's window, and the expanding
        # and shrinking around that cost a set_mode each way - the SDL
        # window is recreated and every attribute re-applied - which is what
        # the blinking was (user, 13.09).
        self._frame_size = (w, h)
        screen = self._screen_rect()
        if screen is not None and (w, h) != (screen[2], screen[3]):
            w, h = screen[2], screen[3]
            if (self.width, self.height) == (w, h):
                return          # already the screen: nothing to recreate
        if self._switch_active:
            self._switch_pending = (w, h)
            return
        self.screen = pygame.display.set_mode((w, h), self._flags)
        self.width, self.height = w, h
        # A set_mode can recreate the physical window; put it back on the
        # chosen monitor (the origin belongs to the pipeline, not to SDL).
        self._move_to_origin()
        self.set_layer_state(self._layer_state, force=True)
        self.set_menu_opaque(self.menu.visible)
        self.set_excluded_from_capture(self._excluded)
        self.set_menu_input(self._menu_input)

    def set_fullscreen_layer(self, full_w: int, full_h: int) -> None:
        """Expand the HUD layer to the whole screen (menu open in window mode).

        In one-window mode the layer is the size of the captured window, so a
        menu dragged near the edge would be clipped by the window bounds. While
        the menu is open the layer is expanded to the full screen - the menu
        is then always fully visible - and set_window_layer() puts it back on
        the window when the menu closes. The capture affinity is re-applied
        because the window is recreated (user: menu lost outside a small
        window).

        While the mode-switch veil is up this is a NO-OP: the layer was
        already expanded to the full screen by enter_switch_mode, and the
        layering attributes (LWA_COLORKEY/ALPHA) belong to the veil until
        the teardown re-applies the pipeline's ones - tearing them down
        mid-veil turns the translucent layer opaque (audit M1).
        """
        if self._switch_active:
            return
        try:
            # set_mode returns a NEW surface - it must become self.screen,
            # otherwise main keeps drawing the menu on the old (window-sized)
            # surface and the layer never actually expands (user: the menu
            # was clipped and could not be dragged above the captured window).
            self.screen = pygame.display.set_mode((full_w, full_h), self._flags)
            self.width, self.height = full_w, full_h
            # set_mode alone does NOT resize the physical window in SDL2 -
            # force it, exactly like __init__ does (SWP_NOZORDER, with size),
            # at the chosen monitor's origin.
            hwnd = pygame.display.get_wm_info()['window']
            x, y = getattr(self, "_origin", (0, 0))
            user32.SetWindowPos(hwnd, 0, x, y, full_w, full_h, 0x0004)
            self._set_topmost()
            # The recreated window lost EVERYTHING: the layered attributes
            # (colorkey + alpha), the capture affinity and the input styles.
            self.set_layer_state(self._layer_state, force=True)
            self.set_menu_opaque(self.menu.visible)
            self.set_excluded_from_capture(self._excluded)
            self.set_menu_input(self._menu_input)
        except Exception as exc:
            print(f'Display: WARNING cannot expand the layer: {exc}')

    def set_window_layer(self, x: int, y: int, w: int, h: int) -> None:
        """Shrink the HUD layer back onto the captured window.

        While the mode-switch veil is up the size is DEFERRED, exactly
        like resize(): shrinking the layer mid-veil would uncover the
        desktop around the mark (the reported glitch), so the request
        waits and the veil's teardown lands the layer on it. The move
        happens then too - one place applies the geometry, not two.
        """
        # Where the captured window is, in desktop pixels. The layer is NOT
        # shrunk onto it any more - see below - but the frame blit needs the
        # offset when the worker is not presenting.
        self._window_layer = (int(x), int(y), int(w), int(h))
        if self._switch_active:
            return
        # It used to shrink the layer onto the window here, and the menu
        # expanded it again on the way in. That dance cost a
        # pygame.display.set_mode each way - the SDL window is recreated and
        # every attribute re-applied - and once alerts started needing the
        # full screen too it became several per second: "a lot of blinking
        # of both the alert and the menu when picking windows" (user, 13.09).
        #
        # So the layer stays the size of the screen in one-window mode. It is
        # click-through and colour-keyed, so a layer nobody has drawn on is
        # not visible; what changes is that the HUD, the menu and the alerts
        # are placed against the screen instead of against somebody's window,
        # which is where they were asked to be in the first place.
        try:
            screen = self._screen_rect()
            if screen is not None and (self.width, self.height) != (screen[2], screen[3]):
                self.set_fullscreen_layer(screen[2], screen[3])
        except Exception as exc:
            print(f'Display: WARNING cannot hold the layer on the screen: {exc}')

    def set_menu_opaque(self, opaque: bool) -> None:
        """Drop the global window translucency while the menu is open.

        A sub-state of LAYER_HUD: the layer lives at BG_ALPHA so the HUD
        does not plaster over the picture, but the menu panel is nearly
        black and a bright frame shows through it - it reads as "too
        transparent". While the menu is up the HUD alpha goes to 255. The
        other two layer states are opaque by construction, so nothing is
        written for them; the flag is remembered for the next HUD apply,
        and the single writer turns it into the alpha.
        """
        opaque = bool(opaque)
        if opaque == self._menu_opaque:
            return
        self._menu_opaque = opaque
        if self._layer_state == LAYER_HUD:
            self._write_layer_attrs()

    def set_layer_state(self, state: str, force: bool = False) -> None:
        """Request the layer's appearance - the ONE entry point for it.

        LAYER_OPAQUE: alpha 255, no key. Python shows a picture that
        covers the layer on a plain opaque window.
        LAYER_HUD: the colour key plus BG_ALPHA (255 while the menu is
        open - the panel would read as translucent over a bright frame
        otherwise). The worker draws the frame in its own window, only
        the HUD stays here.
        LAYER_PICTURE_KEYED: the colour key plus alpha 255. Python shows
        a window-sized picture and the surround is cut out, so the live
        desktop shows through around it. Alpha stays 255: this layer
        carries the picture, and BG_ALPHA would make the processed frame
        see-through.

        Idempotent: the same request twice writes nothing, so a
        per-frame caller (show) cannot flap the attributes - two
        writers with different values was the blink of d10cfd4/e46a24b.
        force=True re-applies the value even when it did not change: a
        window recreation (set_mode) drops every layered attribute, and
        the re-assert must restore the state that is actually in force.
        """
        if state == self._layer_state and not force:
            return
        self._layer_state = state
        self._write_layer_attrs()

    def set_hud_only(self, enabled: bool, force: bool = False) -> None:
        """The pipeline's HUD switch: True - LAYER_HUD, False - LAYER_OPAQUE.

        HUD mode: the worker's window carries the picture, and only the
        HUD, alerts and menu stay here, cut out of what is below by the
        colour key. Off: this window owns the picture again, on a plain
        opaque layer. (The third state, LAYER_PICTURE_KEYED, is requested
        by show() when a window-sized frame leaves a surround; it is never
        asked for through this switch.)
        """
        self.set_layer_state(LAYER_HUD if enabled else LAYER_OPAQUE, force)

    def _write_layer_attrs(self) -> None:
        """The ONE writer of SetLayeredWindowAttributes for the pipeline.

        Resolves the layer's state into a (key, alpha, flags) triple and
        applies it. Every appearance change - the state request, the
        menu-opaque sub-state, the re-applies after a window recreation -
        goes through here, so no two calls can fight over the window.

        Skipped while the mode-switch veil is up: for the duration of a
        switch the veil owns the window's attributes (its own alpha ramp,
        no key - see _apply_switch_window_alpha), and a pipeline write
        here would cut the fade short. The teardown re-applies the state
        once the veil comes down.
        """
        if self._switch_active:
            return
        try:
            hwnd = pygame.display.get_wm_info()["window"]
        except Exception as exc:
            print(f"Display: WARNING cannot get hwnd for the layer: {exc}")
            return
        if self._layer_state == LAYER_HUD:
            # COLORREF is 0x00BBGGRR, not RGB.
            r, g, b = CHROMA_KEY
            key = (b << 16) | (g << 8) | r
            # The panel translucency comes from the window's GLOBAL alpha
            # (LWA_ALPHA), NOT from the alpha of the panel pixels: a
            # semi-transparent SRCALPHA blend with the magenta background
            # would give a colour != key and a pink slab (the colour key
            # does not cut out a blended colour). The colour key removes
            # the background entirely, and the opaque panel (plus text)
            # becomes slightly see-through through the global alpha.
            # The translucency is NR-only. Under Frame Generation the picture
            # beneath the panel runs at 2-3x the network rate, and the 8%
            # see-through lets it modulate the panel text every recompose -
            # the panel shimmers at the FG rate (user 14.09: active flicker
            # over the program window with FG on). Opaque panel while FG
            # interpolates; back to 235 when only the network drives it.
            alpha = 255 if (self._menu_opaque or self._hud.get("display_fps")) else BG_ALPHA
            ok = user32.SetLayeredWindowAttributes(
                hwnd, key, alpha, LWA_COLORKEY | LWA_ALPHA)
        elif self._layer_state == LAYER_PICTURE_KEYED:
            # The key with NO translucency: the frame is the picture and
            # must not become see-through - only the surround is cut out.
            r, g, b = CHROMA_KEY
            key = (b << 16) | (g << 8) | r
            ok = user32.SetLayeredWindowAttributes(
                hwnd, key, 255, LWA_COLORKEY | LWA_ALPHA)
        else:
            ok = user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
        if not ok:
            print(f"Display: WARNING SetLayeredWindowAttributes failed "
                  f"(layer state {self._layer_state})")
        self._last_overlay = 0.0  # the next draw_overlay redraws immediately

    def follow_taskbar_desktop(self) -> None:
        """Put the overlay windows on the same virtual desktop as the button.

        Windows 11 keeps the 1x1 taskbar window with the desktop the user
        moved the program to (through Task View), while the borderless HUD
        and picture windows can stay behind - the menu then opens on a
        desktop the user is not looking at and reads as "the program does
        not expand" (issue #93, user Saymoin: "if you put the NeuralScreen
        on desktop 2 ... does not expand").

        Quietly does nothing when the interface or the taskbar window is
        unavailable, and when both windows are already together (so it is
        safe to call on every menu show).
        """
        try:
            ref = user32.FindWindowW("NeuralScreenTaskbar", "NeuralScreen")
            if not ref:
                return
            hud = None
            try:
                hud = pygame.display.get_wm_info()["window"]
            except Exception:
                hud = None
            # The HUD only. MoveWindowToDesktop works on the calling
            # process's own windows, and the picture belongs to the worker -
            # it follows the panel itself (FollowPanelDesktop in the worker).
            moved = bool(hud) and vdesk.follow_window(hud, ref)
            if moved:
                self._last_overlay = 0.0  # force a redraw after the move
        except Exception:
            pass

    def raise_topmost(self) -> None:
        """Keep the HUD above the worker's picture, and the pair above strangers.

        Both windows are topmost. When a stranger takes the top, the HUD goes
        up first and the picture is inserted directly BELOW it - one order,
        with no moment in between where the picture is above the panel. It
        used to raise the picture first and the HUD second, and a refresh that
        landed between the two calls showed the picture over the panel: the
        blink #96 describes. The worker's own reveal has always used this
        insert-after order, measured working on the reporters' machines.
        """
        # SWP_NOACTIVATE: the 30-frame re-assert must not steal the keyboard
        # focus back from the user (audit 10.09 F2: with the menu open the
        # overlay has WS_EX_NOACTIVATE removed, and a SetWindowPos that
        # activates re-steals focus <=0.5 s after Alt+Tab / minimizing
        # another window).
        # Picture first, HUD last. Keep the two raises independent: a missing
        # or not-yet-created present window must never hide the HUD raise.
        hud = None
        try:
            hud = pygame.display.get_wm_info()["window"]
        except Exception:
            hud = None
        # Read once per decision, before anything is raised, so the line says
        # what the state WAS when the verdict was made.
        self._hud_state_last = self._hud_state(hud) if hud else "hud=none"
        present = None
        top = None
        top_is_foreign = False
        # Only a window over OUR layer covers it: one on the other monitor, or
        # beside the panel in window mode, is none of the guard's business,
        # and raising the pair over it was churn (#96).
        area = None
        if hud:
            try:
                rect = wintypes.RECT()
                if user32.GetWindowRect(hud, ctypes.byref(rect)):
                    area = (rect.left, rect.top, rect.right - rect.left,
                            rect.bottom - rect.top)
            except Exception:
                area = None
        try:
            present = user32.FindWindowW("NeuralScreenPresent", "NeuralScreen")
            self._hud_state_last += " | " + self._picture_state(present)
            top = self._top_real_window(
                tuple(w for w in (hud, present) if w), area)
            # "Foreign" is decided by HWND, not by the window class: a game
            # or test helper built on SDL/pygame IS class "pygame" too, and
            # the old class check read it as our own HUD and skipped the
            # raise - the NR output stayed under a topmost foreign window
            # (the focus z-order test caught exactly this).
            top_is_foreign = top is not None and top != hud and top != present
            # Without a panel there is no order to keep: the picture alone
            # goes back over the stranger. Only when something ELSE took the
            # top slot: an unconditional insert every 30 frames churns the
            # pair's z-order (flicker audit, finding 1).
            if present and top_is_foreign and hud is None:
                user32.SetWindowPos(present, -1, 0, 0, 0, 0,
                                    0x0001 | 0x0002 | 0x0010)
        except Exception:
            present = None
            top = None
        try:
            if hud is None:
                return
            if top == hud:
                # The HUD is on top; nothing to do. Logged anyway: a user log
                # that never shows this line means the guard never ran or
                # never reached here, which is a different bug from "it ran
                # and chose wrong" - and telling those apart is the whole
                # point of the decision log.
                self._zlog("hud-on-top", f"top={describe_window(top)}")
            elif present is not None and top == present:
                # `present is not None` is load-bearing: the walk and the
                # FindWindow above share one try/except, and when it fires
                # BOTH end up None - `None == None` then read as "the picture
                # is above the panel" and raised a window that may not even
                # exist. Exposed by the decision-log test the moment the walk
                # started taking an argument (audit 20.09).
                #
                # The picture took the band (a worker restart re-asserts it
                # HWND_TOPMOST): bring the HUD back above it. NO SWP_NOZORDER
                # here - that flag makes SetWindowPos ignore hWndInsertAfter,
                # so the call was a silent no-op (#94: "the panel disappears
                # while I change settings"; the same after an NR/FG toggle,
                # which restarts the worker).
                #
                # Inserting after `present` does NOT work while both windows
                # are TOPMOST: inside the topmost band the order is decided by
                # the band, not by hWndInsertAfter, so the HUD has to be
                # re-asserted as topmost itself - it goes to the top of the
                # band. Measured: hud z=15 / present z=13 stayed that way with
                # the insert-after call, and flipped with this one.
                user32.SetWindowPos(hud, -1, 0, 0, 0, 0,
                                    0x0001 | 0x0002 | 0x0010)
                self._zlog("picture-above-hud", f"top={describe_window(top)}")
            elif top_is_foreign:
                # A foreign window took the topmost slot: re-assert the pair,
                # the panel first and the picture directly below it (see the
                # docstring). hWndInsertAfter=hud means "just under the hud":
                # the old note that insert-after "does not work" inside the
                # topmost band measured SetWindowPos(hud, present), which puts
                # the HUD under the picture by definition.
                user32.SetWindowPos(hud, -1, 0, 0, 0, 0,
                                    0x0001 | 0x0002 | 0x0010)
                if present:
                    user32.SetWindowPos(present, hud, 0, 0, 0, 0,
                                        0x0001 | 0x0002 | 0x0010)
                self._zlog("foreign-above-hud", f"top={describe_window(top)}")
            else:
                # top is None or the HUD is already above. "Nothing is above
                # us" and "the walk gave up before it got to us" used to print
                # the SAME line, and that is what made three packages from #96
                # look healthy while the guard was blind. They are two
                # different facts and they are two different words now.
                if top == hud:
                    decision = "hud-on-top"
                elif getattr(self, "_walk_exhausted", False):
                    decision = "walk-exhausted"
                else:
                    decision = "nothing-covers"
                self._zlog(decision,
                           f"top={describe_window(top) if top else 'none'}")
        except Exception:
            pass

    def _hud_state(self, hud) -> str:
        """Where our own panel is and what it looks like, as one field list.

        The guard could always name the window that took the top and never
        said a word about OUR window. Three different faults - the panel under
        the picture, the panel on the monitor the user is not watching, and
        the panel at alpha 0 - are the same log from here, which is how #96
        survived three diagnostic packages.

        The alpha and the key are READ BACK from the window rather than
        printed from what we last wrote: the layer's attributes have been
        disturbed from outside before, and a value we remember writing is not
        evidence that it is in effect.
        """
        try:
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hud, ctypes.byref(rect)):
                return "hud=?"
            visible = 1 if user32.IsWindowVisible(hud) else 0
            # IsWindowVisible says yes for a window DWM has cloaked (another
            # virtual desktop, a shell transition): it draws none of it.
            cloaked = _cloaked(hud)
            ex = user32.GetWindowLongW(hud, -20)          # GWL_EXSTYLE
            topmost = 1 if (ex & 0x00000008) else 0       # WS_EX_TOPMOST
            key = wintypes.COLORREF()
            alpha = ctypes.c_ubyte(0)
            flags = wintypes.DWORD(0)
            got = user32.GetLayeredWindowAttributes(
                hud, ctypes.byref(key), ctypes.byref(alpha),
                ctypes.byref(flags))
            attrs = (f"alpha={alpha.value} lwa=0x{flags.value:X}" if got
                     else "alpha=? lwa=?")
            return (f"hud=({rect.left},{rect.top},{rect.right},{rect.bottom}) "
                    f"visible={visible} cloaked={cloaked} topmost={topmost} "
                    f"layer={self._layer_state} {attrs}")
        except Exception:
            return "hud=?"

    def _picture_state(self, present) -> str:
        """The worker's picture window, as the same kind of field list.

        With the panel's own state on every [z] line the log could say where
        the panel was, but not where the picture that covers it was - and the
        two rectangles side by side are what separates "under the picture"
        from "on the other monitor".
        """
        if not present:
            return "pic=none"
        try:
            rect = wintypes.RECT()
            if not user32.GetWindowRect(present, ctypes.byref(rect)):
                return "pic=?"
            visible = 1 if user32.IsWindowVisible(present) else 0
            return (f"pic=({rect.left},{rect.top},{rect.right},{rect.bottom}) "
                    f"visible={visible} cloaked={_cloaked(present)}")
        except Exception:
            return "pic=?"

    def _zlog(self, decision: str, detail: str) -> None:
        """One throttled line per z-order decision.

        A user's log is the only view we get of this guard, and it runs every
        frame while the menu is open: printing every call would bury the
        interesting one. A line is written when the DECISION CHANGES, and
        again after QUIET seconds so a state that persists is still visible
        next to a later event. Never raises - diagnostics must not be able to
        break the render path.
        """
        try:
            now = time.monotonic()
            # The panel's own state is part of the signature: a panel that
            # moves to another monitor or loses its alpha is a CHANGE worth a
            # line, even when the verdict about other windows is unchanged.
            self._hud_state_cached = getattr(self, "_hud_state_last", "hud=?")
            sig = (decision, detail, self._hud_state_cached)
            if sig == self._zlog_sig and now - self._zlog_t < 5.0:
                return
            changed = sig != self._zlog_sig
            self._zlog_sig = sig
            self._zlog_t = now
            print(f"[z] {decision}{' (changed)' if changed else ''} "
                  f"{detail} | {self._hud_state_cached}")
        except Exception:
            pass

    def _top_real_window(self, ours: tuple = (),
                         area: tuple | None = None) -> int | None:
        """The first VISIBLE window in the z-order walk that can COVER us.

        The old guard took GetTopWindow() at face value - and on this
        machine the top of the z-order is a stack of helper windows:
        invisible 0x0 IME/MSCTFIME/ForegroundStaging entries, a VISIBLE
        1x1 ThumbnailDeviceHelperWnd (dwm) and an off-screen 20x20
        NarratorHelperWindow. Every 30-frame check read the first of those
        that passed a naive size test as "a foreign window took the top
        slot" and re-asserted the pair with two SetWindowPos calls - a DWM
        recompose ~10 times a second, the hard blinking of the panel over
        the picture window (user, 15.09; the worker's own
        ReassertPresentTopmost skips zero-sized windows, the client guard
        never did).

        `ours` are the HWNDs of our own pair: the walk stops on them too,
        because "is the picture above the panel" cannot be answered by a walk
        that only looks for strangers.

        THE BOUND USED TO BE SIXTEEN STEPS, and that was the bug behind #96.
        Helper windows are skipped but still spend steps, and on a busy
        desktop our windows sit deeper than that. Measured on the dev machine
        (20.09.2026): walking 200 entries, the first window that could cover
        anything was the SIXTEENTH and the next the forty-seventh - fifteen of
        the first sixteen were helpers. The walk ended exactly at the boundary
        and returned "found nothing", on which the guard does nothing at all.
        In the reporter's packages the healthy verdict `hud-on-top` appears
        ZERO times in two sessions, against 63 on a machine where the same
        code works.

        A window counts only if it can actually be covering our layer:
        visible, at least HELPER_MIN_PX in both dimensions, and its rect
        intersecting the VIRTUAL desktop - not the primary screen. On a
        second monitor to the left of the primary every window has a
        negative x (a full-screen window on it is (-2560, 0, 0, 1440)), and
        the old test `rect.right > 0 and rect.left < screen_w` rejected it:
        the walk found no real window, the HUD was never re-asserted above
        the picture, and the panel stayed hidden while still being drawn
        (#89 - visible in a screenshot, invisible on screen). The Narrator
        helper lives at -40000,-40000, so it still fails the intersection
        with the virtual bounds.

        `area` narrows that to our own layer (x, y, w, h) when the caller
        knows it: a window on the other monitor cannot cover the panel. And a
        window DWM has cloaked (an immersive host on another virtual desktop,
        the Start and Search hosts at rest) is visible to IsWindowVisible and
        drawn by nobody - it does not count either.
        """
        HELPER_MIN_PX = 16
        self._walk_exhausted = False
        try:
            hwnd = user32.GetTopWindow(None)
            for _ in range(WALK_LIMIT):
                if not hwnd:
                    return None
                visible = bool(user32.IsWindowVisible(hwnd))
                # OUR two windows end the walk as themselves, before the size
                # test: which of them is higher is the whole question this
                # guard exists to answer, and a picture window sized to a
                # small captured window could otherwise be skipped as a
                # helper. An INVISIBLE one is not a stopping point - the thing
                # above us is then whatever is above IT.
                if visible and hwnd in ours:
                    return hwnd
                ok = False
                if visible and not _cloaked(hwnd):
                    rect = wintypes.RECT()
                    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        ok = window_can_cover(rect, self._virtual_screen(),
                                              HELPER_MIN_PX)
                        if ok and area is not None:
                            ok = window_can_cover(rect, area, HELPER_MIN_PX)
                        # Shell chrome and our own program's windows are not
                        # "something that covered us": raising the pair over
                        # them is the churn that reads as the panel blinking
                        # (#96 taskbar, #107 Save As dialog), and the worker
                        # applies the same rule in ReassertPresentTopmost.
                        if ok and self._own_or_shell_window(hwnd):
                            ok = False
                if ok:
                    return hwnd
                hwnd = user32.GetWindow(hwnd, 2)   # GW_HWNDNEXT
            # The chain outlasted the limit. NOT the same answer as "nothing
            # is above us", and it used to be logged as if it were.
            self._walk_exhausted = True
        except Exception:
            pass
        return None

    def _own_or_shell_window(self, hwnd) -> bool:
        """Whether a window belongs to us or to the shell, not to a stranger.

        Two windows that take the topmost slot on a normal desktop and mean
        nothing by it - both named in a reporter's log (#107):

          Shell_TrayWnd, XamlExplorerHostIslandWindow, tooltips_class32,
          SysDragImage      - the shell, taken whenever the pointer is near
                              the taskbar (pid 8020 in that session);
          #32770 'Save As'  - the program's OWN modal dialog, which is what
                              the user is looking at while a screenshot is
                              being written (pid 46044, the client itself).

        Reading either as "a stranger took the top" made the guard raise the
        picture and then the HUD - two SetWindowPos on a state that was
        already correct, which is one DWM recompose with the picture over the
        panel in between. That is the flash this guard was accused of.

        The shell's pid comes from its desktop window (GetShellWindow) rather
        than from the taskbar's class name: the flyouts above the taskbar are
        not Shell_TrayWnd, and a class list would have to name every one.
        """
        try:
            kernel32 = ctypes.windll.kernel32
            user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            # Re-read when unknown, when it was 0 (autostart before the shell
            # was up) and every few seconds (explorer restarted under us):
            # a stale pid made the new taskbar read as a stranger again.
            now = time.monotonic()
            if (not self._shell_pid
                    or now - getattr(self, "_shell_pid_t", 0.0) > 5.0):
                shell = user32.GetShellWindow()
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(shell, ctypes.byref(pid))
                self._shell_pid = int(pid.value) if shell else 0
                self._shell_pid_t = now
            if self._own_pid is None:
                self._own_pid = int(kernel32.GetCurrentProcessId())
            pid = wintypes.DWORD(0)
            if not user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid)):
                return False
            got = int(pid.value)
            return got != 0 and got in (self._own_pid, self._shell_pid)
        except Exception:
            return False

    @staticmethod
    def _virtual_screen() -> tuple[int, int, int, int]:
        """The whole desktop as (x, y, w, h) - negative origins included.

        SM_XVIRTUALSCREEN..SM_CYVIRTUALSCREEN cover every monitor; the plain
        screen metrics answer for the PRIMARY one only, which is why a window
        on a left-hand monitor (negative x) used to be invisible to the
        z-order guard (#89).
        """
        try:
            vx = user32.GetSystemMetrics(76)      # SM_XVIRTUALSCREEN
            vy = user32.GetSystemMetrics(77)      # SM_YVIRTUALSCREEN
            vw = user32.GetSystemMetrics(78)      # SM_CXVIRTUALSCREEN
            vh = user32.GetSystemMetrics(79)      # SM_CYVIRTUALSCREEN
            if vw > 0 and vh > 0:
                return vx, vy, vw, vh
            if vw <= 0:
                vx, vw = 0, 1
            if vh <= 0:
                vy, vh = 0, 1
            return vx, vy, vw, vh
        except Exception:
            # A headless or odd session: an empty desktop is the safe answer
            # (the guard then skips nothing, which is what it did before).
            return 0, 0, 0, 0

    def enter_switch_mode(self, last_frame: "np.ndarray | None" = None,
                          full_w: int = 0, full_h: int = 0) -> None:
        """Freeze a blurred copy of the screen with the assembling mark on top.

        Called when the pipeline is being rebuilt (window-mode switch, monitor
        change, resolution change): the previous worker dies, the next one
        warms up for ~1 s and the desktop would sit bare underneath. The layer
        is expanded to the full screen (full_w/full_h) and the frozen frame
        (darkened + blurred) covers the gap (user: mode-switch flashes
        black/translucent).

        The veil eases IN instead of cutting to full strength - on the HUD
        path the fade rides on the window's own alpha; on the fallback path
        the frozen base is re-blitted and the dimmed copy fades in over it.
        Both ramps are advanced by _draw_switch / _finish_switch_if_due.
        """
        if self._switch_active:
            # A switch during the fade-out (the pair of calls a window
            # probe makes, or a second command): bring the veil back to
            # full instead of letting it dissolve under the rebuild.
            self._switch_phase = "on"
            self._switch_alpha = float(SWITCH_ALPHA)
            self._switch_deadline = time.monotonic() + SWITCH_HOLD_MAX
            if last_frame is not None:
                self._freeze_switch_frame(last_frame, *self.screen.get_size())
            self._apply_switch_window_alpha()
            self.draw_overlay(0.0)
            return
        cw, ch = self.screen.get_size()
        fw = full_w if full_w > 0 else cw
        fh = full_h if full_h > 0 else ch
        self._switch_active = True
        self._switch_phase = "on"
        self._switch_mark_t0 = time.monotonic()
        self._switch_deadline = self._switch_mark_t0 + SWITCH_HOLD_MAX
        self._switch_ret = (cw, ch)
        self._switch_alpha = 0.0
        print(f"[main] switch overlay ON (layer {cw}x{ch} -> {fw}x{fh}, "
              f"frame={'yes' if last_frame is not None else 'none'})")
        self._freeze_switch_frame(last_frame, fw, fh)
        if self._switch_dim_soft is None:
            # Nothing to freeze: a light semi-opaque veil, NOT black - the
            # desktop shows through (user: the veil must not go dark). The
            # alpha rides the same ramp, so this one fades too.
            try:
                # No SRCALPHA: the per-frame global alpha must apply on
                # the blit, and a plain surface is the reliable way.
                self._switch_fill = pygame.Surface((fw, fh))
                self._switch_fill.fill((36, 34, 38))
            except Exception:
                self._switch_fill = None
        # Expand the layer to the full screen. The overlay is SEMI-transparent
        # (LWA_ALPHA): the live desktop stays visible behind it, so when no
        # frozen frame exists (WNDO mode - the pixels never come to Python)
        # the user sees the desktop through a light veil instead of a black
        # screen (user: the switch overlay must not go dark). The window's
        # alpha is written directly, not through _write_layer_attrs(): for
        # the duration of a switch the veil owns the window's attributes
        # (its own ramp, no key), and the state setter must not fight it -
        # _layer_state keeps the pipeline's state for the teardown.
        try:
            self.screen = pygame.display.set_mode((fw, fh), self._flags)
            hwnd = pygame.display.get_wm_info()["window"]
            x, y = getattr(self, "_origin", (0, 0))
            ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, fw, fh, 0x0004)
            self._set_topmost()
            # The colour key is dropped for the veil: it must not mix with
            # the veiled pixels into a magenta wash. The global alpha is
            # the veil's own; the out-fade moves it back to the value the
            # pipeline expects before the teardown re-applies it.
            self._apply_switch_window_alpha()
            # set_mode re-created the window: every exstyle bit is gone
            # (TOOLWINDOW/TRANSPARENT/NOACTIVATE/LAYERED). Re-assert them or
            # the overlay gets a taskbar thumbnail again and eats clicks
            # during the switch (audit 10.09: enter/exit_switch_mode was the
            # only re-creation path that never restored the styles - a
            # fullscreen->fullscreen Num5 with the menu closed lost them
            # until the next menu open or pipeline resize).
            self.set_menu_input(self._menu_input)
        except Exception as exc:
            print(f"Display: WARNING cannot set the switch overlay up: {exc}")

        # A short synchronous fade-in. The caller is about to block the
        # loop for about a second (the worker stops and a new one warms
        # up), so a time-based ramp would not get a single frame - the
        # first frame the loop ever draws would show the veil at full
        # strength and the entrance would read as a cut. Six steps of
        # ~35 ms are drawn right here instead: the sharp freeze blurs
        # into the dim (the blur-in) and the mark arrives, visibly,
        # whatever happens next.
        self._switch_phase = "in"
        self._apply_switch_window_alpha()
        steps = 6
        step_s = SWITCH_FADE_IN / steps
        for i in range(1, steps + 1):
            frac = i / steps
            self._switch_alpha = float(SWITCH_ALPHA) * frac
            try:
                self._draw_veil(frac, time.monotonic())
                pygame.display.flip()
            except Exception as exc:
                print(f"Display: WARNING switch fade-in: {exc}")
                break
            time.sleep(step_s)
        self._switch_phase = "on"
        self._switch_alpha = float(SWITCH_ALPHA)
        self._apply_switch_window_alpha()

    def exit_switch_mode(self) -> None:
        """Drop the switch overlay - the first frame of the new pipeline is
        on its way (the caller shows it right after).

        The veil fades OUT instead of vanishing: the frame underneath is
        already being presented, and an instant cut back to it reads as a
        flash. The teardown (layer size restore, HUD attributes) waits
        until the fade finishes - is_switch_active() stays True for the
        duration, so follow_window and the resize paths keep their hands
        off the layer until it is really done.
        """
        if not self._switch_active:
            return
        if self._switch_phase == "out":
            # Already fading out: keep advancing the ramp. main calls this
            # on every frame of every output branch, so the fade finishes
            # even when draw_overlay is the only other call site.
            self._finish_switch_if_due(time.monotonic())
            return
        print("[main] switch overlay OFF")
        self._switch_phase = "out"
        self._switch_alpha0 = self._switch_alpha
        self._switch_ramp_t0 = time.monotonic()
        # The ramp ends at zero in both window modes: in plain mode the
        # dim fades off the frozen base, in HUD mode the window's own alpha
        # fades away to reveal the worker's window.
        self._finish_switch_if_due(time.monotonic())

    def _teardown_switch_overlay(self) -> None:
        """Drop the veil for good - the fade has finished.

        The resize that was deferred while the veil was up (the pipeline
        rebuilt underneath, the window size changed); apply it now, before
        the layered attributes are re-asserted. NOT while the menu is open:
        the menu keeps the layer expanded to the full screen
        (set_fullscreen_layer owns the size in that state) - shrinking it
        here is exactly the clipped-menu regression (user: menu cut off
        near the middle after a window-mode switch).

        A pending resize WINS over the restore-to-_switch_ret: on a monitor
        switch the pending size IS the new monitor's resolution (the overlay
        was expanded to it by enter_switch_mode) - falling through to the
        else would shrink the layer back to the OLD monitor size and leave
        stale geometry for the whole session (audit C1).
        """
        pending = self._switch_pending
        self._switch_pending = None
        if pending is not None and not self.menu.visible and \
                (self.screen.get_width(), self.screen.get_height()) != pending:
            self.resize(pending[0], pending[1])
        elif pending is None:
            ret = self._switch_ret
            # The layer was expanded to the full screen for the veil; put
            # it back on the size the pipeline expects when no explicit
            # resize came in (resize also re-applies the layered attributes
            # and the capture affinity). Still not while the menu is open -
            # the same clipped-menu rule as above.
            if not self.menu.visible and \
                    (self.screen.get_width(), self.screen.get_height()) != ret:
                self.resize(ret[0], ret[1])
        # The layer is back to its regular appearance - the state that is
        # actually in force, which may have changed while the veil owned the
        # window (a HUD request from a present enable, a window-sized
        # picture). In HUD mode the surface still holds the veil's pixels;
        # wipe them first, or the attribute swap (alpha -> BG_ALPHA) shows
        # the frozen dim for one frame.
        if self._layer_state == LAYER_HUD:
            try:
                self.screen.fill(CHROMA_KEY)
            except Exception:
                pass
        self.set_layer_state(self._layer_state, force=True)
        self._last_overlay = 0.0  # the next draw_overlay redraws immediately

    def is_switch_active(self) -> bool:
        """True while the mode-switch veil is up - fading in, holding or
        fading out. The layer belongs to the veil until this is False."""
        return self._switch_active

    def drop_switch_mode(self) -> None:
        """Take the veil down without waiting for a fade or a frame.

        The fade is advanced by draw_overlay, so a caller that is not
        drawing (the idle branch with the menu closed, where no frame will
        ever arrive to end the transition honestly) has to end it here.
        No-op when no veil is up.
        """
        if self._switch_active:
            print("[main] switch overlay dropped (no frame to end it)")
            self._drop_switch_now()

    def _finish_switch_if_due(self, now: float) -> None:
        """Complete a fade ramp and, for the out phase, tear the veil down.

        Called from draw_overlay (every frame while the veil animates) and
        from exit_switch_mode itself, so a veil whose caller stops drawing
        still comes down. The teardown is the old instant path - restore
        the pipeline size, the layered attributes, the cursor - now run
        once the fade is over.

        The hold cap is checked first: a veil past SWITCH_HOLD_MAX is
        dropped even with no frame and no draw on the way (the NR-OFF
        rebuild path), because nothing else would ever take it down.
        """
        if not self._switch_active:
            return
        if self._switch_deadline and now >= self._switch_deadline:
            self._drop_switch_now()
            return
        if self._switch_phase == "out":
            frac = (now - self._switch_ramp_t0) / SWITCH_FADE_OUT
            self._switch_alpha = max(0.0, self._switch_alpha0 * (1.0 - frac))
            self._apply_switch_window_alpha()
            if self._switch_alpha <= 0.0:
                self._drop_switch_now()

    def _drop_switch_now(self) -> None:
        """Take the veil down at once, whatever phase it is in.

        The honest way down is exit_switch_mode() plus a drawn frame, but
        the NR-OFF rebuild path has neither: the loop sits in the idle
        branch, no frame is received, and draw_overlay refuses to paint
        anything while the veil is up - so the menu stays covered by the
        frozen picture and is unreachable (issues #89/#96). Called from
        the fade completion and from the hold cap.
        """
        self._switch_active = False
        self._switch_phase = "off"
        self._switch_alpha = 0.0
        self._switch_deadline = 0.0
        self._switch_dim_soft = None
        self._switch_base = None
        self._switch_fill = None
        self._teardown_switch_overlay()

    def _freeze_switch_frame(self, last_frame, fw: int, fh: int) -> None:
        """Build the veil's two layers from the last picture.

        `_switch_base` is the sharp freeze (what the user was just
        looking at); `_switch_dim_soft` is the blurred and darkened copy
        the blur-in crossfades to. Both derive from the FRAME, never from
        the window surface: on the HUD path the surface holds the magenta
        key, which must not reach the veil. A missing frame leaves both
        None - the veil falls back to a flat fill.
        """
        if last_frame is None:
            return
        try:
            frame = pygame.image.frombuffer(
                last_frame, (last_frame.shape[1], last_frame.shape[0]), "RGBX")
            self._switch_base = pygame.transform.smoothscale(frame, (fw, fh))
            # Blur cheaply: downscale to a small target in one step,
            # upscale back. The downscale ratio sets the blur radius.
            small = pygame.transform.smoothscale(
                frame, (max(8, fw // 3), max(8, fh // 3)))
            dim = pygame.transform.smoothscale(small, (fw, fh))
            # Darken the frozen frame: the pipeline is being rebuilt, the
            # picture is stale. A 40% veil reads as "transition" rather
            # than "frozen desktop".
            dim.fill((96, 96, 96), special_flags=pygame.BLEND_RGB_MULT)
            # One copy carries the per-frame alpha (see _draw_switch).
            self._switch_dim_soft = dim
        except Exception as exc:
            print(f"Display: WARNING cannot freeze the switch frame: {exc}")
            self._switch_base = None
            self._switch_dim_soft = None

    def _apply_switch_window_alpha(self) -> None:
        """The window's global alpha for the veil's current phase.

        With a frozen picture the veil owns the whole screen, and its
        reveal differs by path: the HUD path fades the veil away to
        nothing, uncovering the new worker's window below; the plain
        path returns to the opaque layer while the dim lifts off the
        fresh frame pixel-side.

        Without a picture (WNDO before the first pixels) there is
        nothing to hold the desktop back - the window stays at the
        veil's own alpha in every phase; ramping it would bare the
        desktop, the flash the veil exists to hide.
        """
        ph = self._switch_phase
        # The key this write carries: OFF while the veil's own pixels cover
        # the screen (they must not be cut out - the frozen base is the last
        # captured frame and could contain an exact key pixel), and ON for
        # the fade-OUT over a KEYED picture. That picture's surround is
        # painted pure key, and with the key off that band showed as magenta
        # for the whole ramp - measured on the composed frames: the last
        # steps of the fade read (241,3,236) at alpha 248 instead of a
        # cut-out desktop. The teardown re-applies the state anyway.
        key_ref = 0
        flags = LWA_ALPHA
        if ph == "out" and self._layer_state == LAYER_PICTURE_KEYED:
            r, g, b = CHROMA_KEY
            key_ref = (b << 16) | (g << 8) | r
            flags = LWA_COLORKEY | LWA_ALPHA
        if ph == "off":
            a = 255.0
        elif ph == "out":
            v = max(0.0, min(1.0, self._switch_alpha / float(SWITCH_ALPHA)))
            if self._layer_state == LAYER_HUD:
                # Dissolve the veil to nothing, uncovering the new
                # worker's window below it.
                a = float(SWITCH_ALPHA) * v
            else:
                # Plain path (an opaque or a keyed picture layer): land
                # on the opaque window the pipeline expects - the pixel
                # fade inside it lifts the dim off the fresh frame, and
                # the window ends where the teardown picks it up.
                a = 255.0 - (255.0 - float(SWITCH_ALPHA)) * v
        else:
            # "in" and "on": the established veil look - translucent, the
            # desktop showing through (user: the veil must not go dark).
            # The entrance crossfade is pixel-side on both paths.
            a = float(SWITCH_ALPHA)
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            user32.SetLayeredWindowAttributes(hwnd, key_ref, int(round(a)), flags)
        except Exception as exc:
            print(f"Display: WARNING cannot set the veil alpha: {exc}")

    def _draw_switch(self) -> None:
        """The veil, as the loop draws it: advance the fade, then paint.

        The fade-out is the only time-based ramp left (the entrance is
        synchronous, see enter_switch_mode); when it completes inside
        this call the teardown has already run and the caller draws the
        regular layer in the same frame.
        """
        now = time.monotonic()
        self._finish_switch_if_due(now)
        if not self._switch_active:
            return
        strength = max(0.0, min(1.0, self._switch_alpha
                                 / float(SWITCH_ALPHA)))
        self._draw_veil(strength, now)

    def _draw_veil(self, strength: float, now: float) -> None:
        """Paint the veil at the given strength (0..1).

        One painter for both callers: the synchronous entrance passes the
        ramp fraction it is drawing, the loop's fade-out passes the
        current alpha over SWITCH_ALPHA. Layer 1 is the frozen picture -
        the sharp freeze with the blurred dim crossfading over it (the
        blur-in). Layer 2 is the mark: five tiles fly into a tight
        cluster, hold with a breath and scatter - the Assemble animation
        picked from the concept board, drawn procedurally (no assets, no
        deps). The mark comes up quickly and stays legible over the dim;
        the veil itself (window alpha on the HUD path, the dim's pixel
        fade elsewhere) carries the transition around it.
        """
        w, h = self.screen.get_size()
        ph = self._switch_phase
        strength = max(0.0, min(1.0, strength))
        # Layer 1: the frozen picture. The sharp freeze goes down first
        # and the blurred dim crossfades over it - the blur-in - on BOTH
        # paths (the pixels are ours to draw; the window alpha only ever
        # carried the veil's translucency). Once the veil starts
        # dissolving the base is skipped, so the live picture below
        # (show()'s fresh frame, or the worker's window) shows as the
        # dim lifts.
        if self._switch_base is not None and ph != "out":
            self.screen.blit(self._switch_base, (0, 0))
        if self._switch_dim_soft is not None:
            if ph == "in":
                a = int(round(255 * strength))
            elif ph == "out" and self._layer_state != LAYER_HUD:
                # Plain path: the dim lifts off the fresh frame; on the
                # HUD path the window's dissolve carries it instead.
                a = int(round(255 * strength))
            else:
                a = 255
            self._switch_dim_soft.set_alpha(a)
            # While a KEYED picture is fading in under the veil the dim is
            # clipped to the frame: the surround is the pure key, and a dim
            # blended over it would leave pixels that are no longer exactly
            # the key - a magenta band around the window for the last frames
            # of every window-mode switch (measured: (241,3,236) at alpha
            # 248 with the key bit off).
            clip = self._fade_clip()
            if clip is not None:
                self.screen.set_clip(clip)
            self.screen.blit(self._switch_dim_soft, (0, 0))
            if clip is not None:
                self.screen.set_clip(None)
        elif self._switch_fill is not None:
            # No picture to freeze: the flat fill is the veil's body and
            # rides the same crossfade the dim does. Clipped to the frame on
            # the keyed fade-out for the same reason the dim is: pixels
            # blended over the pure-key surround would no longer be cut out.
            if ph == "in" or (ph == "out" and self._layer_state != LAYER_HUD):
                a = int(round(255 * strength))
            else:
                a = 255
            self._switch_fill.set_alpha(a)
            clip = self._fade_clip()
            if clip is not None:
                self.screen.set_clip(clip)
            self.screen.blit(self._switch_fill, (0, 0))
            if clip is not None:
                self.screen.set_clip(None)
        else:
            self.screen.fill((36, 34, 38))
        # Layer 2: the mark.
        t = now - self._switch_mark_t0
        appear = min(1.0, t / 0.2)
        if ph == "out" and self._layer_state != LAYER_HUD:
            # Plain path: the mark goes with the dim, or the tiles would
            # hang over the fresh frame after everything else dissolved.
            mark_a = appear * strength
        else:
            mark_a = appear
        clip = self._fade_clip()
        if clip is not None:
            self.screen.set_clip(clip)
        self._draw_switch_mark(t, mark_a, w, h)
        if clip is not None:
            self.screen.set_clip(None)

    def _fade_clip(self) -> "pygame.Rect | None":
        """The clip rectangle for the veil's pixels during the fade-out.

        While a KEYED picture comes up under the veil, everything the veil
        draws outside the frame would blend over the pure-key surround -
        and blended pixels are no longer exactly the key, so the colour key
        can no longer cut them out: a magenta band around the window for
        the last frames of every switch. The veil's own pixels are clipped
        to the frame instead; inside the frame the veil dissolves over the
        picture exactly as before.
        """
        if (self._switch_phase == "out"
                and self._layer_state == LAYER_PICTURE_KEYED
                and self._frame_layer_rect is not None):
            return self._frame_layer_rect
        return None

    def _draw_switch_mark(self, t: float, alpha: float, w: int, h: int) -> None:
        """The assembling mark at time t, drawn at the given alpha.

        Tiles are solid squares with rounded corners; below full alpha
        each one rides a small per-tile surface so the whole mark can
        dissolve instead of cutting.
        """
        if alpha <= 0.01:
            return
        accent = self._rgb(self.theme["accent"])
        for x, y, size, is_accent in _assemble_tiles(t, w, h):
            s = max(2, int(round(size)))
            color = accent if is_accent else (255, 255, 255)
            pos = (int(round(x - s / 2.0)), int(round(y - s / 2.0)))
            if alpha >= 0.99:
                pygame.draw.rect(self.screen, color,
                                 pygame.Rect(pos[0], pos[1], s, s),
                                 border_radius=max(1, s // 5))
            else:
                pad = pygame.Surface((s, s), pygame.SRCALPHA)
                pad.fill((*color, int(round(255 * alpha))))
                self.screen.blit(pad, pos)


    def refresh_colorkey(self) -> None:
        """Reapply the layer's attributes after something dropped them.

        Needed after operations that can drop the window's layered attributes
        (z-order shuffling with the tkinter settings menu: the window stays
        opaque and the HUD is not visible). Goes through the single writer
        like every other appearance change; recreates nothing.
        """
        self._write_layer_attrs()

    def draw_capture_overlay(self, surface: pygame.Surface) -> None:
        """Bake the open menu into a recorded or screenshot frame.

        The frame is grabbed WITHOUT our pygame layer: the window is marked
        WDA_EXCLUDEFROMCAPTURE, otherwise DDA would capture our own output.
        So everything that must end up in the file is drawn here, on top of the
        pixels already received.

        We bake the menu only. The HUD and the watermark used to be baked too,
        but they are not on screen - in the file they looked like someone
        else's caption.
        """
        if not self.menu.visible:
            return
        # The "grabbed an edge" highlight is cursor state and has no place in
        # the file: it would freeze on the frame for no visible reason.
        hover, self.menu.hover = self.menu.hover, None
        # The panel is placed against the SCREEN, not against the frame. `draw`
        # re-lays it out for whatever surface it is handed, so passing the
        # captured frame put the panel at the frame's centre instead of the
        # screen's - measured 524 px away in one-window mode, which is what the
        # reporter saw as "transparent / wrongly composited" (#107). The frame
        # gets the panel shifted by its own origin, so it appears where the user
        # saw it.
        origin = getattr(self, "_window_layer", None)
        # self.width/height is the SCREEN the layer covers; _window_layer is the
        # captured window inside it. In one-window mode the frame being saved is
        # that window, so the panel - laid out for the screen - must be shifted by
        # the window's origin.
        screen = (self.width, self.height)
        same_view = (origin is None
                     or tuple(surface.get_size()) == (int(screen[0]),
                                                      int(screen[1])))
        try:
            self.menu.set_stats(self._hud)
            if same_view:
                self.menu.draw(surface)
            else:
                self._draw_menu_at_screen_position(surface, screen, origin)
        finally:
            self.menu.hover = hover

    def _draw_menu_at_screen_position(self, surface, screen, origin) -> None:
        """Draw the menu where the screen puts it, onto a smaller frame.

        The menu is composed on a screen-sized scratch surface and then blitted
        at the negative of the frame's origin, so only the part that belongs
        inside the frame lands in it. Composed on scratch rather than drawn
        straight onto the frame because the layout must use the SCREEN's size:
        drawing onto the frame would lay it out for the frame again.
        """
        sw, sh = int(screen[0]), int(screen[1])
        # The layer's own corner, subtracted exactly as show() subtracts it.
        # `_window_layer` is in VIRTUAL-DESKTOP pixels while the layer starts at
        # this monitor's corner, so on any monitor but the primary the raw
        # number is the origin too large: a window at desktop x=4000 on a
        # monitor starting at 3840 would push the menu 3840 px off the frame and
        # out of the file altogether.
        lx, ly = getattr(self, "_origin", (0, 0))
        ox, oy = int(origin[0]) - int(lx), int(origin[1]) - int(ly)
        scratch = pygame.Surface((sw, sh), pygame.SRCALPHA)
        scratch.fill((0, 0, 0, 0))
        saved = list(self.menu.offset)
        try:
            # The menu's own offset is relative to the screen; the scratch is the
            # screen, so only the frame's origin is subtracted.
            self.menu.offset = [saved[0], saved[1]]
            self.menu.draw(scratch)
        finally:
            self.menu.offset = saved
        surface.blit(scratch, (-ox, -oy))

    def draw_overlay(self, min_interval: float = 0.1) -> None:
        """Redraw the HUD over the frame the worker is showing.

        Throttling: a 4K fill plus flip costs ~5 ms while the HUD changes a
        couple of times per second - doing it every frame is wasted time.
        Alerts appear and disappear out of band, so for them the redraw is
        immediate.
        """
        try:
            pygame.event.pump()
        except Exception:
            pass
        now = time.monotonic()
        alerts = len(self._alerts)
        # With the menu open the throttle is disabled - a slider under the
        # mouse jitters at 10 Hz. But an UNTOUCHED menu over the picture used
        # to flip the whole fullscreen layer at the main loop's rate (60-144
        # Hz): every flip recomposes the topmost layer over the picture
        # window, and the HUD's 8% translucency let the moving picture modulate
        # the panel - the shimmer the user sees whenever the program's UI sits
        # above the effect window (user, 14.09). Keep the full rate only while
        # the mouse is actually interacting with the menu (a drag, or the
        # cursor over the panel); a menu nobody touches redraws at the HUD's
        # own 10 Hz.
        if self.menu.visible and not self.menu.dragging \
                and not self.menu.hover:
            pass  # fall through to the normal throttle below
        elif self.menu.visible:
            min_interval = 0.0
        if self._switch_active:
            # The veil animates - the mark must not run at the HUD's 10 Hz.
            min_interval = 0.0
        if now - self._last_overlay < min_interval and alerts == self._last_alert_count:
            return
        self._last_overlay = now
        self._last_alert_count = alerts
        if self._switch_active:
            # The veil replaces everything else: no menu, no HUD, no
            # cursor - just the frozen picture and the mark. It animates,
            # so the throttle must not skip the redraw. If the fade-out
            # finishes inside this call, fall through and draw the
            # regular layer in the same frame - nothing goes missing.
            self._draw_switch()
            if self._switch_active:
                pygame.display.flip()
                return
        self.screen.fill(CHROMA_KEY)
        self._draw_alerts()
        self._draw_rec_indicator()
        self._draw_fps_badge()
        self.menu.set_stats(self._hud)
        self.menu.draw(self.screen)
        self._draw_window_highlight()
        self._sync_cursor()
        pygame.display.flip()

    def _draw_window_highlight(self) -> None:
        """The amber outline around the window hovered in the windows page.

        The menu reports the hwnd under the cursor (menu.hover_window);
        the frame is drawn in the overlay's own coordinates, so it sits on
        top of everything, exactly like the menu itself. The outline is
        amber (#FFBF00, the brand accent), 3 px, with a 1 px dark inner
        line so it reads on both light and dark windows.
        """
        hwnd = getattr(self.menu, "hover_window", None)
        if not hwnd:
            return
        try:
            rect = wintypes.RECT()
            if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
                return
            if rect.right <= rect.left or rect.bottom <= rect.top:
                return
            # GetWindowRect answers in VIRTUAL-DESKTOP coordinates; this
            # surface is the chosen monitor, whose corner is self._origin.
            # Without the subtraction the outline drew at the window's
            # absolute x - off the right edge on a second monitor, and on
            # the wrong place on any monitor that is not the primary. The
            # same origin mistake as issues #28/#33/#35, in the one place
            # that had not been fixed.
            ox, oy = getattr(self, "_origin", (0, 0))
            r = pygame.Rect(rect.left - ox, rect.top - oy,
                            rect.right - rect.left, rect.bottom - rect.top)
            lw = 3
            pygame.draw.rect(self.screen, (0x0D, 0x11, 0x17), r, lw + 2,
                             border_radius=4)
            pygame.draw.rect(self.screen, (0xFF, 0xBF, 0x00), r, lw,
                             border_radius=4)
        except Exception:
            pass

    def set_hud(self, data: dict) -> None:
        """Update HUD data: fps, status, resolution, params, frames."""
        self._hud = dict(data)

    def alert(self, text: str, duration: float = 2.5) -> None:
        """Show a pop-up alert at the top of the SCREEN (amber border).

        In one-window mode the HUD layer is the size of the captured window
        and sits on it, so an alert could only be drawn inside that window -
        and an alert about the graphics card or the monitor has nothing to
        do with somebody's browser (user, 13.09: wherever it is in
        full-screen mode, that is where it belongs in window mode too).

        So the layer goes to the whole screen for as long as the alert is
        up, and back onto the window when it expires. That is the same
        thing the menu already does for the same reason - it would be
        clipped by the window bounds otherwise - through the same two
        calls, which have been carrying the mode switch since 1.6.
        """
        self._alerts.append((text, time.monotonic() + duration))

    def _fill_around(self, keep: "pygame.Rect") -> None:
        """Paint the layer with the key everywhere outside `keep`.

        Four rectangles rather than one full-screen fill: the frame is about
        to be blitted over the middle anyway, and at 4K the difference is a
        whole 8-megapixel fill per frame.
        """
        w, h = self.width, self.height
        keep = keep.clip(pygame.Rect(0, 0, w, h))
        if keep.width <= 0 or keep.height <= 0:
            self.screen.fill(CHROMA_KEY)
            return
        for rect in (pygame.Rect(0, 0, w, keep.top),
                     pygame.Rect(0, keep.bottom, w, h - keep.bottom),
                     pygame.Rect(0, keep.top, keep.left, keep.height),
                     pygame.Rect(keep.right, keep.top, w - keep.right,
                                 keep.height)):
            if rect.width > 0 and rect.height > 0:
                self.screen.fill(CHROMA_KEY, rect)

    def show(self, frame_rgba: np.ndarray) -> None:
        """Blit frame (RGBA uint8) fullscreen and draw the HUD on top.

        The frame: pygame.image.frombuffer(frame_rgba, ..., "RGBX") is
        zero-copy (the surface references the numpy buffer, without tobytes and
        a 33 MB copy at 4K). The RGBX format (32-bit, no alpha channel) blits
        to the screen faster than an RGBA surface with SRCALPHA (no alpha
        blending needed). The frame's alpha is unused - the frame is opaque
        (A=255).

        IMPORTANT: frombuffer does NOT copy the data - the surface lives as
        long as the numpy buffer does. The frame arrives from WorkerReader as a
        separate array per frame, so the surface is recreated each time (2 ms
        at 4K). event.pump() drains the Windows message queue (WM_PAINT and
        friends) - without it the window freezes on click/focus.
        """
        if frame_rgba is None:
            return
        try:
            pygame.event.pump()
        except Exception:
            pass
        try:
            surface = pygame.image.frombuffer(
                frame_rgba, (frame_rgba.shape[1], frame_rgba.shape[0]), "RGBX")
        except Exception:
            # Fallback: not a numpy array / incompatible shape - via tobytes
            surface = pygame.image.frombuffer(
                frame_rgba.tobytes(), (frame_rgba.shape[1], frame_rgba.shape[0]),
                "RGBA")
        # One blit+flip for both paths (the main RGBX and the RGBA fallback).
        # In one-window mode the layer is the whole screen while the frame is
        # the size of the captured window, so the frame goes where the window
        # is. Only reachable when the worker is NOT presenting - with the
        # worker's own window up, Python draws no frames at all.
        at = (0, 0)
        # A window-sized frame is one that does not COVER the layer. The check
        # used to look at the width alone, so a window as wide as the screen
        # but not as tall (a common shape) got no surround and no key - the
        # stale desktop stayed under its bottom strip.
        windowed = (self._window_layer is not None
                    and (surface.get_width() < self.width
                         or surface.get_height() < self.height))
        if windowed:
            ox, oy = getattr(self, "_origin", (0, 0))
            at = (self._window_layer[0] - ox, self._window_layer[1] - oy)
            # Everything the frame does not cover is the desktop, not our
            # stale back buffer - the swap chain is flip-discard. The fill
            # only reads as a cut-out while the layer is KEYED, so the
            # state request below is part of the fix, not an extra: a keyed
            # fill on an unkeyed layer is a solid magenta screen (what
            # ca1801d shipped). Idempotent, so this cannot flap.
            self._fill_around(surface.get_rect(topleft=at))
        # Where the frame sits, for the fade-out: the veil clips its dim to
        # this rect while a keyed picture is coming up - a dim blended over
        # the key pixels would leave colours the key can no longer cut out
        # (see _draw_veil).
        self._frame_layer_rect = (surface.get_rect(topleft=at)
                                  if windowed else None)
        # The layer follows the frame's geometry: a window-sized frame on a
        # screen-sized layer leaves a surround that must be cut out; a frame
        # covering the layer goes back to the plain opaque window. Whenever
        # show() is reached, PYTHON paints the picture (with the worker's own
        # window up, no frame is blitted here at all), so the geometry is the
        # right answer and a stale HUD state from an empty frame is corrected
        # on the next real one. Idempotent, so this cannot flap.
        self.set_layer_state(
            LAYER_PICTURE_KEYED if windowed else LAYER_OPAQUE)
        self.screen.blit(surface, at)
        self._draw_alerts()
        self.menu.set_stats(self._hud)
        self.menu.draw(self.screen)
        self._sync_cursor()
        # The veil, if one is still fading out: the fresh frame goes
        # UNDER it and the veil dissolves over it. Without this the
        # frame would cut in the instant it arrives (the old behaviour)
        # and the fade-out would never be seen on this path.
        if self._switch_active:
            self._draw_switch()
        pygame.display.flip()

    def poll_events(self) -> List[str]:
        """Return event names: 'quit' (Esc / window close), 'toggle' (Num1/F10).

        This is the local path, for when our own window has the focus - the
        global hotkeys are RegisterHotKey in hotkeys.py. Num1 matches the
        default binding; F10 stays as a local-path compatibility key.
        """
        events = []
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                events.append("quit")
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    events.append("quit")
                elif event.key in (pygame.K_KP1, pygame.K_F10):
                    events.append("toggle")
        return events

    def close(self) -> None:
        pygame.quit()

    # -- HUD --------------------------------------------------------------

    #: Where a corner badge goes, and how the config spells it.
    BADGE_CORNERS = ("tl", "tr", "bl", "br")

    def _badge_origin(self, corner: str, w: int, h: int, row: int = 0) -> tuple[int, int]:
        """The top-left of a badge of size (w, h) in `corner`, `row` badges down.

        The margin is the same on every side, so two corners never look
        differently inset, and `row` stacks a second badge under the first
        instead of on top of it - REC and the counter can be asked for the
        same corner.
        """
        m = int(round(14 * self.ui_scale))
        step = (h + int(round(8 * self.ui_scale))) * row
        x = m if corner in ("tl", "bl") else self.width - w - m
        y = (m + step if corner in ("tl", "tr")
             else self.height - h - m - step)
        return x, y

    def _draw_fps_badge(self) -> None:
        """The rate on screen, with the menu closed (#109).

        The panel says it too, but the panel is not open while anything is
        being watched, and the reading is exactly what somebody watching wants
        to see. Off by default: an overlay nobody asked for is one more thing
        burned into a recording.

        The text is built by the SAME function the status line uses - the rule
        for which numbers appear is not a thing to have two copies of.
        """
        corner = str(self._hud.get("fps_overlay") or "off")
        if corner not in self.BADGE_CORNERS:
            return
        if self.menu.visible:
            return      # the panel is showing the same numbers, one line up
        readings = status_readings(self.menu.state, self._hud,
                                   STRINGS.get(self._lang, STRINGS["en"]))
        if not readings:
            return
        text = "   ".join(readings)
        surf = self._font.render(text, True, self._rgb(self.theme["text"]))
        pad_x = int(round(10 * self.ui_scale))
        pad_y = int(round(6 * self.ui_scale))
        w = surf.get_width() + pad_x * 2
        h = surf.get_height() + pad_y * 2
        # Under the REC badge when both want the top-right: a recording that
        # hides the counter would be the one moment the counter matters most.
        row = 1 if (corner == "tr" and self._hud.get("recording")
                    and self._hud.get("rec_indicator", True)) else 0
        x, y = self._badge_origin(corner, w, h, row)
        rect = pygame.Rect(x, y, w, h)
        radius = int(round(8 * self.ui_scale))
        pygame.draw.rect(self.screen, self._rgb(self.theme["bg"]), rect,
                         border_radius=radius)
        pygame.draw.rect(self.screen, self._rgb(self.theme["border"]), rect,
                         max(1, int(round(self.ui_scale))),
                         border_radius=radius)
        self.screen.blit(surf, (x + pad_x, y + pad_y))

    def _draw_rec_indicator(self) -> None:
        """The recording indicator outside the menu: a red dot + timer.

        The menu shows the REC cell, but with the menu closed there was
        no sign that a recording is running (user 5080 request). The
        indicator lives in the top-right corner of the HUD layer, drawn
        over the worker's window. It is hidden from the recorded frame
        itself: draw_capture_overlay bakes only the menu, and the HUD
        layer is excluded from the capture - the indicator is a live
        status, not part of the file.

        The badge is OPAQUE: the HUD window carries a global alpha
        (LWA_ALPHA), so a bare dot and text blend with the worker's
        frame underneath and read as a translucent raspberry smear. A
        solid badge (like the alerts) keeps the colours true.
        """
        if not self._hud.get("recording"):
            return
        if not self._hud.get("rec_indicator", True):
            return
        c = self.theme
        secs = float(self._hud.get("rec_seconds", 0.0))
        text = f"REC {int(secs) // 60:d}:{int(secs) % 60:02d}"
        surf = self._font.render(text, True, self._rgb(c["text"]))
        pad_x = int(round(10 * self.ui_scale))
        pad_y = int(round(6 * self.ui_scale))
        r = int(round(5 * self.ui_scale))
        dot_d = r * 2 + int(round(4 * self.ui_scale))
        w = dot_d + pad_x + surf.get_width() + pad_x
        h = surf.get_height() + pad_y * 2
        x = self.width - w - int(round(14 * self.ui_scale))
        y = int(round(14 * self.ui_scale))
        rect = pygame.Rect(x, y, w, h)
        radius = int(round(8 * self.ui_scale))
        pygame.draw.rect(self.screen, self._rgb(c["bg"]), rect,
                         border_radius=radius)
        pygame.draw.rect(self.screen, self._rgb(c["border"]), rect,
                         max(1, int(round(self.ui_scale))), border_radius=radius)
        # The dot pulses: a static dot is easy to miss, a blinking one
        # is what every recorder does.
        if int(time.monotonic() * 2) % 2 == 0:
            cy = rect.y + rect.h // 2
            pygame.draw.circle(self.screen, self._rgb(c["danger"]),
                               (rect.x + pad_x + r, cy), r)
        self.screen.blit(surf, (rect.x + dot_d + pad_x,
                                rect.y + pad_y))

    def _own_rect(self) -> tuple[int, int, int, int] | None:
        """Where the overlay window itself sits on the desktop."""
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            rect = wintypes.RECT()
            if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
                return None
            return (rect.left, rect.top,
                    rect.right - rect.left, rect.bottom - rect.top)
        except Exception:
            return None

    def _screen_rect(self) -> tuple[int, int, int, int] | None:
        """The monitor the overlay is on, in desktop coordinates."""
        try:
            hwnd = pygame.display.get_wm_info()["window"]
            monitor = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), 2)  # NEAREST
            if not monitor:
                return None
            info = ctypes.create_string_buffer(40 + 32 * 2)
            ctypes.memmove(info, struct.pack("<I", len(info)), 4)
            if not user32.GetMonitorInfoW(ctypes.c_void_p(monitor), info):
                return None
            left, top, right, bottom = struct.unpack_from("<4i", info, 4)
            return (left, top, right - left, bottom - top)
        except Exception:
            return None

    def _alert_rect(self, w: int, h: int) -> "pygame.Rect":
        """Where an alert of this size goes, in overlay-local pixels.

        Against the MONITOR, not the overlay: the two are the same thing
        only in full-screen mode, and in one-window mode the overlay is the
        size of the window and sits on it. Clamped into the overlay, because
        an alert drawn outside it is an alert nobody sees - a window in the
        bottom corner still gets one, as high and as central as that window
        allows.
        """
        margin = int(round(ALERT_TOP_MARGIN * self.ui_scale))
        screen, own = self._screen_rect(), self._own_rect()
        if screen is not None and own is not None:
            x = screen[0] + screen[2] // 2 - own[0] - w // 2
            y = screen[1] + margin - own[1]
        else:
            x, y = (self.width - w) // 2, margin
        edge = int(round(8 * self.ui_scale))
        if self.width > w + 2 * edge:
            x = max(edge, min(x, self.width - w - edge))
        else:
            x = 0
        if self.height > h + 2 * edge:
            y = max(edge, min(y, self.height - h - edge))
        else:
            y = 0
        return pygame.Rect(x, y, w, h)

    def _draw_alerts(self) -> None:
        """Pop-up alert: the menu palette, top centre OF THE SCREEN.

        It used to hang a third of the way down, centred, as a dark slab with
        an amber border - it clashed with the overall look and got into the
        middle of the frame.

        And then it was centred on the OVERLAY, which is the whole monitor
        only in full-screen mode. In one-window mode the overlay is the size
        of the window and sits on it, so an alert about the GPU or the
        monitor appeared in the middle of somebody's browser (user, 13.09).
        It is placed against the monitor now, and clamped so that it stays
        visible when the overlay does not reach that spot - a window in the
        bottom corner still gets its alert, as high and as central as that
        window allows.

        The gap from the top is a fixed number of scaled pixels rather than
        a share of the height: 4.5% of a 1440-tall screen is 65 px and of a
        window 300 tall is 13, and neither of those is "a small margin".
        """
        now = time.monotonic()
        had = bool(self._alerts)
        self._alerts = [(text, expires) for text, expires in self._alerts if expires > now]
        if not self._alerts:
            return
        c = self.theme
        text, _ = self._alerts[-1]
        surf = self._alert_font.render(text, True, self._rgb(c["text"]))
        pad_x = int(round(26 * self.ui_scale))
        pad_y = int(round(14 * self.ui_scale))
        w = surf.get_width() + pad_x * 2
        h = surf.get_height() + pad_y * 2
        rect = self._alert_rect(w, h)
        radius = int(round(10 * self.ui_scale))
        # The panel is opaque: translucency would blend with the chroma key and
        # give a dirty tint (the colour key does not cut out a blended colour).
        pygame.draw.rect(self.screen, self._rgb(c["bg"]), rect, border_radius=radius)
        pygame.draw.rect(self.screen, self._rgb(c["border"]), rect,
                         max(1, int(round(self.ui_scale))), border_radius=radius)
        self.screen.blit(surf, (rect.x + pad_x, rect.y + pad_y))

def main() -> int:
    """Standalone smoke test: gradient frames + HUD until Esc/Num1."""
    disp = Display(2560, 1440)
    disp.set_hud({
        "fps": 60.0,
        "status": "NR ON",
        "resolution": "2560x1440",
        "params": {"intensity": 0.5, "local_tone": 0.3,
                   "local_structure": 0.7},
        "frames": 0,
    })
    h, w = disp.height, disp.width
    yy, xx = np.mgrid[0:h, 0:w]
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    frame[..., 0] = (xx * 255 // max(w - 1, 1)).astype(np.uint8)   # B
    frame[..., 1] = (yy * 255 // max(h - 1, 1)).astype(np.uint8)   # G
    frame[..., 2] = 40                                              # R
    frame[..., 3] = 255                                             # A
    n = 0
    running = True
    while running:
        disp.show(frame)
        for ev in disp.poll_events():
            if ev == "quit":
                running = False
        n += 1
        disp.set_hud({"fps": disp.clock.get_fps(), "status": "NR ON",
                      "resolution": f"{w}x{h}",
                      "params": {"intensity": 0.5, "local_tone": 0.3,
                                 "local_structure": 0.7},
                      "frames": n})
        disp.clock.tick(60)
    disp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
