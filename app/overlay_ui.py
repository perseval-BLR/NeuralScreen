"""OverlayMenu - the settings menu right inside the overlay layer, like ReShade.

It is drawn on the same pygame surface as the HUD and the alerts, so there is
still a single window: no fight over topmost and focus with the game.

Separation of duties: this module BUILDS THE LAYOUT and DRAWS it. It does not
touch pygame.display, does not read events and knows nothing about the worker.
The layout is a flat list of items with rectangles, and the same list serves as
the hit table for the mouse (hit()).

The layout is line-based: every control has its own label line and its own line
for the control itself. They used to share one line and the label ran into the
value and the arrows.

All sizes are given in 1440p base units and multiplied by scale - the same
factor the HUD uses (display.ui_scale_for).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pygame

from i18n import STRINGS
from resolution_limits import safe_processing_size
from settings_io import THEME_NAMES

# --- Themes. The accent is shared; background and text change ------------
THEMES = {
    "light": {
        "bg": "#F0EEE6",       # warm cream panel background
        "surface": "#E5DED2",  # warm sand for fields and slider tracks
        "border": "#82746B",   # 3.37:1 against surface, 3.88:1 on bg
        "text": "#191919",
        "muted": "#625B55",    # 4.99:1 against surface
        "accent": "#9E3F28",   # dark clay; safe as text and as a fill
        "ok": "#39683F",       # green of the support indicator
        "danger": "#96351F",
        "focus": "#7A321F",    # stronger clay ring, never colour-only fill
        # On cream the fill steps down to the border tone (the mockup's
        # #82746B): the accent would read as "switched on".
        "slider_fill": "#82746B",
    },
    "dark": {
        "bg": "#262624",
        "surface": "#32312E",
        "border": "#403E3A",
        "text": "#F5F4EF",
        "muted": "#A3A099",
        "accent": "#D97757",
        "ok": "#7FB07F",
        "danger": "#E06C4F",
        "focus": "#F2B098",
        # The track fill behind the knob. Not the accent: a slider is a value,
        # not a state, and the accent is reserved for state on this panel.
        "slider_fill": "#A3A099",
    },
    # A third theme, and the only one that is not a taste: an instrument
    # panel. Neon phosphor on near-black is the highest local contrast this
    # panel can carry over an arbitrary picture, which is what a see-through
    # panel needs and what light cannot give. It is opt-in for a reason -
    # green on black is one of the harder combinations for low vision, and
    # over foliage or daylight it loses local contrast like any other hue.
    "contrast": {
        "bg": "#020603",
        "surface": "#07120A",
        "border": "#164A2A",
        "text": "#3DFF9A",       # 15.9:1 on bg
        "muted": "#2CD685",      # 10.2:1 on bg, still clearly secondary
        "accent": "#00FFA3",
        "ok": "#3DFF9A",
        # Recording stays red on every theme: it is the one colour a person
        # reads without looking at the label.
        "danger": "#FF5A5A",
        "focus": "#00FFA3",
        "slider_fill": "#2CD685",
    },
}


# What we show in the remapping page and in which order. On the left is the
# command the hotkey lives under in hotkeys.DEFAULT_BINDINGS and in
# config["hotkeys"].

#: The value each drop-down is neutral AT. Anything not listed is neutral when
#: it holds its first option, which is how these lists are built (the default
#: comes first). Kept next to the page rather than inside it so the rule is
#: readable in one place.
#: A conversion row's button by its state, for a row that does not say (the
#: payload always does - see settings_io._convert_rows; a hand-built test
#: row may not). The same rule as convert_jobs.row_action.
_CONVERT_ROW_ACTION = {"running": "stop", "queued": "remove", "done": "show"}

#: The conversion page's segmented controls and the values each may send -
#: the same lists as settings_io.CONVERT_*, which validates them again.
_CONVERT_SEGMENTS = {
    "convert_dest": ("source", "folder"),
    "convert_codec": ("auto", "av1", "hevc", "h264"),
    "convert_quality": ("high", "balanced", "small"),
    "convert_image_format": ("keep", "png", "jpg"),
}

NEUTRAL_CHOICE = {
    "frame_limit_mode": "unlimited",
    "motion_backend": "nvofa",
    "screenshot_mode": "ask",
    "screenshot_format": "png",
}

HOTKEY_ROWS = (
    ("toggle", "hk_nr"),
    ("framegen", "hk_framegen"),
    ("settings", "hk_menu"),
    ("screenshot_menu", "hk_shot"),
    ("record", "hk_record"),
    ("window_mode", "hk_window"),
    ("scale_up", "hk_scale_up"),
    ("scale_down", "hk_scale_down"),
    ("quit", "hk_quit"),
)


# pygame.key.name() gives "page up" while the parser in hotkeys.parse_binding
# expects "PGUP", and the numpad comes back as "[1]" where the parser wants
# "NUM1". These are the only ones that differ.
_KEY_ALIASES = {"page up": "PGUP", "page down": "PGDN",
                "return": "ENTER", "escape": "ESC",
                "[.]": "Numdot", "[+]": "Numplus", "[-]": "Numminus",
                "[*]": "Nummul", "[/]": "Numdiv"}
# Mixed case on purpose: the parser upper-cases anyway, and "Num3" is what the
# default bindings print on the buttons.
_KEY_ALIASES.update({f"[{n}]": f"Num{n}" for n in range(10)})


def key_text(event) -> str | None:
    """Keyboard event -> a string like "Ctrl+Alt+Q" for parse_binding.

    None when only a modifier is pressed: there is no such thing as a binding
    made of a lone Ctrl.
    """
    name = pygame.key.name(event.key)
    if name in ("left ctrl", "right ctrl", "left alt", "right alt",
                "left shift", "right shift", "left meta", "right meta"):
        return None
    base = _KEY_ALIASES.get(name, name.upper())
    mods = pygame.key.get_mods()
    parts = []
    if mods & pygame.KMOD_CTRL:
        parts.append("Ctrl")
    if mods & pygame.KMOD_ALT:
        parts.append("Alt")
    if mods & pygame.KMOD_SHIFT:
        parts.append("Shift")
    parts.append(base)
    return "+".join(parts)


def palette(theme: str) -> dict:
    """The theme palette. The alerts in display.py need it too - same look."""
    return THEMES.get(theme, THEMES["light"])

#: What mini mode keeps until someone chooses otherwise: the switches a
#: person reaches for during a session. Nothing that is set once and left.
DEFAULT_MINI_ROWS = ("nr", "boost", "frame_generation", "profile")


# --- Base layout (1440p units) --------------------------------------------
PANEL_W = 540
PAD = 26
TITLE_H = 54
LABEL_H = 24
CTRL_H = 26
ROW_GAP = 16
SECTION_GAP = 14
#: The air above a section title, measured from the bottom of the last row
#: before it. ONE number for every block: it used to come out as 22, 22, 14 and
#: 40 px depending on what the preceding builder happened to add after itself,
#: and four different gaps where the eye expects one rhythm is what reads as
#: holes between the sections (user, 20.09).
SECTION_TOP_GAP = 22
#: The interface scale the user can pick, and the ladder the first launch
#: fits from. Five steps rather than a slider: the panel already speaks in
#: segments, five cells are easier to hit than a knob is to drag, and the
#: same ladder is what the automatic fit chooses from - one set of sizes,
#: not two (user, 20.09).
SCALE_STEPS = (0.8, 0.9, 1.0, 1.15, 1.3)

#: How far under the track the ruler sits. Named because the layout reserves
#: the room and the drawer places the ticks, and a slider whose row is shorter
#: than what it draws puts the next control on top of its own scale.
RULER_DROP = 5
SLIDER_H = 6
BTN_GAP = 8
ICON_W = 34        # header button: a rounded square, not a circle
                   # 30 was too small to notice: the user asked where the
                   # collapse button was while looking straight at it.
ACTION_H = 36      # action/primary button: one line of text, the mockup's
                   # padding-10 + 15px. It was 46 while the hotkey caption
                   # was drawn under the name; the main page shows no hotkeys,
                   # so 25 of those units held nothing.
STAT_PAD = 14
RADIUS = 10

FONT_SIZE = 17
TITLE_SIZE = 21
SMALL_SIZE = 14

#: The settings page, in the order the tabs are drawn. Four is the ceiling
#: at this panel width - measured across twelve languages, Polish takes 96%
#: of the strip - so a fifth subject needs a wider panel or a scrolling
#: strip, not another tab squeezed in.
SETTINGS_TABS = ("capture", "rec", "keys", "app")

PARAM_KEYS = ("intensity", "local_tone", "local_structure", "skin_structure")
# The fallback range, used only if the state has no "param_ranges" - the
# real ones are measured and live in settings_io, which owns them. A menu
# built by hand in a test still has to draw something.
PARAM_FALLBACK = (0.0, 1.5)


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _window_record(value: Any) -> dict:
    """Return a window's identity and display label as separate values.

    Production payloads use ``{"hwnd": int, "title": str}``.  The tuple and
    old ``"HEX: title"`` forms are accepted only so an in-process menu built
    by an older caller does not become unusable during an upgrade.  Once the
    record is normalised, drawing and hit-testing never recover identity from
    the visible label; titles may be duplicated and may contain colons.
    """
    if isinstance(value, dict):
        raw_hwnd = value.get("hwnd")
        try:
            hwnd = int(raw_hwnd) if raw_hwnd is not None else None
        except (TypeError, ValueError):
            hwnd = None
        title = str(value.get("title", value.get("label", "")))
        return {"hwnd": hwnd, "label": title,
                # The size travels with the row (it decides whether a pick
                # makes sense); the menu never asks Windows for it, because
                # re-reading the window list is what the freeze prevents.
                "size": str(value.get("size") or "").strip(),
                "identity": hwnd if hwnd is not None else value}
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        try:
            hwnd = int(value[0])
        except (TypeError, ValueError):
            hwnd = None
        return {"hwnd": hwnd, "label": str(value[1]),
                "identity": hwnd if hwnd is not None else value}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"hwnd": value, "label": "", "identity": value}

    # Compatibility with pre-1.13 state assembled by tests/plugins.  This is
    # an identity token from the old contract, not a label emitted by the new
    # payload.  Strip the technical prefix before it can reach the screen.
    legacy = str(value or "")
    prefix, marker, title = legacy.partition(": ")
    try:
        hwnd = int(prefix, 16) if marker else None
    except ValueError:
        hwnd = None
    return {"hwnd": hwnd,
            "label": title if hwnd is not None else legacy,
            "identity": legacy if hwnd is not None else value}


#: The kinds mini mode can keep or drop. Buttons and info rows are not
#: among them: the action strip and what it says are the panel's floor.
MINI_PICKABLE = ("toggle", "slider", "choice", "segmented")


@dataclass
class Item:
    """A layout item: what it is, where it sits, what it belongs to."""
    kind: str                      # "slider" | "button" | "toggle" | "choice"
    key: str
    rect: pygame.Rect              # mouse hit area
    lo: float = 0.0
    hi: float = 1.0
    value: float = 0.0
    payload: Any = None
    extra: dict = field(default_factory=dict)



def status_readings(state: dict, stats: dict, s: dict) -> list[str]:
    """The rate readings, as the status line and the on-screen badge show them.

    ONE implementation because there are now two places that draw them, and a
    second copy of this rule would drift: the pair `FG 178 (60.0)` is not two
    numbers side by side, it is the output rate with the rate it is built on,
    and which of the two is shown depends on what is actually running.

    - the neural pass on, Frame Generation reporting a rate -> the pair;
    - the neural pass on, no FG -> the NR rate alone;
    - the pass off (bypass), FG running -> the FG rate alone, because it is
      the only rate there is (#107);
    - nothing running, or the card cannot run the pass -> nothing. "NR 0.0"
      reads as a broken network rather than a switched-off one.
    """
    # TWO sources, and they are not interchangeable: the rates arrive with
    # every frame through set_stats, while "is the pass on" and "can this card
    # run it" are menu state. Reading a rate out of `state` finds nothing and
    # reading `nr` out of `stats` finds nothing - both silently, which is how
    # this was wrong the first time.
    if not bool(state.get("gpu_ok", True)):
        return []
    paused = not bool(state.get("nr", True))
    out: list[str] = []
    fps = stats.get("fps")
    nr_text = f"{fps:.1f}" if isinstance(fps, (int, float)) else "\u2014"
    shown = stats.get("display_fps")
    has_fg = isinstance(shown, (int, float)) and shown > 0
    if not paused and has_fg:
        out.append(f"{s.get('fg_short', 'FG')} {shown:.0f} ({nr_text})")
    elif not paused:
        out.append(f"{s.get('nr_short', 'NR')} {nr_text}")
    if has_fg and paused:
        out.append(f"{s.get('fg_short', 'FG')} {shown:.0f}")
    return out


class OverlayMenu:
    """The overlay menu: visibility, state, layout, drawing."""

    def __init__(self, scale: float, font_loader: Callable[[int], Any]):
        self.scale = scale
        # Manual multiplier: needed before the fonts are created, they size
        # themselves through _u
        self.user_scale = 1.0
        #: Mini mode: the main page carries only the rows in `mini_rows`.
        #: The other pages are untouched - settings is where you go to set
        #: something, and a shortened settings page would just be a page
        #: with things missing.
        self.mini = False
        #: Choosing what mini mode keeps. While this is on, every row is
        #: laid out again, so a row that was dropped can be put back where
        #: it used to sit: the way out of a mode has to live inside it.
        self.mini_pick = False
        self.mini_rows: set = set(DEFAULT_MINI_ROWS)
        self._load_font = font_loader
        self.visible = False
        self.lang = "en"
        self.state: dict = {
            "nr": True,
            "work_scale": 1.0,
            # Where the work size hits the NGX cap. Sent by main because only it
            # knows the screen size. The slider runs one step past it, and that
            # last step means "the whole screen" - the reduced mode off.
            "work_scale_cap": 1.0,
            # The bottom of the resolution slider (WORK_SCALE_MIN), sent by
            # main so a config value below the range cannot misplace the knob.
            "work_scale_min": 0.1,
            "nr_small": False,
            # What the presenter shows with FG on (from the worker's two-second
            # report); the HUD pairs it with the network fps. None while off.
            "display_fps": None,
            "frame_generation": False,
            "frame_multiplier": 2,
            # The step the presenter really runs, when it differs from the
            # pick above (issue #100: a refused 4x steps down to 2x).
            "frame_multiplier_active": None,
            "frame_limit_mode": "unlimited",
            "frame_limit_custom": 90,
            "screen_size": "",
            "profile": "",
            "profiles": [],
            "params": {},
            # The profile's own numbers, drawn as a tick under each
            # parameter slider (see _draw_slider).
            # (low, high) per parameter, from settings_io.
            "param_ranges": {},
            # The NR cascade: how many passes run over one frame (experiment).
            "nr_passes": 1,
            # What passes 2..N use when they have their own set. `{}` means
            # "the main set for every pass" - the same thing a missing config
            # key means, and the same thing the panel should show.
            "nr_pass_params": {},
            # The numbers `nr_pass_params` was seeded from: what the main
            # sliders were at the moment the second set was switched on. Drawn
            # as the tick under the second set's sliders, so "how far have I
            # moved pass 2 from the main set" is visible instead of remembered
            # - the same job param_defaults does one section above.
            "pass_param_defaults": {},
            "pass_param_ranges": {},
            "convert_busy": False,
            "convert_status": "",
            # The conversion page: the queue's rows (each already carrying
            # its second line and its button - see settings_io._convert_rows),
            # the running file's fraction for the main page's Convert cell,
            # and the page's output choices.
            "convert_jobs": [],
            "convert_progress": None,
            "convert_dest": "source",
            "convert_dir": "",
            "convert_codec": "auto",
            "convert_quality": "high",
            "convert_image_format": "keep",
            "convert_audio": True,
            # version / windows / driver / gpu, from the log header.
            "about": {},
            "compatibility_status": "not_run",
            "compatibility_score": "",
            "style": 1,
            "param_defaults": {},
            "preset_active": False,
            "recording": False,
            "recording_finalizing": False,
            "recording_status": "",
            "recording_details": "",
            "recording_path": "",
            "work_size": "",
            "theme": "light",
            "rec_seconds": 0.0,
            "rec_indicator": True,
            # How a recording is made (GpuRecorder or the CPU path).
            "gpu_record": True,
            # Which corner the on-screen counter sits in, or "off" (#109).
            "fps_overlay": "off",
            # What the taskbar's minimise and close buttons do (#93).
            "tray_on_minimise": False,
            "tray_on_close": False,
            "recording_dir": "",
            "screenshot_dir": "",
            "screenshot_mode": "ask",
            "screenshot_format": "png",
            # The Spout2 bridge flag (RECORDING section). It was missing here
            # in v1.6.0, so set_state dropped it in silence and the toggle
            # always drew as off while the action behind it fired normally.
            "spout": False,
            # HDR compatibility (CAPTURE section): experimental, off. Same
            # reason it is listed here as spout was - a key missing from
            # this dict is dropped by set_state in silence, and the toggle
            # then draws as off while the action behind it fires normally.
            "hdr": False,
            "motion_backend": "nvofa",
            # NR is on while the worker is not evaluating: the picture is raw.
            # Listed HERE for the reason two keys above spell out - set_state
            # drops an unknown key in silence, and the drawer then reads a
            # verdict that never arrived (the spout/hdr trap).
            "nr_not_evaluating": False,
            "open_on_start": True,
            "split": 0.0,
            # Which card this is and whether NR runs on it. gpu_ok:
            # True/False/None (None - the worker has not answered yet).
            "gpu_text": "",
            "gpu_ok": None,
            "window_mode": False,
            "monitor": "0",
            "monitors": [],
            # The current monitor's DXGI devicename - carried so a log or a
            # future control can name the exact display the capture is on.
            "monitor_devicename": "",
            "gpu": "0",
            "gpus": [],
            "autostart": False,
            "windows": [],
            "window_current": "",
            # The header shows the version; the channel label lives in the
            # settings page (user rule 2026-09-08).
            "version": "",
            "channel": "",
        }
        # Pipeline readings: the same ones the HUD shows. The menu is meant to
        # be the single place where both the settings and what is going on are
        # visible.
        self.stats: dict = {}
        self.items: list[Item] = []
        self._build_fonts()
        # The language list shows every language in its own script (Русский,
        # 中文, 日本語, 한국어). The current UI font cannot render CJK - the
        # loader picks the font by the language, so dedicated CJK fonts are
        # loaded once for those labels, chosen by the script: YaHei for
        # Chinese, Yu Gothic for Japanese (kanji/kana), Malgun Gothic for
        # Korean (hangul) (user: Asian names show as boxes).
        self._cjk_fonts = {}
        try:
            import pygame.font as _pf
            for _name in ("microsoftyahei", "yugothic", "malgungothic"):
                try:
                    self._cjk_fonts[_name] = _pf.SysFont(
                        _name, self._u(FONT_SIZE))
                except Exception:
                    pass
        except Exception:
            self._cjk_fonts = {}
        self.panel_rect = pygame.Rect(0, 0, 0, 0)
        #: Which settings tab is open. In-session only: the page is entered
        #: to do one thing, and being returned to last week's tab is not
        #: what anyone wants from it.
        self.settings_tab = SETTINGS_TABS[0]
        self._stats_rect = pygame.Rect(0, 0, 0, 0)
        self._stats_line1_rel = pygame.Rect(0, 0, 0, 0)
        self._gpu_rect = pygame.Rect(0, 0, 0, 0)
        # The relative rects are computed in layout() for the main page
        # only; the defaults keep the non-main pages safe (the drawers are
        # skipped there anyway).
        self._stats_rel = pygame.Rect(0, 0, 0, 0)
        self._tabs_rel = pygame.Rect(0, 0, 0, 0)
        self._gpu_rel = pygame.Rect(0, 0, 0, 0)
        # The panel can be dragged by its title bar and stretched by its
        # corner. The offset is stored relative to the screen centre, so it
        # survives a resolution change without the window ending up off-screen.
        self.offset = [0, 0]
        self._grip = pygame.Rect(0, 0, 0, 0)
        self._title_bar = pygame.Rect(0, 0, 0, 0)
        self._move_from = None
        self._resize_from = None
        # Panel height: None means "fit the content". It is set by dragging the
        # bottom edge; content taller than the height scrolls.
        self.user_height: int | None = None
        self.scroll = 0
        self.content_height = 0
        self._max_scroll = 0
        self._resize_h_from = None
        # The last mouse position: in pygame the wheel arrives without
        # coordinates, and pygame.mouse.get_pos() in a click-through window is
        # not to be trusted.
        self._mouse = (0, 0)
        self._edge = pygame.Rect(0, 0, 0, 0)
        self._viewport = pygame.Rect(0, 0, 0, 0)
        self._scroll_track = pygame.Rect(0, 0, 0, 0)
        self._scroll_thumb = pygame.Rect(0, 0, 0, 0)
        # Which list is currently expanded (profile / language / theme).
        # Cycling with arrows is awkward once there are more than two options.
        self.open_choice: str | None = None
        # The expanded list scrolls: 12 languages do not fit the screen, the
        # panel cannot be stretched down, and the arrows must reach every
        # entry (user: the language list has no scroll). _opt_scroll is the
        # first visible row, _opt_index the highlighted one (arrows/Enter).
        self._opt_scroll = 0
        self._opt_index = 0
        self._opt_max_scroll = 0
        self._opt_track = pygame.Rect(0, 0, 0, 0)
        self._opt_thumb = pygame.Rect(0, 0, 0, 0)
        # The window under the cursor on the windows page: the hwnd whose
        # outline is highlighted on the real screen (None = nothing).
        self.hover_window: int | None = None
        # Menu page: the main window or the settings behind the gear.
        self.page = "main"
        # The command we are currently waiting for a keypress for (or None).
        self.capturing: str | None = None
        # Hotkey captions: command -> "Num1". They come from main together with
        # the bindings, so a remap shows up on the buttons immediately.
        self.hotkeys: dict = {}
        self._sections: list = []
        self._hint_rel = pygame.Rect(0, 0, 0, 0)
        self._rule_rel = pygame.Rect(0, 0, 0, 0)
        # What is under the cursor: "title" (draggable) or "grip" (resizable).
        # Without the highlight these zones are invisible and impossible to
        # find.
        self.hover: str | None = None
        # Keyboard focus is a stable token rather than an item index.  Layout
        # objects are rebuilt on every draw and pages contain repeated keys
        # (notably one row per window), so an index would silently jump to a
        # different control after a payload refresh.
        self.focus_token: tuple | None = None
        # Keyboard focus draws a ring; a click does not. The ring is a
        # keyboard affordance - with the mouse the user already knows what
        # they pressed, and on a full-width row it wrapped the label and the
        # control together (user: "an outline appears when I pick buttons or
        # drag a slider - that is not needed").
        self.focus_from_mouse = False
        self._layout_size: tuple[int, int] | None = None

    @property
    def c(self) -> dict:
        """Colours of the current theme."""
        return palette(self.state.get("theme", "light"))

    # -- helpers -----------------------------------------------------------

    def _u(self, base: float) -> int:
        """1440p base units -> screen pixels."""
        return max(1, int(round(base * self.scale * self.user_scale)))

    def fit_user_scale(self, width: int, height: int) -> float:
        """The largest ladder step whose main page needs no scrolling.

        Laid out for real at each step rather than estimated: the page's height
        depends on which rows are present (Boost hides the resolution slider, a
        custom frame cap adds one), and an estimate that is wrong by one row is
        wrong by more than a step.

        Never above 1.0: the fit exists to bring a panel that does not fit
        DOWN onto the screen, and a big desktop is not a request for a big
        panel - the per-monitor density is already handled a level up, by
        display.ui_scale_for. The steps above 1.0 are there to be chosen.

        The caller's scale is restored before returning - this measures, it
        does not decide.
        """
        saved = self.user_scale
        saved_page, saved_scroll = self.page, self.scroll
        best = SCALE_STEPS[0]
        try:
            for step in (v for v in SCALE_STEPS if v <= 1.0):
                self.set_user_scale(step)
                self.page = "main"
                self.layout(int(width), int(height))
                if self._max_scroll <= 0:
                    best = step
        finally:
            self.set_user_scale(saved)
            self.page, self.scroll = saved_page, saved_scroll
        return best

    def set_user_scale(self, value: float) -> None:
        """Manual panel stretching. The fonts have to be recreated."""
        value = min(2.0, max(0.6, round(value, 2)))
        if abs(value - self.user_scale) < 0.01:
            return
        self.user_scale = value
        self._build_fonts()

    def mini_hides(self, key: str) -> bool:
        """Whether mini mode leaves this row out of the main page."""
        return (self.mini and not self.mini_pick and self.page == "main"
                and key not in self.mini_rows)

    def mini_keep(self, key: str) -> bool:
        """Keep or drop a row; returns whether mini mode keeps it now."""
        if key in self.mini_rows:
            self.mini_rows.discard(key)
        else:
            self.mini_rows.add(key)
        return key in self.mini_rows

    def toggle(self) -> bool:
        self.visible = not self.visible
        return self.visible

    @property
    def visible(self) -> bool:
        """Whether the panel is up. Setting it False ends any interaction.

        The setter is the ONE place that closes the menu, and closing it has
        to finish whatever the pointer started (audit H3). A drag begun on the
        title bar holds SetCapture and a live anchor in `_move_from`; if the
        menu is closed before the button comes up - the min icon, Num2, Esc,
        the tray, the taskbar - the matching MOUSEBUTTONUP is dropped by
        handle_event's own `if not self.visible: return []` guard. The drag
        then never ends: `dragging` stays True, so the main loop skips the
        whole per-frame payload refresh (FPS, REC, the GPU verdict, the
        profile all freeze), and the next buttonless MOUSEMOTION applies the
        anchor, teleporting the panel by the distance the cursor travelled
        meanwhile (measured: offset [0, 0] -> [-460, 153]).

        Routes that assign the attribute directly are covered by this too;
        they used to leave the panel dragging forever.
        """
        return self._visible

    @visible.setter
    def visible(self, value: bool) -> None:
        value = bool(value)
        if not value and getattr(self, "_visible", False):
            self._end_interaction()
        self._visible = value

    def _end_interaction(self) -> None:
        """Cancel every pointer interaction the menu is holding.

        Called when the menu goes away underneath the pointer: the drags and
        resizes lose their anchor, and the window gives up the mouse capture
        it took for them - otherwise clicks keep being routed to a menu the
        user can no longer see.
        """
        self._drag_item = None
        self._move_from = None
        self._resize_from = None
        self._resize_h_from = None
        self._capture_mouse(False)

    @property
    def dragging(self) -> bool:
        """Whether the panel is being manipulated right now.

        Sliders, the title-bar drag, the edge scale and the grip resize all
        count. While one is active the state must not be rebuilt (a slider
        would jump between what the mouse shows and what main has already
        applied), and the per-frame payload rebuild - EnumWindows + the
        monitor scan + the worker log scan - is exactly what made the
        title-bar drag stutter (flicker audit M3: the property used to
        cover sliders only; user, 15.09: "двигается с рывками").
        """
        return (getattr(self, "_drag_item", None) is not None
                or getattr(self, "_move_from", None) is not None
                or getattr(self, "_resize_from", None) is not None
                or getattr(self, "_resize_h_from", None) is not None)

    def set_hotkeys(self, mapping: dict) -> None:
        """Hotkey captions: command -> "Num1". Sourced from the real bindings."""
        self.hotkeys = dict(mapping or {})

    def set_stats(self, hud: dict) -> None:
        self.stats = dict(hud or {})

    def set_state(self, payload: dict) -> None:
        for k, v in payload.items():
            if k == "lang":
                self.lang = v
                self._reload_fonts()
            elif k == "params" and isinstance(v, dict):
                self.state["params"] = dict(v)
            elif k in self.state:
                was = self.state.get(k)
                self.state[k] = v
                # The contrast theme draws in another face, so a theme change
                # is a font change too - and the faces are cached per size.
                if k == "theme" and (was == "contrast") != (v == "contrast"):
                    self._build_fonts()

    def _load(self, size: int, mono: bool = False):
        """One face, through the loader the caller handed us.

        The live app passes display._load_font, which takes the role; the
        offscreen renderers and the tests pass a one-argument callable, and
        those get the proportional face for everything - which is what they
        had before the split.
        """
        try:
            return self._load_font(size, mono=mono)
        except TypeError:
            return self._load_font(size)

    def _build_fonts(self) -> None:
        """The menu's faces, built together.

        Called from __init__ and after every scale/language change - the
        sizes and the language both change what the loader returns. Which
        faces those are lives in fonts.py; this only asks for them.

        Two roles, as fonts.py divides them: the proportional face carries
        language - titles, labels, hints, buttons - and the monospaced one
        carries readings, where a fixed advance keeps digits from dancing
        sideways as they change.

        The contrast theme is the exception, and only that one: it draws
        EVERY role in the monospaced face. fonts.py records why the program
        stopped doing that in 1.6.0 - "one monospaced face for a whole
        interface is why the menu read as a debug console rather than as a
        program" - which is an objection to the look, and the look is what
        this theme is for. Light and dark keep the split.
        """
        ui_mono = self.state.get("theme") == "contrast"
        self._font = self._load(self._u(FONT_SIZE), mono=ui_mono)
        self._title_font = self._load(self._u(TITLE_SIZE), mono=ui_mono)
        self._small_font = self._load(self._u(SMALL_SIZE), mono=ui_mono)
        self._mono = self._load(self._u(FONT_SIZE), mono=True)
        self._mono_small = self._load(self._u(SMALL_SIZE), mono=True)

    def _reload_fonts(self) -> None:
        """Recreate the fonts after a language switch.

        The CJK scripts (zh/ja/ko) have no glyphs in the default font -
        Consolas renders them as tofu boxes. The loader picks the font by
        the language, so the cached font objects must be rebuilt.
        """
        self._build_fonts()

    def title_center(self) -> tuple[int, int]:
        """The centre of the title bar in screen coordinates.

        The mouse lands here when the menu opens, so the user does not have
        to hunt for the pointer (user request). Valid after layout().
        """
        r = self._title_bar
        return (r.x + r.w // 2, r.y + r.h // 2)

    def _capture_mouse(self, on: bool) -> None:
        """Capture the mouse while dragging the panel by its title bar.

        Without it the drag dies the moment the cursor leaves the window:
        pygame stops delivering MOUSEMOTION outside the window, and the
        release click outside is lost too - the panel "stops and has to be
        grabbed again" (user report). SetCapture keeps the events coming
        until the button is released.
        """
        try:
            import ctypes
            hwnd = pygame.display.get_wm_info()["window"]
            if on:
                ctypes.windll.user32.SetCapture(hwnd)
            else:
                ctypes.windll.user32.ReleaseCapture()
        except Exception:
            pass

    # -- keyboard focus ---------------------------------------------------

    @staticmethod
    def _focus_id(item: Item) -> tuple:
        """Stable identity for a control across the next layout rebuild."""
        identity = None
        if item.kind == "option" and item.key == "window":
            identity = item.extra.get("hwnd")
            if identity is None:
                identity = repr(item.payload)
        return item.kind, item.key, identity

    @staticmethod
    def _is_focusable(item: Item) -> bool:
        if item.extra.get("disabled") or item.key == "no_windows":
            return False
        if item.kind == "info":
            return item.key == "source_now"
        return item.kind in {
            "tab",
            "icon", "action", "hotkey", "button", "toggle", "choice",
            "slider", "segmented", "option",
        }

    def _focusables(self) -> list[Item]:
        """Interactive controls in visual reading order."""
        controls = [item for item in self.items if self._is_focusable(item)]
        # Scrolling moves screen rectangles but must not reorder traversal.
        # Header icons are pinned; content uses its unscrolled Y coordinate.
        def order(item: Item) -> tuple:
            if item.kind == "icon":
                return 0, item.rect.left, item.rect.top, item.key
            return 1, item.rect.top + self.scroll, item.rect.left, item.key
        return sorted(controls, key=order)

    @property
    def focused_item(self) -> Item | None:
        """The current live layout item, or None before keyboard navigation."""
        if self.focus_token is None:
            return None
        return next((item for item in self.items
                     if self._focus_id(item) == self.focus_token), None)

    def _set_focus(self, item: Item | None, *, from_mouse: bool = False) -> None:
        self.focus_token = self._focus_id(item) if item is not None else None
        # A mouse click keeps the focus token (arrows still act on what was
        # clicked) but must not paint the keyboard ring around it.
        self.focus_from_mouse = from_mouse and item is not None
        if self.page == "windows":
            self.hover_window = (item.extra.get("hwnd")
                                 if item is not None
                                 and item.kind == "option"
                                 and item.key == "window" else None)

    def _reconcile_focus(self) -> None:
        """Drop a token when its control disappeared after a page rebuild."""
        if self.focus_token is not None and self.focused_item is None:
            self.focus_token = None
            if self.page == "windows":
                self.hover_window = None

    def _relayout(self) -> None:
        if self._layout_size is not None and not getattr(self, "_measuring", False):
            self.layout(*self._layout_size)

    def _ensure_focus_visible(self) -> None:
        """Scroll just enough to keep the keyboard target inside the viewport."""
        item = self.focused_item
        if item is None or item.kind == "icon" or self._max_scroll <= 0:
            return
        margin = self._u(6)
        top = self._viewport.top + margin
        bottom = self._viewport.bottom - margin
        new_scroll = self.scroll
        if item.rect.top < top:
            new_scroll -= top - item.rect.top
        elif item.rect.bottom > bottom:
            new_scroll += item.rect.bottom - bottom
        new_scroll = min(max(0, int(new_scroll)), self._max_scroll)
        if new_scroll != self.scroll:
            self.scroll = new_scroll
            # Rebuild now, not one frame later: keyboard users must never
            # focus an off-screen rectangle, even between two draw calls.
            self._relayout()

    def _cycle_focus(self, direction: int) -> None:
        controls = self._focusables()
        if not controls:
            self._set_focus(None)
            return
        current = next((idx for idx, item in enumerate(controls)
                        if self._focus_id(item) == self.focus_token), None)
        if current is None:
            index = 0 if direction > 0 else len(controls) - 1
        else:
            index = (current + direction) % len(controls)
        self._set_focus(controls[index])
        self._ensure_focus_visible()

    def _open_choice(self, item: Item) -> None:
        if self.open_choice == item.key:
            self.open_choice = None
            return
        self.open_choice = item.key
        options = list(item.payload or [])
        current = str(item.extra.get("current", ""))
        self._opt_index = next(
            (idx for idx, value in enumerate(options)
             if str(value) == current), 0)
        # Start at the selected row. layout() clamps this back to zero when
        # every option fits, and to the last valid page otherwise.
        self._opt_scroll = self._opt_index
        self._relayout()

    def _keep_option_visible(self) -> None:
        visible = max(1, len(getattr(self, "options", [])))
        if self._opt_index < self._opt_scroll:
            self._opt_scroll = self._opt_index
        elif self._opt_index >= self._opt_scroll + visible:
            self._opt_scroll = self._opt_index - visible + 1

    def _activate_item(self, item: Item, *, keyboard: bool = False) -> list[tuple]:
        """Activate a control without deriving identity from its caption."""
        if not self._is_focusable(item):
            return []
        old_page = self.page
        out: list[tuple] = []
        if item.kind == "icon":
            out.extend(self._icon_click(item.key))
        elif item.kind == "action":
            out.extend(self._action_click(item.key))
        elif item.kind == "hotkey":
            self.capturing = item.key
            out.append(("capture", item.key))
        elif item.kind == "tab":
            # A tab selects a PAGE, not a value: it re-runs the layout with the
            # new tab, which is what the old pill row did through `segmented`.
            if item.key in SETTINGS_TABS and item.key != self.settings_tab:
                self.settings_tab = item.key
                out.append(("settings_tab", item.key))
        elif item.kind == "segmented":
            options = list(item.payload or [])
            current = str(item.extra.get("current", ""))
            value = next((value for value in options
                          if str(value) == current), None)
            if value is not None:
                out.extend(self._pick(item.key, value))
        elif item.kind == "info" and item.key == "source_now":
            self.page = "windows"
            self.scroll = 0
            self.capturing = None
            out.append(("capture", None))
        elif item.kind == "toggle":
            out.append(("nr",) if item.key == "nr" else ("toggle", item.key))
        elif item.kind == "button":
            out.extend(self._button_click(item.key))
        elif item.kind == "option":
            out.extend(self._pick(item.key, item.payload))
            self.open_choice = None
        elif item.kind == "choice":
            self._open_choice(item)

        if self.page != old_page:
            self._set_focus(None)
            self._relayout()
            if keyboard:
                self._cycle_focus(1)
        return out

    def _step_segmented(self, item: Item, direction: int) -> list[tuple]:
        options = list(item.payload or [])
        if not options:
            return []
        current = str(item.extra.get("current", ""))
        index = next((idx for idx, value in enumerate(options)
                      if str(value) == current), 0)
        index = min(max(0, index + direction), len(options) - 1)
        value = options[index]
        if str(value) == current:
            return []
        item.extra["current"] = str(value)
        out = self._pick(item.key, value)
        self._relayout()
        self._ensure_focus_visible()
        return out

    def _step_toggle(self, item: Item, turn_on: bool) -> list[tuple]:
        if bool(item.value) == turn_on:
            return []
        item.value = 1.0 if turn_on else 0.0
        self.state[item.key] = turn_on
        return [("nr",) if item.key == "nr" else ("toggle", item.key)]

    def _handle_focused_key(self, event) -> list[tuple]:
        item = self.focused_item
        if item is None:
            return []
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            return self._activate_item(item, keyboard=True)
        if event.key not in (pygame.K_LEFT, pygame.K_RIGHT,
                             pygame.K_UP, pygame.K_DOWN):
            return []
        direction = (1 if event.key in (pygame.K_RIGHT, pygame.K_UP) else -1)
        if item.kind == "slider":
            return self._step_slider(item, direction)
        if item.kind == "segmented" and event.key in (pygame.K_LEFT,
                                                       pygame.K_RIGHT):
            return self._step_segmented(item, direction)
        if item.kind == "choice":
            choice_direction = (1 if event.key in (pygame.K_RIGHT,
                                                    pygame.K_DOWN) else -1)
            self._open_choice(item)
            if self.open_choice:
                total = len(item.payload or [])
                self._opt_index = min(max(
                    0, self._opt_index + choice_direction), max(0, total - 1))
                self._keep_option_visible()
                self._relayout()
            return []
        if item.kind == "toggle" and event.key in (pygame.K_LEFT,
                                                    pygame.K_RIGHT):
            return self._step_toggle(item, direction > 0)
        return []

    # -- layout ------------------------------------------------------------

    def layout(self, screen_w: int, screen_h: int) -> None:
        """Recompute the rectangles.

        We walk top to bottom in relative coordinates, learn the height at the
        end and shift everything at once - that way the panel height cannot
        drift apart from the content (it used to come from a formula and lag
        behind).

        The settings page resizes itself with every tab - one tab taller than
        the other - and the jumping panel reads as broken. So on the settings
        page the panel is sized to the TALLEST tab, always: a guarded pass
        measures every tab's content height, and the real pass pads the
        current tab out to that height (the back button lands at the bottom
        of the tallest tab's panel, on every tab).
        """
        self._layout_size = (int(screen_w), int(screen_h))
        if self.page == "settings" and not getattr(self, "_measuring", False):
            tallest = 0
            saved_tab = self.settings_tab
            self._measuring = True
            try:
                for tab in SETTINGS_TABS:
                    self.settings_tab = tab
                    self.layout(screen_w, screen_h)
                    tallest = max(tallest, self.content_height)
            finally:
                self._measuring = False
                self.settings_tab = saved_tab
            self._settings_content_h = tallest
        s = STRINGS.get(self.lang, STRINGS["en"])
        w = self._u(PANEL_W)
        pad = self._u(PAD)
        label_h = self._u(LABEL_H)
        ctrl_h = self._u(CTRL_H)
        gap = self._u(ROW_GAP)
        inner_w = w - pad * 2
        act_h = self._u(ACTION_H)

        items: list[Item] = []
        #: Mini mode, and not choosing: the page is the kept rows plus the
        #: action strip. Named once here because three blocks outside the
        #: row builders have to honour it.
        mini_short = (self.mini and not self.mini_pick
                      and self.page == "main")
        # Header icons: help, settings and the collapse button. The collapse
        # used to be a footer button next to "quit the program" - the two
        # looked equally harmless, even though one hides the menu and the
        # other unloads the program. A real window has its collapse in the
        # title bar, so it moved here: [help] [gear] [min].
        iw = self._u(ICON_W)
        igap = self._u(8)
        iy = self._u(TITLE_H) // 2 - iw // 2
        order = (["help", "gear"]
                 + (["pick"] if self.mini else [])
                 + ["mini", "min"]
                 if self.page == "main"
                 else ["close"] if self.page == "settings"
                 else [])  # the windows page: no header icons at all - the
        # Back button in the footer is the only way out (user rule 10.09).
        # Laid out right to left: the right edge is the last icon.
        for idx, kind in enumerate(reversed(order)):
            ix = pad + inner_w - iw - idx * (iw + igap)
            items.append(Item("icon", kind,
                              pygame.Rect(ix, iy, iw, iw)))
        cy = self._u(TITLE_H) + self._u(SECTION_GAP)

        # The readings block and the GPU line belong to the MAIN page only:
        # the settings and windows pages are about configuration, and the
        # live indicators (FPS/RES/WORK/FRAMES/REC/PROFILE + the GPU dot)
        # are noise there (user rule 10.09: the main page shows the state,
        # the other pages do the work). The rects are still computed for the
        # main page - the drawers check the page before drawing.
        if self.page == "main":
            # ONE status line again, but with the readings anchored to the
            # RIGHT edge and built BEFORE the card name is placed, so the name
            # can only ever eat its own space. The old line laid the readings
            # out from the right in reverse order and skipped whatever did not
            # fit - and the order made the FIRST casualty NR, the rate people
            # watch, while the resolution (printed again in the source section)
            # stayed. Measured at 4K with a real card name: NR and FG gone,
            # "SKIP 0  3840x2160" on screen. A reading nobody can see reads as
            # a counter that does not work (report: "no frame count at all").
            #
            # The readings are what cannot be read anywhere else: the NR rate
            # and the FG rate. Resolution, the skipped-frame count and the
            # frame counter are gone from the line by decision (19.09) - the
            # first is already printed above, the other two are numbers nobody
            # acts on. Order left to right: NR, FG.
            line_h = self._u(SMALL_SIZE) + self._u(18)
            status_h = line_h
            self._stats_rel = pygame.Rect(pad, cy, inner_w, status_h)
            self._stats_line1_rel = pygame.Rect(pad, cy, inner_w, line_h)
            self._gpu_rel = pygame.Rect(0, 0, 0, 0)   # folded into the line
            cy += status_h + gap


        # The content is split into titled blocks: eight identical rows in a
        # row gave the eye nothing to hold on to. The titles are not
        # interactive, so they live in their own list rather than in items.
        self._sections: list[tuple[str, pygame.Rect]] = []
        #: Segment groups built by toggle(..., inline_right=...): the outer frame
        #: and the dividers, drawn after the cells so the cells can fill their
        #: own boxes first. (rect, cell count)
        #: (rect, divider offsets from the rect's left edge).
        self._segment_groups: list[tuple[pygame.Rect, tuple[int, ...]]] = []
        #: The same groups in SCREEN coordinates (derived at the end of layout).
        self._segment_rects: list[tuple[pygame.Rect, tuple[int, ...]]] = []
        sec_h = self._u(SMALL_SIZE) + self._u(10)

        # Which tab the rows being built belong to. section() sets it and
        # every builder below honours it, so a hidden tab costs no layout and
        # no re-indentation of the page that was here before tabs.
        show = True

        #: Whether the rows built from here carry a state square. Set by
        #: section(), read by every builder below - the mockup decides this per
        #: SECTION, and the same control kind appears both with and without one.
        squares = False

        def section(title: str, tab: str | None = None,
                    squares_here: bool = False) -> None:
            nonlocal cy, show, squares
            show = tab is None or tab == self.settings_tab
            squares = squares_here
            if not show:
                return
            # Mini mode is a handful of rows, not an outline of them: four
            # titles over four rows is more furniture than content. The
            # flags above are still set - the builders below read them.
            if self.mini and self.page == "main":
                return
            # Measured from what is actually on the page, not from whatever the
            # last builder left in `cy`: each of them adds its own trailing air
            # (one adds ROW_GAP, the preset row adds 8, a hinted slider adds a
            # caption line), and those differences landed straight in the gap
            # before the next title. The items know where the content really
            # ends, so ask them.
            #
            # The header icons are NOT content: they sit above the scroll area,
            # and taking them as "the last row" put the first section title on
            # top of the status line.
            bottom = max((it.rect.bottom for it in items
                          if it.kind != "icon"), default=None)
            if bottom is None and self._stats_rel.h:
                # Nothing laid out yet on the main page: the status line is what
                # the first title follows.
                bottom = self._stats_rel.bottom
            if bottom is not None:
                cy = bottom + self._u(SECTION_TOP_GAP)
            else:
                cy += self._u(6)
            self._sections.append((title, pygame.Rect(pad, cy, inner_w, sec_h)))
            cy += sec_h

        def slider(key: str, lo: float, hi: float, value: float,
                   label: str, hint: str = "", value_text: str = "",
                   mark: float | None = None, bare: bool = False,
                   ticks: int = 5) -> None:
            nonlocal cy
            if not show or self.mini_hides(key):
                return
            # `bare`: the track alone, with no caption line and no value cell.
            # The ruler under it carries the position instead - for a control
            # whose whole subject is already named by its section, those two
            # lines of furniture said nothing the block title had not already
            # said (user, 20.09).
            row_label_h = 0 if bare else label_h
            item = Item("slider", key,
                              pygame.Rect(pad, cy, inner_w, row_label_h + ctrl_h),
                              lo=lo, hi=hi, value=value,
                              extra={"label": label, "hint": hint,
                                     "mark": mark,
                                     "value_text": value_text,
                                     "label_h": row_label_h,
                                     "bare": bare, "ticks": ticks,
                                     "square": squares and not bare})
            # The hit zone is the TRACK, not the row: a click on the label
            # or on the blank space left of the track must not jump the
            # value (user rule 16.09). The track's rect is computed
            # exactly as _draw_slider computes it.
            track_y = cy + row_label_h + self._u(10)
            item.extra["hit"] = pygame.Rect(
                pad, track_y - self._u(6), inner_w,
                self._u(SLIDER_H) + self._u(12))
            # What is drawn UNDER the track is part of the row, and the row has
            # to be that tall or the next control lands on it. Two things live
            # there, in this order: the ruler, then the caption.
            #
            # The caption used to be reserved as exactly one line and drawn
            # unwrapped, so a real sentence ran past the panel edge and a
            # translated one overprinted the row below - the same defect #109
            # named, fixed for the drop-downs and missed here.
            below = self._u(RULER_DROP) + self._u(4)
            if hint:
                below += self._u(6) + self._hint_height(str(hint), inner_w)
            item.extra["below"] = below
            items.append(item)
            item.rect.h = row_label_h + ctrl_h + below
            # The air AFTER the row. A plain slider ends in its ruler, and the
            # ruler is thin, quiet and reads as part of the track - it already
            # does the separating that the full row gap is there for, so paying
            # both spends the height twice. (It used to be paid once only
            # because the row was shorter than what it drew and the ruler
            # overhung into the next row's air.) A slider with a caption keeps
            # the full gap: a line of text needs air under it, not beside it.
            cy += item.rect.h + (gap if hint
                                 else max(self._u(8), gap - below))

        def choice(key: str, label: str, current: str, options: list,
                   labels: list | None = None, hint: str = "") -> None:
            nonlocal cy
            if not show or self.mini_hides(key):
                return
            extra = {"label": label, "current": current,
                     "labels": list(labels or options),
                     "label_h": label_h, "square": squares}
            hint_h = 0
            if hint:
                extra["hint"] = hint
                line_h = self._small_font.get_height() + self._u(4)
                # The height comes from the hint AS IT WILL BE DRAWN: the hint
                # is wrapped now, so counting "\n" reserved one line for a
                # sentence that needs three and the next control was drawn over
                # its tail.
                hint_h = (self._u(8)
                          + self._hint_height(str(hint), inner_w)
                          - self._u(4))
            item = Item("choice", key,
                              pygame.Rect(pad, cy, inner_w, label_h + ctrl_h + hint_h),
                              payload=list(options),
                              extra=extra)
            # The hit zone is the select FIELD, not the row: the label above
            # it is a caption (user rule 16.09). _draw_choice writes the
            # same rect back into extra["strip"], and the hint guard below
            # uses it too.
            item.extra["hit"] = pygame.Rect(pad, cy + label_h, inner_w,
                                            self._u(CTRL_H))
            items.append(item)
            cy += label_h + ctrl_h + hint_h + gap

        def segmented(key: str, label: str, current: str, options: list,
                      labels: list | None = None, height: int | None = None) -> None:
            """A two- or three-way switch instead of a drop-down list.

            A list for the sake of two values is an extra click and extra
            expand/collapse machinery; here both options are visible at once.
            """
            nonlocal cy
            if self.mini_hides(key):
                return
            if not show:
                return
            if label:
                # The control is as wide as its captions need, not a fixed
                # 60 units per option. Those captions are real words in
                # twelve languages - "Натуральный" ran past a 60-unit cell
                # and into its neighbour, and _draw_segmented centres the
                # caption and lets it spill, so the overflow reads as a
                # misspelling rather than as a clipped word.
                # test_settings_hints measures this the way it measures
                # hints; the label column keeps at least 150 units.
                widest = max((self._small_font.size(str(t))[0]
                              for t in (labels or options)), default=0)
                need = max(self._u(60), widest + self._u(22))
                seg_w = min(inner_w - self._u(150),
                            need * len(options) + self._u(60))
            else:
                # No label, no reason to squeeze: the captions are the
                # control. "Window mode" ran off the panel at the old width.
                seg_w = inner_w
            seg_h = height or ctrl_h
            rect = pygame.Rect(pad + inner_w - seg_w, cy, seg_w, seg_h)
            items.append(Item("segmented", key, rect, payload=list(options),
                              extra={"label": label, "current": current,
                                     "labels": list(labels or options),
                                     "square": squares}))
            cy += seg_h + gap

        def toggle(key: str, label: str, on: bool, hint: str = "",
                   inline_right: list[tuple[str, str, bool]] | None = None,
                   segments_only: bool = False) -> None:
            nonlocal cy
            if not show or self.mini_hides(key):
                return
            # Small inline buttons at the row's right end, beside the switch:
            # the multiplier rides the FG row itself (user, 14.09) instead of
            # a second full-width row below it. Returns their x-span so the
            # caller can lay them out.
            #
            # `segments_only` drops the switch itself and lets the segment group
            # BE the control (the mockup's FG row: Off / x2 / x3 / x4, nothing
            # else). It is only for rows whose group carries every state: with no
            # switch left, a group that could not express "off" would strand the
            # feature on.
            inline_x = None
            if inline_right:
                btn_w = self._u(44)
                btn_h = self._u(CTRL_H) - self._u(8)
                inline_x = pad
            extra = {"label": label, "square": squares}
            hint_h = 0
            if hint:
                extra["hint"] = hint
                # Multi-line hints: the Spout2 toggle explains two capture
                # paths and does not fit one line at 1440p. The block is
                # measured with the real font height, and the last line
                # carries no trailing space of its own - with it a hinted
                # control sat further from its neighbour than two plain ones.
                line_h = self._small_font.get_height() + self._u(4)
                hint_h = (self._u(8) + (str(hint).count("\n") + 1) * line_h
                          - self._u(4))
            # The hit zone is the SWITCH, not the row (user rule 16.09:
            # "only explicit switches and choices should react"). A toggle
            # row spans the whole panel width, and clicking its empty left
            # half - or the label - used to flip the switch. The switch
            # itself is drawn at the row's right end (_draw_toggle), so the
            # zone is that pill plus a small margin for the fingertip; the
            # label is a caption, not a control.
            switch_size = self._u(20)
            switch_w = int(switch_size * 1.8)   # the drawn pill (see _draw_toggle)
            switch_x = pad + inner_w - switch_w
            items.append(Item("toggle", key,
                              pygame.Rect(pad, cy, inner_w, ctrl_h + hint_h),
                              value=1.0 if on else 0.0,
                              extra=extra))
            items[-1].extra["segments_only"] = segments_only
            # A segments_only row has no switch of its own, and its hit zone must
            # not fall back to the row rect: the row spans the panel and would
            # swallow clicks meant for the cells. A zero-size rect placed far off
            # the panel is the honest "no zone here" - an empty Rect is falsy, so
            # callers that do  would use the row.
            #  is None for a row whose control is the segment group: the
            # group's own cells carry the zones. A zero-size Rect would NOT work
            # - every consumer reads the zone as , and
            # an empty Rect is falsy, so the whole row fell back to its 488px
            # rect and swallowed the cells' clicks.
            items[-1].extra["hit"] = (None if segments_only else
                                      pygame.Rect(switch_x - self._u(6), cy,
                                                  switch_w + self._u(12), ctrl_h))
            if inline_x is not None:
                # The cells: one segment GROUP against the row's right edge,
                # touching, with the group frame and dividers drawn by
                # `_draw_segment_groups` after the cells.
                cell_h = self._u(CTRL_H)
                # Each cell takes the width ITS OWN label needs. Sizing them all
                # to the widest was the tidier rule and it does not survive
                # translation: "off" is three characters in English and eleven
                # in Spanish ("desactivado"), so three two-character steps were
                # each given 95 px, the group took 380 of the row's 488, and the
                # control's own name - "DLSS 4.5 FG", 94 px - was clipped in the
                # 91 px left over. Measured with the product's fonts: es was the
                # one that clipped, pt and it cleared it by under 33 px. Per-cell
                # widths bring the same group down to 227 px in every locale.
                widths = [max(self._u(44),
                              self._small_font.size(lbl)[0] + self._u(20))
                          for _, lbl in inline_right]
                group_w = sum(widths)
                right = pad + inner_w
                bx = right - group_w
                group_x, group_y = bx, cy + (ctrl_h - cell_h) // 2
                #: Where each divider goes, measured from the group's left edge:
                #: the cells are no longer equal, so an even split would draw the
                #: lines away from the boundaries they mark.
                splits = []
                for (opt_key, opt_label), cell_w in zip(inline_right, widths):
                    items.append(Item("button", opt_key,
                                      pygame.Rect(bx, group_y,
                                                  cell_w, cell_h),
                                      extra={"label": opt_label,
                                             "filled": False,
                                             "small": True,
                                             "segment": True}))
                    bx += cell_w
                    if bx < right:
                        splits.append(bx - group_x)
                # Content coordinates, like every other rect here: the screen
                # rect is derived at the end of the layout (see `_segment_rects`).
                self._segment_groups.append((
                    pygame.Rect(group_x, group_y, group_w, cell_h),
                    tuple(splits)))
            cy += ctrl_h + hint_h + gap

        # The windows page: the full list of capturable windows, one row per
        # window. Hovering a row highlights the real window's outline on the
        # screen (main draws the frame); clicking switches the capture.
        if self.page == "windows":
            section(s["sec_windows"])
            wins = self.state.get("windows") or []
            current_window = _window_record(self.state.get("window_current"))
            if not wins:
                items.append(Item("button", "no_windows",
                                  pygame.Rect(pad, cy, inner_w, act_h),
                                  extra={"label": s.get("win_none", "No windows"),
                                         "filled": False}))
                cy += act_h + pad
            else:
                row_h = self._u(CTRL_H) + self._u(8)
                for raw_window in wins:
                    window = _window_record(raw_window)
                    items.append(Item("option", "window",
                                      pygame.Rect(pad, cy, inner_w, row_h),
                                      payload=window["identity"],
                                      extra={"label": window["label"],
                                             "hwnd": window["hwnd"],
                                             "size": window.get("size", ""),
                                             "selected": (
                                                 window["hwnd"] is not None
                                                 and window["hwnd"]
                                                 == current_window["hwnd"])}))
                    cy += row_h + self._u(4)
            cy += gap

        elif self.page == "convert":
            # The conversion page. Laid out like the windows page - rows, and
            # Back beside the page's own action in the footer - because it is
            # the same kind of page: a list the user acts on, not settings set
            # once in a lifetime. Top to bottom: what the page does, the queue,
            # and what the files come out as.
            jobs = [dict(j) for j in (self.state.get("convert_jobs") or [])
                    if isinstance(j, dict)]
            hint = s.get("convert_page_hint", "")
            if hint:
                note_h = self._note_height(hint, inner_w)
                items.append(Item("note", "convert_hint",
                                  pygame.Rect(pad, cy, inner_w, note_h),
                                  extra={"text": hint}))
                cy += note_h + gap
            section(s.get("sec_convert_queue", "files"))
            if not jobs:
                # The empty state is also the drop target's outline: files
                # dropped anywhere on the screen land here, and the dashed box
                # is what says so before anyone tries.
                empty = s.get("convert_empty", "")
                inset = self._u(14)
                box_h = self._note_height(empty, inner_w - 2 * inset) + 2 * inset
                items.append(Item("note", "convert_empty",
                                  pygame.Rect(pad, cy, inner_w, box_h),
                                  extra={"text": empty, "boxed": True,
                                         "inset": inset}))
                cy += box_h + gap
            else:
                act_w = self._convert_action_width(s)
                row_pad = self._u(8)
                row_h = (row_pad + label_h
                         + self._small_font.get_height() + self._u(4)
                         + self._u(4) + row_pad)
                btn_h = ctrl_h - self._u(4)
                for job in jobs:
                    rect = pygame.Rect(pad, cy, inner_w, row_h)
                    action = str(job.get("action")
                                 or _CONVERT_ROW_ACTION.get(job.get("status"),
                                                            "retry"))
                    items.append(Item("job", f"convert_row:{job.get('id')}",
                                      rect, payload=job.get("id"),
                                      extra={"job": job, "act_w": act_w,
                                             "row_pad": row_pad}))
                    # The row's one button, keyed by the job and the action:
                    # the key IS the command (commands.convert_row_action),
                    # and a key per row keeps hover and focus on this row.
                    items.append(Item(
                        "button", f"convert_job:{job.get('id')}:{action}",
                        pygame.Rect(rect.right - row_pad - act_w,
                                    rect.y + row_pad + (label_h - btn_h) // 2,
                                    act_w, btn_h),
                        extra={"label": s.get(f"convert_act_{action}", action),
                               "small": True}))
                    cy += row_h + self._u(6)
                cy += self._u(4)
                finished = any(j.get("status") in ("done", "failed", "cancelled")
                               for j in jobs)
                active = any(j.get("status") in ("queued", "running")
                             for j in jobs)
                bw = inner_w // 2
                for idx, (key, label, enabled) in enumerate((
                        ("convert_clear", s.get("convert_clear", "Clear finished"),
                         finished),
                        ("convert_stop_all", s.get("convert_stop_all", "Stop all"),
                         active))):
                    cw = bw if idx == 0 else inner_w - bw
                    items.append(Item("button", key,
                                      pygame.Rect(pad + idx * bw, cy, cw, act_h),
                                      extra={"label": label, "filled": False,
                                             "pair": "left" if idx == 0 else "right",
                                             "disabled": not enabled}))
                cy += act_h + self._u(8)

            section(s.get("sec_convert_output", "output"))
            dest = str(self.state.get("convert_dest", "source"))
            segmented("convert_dest", s.get("convert_dest", "Save to"), dest,
                      ["source", "folder"],
                      labels=[s.get("convert_dest_source", "Beside original"),
                              s.get("convert_dest_folder", "One folder")])
            if dest == "folder":
                # The folder and the action beside it - the screenshot
                # folder's row, for the same reasons (the path elided in the
                # middle, the button saying only what it does).
                folder = str(self.state.get("convert_dir") or "")
                bgap = self._u(BTN_GAP)
                btn_w = max(self._u(120), inner_w // 3)
                items.append(Item("info", "convert_dir_path",
                                  pygame.Rect(pad, cy, inner_w - btn_w - bgap,
                                              ctrl_h),
                                  extra={"label": "",
                                         "value": self._elide_path(folder)}))
                items.append(Item("button", "convert_dir",
                                  pygame.Rect(pad + inner_w - btn_w, cy,
                                              btn_w, ctrl_h),
                                  extra={"label": s.get("change_folder",
                                                        "Change folder..."),
                                         "small": True}))
                cy += ctrl_h + gap
            segmented("convert_codec", s.get("convert_codec", "Video codec"),
                      str(self.state.get("convert_codec", "auto")),
                      ["auto", "av1", "hevc", "h264"],
                      labels=[s.get("convert_codec_auto", "Auto"),
                              "AV1", "HEVC", "H.264"])
            segmented("convert_quality", s.get("convert_quality", "Quality"),
                      str(self.state.get("convert_quality", "high")),
                      ["high", "balanced", "small"],
                      labels=[s.get("convert_quality_high", "Best"),
                              s.get("convert_quality_balanced", "Balanced"),
                              s.get("convert_quality_small", "Smaller")])
            segmented("convert_image_format",
                      s.get("convert_image_format", "Image format"),
                      str(self.state.get("convert_image_format", "keep")),
                      ["keep", "png", "jpg"],
                      labels=[s.get("convert_image_keep", "Original"),
                              "PNG", "JPG"])
            toggle("convert_audio", s.get("convert_audio", "Keep audio"),
                   bool(self.state.get("convert_audio", True)))

        elif self.page == "settings":
            # Four tabs where six sections used to run one after another. The
            # page is where everything set once in a lifetime lives, and it
            # is where every new setting will land - a single column of
            # sections is what made the old menu grow without bound.
            # A row of TABS, not a segment: the active one is a raised cell
            # with an accent square, not an accent-filled cell. An accent fill
            # is this panel's word for "on", and a tab is a place, not a switch.
            tab_h = self._u(15) + 2 * self._u(10)
            self._tabs_rel = pygame.Rect(pad, cy, inner_w, tab_h)
            cy += tab_h + self._u(4)
            # The tabs as items: one per group, in a row. They are built here
            # rather than by the `segmented` helper because their chosen state
            # is drawn as a square, and the helper's whole contract is "the
            # chosen cell is accent-filled".
            tab_w = inner_w // len(SETTINGS_TABS)
            for idx, t in enumerate(SETTINGS_TABS):
                # `cw`, NOT `w`: `w` is the panel's width for the rest of this
                # function, and reusing the name here made every rect below the
                # loop 122 wide - the panel's viewport included, so the tabs
                # could not be hit.
                cw = tab_w if idx < len(SETTINGS_TABS) - 1 \
                    else inner_w - tab_w * (len(SETTINGS_TABS) - 1)
                items.append(Item(
                    "tab", t,
                    pygame.Rect(pad + idx * tab_w, self._tabs_rel.y, cw,
                                self._tabs_rel.h),
                    extra={"label": s[f"tab_{t}"],
                           "active": t == self.settings_tab}))

            section(s["sec_capture"], "capture")
            monitors = self.state.get("monitors") or []
            if monitors:
                # The hint is here because two people asked the same question
                # in different words (#33, #35): they tried to DRAG the
                # picture onto another screen, and with Win+Shift+arrow. It
                # is not a window - it is a layer covering the whole chosen
                # monitor - so nothing happens, and this row is the control
                # they were looking for. Only shown with more than one
                # monitor: on a single display the sentence is noise.
                choice("monitor", s.get("monitor", "Monitor"),
                       str(self.state.get("monitor", "0")), monitors,
                       hint=s.get("monitor_hint", "") if len(monitors) > 1 else "")
            # The card the network and the capture run on. Shown only when
            # there is something to choose: on one card the row would be a
            # control that cannot do anything.
            gpus = self.state.get("gpus") or []
            if len(gpus) > 1:
                choice("gpu", s.get("gpu", "GPU"),
                       str(self.state.get("gpu", gpus[0])), gpus,
                       hint=s.get("gpu_hint", ""))
            # HDR compatibility. It belongs to CAPTURE because that is what
            # it changes first: the display is duplicated in FP16 scRGB
            # instead of 8-bit, and everything after follows from that.
            # Experimental, off by default, and a worker restart - which is
            # why it is here and not on the main page.
            toggle("hdr", s.get("hdr_mode", "HDR compatibility"),
                   bool(self.state.get("hdr")),
                   hint=s.get("hdr_mode_hint", ""))
            choice("motion_backend", s.get("motion_backend", "Motion estimation"),
                   self.state.get("motion_backend", "nvofa"), ["nvofa", "cpu"],
                   labels=[s.get("motion_nvofa", "NVOFA (experimental)"),
                           "CPU DIS"],
                   hint=s.get("motion_hint", "Restarts the worker; CPU fallback if unavailable"))
            choice("screenshot_mode", s.get("screenshot_mode", "Screenshot saving"),
                   str(self.state.get("screenshot_mode", "ask")),
                   ["ask", "auto"],
                   labels=[s.get("screenshot_ask", "Save As"),
                           s.get("screenshot_auto", "Save automatically")])
            # Three fixed options in the mockup, two in fact: BMP is not a
            # format this app writes, and offering a value the program cannot
            # honour is worse than showing one option fewer. The rule is the
            # affordance - a fixed short list is a segment, only open-ended
            # lists stay drop-downs.
            segmented("screenshot_format", s.get("screenshot_format",
                                                 "Screenshot format"),
                      str(self.state.get("screenshot_format", "png")),
                      ["png", "jpg"], labels=["PNG", "JPG"])
            # The screenshot folder: the PATH and the action, side by side.
            # It used to be one button whose caption carried the path, and a
            # long path ate the caption. Now the destination is its own line
            # (mono, elided in the middle so the drive and the last folder both
            # survive) and the button says only what it does.
            shot_dir = self.state.get("screenshot_dir") or ""
            if show:
                bgap = self._u(BTN_GAP)
                btn_w = max(self._u(120), inner_w // 3)
                path_rect = pygame.Rect(pad, cy, inner_w - btn_w - bgap,
                                        ctrl_h)
                items.append(Item("info", "shot_dir_path", path_rect,
                                  extra={"label": "",
                                         "value": self._elide_path(shot_dir)}))
                items.append(Item("button", "shot_dir",
                                  pygame.Rect(pad + inner_w - btn_w, cy,
                                              btn_w, ctrl_h),
                                  extra={"label": s.get("change_folder",
                                                        "Change folder..."),
                                         "small": True}))
                cy += ctrl_h + gap

            # Recording: everything about what leaves the program besides
            # the screen itself. Spout2 (off by default) publishes the
            # processed picture for external recorders; the recording
            # indicator is a display preference of the same subject.
            section(s["sec_recording"], "rec")
            record_dir = self.state.get("recording_dir") or ""
            record_label = s.get("record_dir_btn", "Recording folder...")
            if record_dir:
                record_label = f"{record_label}  ·  {record_dir}"
            if show:
                items.append(Item("button", "record_dir",
                                  pygame.Rect(pad, cy, inner_w, ctrl_h),
                                  extra={"label": record_label}))
                cy += ctrl_h + gap
            # On the GPU the recording costs no frame rate, but the worker
            # never sees the menu window; off, the old path draws it in.
            toggle("gpu_record", s.get("gpu_record", "Record on the GPU"),
                   bool(self.state.get("gpu_record", True)),
                   hint=s.get("gpu_record_hint", ""))
            toggle("spout", s.get("spout", "Spout2 output (OBS)"),
                   bool(self.state.get("spout")),
                   hint=s.get("spout_hint", ""))
            toggle("rec_indicator", s.get("rec_indicator", "Recording indicator"),
                   bool(self.state.get("rec_indicator", True)))
            # The frame counter on screen and the corner it sits in (#109).
            # The corners are ARROWS, not words: four translated corner names
            # would not fit a five-cell segment in any of the long languages,
            # and an arrow needs no translation at all. Measured: every
            # bundled face carries U+2196..U+2199.
            segmented("fps_overlay",
                      s.get("fps_overlay", "Frame counter"),
                      str(self.state.get("fps_overlay", "off")),
                      ["off", "tl", "tr", "bl", "br"],
                      # Five GLYPHS, including the off cell. The word did not
                      # fit: es "desactivado" measured 112% of a fifth of the
                      # row, it 99%, pt 101% - and a five-cell group has no
                      # width to give. A cross beside four corner arrows reads
                      # as "nowhere", the row label says what is being placed,
                      # and none of the five needs translating.
                      #
                      # The dash, not a cross: U+2715 came out as tofu in the
                      # panel's own face, and the dash is already this app's
                      # word for "no reading" - the status line prints it when
                      # there is no rate.
                      labels=["—", "↖", "↗",
                              "↙", "↘"])
            rec_status = str(self.state.get("recording_status") or "")
            rec_details = str(self.state.get("recording_details") or "")
            rec_path = str(self.state.get("recording_path") or "")
            for key, info_label, value in (
                    ("record_status", s.get("record_state", "State"),
                     s.get(f"record_status_{rec_status}", rec_status)),
                    ("record_details", s.get("record_format", "Format"), rec_details),
                    ("record_path", s.get("record_path", "Path"), rec_path)):
                if show and value:
                    items.append(Item("info", key,
                                      pygame.Rect(pad, cy, inner_w,
                                                  self._u(LABEL_H)),
                                      extra={"label": info_label,
                                             "value": value}))
                    cy += self._u(LABEL_H) + self._u(4)

            # File conversion. It belongs on this tab because this tab is
            # about the ways a picture leaves the program: recording is the
            # screen over time, this is a file on disk. The settings are not
            # repeated here on purpose - a conversion uses the profile and
            # the sliders the panel is already showing, so there is one place
            # to tune the look and not two that can disagree.
            section(s.get("sec_convert", "file conversion"), "rec")
            busy = bool(self.state.get("convert_busy"))
            if show:
                # Opens the conversion page, where the queue and the output
                # choices live; the main page's Convert cell goes there too.
                items.append(Item("button", "convert",
                                  pygame.Rect(pad, cy, inner_w, ctrl_h),
                                  extra={"label": s.get(
                                      "convert_pick", "Convert files..."),
                                         "filled": False}))
                cy += ctrl_h + gap
                # One row that says either what is happening or what the
                # button is for, so the section is never silent.
                status = str(self.state.get("convert_status") or "")
                items.append(Item("info", "convert_status",
                                  pygame.Rect(pad, cy, inner_w,
                                              self._u(LABEL_H)),
                                  extra={"label": s.get(
                                      "record_state", "State") if busy else "",
                                         "value": status or s.get(
                                             "convert_hint", "")}))
                cy += self._u(LABEL_H) + self._u(4)

            section(s["sec_behaviour"], "app")
            toggle("open_on_start", s["open_on_start"],
                   bool(self.state.get("open_on_start")))
            toggle("autostart", s.get("autostart", "Autostart with Windows"),
                   bool(self.state.get("autostart")))
            # #93: what the taskbar button's minimise and close do. Both say
            # the same thing in their hint, because it is the thing a user is
            # entitled to worry about: going to the tray does not stop the
            # neural pass - the picture on screen is the program's output, and
            # a "minimise" that changed it without saying so would be a
            # different feature.
            toggle("tray_on_minimise",
                   s.get("tray_on_minimise", "Minimise to tray"),
                   bool(self.state.get("tray_on_minimise")),
                   hint=s.get("tray_hint", ""))
            toggle("tray_on_close",
                   s.get("tray_on_close", "Close to tray"),
                   bool(self.state.get("tray_on_close")))

            section(s["sec_hotkeys"], "keys")
            # The remapping fields. The captions on the buttons come from these
            # same values, so a key change is visible across the whole menu at
            # once.
            field_h = self._u(CTRL_H)
            for cmd, label in HOTKEY_ROWS if show else ():
                hk = Item("hotkey", cmd,
                          pygame.Rect(pad, cy, inner_w, field_h),
                          extra={"label": s.get(label, label),
                                 "key": self.hotkeys.get(cmd, "—"),
                                 "capturing": self.capturing == cmd})
                # Only the key FIELD reacts, not the whole row (user rule
                # 16.09): the action label on the left is a caption.
                # _draw_hotkey draws the field 170 units wide at the row's
                # right end - the same rect.
                hk.extra["hit"] = pygame.Rect(
                    pad + inner_w - self._u(170), cy,
                    self._u(170), field_h)
                items.append(hk)
                cy += field_h + self._u(6)
            # The caption belongs to the rows above it, not to the section
            # below: a full row gap on both sides left 96 px of nothing
            # before APPEARANCE.
            cy += self._u(8)
            if show:
                self._hint_rel = pygame.Rect(pad, cy, inner_w,
                                             self._u(SMALL_SIZE) + self._u(6))
                cy += self._hint_rel.h + gap
            else:
                # Not on this tab - and the rect has to be emptied, not just
                # left unset: it is a member, the draw reads it every frame,
                # and the caption from the keys tab floated under Theme on
                # the program tab (user, 13.09).
                self._hint_rel = pygame.Rect(0, 0, 0, 0)

            # Appearance: language and theme moved here from the main page
            # (user rule 10.09: the main page is the main page - settings
            # live behind the gear). The segmented controls emit the same
            # ("lang", ...) / ("theme", ...) actions main already handles.
            section(s["sec_view"], "app")
            # The language list: a drop-down, not segments - the full set
            # of popular languages (12) cannot fit in a segmented row
            # (user rule 10.09: the list expands, it is not cycled).
            langs = list(STRINGS.keys())
            choice("lang", s["language"], self.lang, langs,
                   labels=[STRINGS[L].get(f"lang_{L}", L) for L in langs])
            segmented("theme", s["theme"], self.state.get("theme", "light"),
                      list(THEME_NAMES),
                      [s[f"theme_{t}"] for t in THEME_NAMES])
            # No extra gap here: segmented() already ends with one, and
            # section() opens with its own - three stacked was a hole
            # (user, 13.09: the padding below is excessive).

            # The channel label: the header shows the version, the channel
            # lives here (user rule 2026-09-08). A button item - the only
            # non-interactive kind the drawer supports - with the label as
            # its caption.
            channel = self.state.get("channel") or ""
            about = self.state.get("about") or {}
            if channel or about:
                section(s["sec_about"], "app")
                # The same four facts the log header opens with. Every issue
                # starts by asking which version and which driver; this is
                # the answer, where it can be read without finding the log.
                for key, label in (("version", s["about_version"]),
                                   ("gpu", s["gpu"]),
                                   ("driver", s["about_driver"]),
                                   ("windows", s["about_windows"])):
                    value = str(about.get(key) or "")
                    if not value or not show:
                        continue
                    items.append(Item("info", f"about_{key}",
                                      pygame.Rect(pad, cy, inner_w,
                                                  self._u(LABEL_H)),
                                      extra={"label": label, "value": value}))
                    cy += self._u(LABEL_H) + self._u(4)
                compat_status = str(
                    self.state.get("compatibility_status") or "not_run")
                compat_score = str(self.state.get("compatibility_score") or "")
                if show:
                    status_label = s.get(
                        f"compatibility_{compat_status}", compat_status)
                    value = status_label + (f" · {compat_score}" if compat_score else "")
                    items.append(Item("info", "compatibility",
                                      pygame.Rect(pad, cy, inner_w,
                                                  self._u(LABEL_H)),
                                      extra={"label": s.get(
                                          "compatibility", "Compatibility"),
                                             "value": value}))
                    cy += self._u(LABEL_H) + self._u(8)
                    items.append(Item("button", "diagnostics",
                                      pygame.Rect(pad, cy, inner_w, act_h),
                                      extra={"label": s.get(
                                          "diagnostics_create",
                                          "Create diagnostic package"),
                                             "filled": False}))
                    cy += act_h + self._u(6)
                if show and about:
                    cy += self._u(6)
            if channel:
                act_h = self._u(ACTION_H)
                if show:
                    items.append(Item("button", "channel",
                                      pygame.Rect(pad, cy, inner_w, act_h),
                                      extra={"label": channel,
                                             "filled": False}))
                    # The footer below opens with its own rule and spacing;
                    # a full PAD on top of that was the second hole.
                    cy += act_h + self._u(6)
        else:
            section(s["sec_processing"], squares_here=True)
            nr_on = bool(self.state.get("nr"))
            # No key name here. The main page used to print "Num1" beside the
            # switch, and every control that had a key printed it - furniture
            # nobody reads twice, in the one place where the picture is being
            # judged. The keys live on the settings page, which is where you
            # go when you want to know or change them (user, 13.09).
            # The row is named for what the feature is, not for its state -
            # the switch at the right end already carries on/off (user, 14.09).
            toggle("nr", "DLSS 5 NR", nr_on)

            # Boost: the network runs at a reduced resolution and the detail
            # comes back off the native frame (the matched residual
            # composite). It sits directly under the DLSS 5 switch because it
            # is the same subject - how hard the network works - and because
            # nobody found it where it was.
            #
            # Measured on a 5070 Ti at 4K, 12.09: the network costs 16.0 ms
            # against 5.0-7.3 ms, the whole program runs 45.7 -> 72.6 fps at
            # 0.65, and at 1:1 on text, a game scene and photographic content
            # the difference is not visible. The residual is what makes that
            # true: without it the same setting is visibly soft.
            fg = bool(self.state.get("frame_generation"))
            multiplier = int(self.state.get("frame_multiplier", 2))
            # The multiplier rides the FG row: three small buttons between the
            # label and the switch, the selected one filled (user, 14.09).
            # Selectable whether or not FG runs (user, 15.09): it is a
            # preference for the next attempt, not a live control - locking it
            # behind the switch deadlocked a 40-series card (caps at 2x) when
            # the first attempt refused and flipped itself back off.
            # The live step, when the runtime refused the pick and the worker
            # stepped down (issue #100). The buttons show the PREFERENCE (they
            # stay where the user put them, so the next attempt asks for it
            # again); this hint says what is actually running, so a 4x pick on
            # a 2x-capable card does not silently differ from the FPS counter.
            live = self.state.get("frame_multiplier_active")
            fg_hint = ""
            if fg and isinstance(live, int) and live != multiplier:
                fg_hint = s.get(
                    "fg_capped",
                    "the runtime caps this card at x{live} - FG runs there, "
                    "your x{want} is asked for again on the next attempt"
                ).format(live=live, want=multiplier)
            toggle("frame_generation", "DLSS 4.5 FG", fg, hint=fg_hint,
                   segments_only=True,
                   inline_right=[("frame_generation:off", s.get("off", "off")),
                                 ("frame_multiplier:2", "×2"),
                                 ("frame_multiplier:3", "×3"),
                                 ("frame_multiplier:4", "×4")])
            # The four cells are ONE segment group (the mockup's direction): Off
            # plus the three steps, the live one filled. Off is filled while FG
            # is off, a step while it runs - so the group always says exactly
            # what is happening.
            #
            # The steps stay clickable with FG off, and that is deliberate: they
            # are a preference for the NEXT attempt, not a live control. Locking
            # them behind the switch deadlocked a 40-series card (caps at x2)
            # when the first attempt was refused and flipped itself back off.
            # Picking a step with FG off therefore also asks for FG on - one
            # click instead of two, and no dead end.
            for idx, value in enumerate((2, 3, 4), start=1):
                btn = items[-4 + idx]
                btn.extra["filled"] = fg and multiplier == value
            off_btn = items[-4]
            off_btn.extra["filled"] = not fg

            limit_mode = str(self.state.get("frame_limit_mode", "unlimited"))
            # The hint is not decoration: the cap paces how often a frame is
            # taken from the worker, so it counts SOURCE frames. Frame
            # Generation runs inside the worker and reports its own rate, which
            # is why a 60 cap and a 170 fps counter are both true at once. The
            # reporter had to work that out from the numbers (#109).
            choice("frame_limit_mode", s.get("frame_limit", "Frame limit"),
                   limit_mode, ["30", "60", "custom", "unlimited"],
                   labels=[s.get("frame_limit_30", "30 fps"),
                           s.get("frame_limit_60", "60 fps"),
                           s.get("frame_limit_custom", "Custom"),
                           s.get("frame_limit_unlimited", "Unlimited")],
                   hint=s.get("frame_limit_hint", ""))
            if limit_mode == "custom":
                custom = int(self.state.get("frame_limit_custom", 90))
                slider("frame_limit_custom", 15, 240, custom,
                       s.get("frame_limit_custom_value", "Custom limit"),
                       value_text=f"{custom} fps")

            boost = bool(self.state.get("nr_small"))
            toggle("boost", s["boost"], boost)

            # The resolution the network runs at - only while Boost is on.
            #
            # Without Boost this slider changes nothing whatsoever: the
            # network runs at the full frame size no matter where the knob
            # is, and the output frames come back bit-identical at every
            # position (measured on four real 4K frames, 12.09). That is why
            # the two used to be one control, with the top step standing in
            # for "off" - a slider that answers and does nothing is worse
            # than no slider. The switch above carries that meaning now, so
            # the slider carries only the resolution, and it is simply not on
            # screen when it would be inert.
            if boost:
                # No section heading of its own: the slider belongs to the
                # switch above it, and "PROCESSING / Boost / RESOLUTION /
                # Resolution the network runs at" says the same word three
                # times before saying anything.
                # The top of the range is the NGX cap where one binds (0.65
                # on a 4K screen, where 2560x1440 is reached) and the whole
                # source where it does not - on 1440p and below the scale
                # runs all the way to 1.00.
                cap = float(self.state.get("work_scale_cap", 1.0))
                pos = float(self.state.get("work_scale", cap))
                # The size the network actually runs at. It used to say
                # "3840x2160 - full" at the top, which is not true on a 4K
                # screen: NGX is capped at 2560x1440 and the network never
                # saw more than that (user, 12.09).
                work = str(self.state.get("work_size") or "")
                value_text = work if work else f"{pos:.2f}"
                # The lower bound follows WORK_SCALE_MIN (0.1), not a
                # hardcoded 0.30: a config value below the slider range would
                # put the knob at the bottom while the label shows a
                # different resolution.
                lo = float(self.state.get("work_scale_min", 0.1))
                # No caption under this one. It used to name the trade at
                # both ends of the track; the redesign replaced the two words
                # with the ruler and stopped drawing them, so all the pair did
                # afterwards was reserve a blank line - the hole under this
                # slider (user, 20.09). The value cell says the size, which is
                # the number the choice is actually made on.
                slider("nr_res", lo, cap, pos, s["nr_res"],
                       value_text=value_text)
                # The cascade. It lives under Boost because it only runs in
                # that mode - outside it the network writes the full-res
                # output directly and a second pass would need a full-res
                # scratch. An experiment, and priced like one: each extra
                # pass is another full evaluation. Measured: the second
                # pass costs about a THIRD of the frame rate (not the half
                # this comment used to claim), and each extra pass carries its
                # own network - about 640 MB at a 2560x1440 work size, scaling
                # with that size. Four passes took the worker to 3 GB.
                passes_now = int(self.state.get("nr_passes", 1) or 1)
                segmented("nr_passes", s.get("nr_passes", "NR passes"),
                          str(max(1, min(4, passes_now))),
                          ["1", "2", "3", "4"], labels=["1", "2", "3", "4"])
                items[-1].extra["state_default"] = "1"
                # What passes 2..N do. Only shown from two passes: with one
                # pass there is nothing for a second set to apply to, and the
                # row would be a control that cannot do anything - the same
                # reason the resolution slider is hidden without Boost.
                #
                # This is the point of the whole cascade. Every pass used to
                # read the same numbers, so pass 2 repeated pass 1 exactly: the
                # frame cost a third of the frame rate more and looked the
                # same. Measured 22.09: pass 2 on Faithful against a Strong
                # main set moves 100% of pixels (mean 9.4 codes, max 20) while
                # sending the same numbers through the same command moves
                # none - so the difference is the numbers, not the command.
                per_pass = self.state.get("nr_pass_params") or {}
                if passes_now >= 2:
                    # The switch first, then what it governs: with the set off
                    # there is nothing under it, and a row of sliders that
                    # silently do nothing is the worst of the three states.
                    #
                    # The hint goes through the toggle's own argument rather
                    # than into extra afterwards: the row's height is worked
                    # out from the hint when the item is built, so a hint set
                    # later is drawn inside a rect that was measured without
                    # it - the text is then clipped by the next row.
                    toggle("pass_params", s.get("pass_params", "Own set"),
                           bool(per_pass), s.get("pass_params_hint", ""))
                    if per_pass:
                        # Style picks which network runs, so it is the first
                        # control of the set - the same order the main section
                        # uses, one section up.
                        segmented("pass_style", s["style"],
                                  str(int(per_pass.get("style", 1))),
                                  ["0", "1", "2"],
                                  labels=[s["style_0"], s["style_1"],
                                          s["style_2"]])
                        defaults = self.state.get("pass_param_defaults") or {}
                        ranges = self.state.get("pass_param_ranges") or {}
                        for key in PARAM_KEYS:
                            lo, hi = ranges.get(key) or PARAM_FALLBACK
                            val = float(per_pass.get(key, 0.0))
                            # The same four names as the main sliders, on
                            # purpose: they are the same four parameters, and
                            # translating a second copy of each into twelve
                            # languages would be twelve copies of the same
                            # word. The switch above them is what says whose
                            # set this is.
                            slider(f"pass_{key}", float(lo), float(hi), val,
                                   s[key], value_text=f"{val:.2f}",
                                   mark=defaults.get(key))

            # What is being processed - the first question anyone has, and
            # until now the only one answered on another page. The segment
            # sends what the Actions buttons used to send; the list of
            # windows still opens on its own page.
            section(s["sec_source"], squares_here=True)
            in_window = bool(self.state.get("window_mode"))
            segmented("source", "", "window" if in_window else "fullscreen",
                      ["fullscreen", "window"],
                      labels=[s["mode_fullscreen"], s["mode_window"]],
                      height=act_h)
            # Which window, only in window mode. On the whole screen the
            # source is the monitor, and the monitor is chosen on the
            # settings page - naming it here as well was the same thing
            # said twice (user, 12.09).
            if in_window:
                current = _window_record(self.state.get("window_current"))
                items.append(Item("info", "source_now",
                                  pygame.Rect(pad, cy, inner_w, self._u(LABEL_H)),
                                  extra={"label": (current["label"]
                                                   or s["mode_window"]),
                                         # A number, so it is drawn in the same
                                         # cell as every other number (1c rule).
                                         "cell": True,
                                         "value": str(self.state.get("work_size")
                                                      or "")}))
                cy += self._u(LABEL_H) + gap

            # The profile and the four effect sliders are their own subject -
            # what the picture looks like, not how hard the network works.
            section(s["sec_effect"])
            choice("profile", s["profile"], str(self.state.get("profile", "")),
                   list(self.state.get("profiles") or []))
            # "modified - revert", and only when it is true. A profile is a
            # starting point, and until now the menu gave no way to tell
            # whether you were still on one: the tick under each slider says
            # where the profile put THAT value, and nothing said "you have
            # moved four of them". Reverting is picking the same profile
            # again, which is exactly what the command already does.
            if self._profile_modified():
                items.append(Item("button", "revert_profile",
                                  pygame.Rect(pad, cy, inner_w,
                                              self._u(SMALL_SIZE) + self._u(6)),
                                  extra={"label": s["profile_modified"],
                                         "flat": True}))
                cy += self._u(SMALL_SIZE) + self._u(6) + self._u(4)
            # Called "Model" in the interface and `style` in the code: the
            # three values really do select three different networks, and
            # "style" next to the visual styles of a picture reads as a look
            # rather than a choice of engine (user, 13.09). The key, the wire
            # field and the config entry keep NVIDIA's name - DLSSNR.Style -
            # because renaming those would break every saved config for a
            # word.
            # Style picks WHICH look the network produces; the profile and
            # the sliders under it say how strongly. Measured, it is the
            # biggest lever there is - the three values are three different
            # outputs, not three strengths - and until now it was buried
            # inside the profile with no way to reach it. Default is the one
            # that suits a desktop; the other two are tuned for games and
            # soften photographs and text (README says so at length; a menu
            # hint would not fit on one line).
            segmented("style", s["style"],
                      str(int(self.state.get("style", 1))),
                      ["0", "1", "2"],
                      labels=[s["style_0"], s["style_1"], s["style_2"]])
            params = self.state.get("params") or {}
            defaults = self.state.get("param_defaults") or {}
            for key in PARAM_KEYS:
                ranges = self.state.get("param_ranges") or {}
                lo, hi = ranges.get(key) or PARAM_FALLBACK
                val = float(params.get(key, 0.0))
                slider(key, float(lo), float(hi), val, s[key],
                       value_text=f"{val:.2f}", mark=defaults.get(key))
            # Save / Delete preset: the user presets live in the same list
            # as the built-in profiles. Delete is only offered while a user
            # preset is active - the built-in profiles are not deletable.
            # One strip, two cells, like the SOURCE control: a pair of
            # bordered boxes weighed as much as anything on the page, and the
            # strip says "these two belong together" without spending a border
            # each to say it (user, 20.09).
            bw = inner_w // 2
            for idx, (key, label) in enumerate(() if mini_short else (
                    ("save_preset", s["save_preset"]),
                    ("delete_preset", s["delete_preset"]))):
                cw = bw if idx == 0 else inner_w - bw
                items.append(Item("button", key,
                                  pygame.Rect(pad + idx * bw, cy, cw, act_h),
                                  extra={"label": label,
                                         "filled": False,
                                         "pair": "left" if idx == 0 else "right",
                                         "disabled": key == "delete_preset"
                                         and not self.state.get("preset_active")}))
            if not mini_short:
                cy += act_h + self._u(8)

            section(s["sec_compare"])
            split_val = float(self.state.get("split", 0.0))
            # The track alone: no row label, no percentage cell (user,
            # 20.09). The block title already says what this is, and the wipe's
            # subject is the picture behind the panel, not a number on it - the
            # user is looking at the seam, not reading a percentage. The ruler
            # carries the position: eleven ticks, so it reads in tenths rather
            # than in the quarters five would give. The one-line hint under it
            # stays - it says what the wipe is FOR, which no tick can.
            slider("split", 0.0, 1.0, split_val, s["split"],
                   hint=s["split_hint"], bare=True, ticks=11)

            section(s["sec_actions"])
            # Two rows of two: Select window + Fullscreen on top, Screenshot
            # + Record below (user rule 10.09: the capture actions belong
            # together in one section, the footer keeps only Exit).
            # Select window and Fullscreen left this section for the source
            # segment above: picking what to process is not an action, it is
            # a setting, and it belongs where the source is named.
            #
            # Convert joined them as a third cell: it is the third way a
            # picture leaves the program (a still of the screen, the screen
            # over time, a file on disk), and behind the gear on the Media tab
            # nobody found it. The cell opens the conversion page, and while a
            # file converts it carries the running file's percentage. Measured
            # with the product font at the small size: the longest caption in
            # any language, es "Captura de pantalla", is 121 px of the 123 a
            # third of the strip leaves it beside its icon.
            progress = self.state.get("convert_progress")
            convert_label = s.get("convert", "Convert")
            if isinstance(progress, (int, float)):
                convert_label = f"{convert_label} {int(progress * 100)}%"
            rows = (
                (("screenshot", s["screenshot"]),
                 ("record", (s.get("record_finalizing", "Finalizing...")
                             if self.state.get("recording_finalizing")
                             else s["record_stop_short"]
                             if self.state.get("recording")
                             else s["record"])),
                 ("convert", convert_label)),
            )
            # The widest caption each cell will ever show: Record turns into
            # Stop and Finalizing, Convert gains a percentage - measured at
            # their longest, the cells do not move when the state changes.
            ik = SMALL_SIZE / float(FONT_SIZE)
            icon_w = int(round(self._u(17) * ik)) + self._u(9)
            longest = {
                "record": (s["record"], s["record_stop_short"],
                           s.get("record_finalizing", "Finalizing...")),
                "convert": (f"{s.get('convert', 'Convert')} 100%",),
            }
            for row in rows:
                needs = [max(self._small_font.size(str(t))[0]
                             for t in longest.get(key, (label,)))
                         + icon_w + self._u(16)
                         for key, label in row]
                if all(n <= inner_w // len(row) for n in needs):
                    # Equal thirds whenever every caption fits one.
                    widths = [inner_w // len(row)] * len(row)
                else:
                    # Otherwise each cell takes what its caption needs and
                    # the rest is shared - the FG row's rule, for the FG
                    # row's reason: ja "スクリーンショット" is 126 px in its
                    # own face, and a third of the strip leaves 123.
                    spare = max(0, inner_w - sum(needs)) // len(row)
                    widths = [n + spare for n in needs]
                widths[-1] = inner_w - sum(widths[:-1])
                x = pad
                for idx, ((key, label), cw) in enumerate(zip(row, widths)):
                    # One strip, its cells side by side - the SOURCE control's
                    # look, which is what the page already uses for things
                    # that belong together (user, 20.09).
                    last = idx == len(row) - 1
                    items.append(Item("button", key,
                                      pygame.Rect(x, cy, cw, act_h),
                                      extra={"label": label,
                                             # The 1c rule: each capture action
                                             # carries a 1.4 px line icon.
                                             "icon": key if key in
                                             ("screenshot", "record",
                                              "convert") else None,
                                             "filled": False,
                                             "pair": ("left" if idx == 0
                                                      else "right" if last
                                                      else "mid"),
                                             "disabled": (key == "record" and
                                                          bool(self.state.get(
                                                              "recording_finalizing")))}))
                    x += cw
                cy += act_h + self._u(8)

        # The footer: actions with the hotkey printed underneath. "Collapse"
        # and "Exit" used to look equally harmless, even though one hides the
        # menu and the other unloads the program.
        # The settings page is sized to the tallest tab (measured by the
        # guarded pass at the head of layout()): the panel keeps one height
        # across tabs instead of jumping. The padding goes BEFORE the footer,
        # so the back button stays at the panel's bottom edge on every tab -
        # padding after it would read as a stretched empty bottom.
        if self.page == "settings" and not getattr(self, "_measuring", False):
            cy = max(cy, self._settings_content_h - self._u(14) - self._u(6)
                     - self._u(ACTION_H) - self._u(PAD))
        if self.page == "main":
            # The interface scale, above Quit (user, 20.09). It lives on the
            # main page rather than behind the gear because it is the answer
            # to "everything is too big", and somebody asking that question is
            # looking at this page, not hunting through settings.
            scale_now = float(getattr(self, "user_scale", 1.0))
            step_now = min(SCALE_STEPS,
                           key=lambda v: abs(v - scale_now))
            segmented("menu_scale", s.get("ui_scale", "Scale"),
                      f"{step_now:g}", [f"{v:g}" for v in SCALE_STEPS],
                      labels=[f"{int(round(v * 100))}%" for v in SCALE_STEPS])
            items[-1].extra["state_default"] = "1"
        cy += self._u(6)
        self._rule_rel = pygame.Rect(pad, cy, inner_w, 1)
        cy += self._u(14)
        act_h = self._u(ACTION_H)
        if self.page in ("settings", "windows", "convert"):
            if self.page in ("windows", "convert"):
                # Back and the page's own action side by side. On the windows
                # page that is Refresh: the list is frozen while the page is
                # open (a re-read every frame made rows shuffle under the
                # cursor - 13.09), so a fresh reading has to be asked for. On
                # the conversion page it is Add files: the one thing the page
                # cannot do without, kept where it cannot scroll away under a
                # long queue.
                bgap = self._u(BTN_GAP)
                bw = (inner_w - bgap) // 2
                items.append(Item("action", "back",
                                  pygame.Rect(pad, cy, bw, act_h),
                                  extra={"label": s["back"],
                                         "filled": False}))
                if self.page == "windows":
                    items.append(Item("action", "refresh_windows",
                                      pygame.Rect(pad + bw + bgap, cy, bw, act_h),
                                      extra={"label": s.get("refresh_list",
                                                            "Refresh list")}))
                else:
                    items.append(Item("action", "convert_add",
                                      pygame.Rect(pad + bw + bgap, cy, bw, act_h),
                                      extra={"label": s.get("convert_add",
                                                            "Add files...")}))
            else:
                items.append(Item("action", "back",
                                  pygame.Rect(pad, cy, inner_w, act_h),
                                  extra={"label": s["back"],
                                         "filled": False}))
            cy += act_h + pad
        elif not mini_short:
            # The name and nothing else, centred: the key and the
            # explanation under it turned one button into a paragraph.
            #
            # Not in mini mode: the panel is four rows there, and a
            # full-width red button under four rows is the one that gets
            # pressed by accident. The menu still quits from settings.
            items.append(Item("action", "exit",
                              pygame.Rect(pad, cy, inner_w, act_h),
                              extra={"label": s["exit_full"], "danger": True,
                                     "square": True, "square_danger": True}))
            cy += act_h + pad

        # The state of every row that draws a square. Decided HERE, where the
        # state payload is, and never in a drawer: a drawer that re-derives a
        # default is a second source of truth for the same question.
        for it in items:
            if it.kind == "choice":
                current = str(it.extra.get("current", ""))
                options = [str(o) for o in (it.payload or [])]
                neutral = NEUTRAL_CHOICE.get(it.key)
                if neutral is None:
                    neutral = options[0] if options else ""
                it.extra["state_filled"] = current != neutral
            elif it.kind == "segmented":
                current = str(it.extra.get("current", ""))
                options = [str(o) for o in (it.payload or [])]
                default = str(it.extra.get("state_default", options[0] if options else ""))
                it.extra["state_filled"] = current != default
            elif it.kind == "slider":
                lo, hi = float(it.lo), float(it.hi)
                mid = (lo + hi) / 2.0
                # A one-way control is neutral at its low end; a two-way one
                # (lo < 0 < hi) at the middle.
                neutral = mid if lo < 0.0 < hi else lo
                span = max(1e-6, hi - lo)
                it.extra["state_filled"] = abs(it.value - neutral) > span * 0.02

        # The content height is known. The panel may be shorter - then the
        # content scrolls: at 1080p a full panel took up almost the whole
        # screen and there was no way around it.
        content_h = cy
        title_h = self._u(TITLE_H)
        min_h = title_h + self._u(140)
        # We never stretch past the content: empty space at the bottom looks
        # broken, not spacious.
        h = content_h if self.user_height is None else int(self.user_height)
        h = max(min(h, content_h, max(0, screen_h - self._u(40))), min(min_h, content_h))
        self.content_height = content_h
        self._max_scroll = max(0, content_h - h)
        self.scroll = min(max(self.scroll, 0), self._max_scroll)
        # Offset from the centre plus a clamp, so the panel always stays
        # fully inside the screen: the user's saved position is honoured
        # (no jumping to a corner on mode switches), and a stale offset
        # (resolution change, monitor swap) is pulled back to the edge
        # instead of leaving the panel half off the desktop (user rule
        # 10.09: fixed position until the user drags it).
        x = (screen_w - w) // 2 + self.offset[0]
        y = (screen_h - h) // 2 + self.offset[1]
        x = min(max(x, 0), max(0, screen_w - w))
        y = min(max(y, 0), max(0, screen_h - h))
        self.panel_rect = pygame.Rect(x, y, w, h)
        self._title_bar = pygame.Rect(x, y, w, title_h)
        grip = self._u(26)
        self._grip = pygame.Rect(x + w - grip, y + h - grip, grip, grip)
        # The bottom edge drags the height while the corner stays in charge of
        # the scale - which is why the edge zone stops short of the corner.
        edge = self._u(7)
        self._edge = pygame.Rect(x, y + h - edge, max(0, w - grip), edge)
        self._viewport = pygame.Rect(x, y + title_h, w, max(0, h - title_h))
        # All the content lives shifted by the scroll; the title bar does not.
        sy = y - self.scroll
        # The stats/GPU rects exist only on the main page (the layout skips
        # them elsewhere) - keep the attributes defined so the drawers and
        # any hit-testing never see a stale rect from a previous page.
        self._stats_rect = (self._stats_rel.move(x, sy)
                            if self.page == "main" else pygame.Rect(0, 0, 0, 0))
        self._stats_line1 = (self._stats_line1_rel.move(x, sy)
                             if self.page == "main" else pygame.Rect(0, 0, 0, 0))
        self._gpu_rect = (self._gpu_rel.move(x, sy)
                          if self.page == "main" else pygame.Rect(0, 0, 0, 0))
        self._hint_rect = self._hint_rel.move(x, sy)
        self._rule_rect = self._rule_rel.move(x, sy)
        self._section_rects = [(t, r.move(x, sy)) for t, r in self._sections]
        # The segment groups travel with the panel too. Without this their
        # frames stayed at the content origin while their cells moved with the
        # items - the group's border and dividers drawn a panel-width to the
        # LEFT of the cells they belong to (user: "the FG strip went left").
        self._segment_rects = [(r.move(x, sy), splits)
                               for r, splits in self._segment_groups]
        if self._max_scroll > 0:
            bar_w = max(2, self._u(3))
            view_h = self._viewport.h
            track = pygame.Rect(x + w - self._u(7) - bar_w,
                                y + title_h + self._u(4),
                                bar_w, max(1, view_h - self._u(8)))
            thumb_h = max(self._u(26), int(track.h * view_h / content_h))
            travel = track.h - thumb_h
            ty = track.y + int(travel * (self.scroll / self._max_scroll))
            self._scroll_track = track
            self._scroll_thumb = pygame.Rect(track.x, ty, bar_w, thumb_h)
        else:
            self._scroll_track = pygame.Rect(0, 0, 0, 0)
            self._scroll_thumb = pygame.Rect(0, 0, 0, 0)
        for it in items:
            # The header icons are pinned to the panel, not to the content:
            # they sit above the scroll area and used to travel with it under
            # the title bar.
            it.rect = it.rect.move(x, y if it.kind == "icon" else sy)
            # The control's own hit zone travels with the row: without this
            # a scrolled control kept its unscrolled zone and clicks landed
            # on the wrong row (or nowhere). extra["hit"] is built in
            # content coordinates, like it.rect was before this move.
            zone = it.extra.get("hit")
            if zone is not None:
                it.extra["hit"] = zone.move(x, y if it.kind == "icon" else sy)
            if it.kind == "choice":
                # The select field is computed here rather than at draw time:
                # the layout of an expanded list is built before the first
                # draw. The strip is the CONTROL, not the whole row: a row
                # with a hint is taller, and the strip must stay the height
                # of the field or the value text and the list would centre
                # on the hint below it.
                it.extra["strip"] = pygame.Rect(
                    it.rect.x, it.rect.y + label_h, it.rect.w, self._u(CTRL_H))
        self.items = items

        # The entries of the expanded list. They lie on top of the rows below,
        # so they are added last and checked first on a mouse hit. The list
        # opens DOWN when the space below the strip fits it, otherwise UP
        # (over the panel content) - 12 languages would otherwise run off
        # the screen, and the panel cannot be stretched down (user: the
        # language list has no scroll).
        self.options: list[Item] = []
        self._opt_max_scroll = 0
        if self.open_choice:
            src = next((i for i in items if i.key == self.open_choice), None)
            if src is not None:
                strip = src.extra.get("strip")
                if strip is not None:
                    oh = self._u(CTRL_H) + self._u(6)
                    labels = src.extra.get("labels") or src.payload or []
                    total = len(src.payload or [])
                    # The list is bounded by the PANEL, not the screen: the
                    # panel is the window, and anything past its edge is
                    # clipped by it (user: the language list is cut off and
                    # the panel cannot be stretched down). Prefer opening
                    # down; flip up when the space below the strip cannot
                    # hold at least two rows and the space above can.
                    # The room above is measured to the VIEWPORT, not to the
                    # panel: the panel's first band is the title bar, which is
                    # the drag handle and carries no content. Rows laid out
                    # into it were drawn over the title text, could not be
                    # clicked (the title bar takes the press, or hit() rejects
                    # them) and could not be reached from the keyboard either -
                    # visible but unreachable (audit H4).
                    # BOTH edges are measured to the viewport, not to the
                    # panel. The room above was fixed that way in audit H4 and
                    # the room below was left as it was - to panel_rect.bottom,
                    # which counts the footer band: the rule and the Back/Quit
                    # button live there, outside the scroll area. A list that
                    # took that room drew its last rows over the footer, where
                    # hit() rejects them, so they were visible and dead - the
                    # same fault H4 named, at the other end.
                    down_room = self._viewport.bottom - strip.bottom - self._u(8)
                    up_room = strip.top - self._viewport.top - self._u(8)
                    open_up = (down_room < 2 * oh and up_room > down_room)
                    room = up_room if open_up else down_room
                    max_rows = max(1, room // oh)
                    # The viewport is the hard cap, whichever way the list
                    # opens: the room on one side can exceed the scroll area
                    # itself (a strip near the bottom has a lot of room above
                    # it), and a list longer than the viewport cannot be
                    # anywhere without hanging out of it.
                    fits_viewport = max(1, self._viewport.h // oh)
                    visible = min(total, max_rows, fits_viewport)
                    self._opt_max_scroll = max(0, total - visible)
                    self._opt_scroll = min(max(0, self._opt_scroll),
                                           self._opt_max_scroll)
                    self._opt_index = min(max(0, self._opt_index), total - 1)
                    base_y = (strip.top - self._u(4) - visible * oh
                              if open_up else strip.bottom + self._u(4))
                    # And clamped, because the arithmetic above assumes the
                    # strip itself is inside the viewport - it is not, while
                    # the page is scrolled far enough for the strip to sit
                    # under the header or past the footer.
                    base_y = max(self._viewport.top,
                                 min(base_y,
                                     self._viewport.bottom - visible * oh))
                    # The list's own scrollbar: a thin track on the right of
                    # the list, thumb proportional to the visible share.
                    self._opt_track = pygame.Rect(
                        strip.right - self._u(7) - max(2, self._u(3)),
                        base_y, max(2, self._u(3)), visible * oh)
                    if self._opt_max_scroll > 0:
                        thumb_h = max(self._u(12),
                                      int(self._opt_track.h * visible / total))
                        travel = self._opt_track.h - thumb_h
                        ty = self._opt_track.y + int(
                            travel * (self._opt_scroll / self._opt_max_scroll))
                        self._opt_thumb = pygame.Rect(
                            self._opt_track.x, ty, self._opt_track.w, thumb_h)
                    else:
                        self._opt_thumb = pygame.Rect(0, 0, 0, 0)
                    for idx in range(self._opt_scroll,
                                     min(total, self._opt_scroll + visible)):
                        opt = src.payload[idx]
                        self.options.append(Item(
                            "option", src.key,
                            pygame.Rect(strip.x, base_y
                                        + (idx - self._opt_scroll) * oh,
                                        strip.w, oh),
                            payload=opt,
                            extra={"label": str(labels[idx] if idx < len(labels)
                                                else opt),
                                   "selected": str(opt) == str(src.extra.get("current")),
                                   "highlighted": idx == self._opt_index}))
        if not getattr(self, "_measuring", False):
            self._reconcile_focus()

    # -- input -------------------------------------------------------------

    def handle_event(self, event) -> list[tuple]:
        """Handle a pygame event. Returns a list of actions for main.

        Actions: ("nr",), ("param", key, value), ("profile", name),
        ("lang", code), ("theme", name), ("button", key), ("drag", dx, dy).
        This module changes only its own display state - everything else is
        decided by main, which holds the source of truth.
        """
        if not self.visible:
            return []
        out: list[tuple] = []
        if event.type == getattr(pygame, "DROPFILE", -1):
            # A file dropped from Explorer. While the menu is open the layer
            # takes the mouse across the whole screen, so a drop anywhere
            # lands here (closed, the layer is click-through and the drop
            # goes to whatever is underneath, as it should). Whatever the page,
            # a dropped file means "convert this": the conversion page opens
            # to show it joining the queue. SDL sends one event per file.
            path = getattr(event, "file", None)
            if path:
                if self.page != "convert":
                    self.page = "convert"
                    self.scroll = 0
                    self.capturing = None
                    self.open_choice = None
                    out.append(("capture", None))
                out.append(("convert_files", [str(path)]))
            return out
        if self.capturing is not None and event.type == pygame.KEYDOWN:
            # While we wait for a key the keyboard belongs to the field. Esc
            # cancels, otherwise the menu would close instead of cancelling the
            # assignment.
            if event.key == pygame.K_ESCAPE:
                self.capturing = None
                out.append(("capture", None))
                return out
            text = key_text(event)
            if text is None:
                return out
            cmd, self.capturing = self.capturing, None
            out.append(("hotkey", cmd, text))
            out.append(("capture", None))
            return out
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_TAB:
                # Tab belongs to traversal unless a hotkey field is actively
                # recording it (handled above).  Leaving a combo closes only
                # that combo, then continues through the page cyclically.
                if self.open_choice:
                    self.open_choice = None
                    self._relayout()
                mods = getattr(event, "mod", 0)
                if not mods:
                    try:
                        mods = pygame.key.get_mods()
                    except Exception:
                        mods = 0
                self._cycle_focus(-1 if mods & pygame.KMOD_SHIFT else 1)
                return out
            if self.open_choice:
                src = next((item for item in self.items
                            if item.key == self.open_choice
                            and item.kind == "choice"), None)
                options = list(src.payload or []) if src is not None else []
                total = len(options)
                if event.key in (pygame.K_UP, pygame.K_LEFT):
                    if self._opt_index > 0:
                        self._opt_index -= 1
                elif event.key in (pygame.K_DOWN, pygame.K_RIGHT):
                    if self._opt_index < total - 1:
                        self._opt_index += 1
                elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER,
                                   pygame.K_SPACE):
                    if src is not None and 0 <= self._opt_index < total:
                        value = options[self._opt_index]
                        src.extra["current"] = str(value)
                        out.extend(self._pick(self.open_choice, value))
                    self.open_choice = None
                elif event.key == pygame.K_ESCAPE:
                    self.open_choice = None
                if self.open_choice:
                    self._keep_option_visible()
                self._relayout()
                return out
            if event.key == pygame.K_ESCAPE:
                # Preserve the existing hierarchy: capture -> open list ->
                # panel.  Settings/windows remain ordinary panel pages here.
                out.append(("button", "close"))
                return out
            return self._handle_focused_key(event)
        if event.type == pygame.MOUSEMOTION:
            self._mouse = event.pos
            # The windows page: hovering a row highlights the real window's
            # outline.  HWND is metadata on the row, never part of its label.
            self.hover_window = None
            if self.page == "windows":
                for it in self.items:
                    if it.kind == "option" and it.rect.collidepoint(event.pos):
                        self.hover_window = it.extra.get("hwnd")
                        break
            if self._grip.collidepoint(event.pos):
                self.hover = "grip"
            elif self._edge.collidepoint(event.pos):
                self.hover = "edge"
            elif self._title_bar.collidepoint(event.pos):
                self.hover = "title"
                for it in self.items:
                    if it.kind == "icon" and it.rect.collidepoint(event.pos):
                        self.hover = f"icon:{it.key}"
                        break
            else:
                self.hover = None
                for it in self.items:
                    if it.extra.get("hit", True) is None:
                        # The row declares no zone of its own - its cells
                        # carry them (the segments_only FG row). Falling
                        # back to `it.rect` here let the row swallow the
                        # hover for its own cells: the toggle is built
                        # before them and its rect spans the panel, so the
                        # cursor over x3 matched the toggle first and broke
                        # out of the loop. No cell ever highlighted, and the
                        # row itself draws no ring - the whole row gave no
                        # feedback at all. The click path and the focus ring
                        # already skip this case; this loop did not.
                        continue
                    # "info" is in the list for one row: the captured
                    # window, which opens the picker.
                    # The hover follows the same CONTROL zone the click uses
                    # (user rule 16.09): highlighting the whole row while
                    # only the switch reacts promises a click target that is
                    # not there.
                    if (it.kind in ("action", "hotkey", "button",
                                    "toggle", "slider", "choice")
                        or (it.kind == "info"
                            and it.key == "source_now")) and \
                            (it.extra.get("hit") or it.rect).collidepoint(event.pos):
                        self.hover = f"{it.kind}:{it.key}"
                        break
            # The windows page rows: the row itself is highlighted too, like
            # the buttons (the outline on the screen is easy to miss).
            if self.page == "windows" and self.hover_window is not None:
                for it in self.items:
                    if it.kind == "option" and it.rect.collidepoint(event.pos):
                        self.hover = f"woption:{it.payload}"
                        break
            # The expanded list rows: hovering a row highlights it (the rows
            # are a pop-up layer above the content, so they win over the items
            # underneath).
            if self.open_choice:
                for i, opt in enumerate(self.options):
                    if opt.rect.collidepoint(event.pos):
                        self.hover = f"option:{i}"
                        break
        if event.type == pygame.MOUSEWHEEL:
            # Scroll only while the cursor is over the panel: otherwise the
            # wheel inside the game would end up scrolling the menu. The
            # cached position is refreshed on MOUSEMOTION; when the menu was
            # just opened by a hotkey the cache may still be (0,0) - fall
            # back to the real cursor position once.
            if self._mouse == (0, 0):
                try:
                    self._mouse = pygame.mouse.get_pos()
                except Exception:
                    pass
            # The expanded list scrolls with the wheel whenever it is open:
            # the user opened it, so the wheel belongs to the list, not to
            # the page (12 languages do not fit - user: no scroll in the
            # language list).
            if self.open_choice and self._opt_max_scroll > 0:
                self._opt_scroll = min(
                    max(0, self._opt_scroll - event.y), self._opt_max_scroll)
                return out
            if self._max_scroll > 0 and self.panel_rect.collidepoint(self._mouse):
                self.scroll = min(max(self.scroll - event.y * self._u(48), 0),
                                  self._max_scroll)
            return out
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            # The icons sit in the header, and the header is the drag handle,
            # which returns immediately. So the icons are checked first.
            for it in self.items:
                if it.kind == "icon" and it.rect.collidepoint(event.pos):
                    self._set_focus(it, from_mouse=True)
                    out.extend(self._activate_item(it))
                    return out
            if self._grip.collidepoint(event.pos):
                self._resize_from = (event.pos, self.user_scale)
                return out
            if self._edge.collidepoint(event.pos):
                # Dragging the height. If the height has never been set, take
                # the current one - otherwise the very first pixel of the drag
                # would collapse the panel.
                base = self.user_height if self.user_height is not None \
                    else self.panel_rect.h
                self._resize_h_from = (event.pos[1], int(base))
                return out
            if self._title_bar.collidepoint(event.pos):
                self._move_from = (event.pos, tuple(self.offset))
                self._capture_mouse(True)
                return out
            if self.mini_pick and self.page == "main":
                # Choosing, so the ROW is the target and not its control: a
                # click that both kept a row and moved its slider would be
                # one nobody could undo. This cannot go through `hit` - that
                # one answers with the CONTROL's own rectangle by design
                # (rule 16.09), which while choosing is the one place a
                # click must not land, and which leaves the dot and the
                # label dead.
                picked = self._mini_hit(event.pos)
                if picked is not None:
                    self.mini_keep(picked.key)
                    self._set_focus(picked, from_mouse=True)
                    return out + [("mini_rows", sorted(self.mini_rows))]
            item = self.hit(event.pos)
            if item is None:
                self._drag_item = None
                self.open_choice = None
                return out
            self._set_focus(item if self._is_focusable(item) else None,
                            from_mouse=True)
            if item.kind == "segmented":
                cells = item.extra.get("cells") or []
                for idx, cr in enumerate(cells):
                    if cr.collidepoint(event.pos) and idx < len(item.payload or []):
                        old_page = self.page
                        out.extend(self._pick(item.key, item.payload[idx]))
                        if self.page != old_page:
                            self._set_focus(None)
                            self._relayout()
                        break
            elif item.kind == "slider":
                self._drag_item = item
                out.extend(self._slide(item, event.pos[0]))
            else:
                out.extend(self._activate_item(item))
        elif event.type == pygame.MOUSEMOTION and self._resize_h_from is not None:
            start_y, base = self._resize_h_from
            self.user_height = max(1, base + event.pos[1] - start_y)
        elif event.type == pygame.MOUSEMOTION and self._move_from is not None:
            start, base = self._move_from
            self.offset = [base[0] + event.pos[0] - start[0],
                           base[1] + event.pos[1] - start[1]]
        elif event.type == pygame.MOUSEMOTION and self._resize_from is not None:
            start, base = self._resize_from
            # Dragging right and down grows the panel. The step is chosen so
            # that a pass across the screen diagonal gives roughly double the
            # size.
            delta = ((event.pos[0] - start[0]) + (event.pos[1] - start[1])) / 900.0
            self.set_user_scale(base + delta)
        elif event.type == pygame.MOUSEMOTION and getattr(self, "_drag_item", None):
            if event.buttons and event.buttons[0]:
                out.extend(self._slide(self._drag_item, event.pos[0]))
            else:
                self._drag_item = None
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            self._drag_item = None
            self._move_from = None
            self._resize_from = None
            self._capture_mouse(False)
            if self._resize_h_from is not None:
                self._resize_h_from = None
                # The layout clamps the height by the content and the screen -
                # we take the clamped value so nothing raw leaks into the
                # config.
                self.user_height = (None if self._max_scroll == 0
                                    else self.panel_rect.h)
        return out

    def _icon_click(self, key: str) -> list[tuple]:
        """The header: help, settings, mini mode, collapse, and back."""
        if key == "help":
            return [("button", "github")]
        if key == "mini":
            self.mini = not self.mini
            # Leaving mini mode ends the choosing with it: the dots mean
            # nothing on a page that shows everything anyway.
            if not self.mini:
                self.mini_pick = False
            self.scroll = 0
            return [("mini", self.mini)]
        if key == "pick":
            self.mini_pick = not self.mini_pick
            self.scroll = 0
            return [("mini_pick", self.mini_pick)]
        if key == "gear":
            self.page = "settings"
            self.scroll = 0
            self.capturing = None
            return [("capture", None)]
        if key == "min":
            # The collapse button: hide the menu, exactly like the old
            # footer "Collapse" did - and let go of the keyboard first.
            # Collapsing while a hotkey field was waiting for a key left
            # `capturing` set and never sent ("capture", None), so the global
            # hotkeys stayed suspended: no Num2 to reopen the menu, no Num1,
            # nothing. The program looked dead (audit).
            out: list[tuple] = []
            if self.capturing is not None:
                self.capturing = None
                out.append(("capture", None))
            out.append(("button", "close"))
            return out
        if key == "close":
            self.page = "main"
            self.scroll = 0
            self.capturing = None
            return [("capture", None)]
        return []

    def _action_click(self, key: str) -> list[tuple]:
        """The footer. "Fullscreen" returns the capture to the whole screen
        (the window-mode exit), "Exit" unloads the program - which is why
        they are different actions with different captions rather than a
        single cross."""
        if key == "back":
            self.page = "main"
            self.scroll = 0
            self.capturing = None
            self.hover_window = None
            return [("capture", None)]
        if key == "refresh_windows":
            # main drops the cached list and the next payload rebuild takes a
            # fresh reading. The page stays open - that is the point of the
            # button: re-read without losing your place.
            return [("refresh_windows", None)]
        return [("button", key)]

    def _profile_modified(self) -> bool:
        """Do the live values still match the profile they came from?

        `param_defaults` is the chosen profile's own numbers, sent with every
        payload. Floats are compared with a tolerance a slider cannot land
        inside: the sliders step in hundredths, and a saved config comes back
        through float() twice.

        The model does NOT count (user rule 15.09): it is its own control,
        reverting the profile restores the four sliders and leaves the
        model where the user put it.
        """
        defaults = self.state.get("param_defaults") or {}
        if not defaults:
            return False
        params = self.state.get("params") or {}
        for key in PARAM_KEYS:
            if key not in defaults:
                continue
            if abs(float(params.get(key, 0.0))
                   - float(defaults[key])) > 0.005:
                return True
        return False

    def _button_click(self, key: str) -> list[tuple]:
        """A plain button. The windows button opens the window list page,
        the fullscreen button returns the capture to the whole screen (the
        window-mode exit, same as the Num5 hotkey)."""
        if key in ("windows", "convert"):
            # Both open a page of their own (the window list; the conversion
            # queue), and navigation is the menu's business, not main's.
            self.page = key
            self.scroll = 0
            self.capturing = None
            return [("capture", None)]
        if key == "fullscreen":
            return [("button", "window_mode")]
        if key == "revert_profile":
            return [("profile", str(self.state.get("profile", "")))]
        return [("button", key)]

    def _pick(self, key: str, value: Any) -> list[tuple]:
        """A list entry was picked."""
        if key == "window":
            # In the v1.13 payload this is the integer HWND carried by the
            # row.  A legacy identity token may still pass through unchanged,
            # but the visible title is never parsed here.
            return [("window", value)]
        value = str(value)
        if key == "profile":
            return [("profile", value)]
        if key == "lang":
            return [("lang", value)]
        if key == "theme":
            self.state["theme"] = value
            return [("theme", value)]
        if key == "settings_tab":
            # Navigation inside the page: nothing for main to do, and the
            # menu redraws itself on the next frame.
            if value in SETTINGS_TABS:
                self.settings_tab = value
                self.scroll = 0
            return []
        if key == "style":
            # Optimistic, like the theme: the control shows the new choice
            # at once and main applies it. Without this the segment would
            # snap back to the old cell until the next payload arrives.
            self.state["style"] = int(value)
            return [("style", value)]
        if key == "monitor":
            return [("monitor", value)]
        if key == "gpu":
            return [("gpu", value)]
        if key == "motion_backend":
            return [("motion_backend", value)]
        if key == "screenshot_mode":
            if value in ("ask", "auto"):
                self.state["screenshot_mode"] = value
                return [("screenshot_mode", value)]
            return []
        if key == "screenshot_format":
            if value in ("png", "jpg"):
                self.state["screenshot_format"] = value
                return [("screenshot_format", value)]
            return []
        if key == "nr_passes":
            try:
                step = int(value)
            except (TypeError, ValueError):
                return []
            if not 1 <= step <= 4:
                return []
            self.state["nr_passes"] = step
            return [("nr_passes", step)]
        if key == "pass_style":
            # The second set's model. Held in the same dict as its four
            # numbers, and sent as part of that set rather than as the main
            # style - the whole point is that the two can differ.
            try:
                picked = int(value)
            except (TypeError, ValueError):
                return []
            if picked not in (0, 1, 2):
                return []
            per_pass = dict(self.state.get("nr_pass_params") or {})
            if not per_pass:
                return []
            per_pass["style"] = picked
            self.state["nr_pass_params"] = per_pass
            return [("pass_param", "style", picked)]
        if key == "fps_overlay":
            if value in ("off", "tl", "tr", "bl", "br"):
                self.state["fps_overlay"] = value
                return [("fps_overlay", value)]
            return []
        if key in _CONVERT_SEGMENTS:
            # The conversion page's output choices. Optimistic, like the
            # screenshot format: the cell answers the click at once.
            if value in _CONVERT_SEGMENTS[key]:
                self.state[key] = value
                return [(key, value)]
            return []
        if key == "menu_scale":
            # Applied at once so the click is answered by the thing the click
            # is about; main persists it and marks the automatic fit as spent.
            try:
                step = float(value)
            except (TypeError, ValueError):
                return []
            self.set_user_scale(step)
            return [("menu_scale", step)]
        if key == "frame_multiplier":
            # Optimistic like style: the segment highlights at once, main
            # applies the new multiplier to the worker. Turning Frame Generation
            # ON when a step is picked with it off is main's business
            # (commands.apply_menu_action) - the menu only reports the click, so
            # there is exactly one place that decides what a click means.
            self.state["frame_multiplier"] = int(value)
            return [("frame_multiplier", int(value))]
        if key == "frame_limit_mode":
            if value in ("30", "60", "custom", "unlimited"):
                self.state["frame_limit_mode"] = value
                return [("frame_limit_mode", value)]
            return []
        if key == "source":
            # The same two commands the Actions buttons sent: back to the
            # whole screen, or the window list page.
            if value == "window":
                self.page = "windows"
                self.scroll = 0
                self.capturing = None
                return [("capture", None)]
            return ([("button", "window_mode")]
                    if self.state.get("window_mode") else [])
        return []

    def _slide(self, item: Item, mouse_x: int) -> list[tuple]:
        """The slider value from the mouse position, rounded to a 0.05 step."""
        track = item.extra.get("track")
        if track is None or track.w <= 0:
            return []
        frac = min(1.0, max(0.0, (mouse_x - track.x) / track.w))
        value = item.lo + frac * (item.hi - item.lo)
        return self._set_slider_value(item, value)

    def _step_slider(self, item: Item, direction: int) -> list[tuple]:
        """Move a focused slider by one meaningful keyboard step."""
        step = (1 if item.key == "frame_multiplier"
                else 5 if item.key == "frame_limit_custom"
                else 0.05)
        return self._set_slider_value(item, item.value + direction * step)

    def _set_slider_value(self, item: Item, value: float) -> list[tuple]:
        value = min(item.hi, max(item.lo, value))
        if item.key == "frame_multiplier":
            value = min(4, max(2, int(value + 0.5)))
            if value == item.value:
                return []
            item.value = value
            self.state["frame_multiplier"] = value
            return [("frame_multiplier", value)]
        if item.key == "frame_limit_custom":
            value = min(240, max(15, int(value + 0.5)))
            if value == item.value:
                return []
            item.value = value
            self.state["frame_limit_custom"] = value
            item.extra["value_text"] = f"{value} fps"
            return [("frame_limit_custom", value)]
        value = round(round(value / 0.05) * 0.05, 2)
        if abs(value - item.value) < 1e-9:
            return []
        item.value = value
        if item.key == "split":
            self.state["split"] = value
            return [("split", value)]
        if item.key == "nr_res":
            # The slider is only on screen while Boost is on, so every
            # position means a work resolution now - there is no "off" step
            # at the top any more.
            self.state["work_scale"] = value
            # The label shows the work resolution; recompute it here so it
            # follows the knob while dragging (main confirms on the way
            # back). The formula mirrors _work_size in main.py.
            try:
                sw, sh = (int(x) for x in
                          str(self.state.get("screen_size", "0x0")).split("x"))
                w = max(64, int(round(sw * value / 2) * 2))
                h = max(64, int(round(sh * value / 2) * 2))
                if w > 2560 or h > 1440:
                    k = min(2560 / w, 1440 / h)
                    w = max(64, int(round(w * k / 2) * 2))
                    h = max(64, int(round(h * k / 2) * 2))
                w, h = safe_processing_size(sw, sh, w, h)
                self.state["work_size"] = f"{w}x{h}"
            except Exception:
                pass
            return [("nr_res", value)]
        if item.key.startswith("pass_"):
            # The second set's own slider. Held per key, and `pass_style` is a
            # segment rather than a slider so it never reaches here - the two
            # namespaces only collide on the "pass_" prefix, which is why the
            # prefix is enough to tell them apart.
            per_pass = dict(self.state.get("nr_pass_params") or {})
            per_pass[item.key[len("pass_"):]] = value
            self.state["nr_pass_params"] = per_pass
            return [("pass_param", item.key[len("pass_"):], value)]
        params = dict(self.state.get("params") or {})
        params[item.key] = value
        self.state["params"] = params
        return [("param", item.key, value)]

    @property
    def desired_cursor(self):
        """The cursor for the current zone. display sets it - it owns pygame."""
        if self.hover == "grip" or self._resize_from is not None:
            return pygame.SYSTEM_CURSOR_SIZENWSE
        if self.hover == "edge" or self._resize_h_from is not None:
            return pygame.SYSTEM_CURSOR_SIZENS
        if isinstance(self.hover, str) and self.hover.startswith(
                ("icon:", "action:", "hotkey:", "button:")):
            return pygame.SYSTEM_CURSOR_HAND
        if self.hover == "title" or self._move_from is not None:
            return pygame.SYSTEM_CURSOR_SIZEALL
        return pygame.SYSTEM_CURSOR_ARROW

    def hit(self, pos: tuple[int, int]) -> Item | None:
        # Scrolled content is drawn clipped to _viewport, so hits outside it
        # must not count either: a row that has travelled under the title bar
        # is invisible, yet its rectangle still exists.
        for it in self.items:
            if it.kind == "icon" and it.rect.collidepoint(pos):
                return it
        if self._viewport.h > 0 and not self._viewport.collidepoint(pos):
            return None
        for opt in getattr(self, "options", []):
            if opt.rect.collidepoint(pos):
                return opt
        # The SMALLEST hit wins: inline widgets live inside a row's rect
        # (the multiplier buttons share the FG toggle's row), and the row
        # must not swallow their clicks (user 14.09: clicking "×3" flipped
        # the whole FG switch instead).
        best: "Item | None" = None
        for item in self.items:
            # Only the CONTROL reacts, not the whole row (user rule 16.09:
            # "only explicit switches and choices should be clickable").
            # A toggle/slider/choice row spans the panel width, and the
            # label - or the empty space beside the control - used to
            # activate it. extra["hit"] is the control's own rectangle
            # (the switch pill, the slider track, the select field, the
            # hotkey field), computed by layout.
            zone = item.extra.get("hit") or item.rect
            if item.extra.get("hit", True) is None:
                continue          # the row declares no zone (its cells own it)
            if not zone.collidepoint(pos):
                continue
            # A hint under a toggle is a caption, not a hit target:
            # clicking it must not flip the switch (the Spout2 toggle
            # restarts the worker - a stray click on the explanation
            # would freeze the screen for seconds).
            if item.kind == "toggle" and item.extra.get("hint") and \
                    pos[1] > item.rect.y + self._u(CTRL_H):
                continue
            if item.kind == "choice" and item.extra.get("hint"):
                strip = item.extra.get("strip")
                if strip is not None and pos[1] > strip.bottom:
                    continue
            if best is None or zone.w * zone.h < best.extra.get("hit", best.rect).w * \
                    best.extra.get("hit", best.rect).h:
                best = item
        return best

    def inside(self, pos: tuple[int, int]) -> bool:
        return self.panel_rect.collidepoint(pos)

    # -- drawing -----------------------------------------------------------

    def draw(self, surface: pygame.Surface) -> None:
        if not self.visible:
            return
        self.layout(surface.get_width(), surface.get_height())
        s = STRINGS.get(self.lang, STRINGS["en"])
        pad = self._u(PAD)
        r = self.panel_rect

        pygame.draw.rect(surface, _rgb(self.c["bg"]), r, border_radius=self._u(RADIUS))
        pygame.draw.rect(surface, _rgb(self.c["border"]), r, self._u(1),
                         border_radius=self._u(RADIUS))

        if self.hover == "title" or self._move_from is not None:
            # The title bar is the drag handle. We highlight it with a strip
            # and an accent edge: there is otherwise no way to guess it exists.
            tb = self._title_bar
            pygame.draw.rect(surface, _rgb(self.c["surface"]), tb,
                             border_top_left_radius=self._u(RADIUS),
                             border_top_right_radius=self._u(RADIUS))
            pygame.draw.line(surface, _rgb(self.c["accent"]),
                             (tb.x + self._u(RADIUS), tb.bottom - 1),
                             (tb.right - self._u(RADIUS), tb.bottom - 1),
                             max(2, self._u(2)))
        head = (s.get("settings_title", "Settings") if self.page == "settings"
                else s.get("windows_title", "Select window")
                if self.page == "windows"
                else s.get("convert_title", "File conversion")
                if self.page == "convert" else s["title"])
        title = self._title_font.render(head, True, _rgb(self.c["text"]))
        surface.blit(title, (r.x + pad, r.y + self._u(16)))
        # The version right after the title: the top right corner is taken by
        # the icons. The channel label lives in the settings page (user rule
        # 2026-09-08).
        ver = self.state.get("version") or ""
        if ver:
            ver_text = self._mono_small.render(f"v{ver}", True,
                                               _rgb(self.c["muted"]))
            surface.blit(ver_text, (r.x + pad + title.get_width() + self._u(10),
                                    r.y + self._u(22)))

        # The content is drawn clipped to the scroll area, otherwise scrolled
        # rows would spill outside the panel.
        prev_clip = surface.get_clip()
        surface.set_clip(self._viewport)
        # The live indicators are main-page only (user rule 10.09): the
        # settings/windows pages skip them entirely - the rects are not even
        # computed there, so the drawers must not run.
        if self.page == "main":
            self._draw_stats(surface, s)
        self._draw_sections(surface)
        self._draw_rules(surface, s)
        # The resize corner: three short strokes, as resize handles usually go
        g = self._grip
        active = self.hover == "grip" or self._resize_from is not None
        if active:
            # A backing under the corner: three strokes on their own get lost
            # against the panel and the zone is invisible until you poke it.
            # NOT SRCALPHA: a translucent backing blends with the magenta
            # background (CHROMA_KEY) -> colour != key -> the colour key does
            # not cut it out -> a pink slab. An opaque backing in the panel
            # colour is cut out along with the background, and the accent
            # border stays.
            pad = pygame.Surface(g.size)
            pygame.draw.rect(pad, _rgb(self.c["bg"]), pad.get_rect(),
                             border_bottom_right_radius=self._u(RADIUS))
            pygame.draw.rect(pad, _rgb(self.c["accent"]), pad.get_rect(),
                             self._u(1),
                             border_bottom_right_radius=self._u(RADIUS))
            surface.blit(pad, g.topleft)
        color = self.c["accent"] if active else self.c["muted"]
        width = max(2, self._u(3 if active else 2))
        step = max(3, self._u(6))
        for i in range(1, 4):
            off = i * step
            pygame.draw.line(surface, _rgb(color),
                             (g.right - off, g.bottom - self._u(3)),
                             (g.right - self._u(3), g.bottom - off), width)
        for item in self.items:
            {"toggle": self._draw_toggle, "slider": self._draw_slider,
             "choice": self._draw_choice, "button": self._draw_button,
             "segmented": self._draw_segmented,
             "tab": self._draw_tab,
             "info": self._draw_info,
             "action": self._draw_action,
             "hotkey": self._draw_hotkey,
             # The conversion page: a queued file, and a line of explanation.
             "job": self._draw_job,
             "note": self._draw_note,
             # The header icons sit above the scroll area - we draw them after
             # the clip is lifted, otherwise they get cut off.
             "icon": lambda *_: None,
             # The windows page rows are drawn by _draw_options (they are
             # option items, like the entries of an expanded list).
             "option": lambda *_: None}[item.kind](surface, item, s)
        self._draw_segment_groups(surface)
        if self.mini_pick and self.page == "main":
            self._draw_mini_marks(surface)
        if self.open_choice and self.options:
            # The expanded list fades at its edges: a soft gradient around
            # the rows (top/bottom/left/right) instead of dimming the whole
            # panel - the list reads as a floating layer while the content
            # underneath stays fully readable.
            first = self.options[0].rect
            last = self.options[-1].rect
            list_rect = pygame.Rect(first.x, first.y, first.w,
                                     last.bottom - first.y)
            fade = self._u(18)
            shade = pygame.Surface(
                (list_rect.w + 2 * fade, list_rect.h + 2 * fade),
                pygame.SRCALPHA)
            for off in range(fade):
                a = int(95 * (1 - off / fade))
                # top edge
                pygame.draw.line(shade, (0, 0, 0, a),
                                 (0, fade - off),
                                 (shade.get_width(), fade - off))
                # bottom edge
                pygame.draw.line(shade, (0, 0, 0, a),
                                 (0, shade.get_height() - fade + off),
                                 (shade.get_width(),
                                  shade.get_height() - fade + off))
                # left edge
                pygame.draw.line(shade, (0, 0, 0, a),
                                 (fade - off, 0),
                                 (fade - off, shade.get_height()))
                # right edge
                pygame.draw.line(shade, (0, 0, 0, a),
                                 (shade.get_width() - fade + off, 0),
                                 (shade.get_width() - fade + off,
                                  shade.get_height()))
            surface.blit(shade, (list_rect.x - fade, list_rect.y - fade))
        self._draw_options(surface)
        focused = self.focused_item
        if focused is not None and focused.kind != "icon":
            self._draw_focus_ring(surface, focused)
        surface.set_clip(prev_clip)
        for item in self.items:
            if item.kind == "icon":
                self._draw_icon(surface, item, s)
        if focused is not None and focused.kind == "icon":
            self._draw_focus_ring(surface, focused)
        self._draw_scrollbar(surface)

    def _mini_hit(self, pos) -> "Item | None":
        """The row under the pointer while choosing what mini mode keeps.

        The whole width of the panel, so the dot in the left margin, the
        label and the control are one target: the row is what is kept or
        dropped, and three outcomes for one row would be three ways to be
        surprised.
        """
        if self._viewport.h > 0 and not self._viewport.collidepoint(pos):
            return None
        for item in self.items:
            if item.kind not in MINI_PICKABLE:
                continue
            row = pygame.Rect(self.panel_rect.x, item.rect.y,
                              self.panel_rect.w, item.rect.h)
            if row.collidepoint(pos):
                return item
        return None

    def _draw_mini_marks(self, surface) -> None:
        """While choosing: a dot per row, and the dropped ones stand back.

        The dot sits in the panel's left margin rather than in the row, so
        it cannot land on a switch or a slider track - every row kind has
        something at both ends, and the margin belongs to no control.
        """
        radius = max(3, self._u(4))
        x = self.panel_rect.x + self._u(11)
        for item in self.items:
            if item.kind not in MINI_PICKABLE:
                continue
            kept = item.key in self.mini_rows
            if not kept:
                veil = pygame.Surface(item.rect.size, pygame.SRCALPHA)
                veil.fill((*_rgb(self.c["bg"]), 150))
                surface.blit(veil, item.rect.topleft)
            centre = (x, item.rect.y + min(self._u(17), item.rect.h // 2))
            pygame.draw.circle(surface, _rgb(self.c["accent"]), centre,
                               radius, 0 if kept else max(1, self._u(1)))

    def _draw_focus_ring(self, surface, item: Item) -> None:
        """The keyboard focus ring - never drawn for a mouse click.

        With the mouse the user already knows what they pressed, and on a
        full-width row the ring wrapped the label and the control together
        (user rule 16.09: "an outline appears when I pick buttons or drag a
        slider - that is not needed"). Keyboard focus still shows it: there
        the ring is the only thing telling the user where they are.
        """
        if self.focus_from_mouse:
            return
        # The ring marks the CONTROL, not its row: a slider's row carries the
        # label and the value, and wrapping those in the ring is what the
        # user saw as "a big outline over the labels and the bar" (16.09).
        # extra["hit"] is exactly the rect a click tests, so the ring and the
        # click target agree.
        rect = item.extra.get("hit") or item.rect
        if item.extra.get("hit", True) is None:
            return                # the row declares no zone (its cells own it)
        if item.kind == "choice":
            rect = item.extra.get("strip") or rect
        elif item.kind == "hotkey":
            rect = item.extra.get("field") or rect
        gap = self._u(2)
        ring = rect.inflate(gap * 2, gap * 2)
        pygame.draw.rect(surface, _rgb(self.c["focus"]), ring,
                         max(2, self._u(2)),
                         border_radius=self._u(RADIUS // 2) + gap)

    def _draw_scrollbar(self, surface) -> None:
        """A thin strip at the right edge. It appears only when there is
        somewhere to scroll - a permanent bar would be noise."""
        if self._max_scroll <= 0:
            return
        radius = self._scroll_thumb.w // 2
        pygame.draw.rect(surface, _rgb(self.c["surface"]), self._scroll_track,
                         border_radius=radius)
        active = (self.hover in ("edge", "scroll")
                  or self._resize_h_from is not None)
        color = self.c["accent"] if active else self.c["muted"]
        pygame.draw.rect(surface, _rgb(color), self._scroll_thumb,
                         border_radius=radius)

    def status_text(self, s: dict) -> tuple[str, bool]:
        """What the status line says, and whether it is saying "broken".

        A failed verdict outranks everything except the user's own switch.
        The alert that announces it is up for a few seconds and gone; the
        state it announces lasts until the worker is rebuilt, and someone who
        looks at the menu a minute later deserves the same answer. Three
        black-screen reports came from people who never opened the log.

        Frame Generation outranks the NR switch: since v1.16.0 the presenter
        runs on the bypass path too, so "NR off" no longer means "nothing is
        happening to the picture". The old order said "not processing" while
        the screen was actually being interpolated - a v1.16.0 reporter read
        exactly that and concluded Frame Generation was dead (#107).
        """
        paused = not bool(self.state.get("nr"))
        failed = self.state.get("gpu_ok") is False and not paused
        # Frame Generation on its own is work the user asked for, and the
        # worker reports the rate it really reaches - the reading is the
        # proof that the presenter is alive, not the switch position. It names
        # the line only while NR is OFF: with NR on, the neural pass is the
        # larger part of the picture and keeps the plain "processing".
        framegen = bool(self.state.get("frame_generation")) and paused
        if framegen and self.state.get("display_fps") is not None:
            return str(s.get("status_fg_only", "frame generation")), False
        if paused:
            if framegen:
                # FG is on but has not reported a rate yet: starting up, or
                # refused. Say what is true instead of "not processing".
                return str(s.get("status_fg_waiting", "frame generation starting")), False
            return str(s.get("status_off", "not processing")), False
        if failed:
            return str(s.get("gpu_no_nr", "no neural pass")), True
        # NR is on and the worker is not evaluating: the picture is raw. The
        # only symptom used to be a counter running too fast, which reads as a
        # broken counter rather than as "the pass stopped" - so the state cell
        # says it outright, in the failure tone.
        if bool(self.state.get("nr_not_evaluating")):
            return str(s.get("nr_not_running",
                             "NR is on but nothing is processed - restart "
                             "the worker or pick another source")), True
        return str(s.get("status_on", "processing")), False

    def _draw_stats(self, surface, s: dict) -> None:
        """The status line: is it working, on what, and how fast.

        One line. The left half is the state and the card - the two values that
        cannot be read anywhere else. The right half is the readings, anchored
        to the RIGHT edge: NR rate then FG rate, with FG at the very edge.

        The reading order matters and was the bug. The old line laid the values
        out from the right in reverse order and skipped whatever ran out of
        room, which made the FIRST casualty NR - the rate people watch - while
        the resolution stayed. At 4K with a real card name the drop was
        measured: NR and FG gone, "SKIP 0  3840x2160" still on screen.

        The readings are placed FIRST and the card name gets what is left, so
        the name can never push a reading off the bar. Resolution, the
        skipped-frame count and the frame counter were removed from the line by
        decision: the first is already printed in the source section above, and
        the other two are numbers nobody acts on.
        """
        rect = self._stats_rect
        if rect.w <= 0:
            return
        pygame.draw.rect(surface, _rgb(self.c["surface"]), rect,
                         border_radius=self._u(RADIUS // 2))
        st = self.stats or {}
        pad = self._u(STAT_PAD)
        ok = self.state.get("gpu_ok")
        paused = not bool(self.state.get("nr"))
        dot = (self.c["muted"] if paused or ok is None
               else self.c["ok"] if ok else self.c["danger"])
        r = max(3, self._u(4))

        line = getattr(self, "_stats_line1", None) or rect
        cyr = line.centery
        pygame.draw.circle(surface, _rgb(dot), (line.x + pad + r, cyr), r)

        text, failed = self.status_text(s)
        label = self._small_font.render(str(text), True, _rgb(self.c["text"]))
        lx = line.x + pad + r * 2 + self._u(9)
        surface.blit(label, (lx, cyr - label.get_height() // 2))

        # ---- the readings, anchored to the right edge ---------------------
        # NR is the rate of real neural evaluations - idle acknowledgements do
        # not inflate it. FG is the worker presenter's reported output rate,
        # not an inferred display refresh. The frame counter is not shown at
        # all: it was asked for once, then dropped again (19.09).
        readings = status_readings(self.state, st, s)

        gap = self._u(14)
        right = line.right - pad
        budget = right - (line.x + pad)

        def _fit(values: list[str]) -> list[tuple[object, int]] | None:
            """Render `values` if they fit the bar, else None."""
            imgs: list[tuple[object, int]] = []
            used = 0
            for value in values:
                img = self._mono_small.render(value, True, _rgb(self.c["muted"]))
                extra = img.get_width() + (gap if imgs else 0)
                if used + extra > budget:
                    return None
                imgs.append((img, img.get_width()))
                used += extra
            return imgs

        # Priority is explicit because dropping the wrong value was the bug.
        # NR is the rate people watch - it survives longest, then FG, then the
        # counter. In practice all three fit at every panel scale with the
        # longest card name, so this only guards the degenerate case
        # (verified from 0.4x to 2.0x).
        imgs = _fit(readings)
        if imgs is None:
            imgs = _fit(readings[:2])
        if imgs is None:
            imgs = _fit(readings[:1])
        if imgs is None:
            return

        # Drawn right to left, last value first, so the last reading - FG -
        # ends up at the right edge and the list still reads NR, FG from left
        # to right.
        x = float(right)
        for img, w in reversed(imgs):
            x -= w
            surface.blit(img, (int(round(x)),
                               cyr - img.get_height() // 2))
            x -= gap

        # ---- the card name, in whatever room is left ----------------------
        name = str(self.state.get("gpu_text") or "")
        if name and imgs:
            left_edge = int(round(x + gap))     # leftmost reading, less its gap
            name_x = lx + label.get_width() + self._u(14)
            room = max(0, left_edge - self._u(14) - name_x)
            if room > self._u(24):
                img = self._clip(self._small_font, name,
                                 _rgb(self.c["muted"]), room)
                surface.blit(img, (name_x, cyr - img.get_height() // 2))

    def _clip(self, font, text: str, color, max_w: int):
        """Render text clipped to max_w with an ellipsis.

        Long localized strings (French, German) and long window titles
        overflow their controls - the panel has no clipping surface, so the
        text bleeds over the neighbours (user: FR button label escapes the
        button, window names overflow the rows, the resolution label covers
        the value). Binary search the longest prefix that fits.
        """
        if max_w <= 8:
            return font.render("", True, color)
        img = font.render(text, True, color)
        if img.get_width() <= max_w:
            return img
        ell = "…"
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.render(text[:mid] + ell, True, color).get_width() <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return font.render(text[:lo] + ell, True, color)

    def _draw_toggle(self, surface, item: Item, s: dict) -> None:
        on = item.value > 0.5
        segments_only = bool(item.extra.get("segments_only"))
        size = self._u(20)
        # The mockup's switch: a 46x24 track with radius 4 and a SQUARE 20x20
        # knob, not a pill. A pill reads as a slider between two states; the
        # squared track reads as a switch, and it is what the direction draws.
        track_w = self._u(46)
        track_h = self._u(24)
        # The switch sits at the row's right end, the label on the left -
        # the reading order every settings panel uses (label, then the
        # control at the edge), and the knob never shifts position when a
        # label changes between "on"/"off" wording (user, 14.09).
        box = pygame.Rect(item.rect.right - track_w,
                          item.rect.centery - track_h // 2, track_w, track_h)
        # A hint grows the row; the switch and the label stay on the first
        # line - only the hint is pushed under them.
        hint = item.extra.get("hint")
        if hint:
            box.y = item.rect.y + (self._u(CTRL_H) - track_h) // 2
        radius = self._u(4)
        if not segments_only:
            # A row whose segment group carries every state has no switch of its
            # own: the group IS the control (the mockup's FG row).
            pygame.draw.rect(surface,
                             _rgb(self.c["accent"] if on else self.c["surface"]),
                             box, border_radius=radius)
            if not on:
                pygame.draw.rect(surface, _rgb(self.c["border"]), box,
                                 self._u(1), border_radius=radius)
            knob = self._u(20)
            pad = self._u(2)
            knob_x = (box.right - pad - knob) if on else (box.x + pad)
            pygame.draw.rect(
                surface,
                _rgb(self.c["bg"] if on else self.c["muted"]),
                pygame.Rect(knob_x, box.y + (track_h - knob) // 2, knob, knob),
                border_radius=radius)
        text = item.extra.get("label")
        if not text:
            text = s["nr_on"] if on else s["nr_off"]
        # The state square, when the SECTION carries them (PROCESSING): filled
        # while the switch is on, hollow while it is off. It stands BEFORE the
        # caption, so the caption moves right by exactly the square plus its gap
        # and its clip budget shrinks by the same - drawn at the label's own x it
        # would paint over the first letter.
        square_shift = self._u(7) + self._u(10) if item.extra.get("square") else 0
        if item.extra.get("square"):
            self._draw_state_square(
                surface, item.rect.x, item.rect.y + self._u(CTRL_H) // 2,
                filled=on, hollow=not on)
        room = item.rect.w - 2 * self._u(12) - square_shift
        if hint:
            room = item.rect.w - square_shift
        label = self._clip(self._font, text,
                           _rgb(self.c["text"] if on else self.c["muted"]),
                           room)
        surface.blit(label, (item.rect.x + square_shift,
                             item.rect.y + (self._u(CTRL_H) - label.get_height()) // 2))
        if hint:
            y = item.rect.y + self._u(CTRL_H) + self._u(8)
            for line in str(hint).split("\n"):
                img = self._clip(self._small_font, line, _rgb(self.c["muted"]),
                                 item.rect.w)
                surface.blit(img, (item.rect.x, y))
                y += self._small_font.get_height() + self._u(4)

    def _draw_slider(self, surface, item: Item, s: dict) -> None:
        label_h = item.extra.get("label_h", self._u(LABEL_H))
        value_text = item.extra.get("value_text") or f"{item.value:.2f}"
        # The value sits on the label line, right-aligned; a long localized
        # label (FR: "Résolution de traitement du réseau") would run under
        # it - clip the label to the space left of the value instead.
        # The value is a NUMBER CELL - the same one every other number on the
        # page uses (see `_draw_number_cell`). Measured through the shared helper
        # so the label can be budgeted against it before it is drawn.
        bare = bool(item.extra.get("bare"))
        cell_w, _cell_h = (0, 0) if bare else self._number_cell_size(value_text)
        shift = self._u(7) + self._u(10) if item.extra.get("square") else 0
        label_max = (item.rect.right - cell_w - self._u(12)
                     - item.rect.x - shift)
        if not bare:
            if item.extra.get("square"):
                self._draw_state_square(
                    surface, item.rect.x,
                    item.rect.y + self._font.get_height() // 2,
                    filled=bool(item.extra.get("state_filled")))
            label = self._clip(self._font, item.extra.get("label", item.key),
                               _rgb(self.c["text"]), label_max)
            surface.blit(label, (item.rect.x + shift, item.rect.y))
            self._draw_number_cell(
                surface, value_text, item.rect.right,
                item.rect.y + self._font.get_height() // 2)

        track_y = item.rect.y + label_h + self._u(10)
        track = pygame.Rect(item.rect.x, track_y, item.rect.w, self._u(SLIDER_H))
        pygame.draw.rect(surface, _rgb(self.c["surface"]), track,
                         border_radius=self._u(SLIDER_H // 2 or 1))
        span = max(1e-6, item.hi - item.lo)
        frac = min(1.0, max(0.0, (item.value - item.lo) / span))
        fill = pygame.Rect(track.x, track.y, int(track.w * frac), track.h)
        # The theme's own fill: muted on dark, the border tone on cream, where
        # an accent bar would read as "switched on". The accent stays reserved
        # for state - the squares, the active segment, the index numbers.
        pygame.draw.rect(surface, _rgb(self.c["slider_fill"]), fill,
                         border_radius=self._u(SLIDER_H // 2 or 1))
        # Where this value sits by default - the profile's own number, or
        # zero for a slider that runs both ways. Without it "how far have I
        # moved this" is a thing to remember rather than to see.
        mark = item.extra.get("mark")
        if mark is None and item.lo < 0.0 < item.hi:
            mark = 0.0
        if mark is not None and item.lo <= mark <= item.hi:
            mx = int(track.x + ((mark - item.lo) / span) * track.w)
            # TEXT, not muted: the track FILL is muted now (the mockup's
            # colour), and a muted mark vanished inside it - exactly where it
            # matters. Bright reads against both the fill and the empty track.
            pygame.draw.rect(
                surface, _rgb(self.c["text"]),
                pygame.Rect(mx - max(1, self._u(1)),
                            track.y - self._u(3),
                            max(2, self._u(2)),
                            track.h + self._u(6)),
                border_radius=max(1, self._u(1)))
        # The handle: a 10x16 rectangle in the TEXT colour. The mockup does not
        # use a circle, and not the accent - the accent is the state language on
        # this panel, and a state-coloured knob would claim the value is a state.
        # (One piece: the old code drew a circle and then carved it with a
        # bg-coloured one to fake a ring.)
        cx = int(track.x + frac * track.w)
        kh = self._u(16)
        kw = self._u(10)
        pygame.draw.rect(
            surface, _rgb(self.c["text"]),
            pygame.Rect(cx - kw // 2, track.centery - kh // 2, kw, kh),
            border_radius=self._u(2))
        # The ruler: five 1x4 ticks spread under the track (the mockup's own
        # count). They say "this is a scale" without spending two captions on
        # it - what the ends mean is already in the label and the value.
        # The ruler under the track. On an ordinary slider it says "this is a
        # scale" and nothing more. On a BARE one it is also the readout: every
        # tick the knob has passed is drawn in the text tone and the rest in the
        # border tone, so the position is legible with no caption and no value
        # cell at all. That is why a bare slider asks for a denser ruler - five
        # ticks resolve to a quarter of the range, which is not a reading.
        n = int(item.extra.get("ticks", 5) or 0)
        if n >= 2:
            ty = track.bottom + self._u(RULER_DROP)
            for i in range(n):
                tx = int(track.x + (track.w - self._u(1)) * (i / (n - 1)))
                passed = bare and tx <= cx
                pygame.draw.rect(
                    surface,
                    _rgb(self.c["text"] if passed else self.c["border"]),
                    pygame.Rect(tx, ty, max(1, self._u(1)), self._u(4)))
        item.extra["track"] = track

        hint = item.extra.get("hint")
        if hint:
            # Under the ruler, wrapped and clipped through the one shared
            # implementation - drawn at +6 it overprinted the ticks, and
            # rendered in one piece it ran off the panel in any language whose
            # sentence is longer than the English one.
            self._draw_hint_lines(
                surface, item, hint, item.rect.x,
                track.bottom + self._u(RULER_DROP) + self._u(4) + self._u(6),
                item.rect.w)

    def _draw_choice(self, surface, item: Item, s: dict) -> None:
        label_h = item.extra.get("label_h", self._u(LABEL_H))
        # Clipped like every other label (audit M1): "the captions are short"
        # is not a rule that survives a translation.
        # The square, when the SECTION asked for one (see the layout).
        shift = self._u(7) + self._u(10) if item.extra.get("square") else 0
        if item.extra.get("square"):
            self._draw_state_square(
                surface, item.rect.x,
                item.rect.y + self._font.get_height() // 2,
                filled=bool(item.extra.get("state_filled")))
        label = self._clip(self._font,
                           str(item.extra.get("label", item.key)),
                           _rgb(self.c["text"]), item.rect.w - shift)
        surface.blit(label, (item.rect.x + shift, item.rect.y))

        # The field is the CONTROL, not the rest of the row. A row with a
        # hint is taller, and taking "everything under the label" drew the
        # box over the hint - and, worse, wrote that rectangle back into
        # extra["strip"], which is the hit target: a click on the
        # explanation opened the drop-down (audit). The layout already
        # measured it; this only falls back to the same height.
        strip = item.extra.get("strip") or pygame.Rect(
            item.rect.x, item.rect.y + label_h, item.rect.w, self._u(CTRL_H))
        pygame.draw.rect(surface, _rgb(self.c["surface"]), strip,
                         border_radius=self._u(RADIUS // 2))
        pygame.draw.rect(surface, _rgb(self.c["border"]), strip, self._u(1),
                         border_radius=self._u(RADIUS // 2))
        cur_val = str(item.extra.get("current", ""))
        labels = item.extra.get("labels") or item.payload or []
        if cur_val in (item.payload or []):
            cur_val = str(labels[item.payload.index(cur_val)])
        # Clipped to the space the field actually leaves (audit M1). The value
        # used to be rendered at full width: the GPU picker's is
        # "<i>: <name>" plus " - <no_nr>" for an adapter that was already
        # refused, so at 1080p/ja it measured 566 px against 460 px of field -
        # it ran under the drop-down arrow and past the border. The arrow is
        # 16 px from the right edge and the text starts 12 px from the left.
        cur = self._clip(self._font, cur_val, _rgb(self.c["text"]),
                         strip.w - self._u(12) - self._u(32))
        surface.blit(cur, (strip.x + self._u(12),
                           strip.centery - cur.get_height() // 2))
        # The caret sits in its own zone, separated by a rule: that is what
        # makes a drop-down read as a control rather than as a field whose
        # value happens to end in a triangle.
        arrow_w = self._u(32)
        arrow_x = strip.right - arrow_w
        pygame.draw.line(surface, _rgb(self.c["border"]),
                         (arrow_x, strip.y + self._u(5)),
                         (arrow_x, strip.bottom - self._u(5)), 1)
        cx = arrow_x + arrow_w // 2
        cy = strip.centery
        size = self._u(5)
        up = self.open_choice == item.key
        pts = ([(cx - size, cy + size // 2), (cx + size, cy + size // 2), (cx, cy - size)]
               if up else
               [(cx - size, cy - size // 2), (cx + size, cy - size // 2), (cx, cy + size)])
        pygame.draw.polygon(surface, _rgb(self.c["accent"]), pts)
        item.extra["strip"] = strip
        hint = item.extra.get("hint")
        if hint:
            # Wrapped as well as clipped: clipping alone still loses the end of
            # a sentence (the Frame limit hint was cut at the panel edge, #109).
            self._draw_hint_lines(surface, item, hint, item.rect.x,
                                  strip.bottom + self._u(8), item.rect.w)

    def _draw_options(self, surface) -> None:
        """The entries of the expanded list - above the rest of the content.

        The windows page rows are option items too, but they live in
        self.items (they are the page's content, not a pop-up list) - draw
        both sets.
        """
        rows = list(getattr(self, "options", []))
        rows += [i for i in self.items if i.kind == "option"]
        for i, opt in enumerate(rows):
            selected = opt.extra.get("selected")
            highlighted = opt.extra.get("highlighted")
            # The mouse hover: the pop-up rows are indexed by their position
            # in self.options; the windows page rows carry their payload
            # (the outline on the screen is easy to miss, so the row itself
            # is highlighted too).
            hovered = ((i < len(self.options)
                        and self.hover == f"option:{i}")
                       or self.hover == f"woption:{opt.payload}")
            # The windows page is a PAGE of rows the user picks from, so its
            # selection is drawn the way this panel draws selection everywhere
            # else: a square. An accent FILL spent the loudest colour on the
            # page on a row whose state fits in 7 pixels, and it made the chosen
            # row look switched on. The drop-down lists below keep their accent
            # fill - they are menu entries, not rows.
            page_rows = self.page == "windows"
            if page_rows:
                if selected:
                    fill = self.c["surface"]
                elif highlighted or hovered:
                    fill = self.c["surface"]
                else:
                    fill = self.c["bg"]
                edge = (self.c["accent"] if selected
                        else self.c["focus"] if highlighted
                        else self.c["border"])
            else:
                if selected:
                    fill = self.c["accent"]
                elif highlighted or hovered:
                    fill = self.c["surface"]
                else:
                    fill = self.c["bg"]
                edge = self.c["focus"] if highlighted else self.c["border"]
            pygame.draw.rect(surface, _rgb(fill), opt.rect,
                             border_radius=self._u(RADIUS // 2))
            pygame.draw.rect(surface, _rgb(edge), opt.rect,
                             max(2, self._u(2)) if (highlighted or
                                                    (page_rows and selected))
                             else self._u(1),
                             border_radius=self._u(RADIUS // 2))
            color = (self.c["text"] if page_rows
                     else self.c["bg"] if selected
                     else self.c["text"])
            text = opt.extra.get("label", "")
            # The language list shows every language in its own script; the
            # CJK names (中文, 日本語, 한국어) need a CJK font - the current
            # UI font renders them as boxes. Pick by the script: hangul
            # (AC00-D7AF) -> Malgun Gothic, kana (3040-30FF) -> Yu Gothic,
            # CJK ideographs -> YaHei (user: Asian names show as squares).
            font = self._font
            if self._cjk_fonts:
                if any(0xAC00 <= ord(ch) <= 0xD7AF for ch in text):
                    font = self._cjk_fonts.get("malgungothic") or font
                elif any(0x3040 <= ord(ch) <= 0x30FF for ch in text):
                    font = self._cjk_fonts.get("yugothic") or font
                elif any(0x4E00 <= ord(ch) <= 0x9FFF for ch in text):
                    font = self._cjk_fonts.get("microsoftyahei") or font
            size_text = str(opt.extra.get("size") or "") if page_rows else ""
            size_img = None
            if size_text:
                size_img = self._mono_small.render(size_text, True,
                                                   _rgb(self.c["muted"]))
            size_room = (size_img.get_width() + self._u(12)) if size_img else 0
            square_shift = 0
            if page_rows:
                # The square, then the label - and the label's budget shrinks by
                # the same amount, or a long title runs under the size column.
                square_shift = self._u(7) + self._u(12)
                self._draw_state_square(
                    surface, opt.rect.x + self._u(12), opt.rect.centery,
                    filled=bool(selected), hollow=not selected)
            label = self._clip(font, text, _rgb(color),
                               opt.rect.w - self._u(24) - square_shift
                               - size_room)
            surface.blit(label, (opt.rect.x + self._u(12) + square_shift,
                                 opt.rect.centery - label.get_height() // 2))
            if size_img is not None:
                surface.blit(size_img,
                             (opt.rect.right - self._u(12)
                              - size_img.get_width(),
                              opt.rect.centery - size_img.get_height() // 2))
        # The list's scrollbar (only when the list actually scrolls).
        if getattr(self, "_opt_track", None) is not None and self._opt_track.w > 0 \
                and self._opt_max_scroll > 0:
            pygame.draw.rect(surface, _rgb(self.c["border"]), self._opt_track,
                             border_radius=self._u(2))
            pygame.draw.rect(surface, _rgb(self.c["muted"]), self._opt_thumb,
                             border_radius=self._u(2))

    def _draw_state_square(self, surface, x: int, cy: int, *,
                           filled: bool = False, hollow: bool = False,
                           danger: bool = False) -> None:
        """The 7x7 state square, at x, vertically centred on cy.

        Returns nothing: the caller keeps its own label position, so adding the
        square cannot shift a caption that was already measured to fit (the
        label moves right by exactly the square plus its gap).

        The three tones are the direction's:
          accent, filled   the row is on / away from neutral
          hollow           off, but a real on/off row
          grey, filled     a neutral setting - nothing to act on
        """
        size = self._u(7)
        box = pygame.Rect(x, cy - size // 2, size, size)
        if filled:
            tone = self.c["danger"] if danger else self.c["accent"]
            pygame.draw.rect(surface, _rgb(tone), box)
        elif hollow:
            pygame.draw.rect(surface, _rgb(self.c["muted"]), box, self._u(1))
        else:
            pygame.draw.rect(surface, _rgb(self.c["muted"]), box)

    def _draw_segment_groups(self, surface) -> None:
        """The frame and dividers of every segment group, once the cells are in.

        The cells fill their own boxes; this draws what makes them ONE control:
        a 1px border around the whole group and a 1px divider between neighbours.
        Drawn after the cells because a filled cell must not paint over the
        group's edge - the direction shows the selected cell sitting inside the
        frame, not on top of it.
        """
        border = _rgb(self.c["border"])
        for rect, splits in getattr(self, "_segment_rects", []):
            if rect.w <= 0:
                continue
            pygame.draw.rect(surface, border, rect, self._u(1),
                             border_radius=self._u(4))
            # The offsets the layout measured, not an even split: the cells
            # are as wide as their own labels need.
            for off in splits:
                x = rect.x + int(off)
                pygame.draw.line(surface, border,
                                 (x, rect.y + self._u(1)),
                                 (x, rect.bottom - self._u(1)), 1)

    def _draw_sections(self, surface) -> None:
        """A block title: a mono index, small caps, a hairline to the right.

        The index (01-05) is not decoration: with five blocks of similar-looking
        rows the eye needs a fixed, countable anchor, and the mockup's whole
        direction rests on it. The counters ride the sections in the order they
        are laid out, so a new block changes nothing but its own number.
        """
        accent = _rgb(self.c["accent"])
        for idx, (title, rect) in enumerate(getattr(self, "_section_rects", []), 1):
            x = rect.x
            if self.page == "main":
                # Mono digits, accent - the one place the index colour is used
                # besides state squares, exactly as the direction specifies.
                num = self._mono_small.render(f"{idx:02d}", True, accent)
                surface.blit(num, (x, rect.y + max(0, (self._u(SMALL_SIZE)
                                                      - num.get_height()) // 2)))
                x += num.get_width() + self._u(10)
            img = self._small_font.render(title.upper(), True,
                                          _rgb(self.c["text"] if self.page == "main"
                                               else self.c["muted"]))
            surface.blit(img, (x, rect.y))
            ly = rect.y + img.get_height() // 2
            x0 = x + img.get_width() + self._u(10)
            if x0 < rect.right:
                pygame.draw.line(surface, _rgb(self.c["border"]),
                                 (x0, ly), (rect.right, ly), 1)

    def _draw_rules(self, surface, s: dict) -> None:
        """The divider before the footer, plus the hint."""
        rect = getattr(self, "_rule_rect", None)
        if rect is not None and rect.w > 0:
            pygame.draw.line(surface, _rgb(self.c["border"]),
                             (rect.x, rect.y), (rect.right, rect.y), 1)
        hint = getattr(self, "_hint_rect", None)
        if self.page == "settings" and hint is not None and hint.w > 0:
            img = self._small_font.render(s["hotkey_hint"], True,
                                          _rgb(self.c["muted"]))
            surface.blit(img, (hint.x, hint.y))


    #: The numeric cell's own padding, in base units. The mockup: 4px 9px.
    NUM_CELL_PAD_X = 9
    NUM_CELL_PAD_Y = 4

    def _number_cell_size(self, text: str) -> tuple[int, int]:
        """The size a numeric cell will take for `text`, without drawing it.

        Kept separate from the drawing so a row can budget space for the cell
        BEFORE the drawer runs - the label's clip depends on it, and measuring
        inside the drawer would mean the label was already clipped wrong.
        """
        img = self._mono.render(text, True, (0, 0, 0))
        return (img.get_width() + 2 * self._u(self.NUM_CELL_PAD_X),
                img.get_height() + 2 * self._u(self.NUM_CELL_PAD_Y))

    def _wrap_hint(self, text: str, width: int) -> list[str]:
        """Break a hint into lines that fit `width`, on its own words."""
        out: list[str] = []
        for para in str(text).split("\n"):
            words = para.split()
            if not words:
                out.append("")
                continue
            line = words[0]
            for word in words[1:]:
                trial = f"{line} {word}"
                if self._small_font.size(trial)[0] <= width:
                    line = trial
                else:
                    out.append(line)
                    line = word
            out.append(line)
        return out

    def _draw_hint_lines(self, surface, item: Item, hint: str,
                         x: int, y: int, width: int) -> int:
        """Draw a wrapped, clipped hint; return the height it used.

        One implementation for every drawer: the drop-down drew its hint as a
        single unclipped line while the slider clipped and wrapped, and a sentence
        of real explanation was cut at the panel edge in the first case.
        """
        used = 0
        for line in self._wrap_hint(hint, width):
            img = self._clip(self._small_font, line, _rgb(self.c["muted"]),
                             width)
            surface.blit(img, (x, y + used))
            used += self._small_font.get_height() + self._u(4)
        return used

    def _hint_height(self, hint: str, width: int) -> int:
        """How tall the wrapped hint will be - the layout needs this."""
        lines = len(self._wrap_hint(hint, width))
        return lines * (self._small_font.get_height() + self._u(4))

    def _draw_number_cell(self, surface, text: str, right: int, mid_y: int,
                          *, mono=None) -> int:
        """Draw `text` in a bordered cell whose right edge is at `right`.

        The ONE place a numeric cell is drawn: the effect values, the wipe % and
        the resolution all go through here, so "the same pattern everywhere" is
        a property of the code rather than a promise in a comment.

        Returns the cell's left edge.
        """
        font = mono or self._mono
        img = font.render(text, True, _rgb(self.c["text"]))
        pad_x, pad_y = self._u(self.NUM_CELL_PAD_X), self._u(self.NUM_CELL_PAD_Y)
        rect = pygame.Rect(0, 0, img.get_width() + 2 * pad_x,
                           img.get_height() + 2 * pad_y)
        rect.right = right
        rect.centery = mid_y
        pygame.draw.rect(surface, _rgb(self.c["surface"]), rect,
                         border_radius=self._u(4))
        pygame.draw.rect(surface, _rgb(self.c["border"]), rect, self._u(1),
                         border_radius=self._u(4))
        surface.blit(img, (rect.x + pad_x, rect.y + pad_y))
        return rect.x

    def _elide_path(self, path: str) -> str:
        """A path shortened in the MIDDLE, so both ends stay readable.

        The tail is what tells two folders apart (NeuralScreen vs Screenshots)
        and the head is what tells two drives apart - the middle is what nobody
        reads. `_clip` cuts from the right, which would hide exactly the part
        that matters, so the shortening happens here and the result is short
        enough to be drawn whole.
        """
        if not path:
            return ""
        limit = 34
        if len(path) <= limit:
            return path
        keep_head = max(6, (limit - 3) // 3)
        keep_tail = limit - 3 - keep_head
        return f"{path[:keep_head]}...{path[-keep_tail:]}"

    # -- the conversion page ----------------------------------------------

    def _font_for_text(self, text: str, base):
        """A face that has the text's script, falling back to `base`.

        A file name can be Korean or Chinese in an English interface, and the
        product face draws those as boxes. The same choice the windows page
        makes for window titles: hangul, kana, then the CJK ideographs.
        """
        if self._cjk_fonts:
            if any(0xAC00 <= ord(ch) <= 0xD7AF for ch in text):
                return self._cjk_fonts.get("malgungothic") or base
            if any(0x3040 <= ord(ch) <= 0x30FF for ch in text):
                return self._cjk_fonts.get("yugothic") or base
            if any(0x4E00 <= ord(ch) <= 0x9FFF for ch in text):
                return self._cjk_fonts.get("microsoftyahei") or base
        return base

    def _wrap_note(self, text: str, width: int) -> list[str]:
        """Lines of `text` that fit `width` in the small face.

        On the words, like _wrap_hint - and then on the characters of a word
        that does not fit on a line of its own. That second rule is every
        sentence in Chinese and Japanese: they have no spaces to break at,
        and wrapped at spaces alone the empty-queue note was one line that
        ran off the panel.
        """
        font = self._small_font
        out: list[str] = []
        for para in str(text).split("\n"):
            line = ""
            for word in para.split(" "):
                trial = f"{line} {word}" if line else word
                if font.size(trial)[0] <= width:
                    line = trial
                    continue
                if line:
                    out.append(line)
                    line = ""
                if font.size(word)[0] <= width:
                    line = word
                    continue
                for ch in word:
                    if line and font.size(line + ch)[0] > width:
                        out.append(line)
                        line = ch
                    else:
                        line += ch
            out.append(line)
        return out

    def _note_height(self, text: str, width: int) -> int:
        """How tall _draw_note will draw `text` at `width`."""
        return len(self._wrap_note(text, width)) * (
            self._small_font.get_height() + self._u(4))

    def _convert_action_width(self, s: dict) -> int:
        """One width for every row's button: the longest action word here.

        The buttons then stand in one column whatever state each row is in -
        a Stop above a Show above a Retry, not a ragged edge.
        """
        widest = max(self._small_font.size(s.get(f"convert_act_{a}", a))[0]
                     for a in ("stop", "remove", "show", "retry"))
        return max(self._u(64), widest + self._u(20))

    def _dashed_rect(self, surface, rect: pygame.Rect, color: str) -> None:
        """A dashed outline - the drop target's usual look."""
        dash, gap = self._u(6), self._u(4)
        width = max(1, self._u(1))
        col = _rgb(color)
        for x0 in range(rect.left, rect.right, dash + gap):
            x1 = min(x0 + dash, rect.right - 1)
            pygame.draw.line(surface, col, (x0, rect.top), (x1, rect.top), width)
            pygame.draw.line(surface, col, (x0, rect.bottom - 1),
                             (x1, rect.bottom - 1), width)
        for y0 in range(rect.top, rect.bottom, dash + gap):
            y1 = min(y0 + dash, rect.bottom - 1)
            pygame.draw.line(surface, col, (rect.left, y0), (rect.left, y1), width)
            pygame.draw.line(surface, col, (rect.right - 1, y0),
                             (rect.right - 1, y1), width)

    def _draw_note(self, surface, item: Item, s: dict) -> None:
        """A line or two of explanation; boxed, it is the empty queue."""
        text = str(item.extra.get("text") or "")
        rect = item.rect
        x, y, width = rect.x, rect.y, rect.w
        if item.extra.get("boxed"):
            self._dashed_rect(surface, rect, self.c["border"])
            inset = int(item.extra.get("inset", self._u(14)))
            x, y, width = rect.x + inset, rect.y + inset, rect.w - 2 * inset
        line_h = self._small_font.get_height() + self._u(4)
        for line in self._wrap_note(text, width):
            img = self._clip(self._small_font, line, _rgb(self.c["muted"]),
                             width)
            surface.blit(img, (x, y))
            y += line_h

    def _draw_job(self, surface, item: Item, s: dict) -> None:
        """One file of the queue: its name, what is happening, how far.

        The second line and its tone arrive worded (settings_io's payload);
        this only draws them. The button on the right is its own item.
        """
        job = item.extra.get("job") or {}
        rect = item.rect
        row_pad = int(item.extra.get("row_pad", self._u(8)))
        act_w = int(item.extra.get("act_w", 0))
        inner = self._u(10)
        label_h = self._u(LABEL_H)
        pygame.draw.rect(surface, _rgb(self.c["surface"]), rect,
                         border_radius=self._u(RADIUS // 2))
        name = str(job.get("name") or "")
        name_img = self._clip(self._font_for_text(name, self._font), name,
                              _rgb(self.c["text"]),
                              rect.w - 2 * inner - act_w - self._u(8))
        top = rect.y + row_pad
        surface.blit(name_img, (rect.x + inner,
                                top + (label_h - name_img.get_height()) // 2))
        tone = {"text": self.c["text"], "ok": self.c["ok"],
                "danger": self.c["danger"]}.get(str(job.get("tone") or ""),
                                                self.c["muted"])
        line_y = top + label_h
        line_img = self._clip(self._small_font, str(job.get("line") or ""),
                              _rgb(tone), rect.w - 2 * inner)
        surface.blit(line_img, (rect.x + inner, line_y))
        if job.get("status") != "running":
            return
        # The bar only while the file runs: a finished row says so in words,
        # and a full bar under every one of them would be noise.
        bar = pygame.Rect(rect.x + inner,
                          line_y + self._small_font.get_height() + self._u(4),
                          rect.w - 2 * inner, self._u(4))
        pygame.draw.rect(surface, _rgb(self.c["border"]), bar,
                         border_radius=self._u(2))
        fraction = job.get("fraction")
        if isinstance(fraction, (int, float)) and fraction > 0:
            fill = pygame.Rect(bar.x, bar.y,
                               max(self._u(4), int(bar.w * min(1.0, fraction))),
                               bar.h)
            pygame.draw.rect(surface, _rgb(self.c["accent"]), fill,
                             border_radius=self._u(2))

    def _draw_info(self, surface, item: Item, s: dict) -> None:
        """A line: what on the left, how big on the right.

        The captured-window row is clickable (it opens the list); it takes
        the accent under the pointer so that it reads as one.
        """
        value = str(item.extra.get("value") or "")
        # The value is clipped too, and the label keeps a floor (audit M1). A
        # recording path is the user's folder plus a fixed tail
        # (neuralscreen-YYYYMMDD-HHMMSS-mmm.mp4), so it is routinely longer
        # than the row: measured at 1080p, 492 px of value in a 365 px row, and
        # the label was handed `room = item.rect.w - value_w - 12` - a NEGATIVE
        # width, so _clip returned an empty surface and the caption vanished as
        # well. Both now share the row: the value takes what it needs up to a
        # share of the row, the label keeps the rest with a readable floor.
        # The floor is for a caption that has to stay readable beside the
        # value. A row with NO caption (the screenshot path) has nothing to
        # protect, and reserving 90 units for it stole them from the value.
        has_label = bool(item.extra.get("label"))
        label_floor = self._u(90) if (value and has_label) else 0
        val_room = max(self._u(60),
                       item.rect.w - label_floor - self._u(12)) if value else 0
        val = (self._clip(self._mono_small, value, _rgb(self.c["muted"]),
                          val_room) if value else None)
        room = item.rect.w - (val.get_width() if val is not None else 0) \
            - self._u(12)
        hot = (item.key == "source_now"
               and self.hover == f"info:{item.key}")
        label = self._clip(self._font, str(item.extra.get("label") or ""),
                           _rgb(self.c["accent"] if hot else self.c["text"]),
                           room)
        y = item.rect.centery
        surface.blit(label, (item.rect.x, y - label.get_height() // 2))
        if value and val is not None:
            if item.extra.get("cell"):
                # A number in a cell, the same cell every other number on the
                # page uses (resolution, effect values, wipe %).
                self._draw_number_cell(surface, value, item.rect.right, y)
            else:
                # A row with no caption is a STANDALONE value (the screenshot
                # path): it reads from the left, with the action button beside
                # it on the right. A captioned row keeps its value on the
                # right, where the number lines up with every other number.
                vx = (item.rect.x if not has_label
                      else item.rect.right - val.get_width())
                surface.blit(val, (vx, y - val.get_height() // 2))


    def _draw_tab(self, surface, item: Item, s: dict) -> None:
        """One cell of the settings tab row (see the layout for the contract).

        The active cell is a raised surface with an accent SQUARE before its
        label - the square carries "you are here", so the accent keeps meaning
        state and a tab cannot be mistaken for a switch that is on.
        """
        rect = item.rect
        active = bool(item.extra.get("active"))
        hot = self.hover == f"tab:{item.key}"
        if active:
            pygame.draw.rect(surface, _rgb(self.c["surface"]), rect)
        elif hot:
            pygame.draw.rect(surface, _rgb(self.c["surface"]), rect)
        shift = 0
        if active:
            # The square, then the label: the pair is centred together, so the
            # square cannot hang outside the cell's optical centre.
            small = self._small_font.render(str(item.extra.get("label", "")),
                                            True, _rgb(self.c["text"]))
            gap = self._u(9)
            sq = self._u(7)
            total = sq + gap + small.get_width()
            left = rect.centerx - total // 2
            self._draw_state_square(surface, left, rect.centery,
                                    filled=True)
            surface.blit(small, (left + sq + gap,
                                 rect.centery - small.get_height() // 2))
        else:
            img = self._clip(self._small_font,
                             str(item.extra.get("label", "")),
                             _rgb(self.c["muted"]), rect.w - self._u(8))
            surface.blit(img, (rect.centerx - img.get_width() // 2,
                               rect.centery - img.get_height() // 2))

    def _draw_segmented(self, surface, item: Item, s: dict) -> None:
        """Two or three options side by side: the chosen one is accent-filled."""
        label = item.extra.get("label")
        if label:
            # The state square, then the caption: a segment group has no switch,
            # so its state is read from the square. `_u(7) + _u(10)` is the
            # square and its gap, and the caption is clipped to what is left so
            # adding the square cannot push a long word under the control.
            shift = self._u(7) + self._u(10) if item.extra.get("square") else 0
            if item.extra.get("square"):
                self._draw_state_square(
                    surface, self.panel_rect.x + self._u(PAD),
                    item.rect.centery,
                    filled=bool(item.extra.get("state_filled")))
            img = self._clip(self._font, label, _rgb(self.c["muted"]),
                             item.rect.w - shift)
            surface.blit(img, (self.panel_rect.x + self._u(PAD) + shift,
                               item.rect.centery - img.get_height() // 2))
        pygame.draw.rect(surface, _rgb(self.c["surface"]), item.rect,
                         border_radius=self._u(RADIUS // 2))
        options = item.payload or []
        labels = item.extra.get("labels") or options
        if not options:
            return
        cell = item.rect.w // len(options)
        current = str(item.extra.get("current", ""))
        cells = []
        for idx, opt in enumerate(options):
            cr = pygame.Rect(item.rect.x + idx * cell, item.rect.y,
                             cell, item.rect.h)
            cells.append(cr)
            active = str(opt) == current
            # The two ways a segment can say "this one is chosen". Without a
            # square, the accent fill IS the state. With one (SOURCE), the fill
            # drops to the raised neutral and the accent moves into the square -
            # otherwise the square would sit invisible on an accent background.
            celled = bool(item.extra.get("square")) and not item.extra.get("label")
            if active:
                fill = self.c["surface"] if celled else self.c["accent"]
                pygame.draw.rect(surface, _rgb(fill), cr,
                                 border_radius=self._u(RADIUS // 2))
            if celled:
                text_col = self.c["text"] if active else self.c["muted"]
            else:
                text_col = self.c["bg"] if active else self.c["muted"]
            txt = self._small_font.render(str(labels[idx]), True,
                                          _rgb(text_col))
            if celled:
                # The square inside the cell: filled on the chosen one, hollow
                # on the other. The caption is centred as the square+text PAIR,
                # so the group reads as one centred unit.
                gap = self._u(10)
                sq_size = self._u(7)
                total = sq_size + gap + txt.get_width()
                left = cr.centerx - total // 2
                self._draw_state_square(surface, left, cr.centery,
                                        filled=active, hollow=not active)
                surface.blit(txt, (left + sq_size + gap,
                                   cr.centery - txt.get_height() // 2))
            else:
                surface.blit(txt, (cr.centerx - txt.get_width() // 2,
                                   cr.centery - txt.get_height() // 2))
        item.extra["cells"] = cells

    def _draw_icon(self, surface, item: Item, s: dict) -> None:
        """A header button: a rounded square with a glyph inside.

        At rest only the outline; on hover a fill and an accent outline:
        circles with a drawn gear looked homemade, and a properly drawn gear is
        unreadable at 30 px anyway - so instead there are three sliders, which
        is also closer in meaning to what the panel holds.
        """
        rect = item.rect
        hot = self.hover == f"icon:{item.key}"
        radius = self._u(8)
        if hot:
            pygame.draw.rect(surface, _rgb(self.c["surface"]), rect,
                             border_radius=radius)
        # At rest the glyph takes the panel's own text colour, not `muted`.
        # Muted is for captions you read once; these are controls, and one of
        # them is the only way to put the menu away. Drawn in muted on the
        # title bar's surface they read as decoration - the collapse button
        # was reported missing while it was on screen.
        col = self.c["accent"] if hot else self.c["text"]
        pygame.draw.rect(surface,
                         _rgb(self.c["accent"] if hot else self.c["border"]),
                         rect, max(1, self._u(1)), border_radius=radius)
        cx, cy = rect.centerx, rect.centery
        lw = max(2, self._u(2))
        if item.key == "help":
            img = self._font.render("?", True, _rgb(col))
            surface.blit(img, (cx - img.get_width() // 2,
                               cy - img.get_height() // 2))
        elif item.key == "mini":
            # Two stacked bars, the lower one short: a full panel and the
            # short one it becomes. Filled while mini mode is on, so the
            # icon says which state you are in rather than what it does.
            w1, w2, h = self._u(13), self._u(7), self._u(4)
            top = pygame.Rect(cx - w1 // 2, cy - h - self._u(2), w1, h)
            low = pygame.Rect(cx - w1 // 2, cy + self._u(2), w2, h)
            fill = 0 if self.mini else max(1, self._u(1))
            pygame.draw.rect(surface, _rgb(col), top, fill,
                             border_radius=self._u(1))
            pygame.draw.rect(surface, _rgb(col), low, fill,
                             border_radius=self._u(1))
        elif item.key == "pick":
            # A pencil on the diagonal: a body and a point. It was a ring
            # with a dot in it - the same mark the rows wear while they are
            # chosen - but an icon has to say what pressing it DOES, and
            # every interface the user has says that with a pencil.
            if self.mini_pick:
                col = self.c["accent"]
            off = max(1, self._u(2))
            tip = (cx - self._u(6), cy + self._u(6))
            base = (cx - self._u(3), cy + self._u(3))
            end = (cx + self._u(6), cy - self._u(6))
            pygame.draw.line(surface, _rgb(col), base, end,
                             max(2, self._u(4)))
            pygame.draw.polygon(surface, _rgb(col),
                                [tip, (base[0] + off, base[1] + off),
                                 (base[0] - off, base[1] - off)])
        elif item.key == "close":
            d = max(3, self._u(5))
            pygame.draw.line(surface, _rgb(col), (cx - d, cy - d),
                             (cx + d, cy + d), lw)
            pygame.draw.line(surface, _rgb(col), (cx + d, cy - d),
                             (cx - d, cy + d), lw)
        elif item.key == "min":
            # The collapse button: a short horizontal bar, like a window's
            # minimise glyph.
            half = max(5, self._u(7))
            pygame.draw.line(surface, _rgb(col), (cx - half, cy),
                             (cx + half, cy), lw)
        else:
            # Three sliders: a full-width line with a knob at its own place on
            # each. It reads smaller than a gear and draws without
            # antialiasing.
            half = max(5, self._u(7))
            step = max(3, self._u(5))
            knobs = (0.65, 0.35, 0.55)
            for i, kx in enumerate(knobs):
                ly = cy + (i - 1) * step
                pygame.draw.line(surface, _rgb(col), (cx - half, ly),
                                 (cx + half, ly), max(1, self._u(1)))
                px = int(cx - half + 2 * half * kx)
                pygame.draw.circle(surface, _rgb(self.c["bg"]), (px, ly),
                                   max(2, self._u(2)))
                pygame.draw.circle(surface, _rgb(col), (px, ly),
                                   max(2, self._u(2)), max(1, self._u(1)))

    def _action_icon(self, surface, key: str, cx: int, cy: int,
                     k: float = 1.0) -> int:
        """Draw the line icon for an action, centred on (cx, cy).

        Returns the x where the label should start, so the caller can centre the
        icon+label PAIR. Geometry and 1.4 px stroke from the mockup's own SVG.

        `k` scales the whole glyph. An icon is sized to the caption it leads:
        drawn at the regular size beside a small caption it stops reading as a
        mark and starts reading as a picture (user, 20.09).
        """
        col = _rgb(self.c["text"])
        _u = self._u
        def u(base: float) -> int:
            return max(1, int(round(_u(base) * k)))
        w = 1 if u(2) < 2 else 2                # 1.4 px at panel scale
        if key == "screenshot":
            # A camera from the front: body, finder bump, lens.
            bw, bh = u(17), u(15)
            x0, y0 = cx - bw // 2, cy - bh // 2
            pygame.draw.rect(surface, col,
                             pygame.Rect(x0, y0 + u(4), bw, bh - u(4)),
                             w, border_radius=u(2))
            pygame.draw.polygon(surface, col, [
                (x0 + u(3), y0 + u(4)),
                (x0 + u(6), y0 + u(1)),
                (x0 + u(11), y0 + u(1)),
                (x0 + u(14), y0 + u(4)),
            ], w)
            pygame.draw.circle(surface, col,
                               (cx, cy + u(2)), u(3), w)
        elif key == "record":
            # A ring with a filled dot: the recording lamp.
            pygame.draw.circle(surface, col, (cx, cy), u(6), w)
            pygame.draw.circle(surface, _rgb(self.c["danger"]), (cx, cy),
                               u(3))
        elif key == "convert":
            # A page with its corner folded and an arrow through it: a file
            # going in one way and coming out another. The same 17 x 15 box
            # and stroke as the camera, so the three cells read as one set.
            bw, bh = u(12), u(15)
            x0, y0 = cx - u(17) // 2, cy - bh // 2
            fold = u(4)
            pygame.draw.lines(surface, col, True, [
                (x0, y0), (x0 + bw - fold, y0), (x0 + bw, y0 + fold),
                (x0 + bw, y0 + bh), (x0, y0 + bh)], w)
            pygame.draw.line(surface, col, (x0 + bw - fold, y0),
                             (x0 + bw - fold, y0 + fold), w)
            pygame.draw.line(surface, col, (x0 + bw - fold, y0 + fold),
                             (x0 + bw, y0 + fold), w)
            ay = cy + u(2)
            pygame.draw.line(surface, col, (x0 + u(3), ay),
                             (x0 + u(17), ay), w)
            pygame.draw.lines(surface, col, False, [
                (x0 + u(14), ay - u(3)), (x0 + u(17), ay),
                (x0 + u(14), ay + u(3))], w)
        else:
            return cx
        return cx + u(9)

    def _draw_action(self, surface, item: Item, s: dict) -> None:
        """A footer button: the name, the hotkey below it, and for exit a note."""
        rect = item.rect
        filled = bool(item.extra.get("filled"))
        danger = bool(item.extra.get("danger"))
        hot = self.hover == f"action:{item.key}"
        radius = self._u(RADIUS // 2)
        if filled:
            pygame.draw.rect(surface, _rgb(self.c["accent"]), rect,
                             border_radius=radius)
            name_col = key_col = self.c["bg"]
        else:
            pygame.draw.rect(surface, _rgb(self.c["surface"]), rect,
                             border_radius=radius)
            # The danger tone marks the EDGE, not the label. As a label it
            # sat one step from the accent that paints every number on the
            # page, so the one destructive action read as another value.
            edge = (self.c["danger"] if danger
                    else self.c["accent"] if hot else self.c["border"])
            pygame.draw.rect(surface, _rgb(edge), rect,
                             self._u(2) if danger else self._u(1),
                             border_radius=radius)
            name_col = self.c["text"]
            key_col = self.c["muted"]
        name = self._font.render(item.extra.get("label", ""), True,
                                 _rgb(name_col))
        # The line icon, when the button carries one: the pair is centred
        # together, so the icon cannot hang outside the button's centre.
        icon_w = self._u(17) + self._u(9) if item.extra.get("icon") else 0
        hk = item.extra.get("hotkey")
        note = item.extra.get("note")
        if hk or note:
            # The two-line layout: the name on top, the caption below. The
            # name sits a little below the top edge so the button reads as
            # centred (user: the Quit label was too close to the top).
            surface.blit(name, (rect.centerx - name.get_width() // 2,
                                rect.y + self._u(10)))
        else:
            # A single-line action (Back without a hotkey): centre it, the
            # top-anchored position was left over from the two-line layout
            # and looked off (user: the Back button is not centred).
            # The leading mark (a state square on Quit, a line icon on
            # Screenshot/Record) and the caption are ONE group: the mark is
            # measured with the caption and the whole group is centred, or the
            # mark pushes the caption off the button's centre. Drawn at the
            # caption's own x it would paint over the first letter.
            gap = self._u(9)
            if item.extra.get("square"):
                lead = self._u(7) + gap
            elif item.extra.get("icon"):
                lead = self._u(17) + gap
            else:
                lead = 0
            left = rect.centerx - (lead + name.get_width()) // 2
            if item.extra.get("square"):
                self._draw_state_square(
                    surface, left, rect.centery, filled=True,
                    danger=bool(item.extra.get("square_danger")))
            elif item.extra.get("icon"):
                self._action_icon(surface, str(item.extra.get("icon")),
                                  left + self._u(17) // 2, rect.centery)
            surface.blit(name, (left + lead,
                                rect.centery - name.get_height() // 2))
        if hk:
            img = self._small_font.render(hk, True, _rgb(key_col))
            surface.blit(img, (rect.centerx - img.get_width() // 2,
                               rect.y + self._u(26)))
        if note:
            img = self._small_font.render(note, True, _rgb(key_col))
            surface.blit(img, (rect.centerx - img.get_width() // 2,
                               rect.y + self._u(44)))

    def _draw_hotkey(self, surface, item: Item, s: dict) -> None:
        """A remap row: the action on the left, the key field on the right."""
        label = self._small_font.render(item.extra.get("label", ""), True,
                                        _rgb(self.c["text"]))
        surface.blit(label, (item.rect.x,
                             item.rect.centery - label.get_height() // 2))
        fw = self._u(170)
        field = pygame.Rect(item.rect.right - fw, item.rect.y, fw, item.rect.h)
        capturing = bool(item.extra.get("capturing"))
        radius = self._u(RADIUS // 2)
        if capturing:
            pygame.draw.rect(surface, _rgb(self.c["bg"]), field,
                             border_radius=radius)
            pygame.draw.rect(surface, _rgb(self.c["accent"]), field,
                             max(2, self._u(2)), border_radius=radius)
            txt = self._small_font.render(s["hotkey_press"], True,
                                          _rgb(self.c["accent"]))
        else:
            hot = self.hover == f"hotkey:{item.key}"
            pygame.draw.rect(surface, _rgb(self.c["surface"]), field,
                             border_radius=radius)
            pygame.draw.rect(surface,
                             _rgb(self.c["accent"] if hot else self.c["border"]),
                             field, self._u(1), border_radius=radius)
            txt = self._mono_small.render(str(item.extra.get("key", "—")),
                                          True, _rgb(self.c["text"]))
        surface.blit(txt, (field.centerx - txt.get_width() // 2,
                           field.centery - txt.get_height() // 2))
        item.extra["field"] = field

    def _draw_button(self, surface, item: Item, s: dict) -> None:
        hot = self.hover == f"button:{item.key}"
        disabled = bool(item.extra.get("disabled"))
        if item.extra.get("flat"):
            # Text only, right-aligned, in the accent: this is a link in
            # weight, and a bordered box here would compete with the real
            # buttons elsewhere on the page. One alignment, because there is
            # one user - the revert link under the profile picker. A centring
            # branch was written here for the preset buttons and never reached
            # them: they became a strip instead, and nothing has set `align`
            # since (audit 20.09).
            tone = (self.c["muted"] if disabled
                    else self.c["focus"] if hot
                    else self.c["accent"])
            img = self._clip(self._small_font,
                             item.extra.get("label", item.key),
                             _rgb(tone), item.rect.w)
            surface.blit(img, (item.rect.right - img.get_width(),
                               item.rect.centery - img.get_height() // 2))
            return
        # "filled": the active choice inside an inline group (the FG
        # multiplier) reads as a selected segment - accent background, the
        # label on it - and NOT as a hover state, so the selection stays
        # visible with the cursor elsewhere (user 14.09: the active
        # multiplier was invisible, the renderer had no filled handling).
        filled = bool(item.extra.get("filled")) and not disabled
        small = bool(item.extra.get("small"))
        pair = item.extra.get("pair")
        if pair:
            # A cell of a two-cell strip, drawn the way the SOURCE control is:
            # one raised surface across the row, a hairline where the two meet,
            # and no border of its own. Two bordered boxes weighed as much as
            # anything else on the page and said twice over that they are two
            # separate things, which is not what a pair is.
            #
            # Per-corner radii rather than one group rect drawn underneath: the
            # groups are painted AFTER the items (the FG row needs its frame on
            # top of a filled cell), and a strip drawn then would cover these
            # captions.
            # A strip of three has a middle cell ("mid"): square on both sides,
            # with its own seam on the left like the right-hand cell.
            radius = self._u(RADIUS // 2)
            left = pair == "left"
            right = pair == "right"
            pygame.draw.rect(
                surface, _rgb(self.c["surface"]), item.rect,
                border_top_left_radius=radius if left else 0,
                border_bottom_left_radius=radius if left else 0,
                border_top_right_radius=radius if right else 0,
                border_bottom_right_radius=radius if right else 0)
            if not left:
                # The seam, drawn once by the cell to its right.
                pygame.draw.line(surface, _rgb(self.c["border"]),
                                 (item.rect.x, item.rect.y + self._u(6)),
                                 (item.rect.x, item.rect.bottom - self._u(6)),
                                 max(1, self._u(1)))
            tone = (self.c["muted"] if disabled
                    else self.c["accent"] if hot
                    else self.c["text"])
            icon = item.extra.get("icon")
            # The glyph follows the caption: SMALL_SIZE / FONT_SIZE of the
            # nominal 17, so the mark and the word are one size step.
            ik = SMALL_SIZE / float(FONT_SIZE)
            icon_w = (int(round(self._u(17) * ik)) + self._u(9)) if icon else 0
            # The SMALL font, like every other caption that lives inside a
            # cell - the SOURCE cells, the Model steps, the FG steps. The
            # regular size is for a button that is its own full-width control
            # (Quit, Back) and for the row labels. Mixing the two inside one
            # strip is what read as "not quite organic" (user, 20.09).
            #
            # It cannot go the other way: measured at 17, the Model segment
            # needs 135 px in a 112 px cell in Russian (121%), so the strips
            # meet at the small size, not at the regular one.
            label = self._clip(self._small_font,
                               item.extra.get("label", item.key),
                               _rgb(tone),
                               item.rect.w - self._u(16) - icon_w)
            x = item.rect.centerx - (icon_w + label.get_width()) // 2
            if icon:
                self._action_icon(surface, str(icon),
                                  x + int(round(self._u(17) * ik)) // 2,
                                  item.rect.centery, k=ik)
            surface.blit(label, (x + icon_w,
                                 item.rect.centery - label.get_height() // 2))
            return
        if small and item.extra.get("segment"):
            # A cell of a segment GROUP (the FG row): the group owns the outer
            # frame, the radius and the dividers, and each cell only fills its
            # own box. Drawn as one frame because that is what the direction
            # shows - four separate rounded buttons read as four unrelated
            # controls with gaps, which is exactly how the first attempt at this
            # row looked.
            if filled:
                pygame.draw.rect(surface, _rgb(self.c["accent"]),
                                 item.rect, border_radius=self._u(1))
            label = self._clip(self._small_font,
                               item.extra.get("label", item.key),
                               _rgb(self.c["bg"] if filled
                                    else self.c["accent"] if (hot and not disabled)
                                    else self.c["muted"]),
                               item.rect.w - self._u(8))
            surface.blit(label, (item.rect.centerx - label.get_width() // 2,
                                 item.rect.centery - label.get_height() // 2))
            return
        pygame.draw.rect(surface,
                         _rgb(self.c["accent"] if filled else self.c["surface"]),
                         item.rect, border_radius=self._u(RADIUS // 2))
        pygame.draw.rect(surface,
                         _rgb(self.c["accent"] if (hot and not disabled) or filled
                              else self.c["border"]),
                         item.rect, self._u(1), border_radius=self._u(RADIUS // 2))
        # The line icon (Screenshot, Record) leads the caption and the two are
        # centred as ONE group: the icon is measured with the caption, or it
        # pushes the caption off the button's centre. `_draw_action` has the same
        # arithmetic for the footer - two drawers, one rule, because the two
        # kinds of button are drawn by different code.
        icon = item.extra.get("icon") if not small else None
        icon_w = self._u(17) + self._u(9) if icon else 0
        label = self._clip(self._small_font if small else self._font,
                           item.extra.get("label", item.key),
                           _rgb(self.c["bg"] if filled
                                else self.c["muted"] if disabled
                                else item.extra.get("color", self.c["text"])),
                           item.rect.w - self._u(12 if small else 16) - icon_w)
        left = item.rect.centerx - (icon_w + label.get_width()) // 2
        if icon:
            self._action_icon(surface, str(icon),
                              left + self._u(17) // 2, item.rect.centery)
        surface.blit(label, (left + icon_w,
                             item.rect.centery - label.get_height() // 2))
