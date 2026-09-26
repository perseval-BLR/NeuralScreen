"""What the menu shows, and what the config keeps.

Two directions of one subject. Out: the payload the overlay menu renders -
profiles, presets, sliders, monitors, hotkey captions, the GPU line and
whether neural rendering is really running on it. In: the two things a user
changes through the menu that have to survive a restart - the menu's own
position and size, and the hotkey assignments.

Product defaults live in config.default.json.  The neighbouring config.json is
the user's copy: it is created from those defaults on first launch and migrated
in place when the schema advances.  Both menu save paths keep using the atomic
writer rather than writing over the live file; a crash mid-write used to
truncate the config and lose every setting.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
import sys
import winreg
from pathlib import Path

from paths import BASE_DIR, DEFAULT_CONFIG_PATH
from capture import devicename_for_output_idx, list_adapters, list_monitors
from i18n import STRINGS as UI_STRINGS
# The work caps are the worker's contract, not a setting: the same two
# numbers size the shared motion buffer in the SHMI handshake.
from protocol import WORK_MAX_H, WORK_MAX_W  # noqa: F401
from winapi import list_capturable_windows, window_frame_rect
from resolution_limits import safe_processing_size


def _work_size(width: int, height: int, scale: float,
               nr_passes: int = 1) -> tuple[int, int]:
    """The NGX work resolution: scale of full, but no larger than
    WORK_MAX_W/H (NGX goes silent at 4K - the limit verified in isolation).

    Two rules that come from being bitten:

    At 1:1 the answer is the frame itself, with no rounding. Rounding to the
    nearest even number turned a 539-pixel-high window (900x500 plus its title
    bar) into a 540-high work size - larger than the frame - and the worker
    died on the header. An odd size at 1:1 stays on the legacy path, which is
    known to work (test_odd_frame_size).

    And a downscale rounds DOWN, never up: the work resolution must never
    exceed the frame it came from.
    """
    if scale >= 1.0:
        w, h = int(width), int(height)
    else:
        w = max(64, int(width * scale) // 2 * 2)
        h = max(64, int(height * scale) // 2 * 2)
    w, h = safe_processing_size(int(width), int(height),
                                min(w, int(width)), min(h, int(height)))
    if w > WORK_MAX_W or h > WORK_MAX_H:
        k = min(WORK_MAX_W / w, WORK_MAX_H / h)
        w = max(64, int(w * k) // 2 * 2)
        h = max(64, int(h * k) // 2 * 2)
    # The cascade needs a work size that DIFFERS from the frame. Its passes
    # ping-pong between two work-resolution scratch buffers and land in the
    # one the residual composite reads, and none of that exists at 1:1: the
    # network writes the full-res output directly there, so the worker forces
    # the count back to one (`v.nr_small = v.upscale && asked`, and then
    # `passes = (v.nr_small && v.nr_alt) ? v.passes_live : 1`). It said
    # nothing while it did it, so a panel showing four passes was driving one
    # (reported in #110), and until v2.0.2 the features for the other three
    # were built and discarded: measured 248 -> 853 MB of video memory at
    # 960x540, ~200 MB per pass. Since v2.0.2 they are not allocated, and the
    # worker's "NR cascade built" line says why a count falls short.
    #
    # So a pass count above one steps the work size down by the smallest even
    # amount that engages the residual path. This costs nothing and gains:
    # the composite keeps the NATIVE frame as the anchor and only adds what
    # the network changed, where 1:1 shows the network's own output wholesale.
    # Measured at 960x540 against the untouched source - detail as a share of
    # the source Laplacian variance:
    #
    #     work      passes  cascade   detail
    #     1:1            1   silent    0.77x
    #     1:1            4   silent    0.77x   (byte-identical to one pass)
    #     native-2       4      ran    0.88x
    #     0.85           4      ran    0.91x
    #
    # 1:1 is the WORST of them for detail, which is the opposite of what
    # "native" sounds like it should mean.
    if int(nr_passes or 1) > 1 and (w, h) == (int(width), int(height)):
        w = max(64, (int(width) - 2) // 2 * 2)
        h = max(64, (int(height) - 2) // 2 * 2)
        # A frame too small to step down from keeps its size (the floor in
        # resolution_limits wins); the cascade cannot run there, and the
        # worker's "NR cascade built" line says why.
        w, h = safe_processing_size(int(width), int(height), w, h)
    return min(w, int(width)), min(h, int(height))


def queued_small(st) -> bool:
    """The Boost state the user has ASKED for: the queued one, or the running.

    The apply is debounced (#115), so between the click and the apply the
    running state is still the old one. Anything that INVERTS or reads the
    switch during that window has to see the user's latest intent, not the
    state that is on its way out - two quick clicks on Boost both read
    `st.nr_small` as False and both asked to turn it ON, so the switch
    stopped toggling.
    """
    pending = getattr(st, "pending_apply", None)
    if pending is not None and len(pending) > 3 and pending[3] is not None:
        return bool(pending[3])
    return bool(getattr(st, "nr_small", False))


def cascade_passes(st) -> int:
    """The pass count to size the work by: the saved one under Boost, one
    without it.

    The cascade runs only in Boost's small-network mode, which is why the
    panel shows the count under the Boost switch and hides it with the switch
    off. The saved count still travels to the worker either way (it comes back
    with Boost), but sizing by it with Boost off stepped a native work size
    aside for passes that cannot run - and moved Boost-off users off the 1:1
    path for nothing.
    """
    if not getattr(st, "nr_small", False):
        return 1
    return int(getattr(st, "nr_passes", 1) or 1)


def hotkey_labels(bindings: dict) -> dict:
    """Bindings -> {command: "Num1"} for the captions on the menu buttons."""
    return {cmd: name for _mods, _vk, cmd, name in bindings.values()}

from i18n import STRINGS as UI_STRINGS


# The project page: README, hotkeys, requirements. Opened from the menu.
REPO_URL = "https://github.com/perseval-BLR/DLSS5-NeuralScreen"


CHANNEL_URL = "https://www.youtube.com/@perseval_BLR/videos"


PRESET_NAME_PREFIX = "Preset"


#: The themes the program offers, in the order the control shows them. One
#: list, because there were three copies of it (this validator, the startup
#: restore, the rebuild restore) and adding a theme to the menu left two of
#: them refusing it: a user who picked the new theme got the old one back
#: after every restart and every monitor switch. The menu control and the
#: action handler read THIS list too, so adding a theme is one edit.
#:
#: Named THEME_NAMES, not THEMES: overlay_ui.THEMES is the palette dict, and
#: two meanings of one name in the same import graph is how this class of
#: bug starts.
THEME_NAMES = ("light", "dark", "contrast")


# The global hotkeys live in hotkeys.py (RegisterHotKey). The layout and the
# reasons behind the combinations are in that module's docstring.
WORK_SCALE_STEP = 0.05


WORK_SCALE_MAX = 1.0


def _next_preset_name(presets: dict) -> str:
    """The first free "Preset N" name (Preset 1, Preset 2, ...)."""
    n = 1
    while f"{PRESET_NAME_PREFIX} {n}" in presets:
        n += 1
    return f"{PRESET_NAME_PREFIX} {n}"


def _set_autostart(enabled: bool) -> bool:
    """Enable/disable autostart with Windows (HKCU Run).

    We launch NeuralScreen.vbs through wscript - a hidden launcher with no
    console. Returns True on success.
    """
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        if enabled:
            vbs = str(BASE_DIR / "NeuralScreen.vbs")
            winreg.SetValueEx(key, "NeuralScreen", 0, winreg.REG_SZ,
                              f'wscript.exe "{vbs}"')
        else:
            try:
                winreg.DeleteValue(key, "NeuralScreen")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as exc:
        print(f"[main] autostart not configured: {exc}", file=sys.stderr)
        return False


# The version shown in the menu header. Kept in sync with native/launcher.rc
# (FileVersion/ProductVersion) and build_release_zip.py at release time.
APP_VERSION = "2.1.6"


# The channel label: the header shows the version, the channel lives in the
# settings page (user rule 2026-09-08).
CHANNEL_LABEL = "@perseval_BLR"


# --- DLSS 5 NR profiles (field order as in the converter) -----------------
#
# local_tone is half a point lower in every profile than it was through
# 1.8.1 (user, 13.09). The local tone mapping is the part that lifts
# shadows and flattens contrast, and at the old values it was doing more of
# that than the picture wanted - most visibly on dark scenes, where the
# brightening this program does anyway meets it head on. The four sliders
# still reach everything they reached: this moves where the profiles sit,
# not what the range allows.
#
# The profiles no longer carry `profile`, `preset` or `ui_correction`.
# Measured on the 310.8.0 runtime: every value of all three produces a
# byte-identical frame (colour-sweep-20260913, and tests/test_param_effect.py
# reports them every run). They are still sent - the wire layout is shared
# with the resize command and with a hundred tests - but they are sent as a
# fixed zero by the two packers, and nobody has to wonder about them again.
#
# Intensity is clamped at 1.00 inside NVIDIA's DLL, so the 1.65 and 2.50 the
# strong profiles used to ask for were the same picture as 1.00 all along.
# Extreme's local_structure comes down from 2.00 to the new 1.50 ceiling,
# which is the one real change here: measured, that is a detail metric of
# -14.4% against -13.5%, about a percent of the picture.
#
# The auto mask is on in every profile. Measured here on three real frames,
# a frozen input so anything moving between consecutive outputs is the
# network trembling rather than the picture changing:
#
#   source  mask   wiggle  shadows  peak   edges  detail  ms/frame
#   text       0    0.209    0.167    10   1.110   0.965      5.26
#   text       1    0.158    0.131    10   1.096   0.932      5.15
#   film       0    0.363    0.207     9   0.971   0.666      5.26
#   film       1    0.314    0.176     7   0.959   0.669      5.30
#   game       0    0.335    0.304     8   1.225   1.176      5.28
#   game       1    0.308    0.286     6   1.213   1.157      5.26
#
# It damps the trembling by 8-24% and the shadows by 6-22%, takes the peaks
# down (9->7, 8->6), and costs nothing in time - the per-frame figures are
# the same within noise. It is not free: on text it costs 3.4% of the fine
# detail. Text is also where it damps the most, and shimmering text is what
# people report, so that is the trade taken.
#
# The other reason is arithmetic: skin_structure is inert without it. With
# the mask off in two of the four profiles, the fourth slider in the menu
# did nothing at all in those two.
PROFILES = {
    "Faithful": dict(style=0, auto_mask=1,
                     intensity=0.70, local_tone=0.25, local_structure=0.75, skin_structure=-1.0),
    "Natural": dict(style=1, auto_mask=1,
                    intensity=1.00, local_tone=0.50, local_structure=1.00, skin_structure=-1.0),
    "Strong / Cinematic": dict(style=2, auto_mask=1,
                               intensity=1.00, local_tone=0.90, local_structure=1.50, skin_structure=1.0),
    "Extreme / Overdrive": dict(style=2, auto_mask=1,
                                intensity=1.00, local_tone=1.50, local_structure=1.50, skin_structure=1.5),
}


WORK_SCALE_MIN = 0.1


# How far each slider really reaches. One shared 0..2.5 was wrong in both
# directions: it promised travel that did nothing (issue #40, "low effect
# strength" - the user was turning a knob that had stopped answering), and
# it allowed values where the picture gets worse rather than stronger.
#
#   intensity        clamped at 1.0 inside NVIDIA's DLL. 1.0, 1.25, 1.5, 2
#                    and 2.5 all hash to the same frame (re-measured 15.09
#                    on 310.8.0 and again with the +0.5 tops request - still
#                    dead). The slider STAYS at 1.0: a longer travel would be
#                    the exact lie the range was rebuilt to remove.
#   tone, structure  the detail metric keeps climbing past 1.5, and so does
#                    the shimmer: the metric counts trembling noise as fine
#                    detail. Measured 15.09: 2.0 still moves the picture
#                    (distinct frames) and is a stronger look, so the top
#                    moved 1.5 -> 2.0 on request; shimmer at 2.0/2.0 is
#                    ~2.1 of 255 against ~1.5 at the old tops (test_param_effect
#                    pins the ceilings).
#   skin_structure   inert unless auto_mask is on, and -1 is "off".
#                    2.5 measured alive on 15.09; the top moved 2.0 -> 2.5.
PARAM_RANGE = {
    "intensity": (0.0, 1.0),
    "local_tone": (0.0, 2.0),
    "local_structure": (0.0, 2.0),
    "skin_structure": (-1.0, 2.5),
}


def param_range(key: str) -> tuple:
    """The (low, high) a parameter is allowed. Unknown keys get the widest."""
    return PARAM_RANGE.get(key, (0.0, 1.5))


def clamp_param(key: str, value: float) -> float:
    """Pull a value into range - for configs written before the range was."""
    lo, hi = param_range(key)
    return min(max(float(value), lo), hi)


# The keys a per-pass set carries. `style` is a whole-number 0/1/2 that picks
# WHICH network runs (see overlay_ui), the rest are the same four strengths the
# main sliders carry - so the two sets are written and read the same way.
PER_PASS_KEYS = ("intensity", "local_tone", "local_structure", "skin_structure")


def clean_per_pass(value) -> dict | None:
    """A per-pass parameter set, or None when there is not a usable one.

    None is the meaningful default and the common case: it means "passes 2..N
    use the main set", which is exactly what a worker does when nobody tells it
    anything. So a config that never had this key is not migrated, not
    defaulted, and not rewritten - it stays a config that says nothing, and the
    program behaves as it always did.

    Anything half-written is dropped whole rather than repaired: a set built
    from one usable number and three defaults would silently change the picture
    in a way nobody asked for, and the failure is invisible in the UI.

    Module level, not nested in validate_config like _fallback: startup reads
    the same key and would otherwise need a second copy of these rules.
    """
    if not isinstance(value, dict):
        return None
    try:
        style = int(value.get("style", 1))
    except (TypeError, ValueError):
        return None
    if style < 0 or style > 2:
        return None
    out = {"style": style}
    for key in PER_PASS_KEYS:
        if key not in value:
            return None
        try:
            out[key] = clamp_param(key, value[key])
        except (TypeError, ValueError):
            return None
    return out


# The four sliders a user preset stores. The same keys as PROFILES carries,
# minus the NGX plumbing (profile/preset/style/auto_mask/ui_correction stay
# tied to the built-in profile the preset was saved from).
PRESET_KEYS = ("intensity", "local_tone", "local_structure", "skin_structure")


DEFAULT_LANG = "en"


# Version 0 is every config written before config.default.json existed.  Keep
# migrations incremental so a future schema adds one small step instead of
# turning load_config() into a pile of unrelated compatibility checks.
CONFIG_SCHEMA_VERSION = 1

# Processing-rate limiter.  The named modes are stable config values; custom
# remains a separate number so switching to 30/60 and back does not erase it.
FRAME_LIMIT_MODES = ("30", "60", "custom", "unlimited")
FRAME_LIMIT_CUSTOM_MIN = 15
FRAME_LIMIT_CUSTOM_MAX = 240

# The conversion page's output choices, as config values. The same lists as
# media_convert's - repeated here rather than imported, because media_convert
# imports pipeline, which imports this module (tests/test_convert_settings
# holds the two copies together).
CONVERT_DESTS = ("source", "folder")
CONVERT_CODECS = ("auto", "av1", "hevc", "h264")
CONVERT_QUALITIES = ("high", "balanced", "small")
CONVERT_IMAGE_FORMATS = ("keep", "png", "jpg")


def frame_limit_fps(cfg: dict) -> int:
    """Resolve the persisted limiter mode to an FPS cap; zero is unlimited."""
    mode = str(cfg.get("frame_limit_mode", "unlimited"))
    if mode in ("30", "60"):
        return int(mode)
    if mode != "custom":
        return 0
    try:
        value = int(cfg.get("frame_limit_custom", 90))
    except (TypeError, ValueError, OverflowError):
        value = 90
    return min(FRAME_LIMIT_CUSTOM_MAX, max(FRAME_LIMIT_CUSTOM_MIN, value))


# The NGX plumbing a preset carries along with the four sliders: the range
# it must be in, and what to use when it is not there at all. Presets saved
# by builds up to 1.8.2 also carry profile/preset/ui_correction; those are
# read and thrown away, because they do nothing (see PARAM_RANGE). A preset
# saved by this build does not have them, and must still load.
_PRESET_INT_KEYS = {
    "style": (0, 2, 1),
    "auto_mask": (0, 1, 0),
}


def load_presets(cfg: dict) -> dict:
    """The user presets from the config, validated.

    A preset is a full params snapshot: the four sliders plus the style and
    the auto mask, so applying it reproduces the look it was saved with.
    A broken entry is dropped - it must neither take the program down nor
    be offered in the menu. A merely OLD entry is not broken: values wider
    than the ranges allow today are pulled in, and the three dead fields a
    pre-1.8.3 preset carries are ignored.
    """
    raw = cfg.get("presets")
    if not isinstance(raw, dict):
        return {}
    presets: dict = {}
    for name, values in raw.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(values, dict):
            continue
        clean = {}
        ok = True
        for key in PRESET_KEYS:
            if key not in values:
                ok = False
                break
            try:
                v = float(values[key])
            except (TypeError, ValueError, OverflowError):
                ok = False
                break
            if (isinstance(values[key], bool) or v != v
                    or v in (float("inf"), float("-inf"))):
                ok = False
                break
            # Out of range is an older build, not a broken preset: the
            # ranges shrank when they were measured, and a preset saved at
            # intensity 2.5 was already giving the picture 1.0 gives. It is
            # pulled in, not thrown away - losing someone's saved look over
            # a number that never did anything would be indefensible.
            clean[key] = clamp_param(key, v)
        if not ok:
            continue
        for key, (lo, hi, fallback) in _PRESET_INT_KEYS.items():
            v = values.get(key, fallback)
            if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
                ok = False
                break
            clean[key] = v
        if ok:
            presets[name.strip()] = clean
    return presets


def _read_config_object(path: Path, label: str) -> dict:
    """Read one JSON object without changing it.

    utf-8-sig: a file saved "with BOM" (Notepad's UTF-8 with BOM, PowerShell
    5.1's Set-Content -Encoding utf8) is still UTF-8, and plain utf-8 refused
    it outright - the error box said "Unexpected UTF-8 BOM" and never which
    file. A real JSON mistake now names the file and where it is.
    """
    with open(path, "r", encoding="utf-8-sig") as fh:
        try:
            value = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{label} is not valid JSON ({path}): {exc.msg} at line "
                f"{exc.lineno}, column {exc.colno}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label}: root must be an object")
    return value


def _schema_version(cfg: dict, label: str) -> int:
    """Return a strict non-negative schema version (missing means legacy 0)."""
    version = cfg.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError(f"{label}: schema_version must be a non-negative integer")
    return version


def _load_default_config() -> dict:
    """Load the shipped defaults and require them to match this executable."""
    defaults = _read_config_object(DEFAULT_CONFIG_PATH, "config.default.json")
    version = _schema_version(defaults, "config.default.json")
    if version != CONFIG_SCHEMA_VERSION:
        raise ValueError(
            "config.default.json: schema_version "
            f"{version} does not match application schema {CONFIG_SCHEMA_VERSION}"
        )
    return defaults


def _migrate_config(cfg: dict, defaults: dict) -> tuple[dict, bool]:
    """Migrate a user config without discarding keys unknown to this build.

    The v0 -> v1 migration overlays the complete old user object on the shipped
    defaults.  This fills fields introduced since the user's install while
    preserving user values, presets, hotkeys, and third-party/experimental
    keys.  Configs written by a newer application are rejected rather than
    silently downgraded.
    """
    version = _schema_version(cfg, "config.json")
    if version > CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"config.json: schema_version {version} is newer than supported "
            f"{CONFIG_SCHEMA_VERSION}"
        )

    migrated = deepcopy(cfg)
    changed = False
    while version < CONFIG_SCHEMA_VERSION:
        if version == 0:
            merged = deepcopy(defaults)
            merged.update(migrated)
            merged["schema_version"] = 1
            migrated = merged
            version = 1
            changed = True
            continue
        raise ValueError(f"config.json: no migration from schema_version {version}")

    # Defaults remain the authoritative list of known settings.  Filling a
    # missing field is safe even when the version marker is already current;
    # the user's complete object is overlaid last, so no value or unknown key
    # is replaced.
    merged = deepcopy(defaults)
    merged.update(migrated)
    if "menu_scale_auto" not in migrated:
        # A config written before the automatic fit existed. Whether it wants
        # one is answered by the size it carries: still at the shipped 1.0
        # means nobody ever adjusted it, and the fit is exactly what that
        # install is missing. Any other value was put there by hand or by the
        # old drag-the-corner resize, and belongs to the user - the fit must
        # not argue with it. Taking the default's `true` here would have
        # resized every existing install on its next launch.
        try:
            scale = float(merged.get("menu_scale", 1.0))
        except (TypeError, ValueError):
            scale = 1.0
        merged["menu_scale_auto"] = abs(scale - 1.0) < 1e-6
    if merged != migrated:
        changed = True
    return merged, changed


def _validate_config(cfg: dict) -> dict:
    """Validate and normalise an already migrated config object."""
    if not isinstance(cfg, dict):
        raise ValueError("config.json: root must be an object")
    required = {"monitor", "width", "height", "fullscreen", "warmup", "profile",
                "intensity", "local_tone", "local_structure", "skin_structure"}
    missing = required - set(cfg)
    if missing:
        raise ValueError(f"config.json: missing fields: {sorted(missing)}")
    if not isinstance(cfg["profile"], str):
        raise ValueError("config.json: field profile must be a string")
    if cfg["profile"] not in PROFILES:
        # A user preset name, or a stale reference to a deleted preset.
        # A stale reference must not take the program down - fall back to
        # the default profile (the menu still lists the surviving presets).
        if cfg["profile"] not in load_presets(cfg):
            print(f"[main] config.json: unknown profile {cfg['profile']!r}; "
                  f"falling back to 'Natural'", file=sys.stderr)
            cfg["profile"] = "Natural"
    for key in ("width", "height", "warmup"):
        if isinstance(cfg[key], bool) or not isinstance(cfg[key], int) or cfg[key] <= 0:
            raise ValueError(f"config.json: field {key} must be a positive integer")
    # Out of range is not an error any more, it is an old config. The
    # ranges shrank when they were measured (intensity 2.5 -> 1.0 and so
    # on), and a user who had 2.5 saved was already getting the picture 1.0
    # gives - refusing to start over a number that never did anything would
    # be the worst of both. Nonsense is still an error: "abc" is a broken
    # file, 2.5 is a file written by an older build.
    for key in ("intensity", "local_tone", "local_structure", "skin_structure"):
        if cfg[key] is None:
            continue
        if isinstance(cfg[key], bool):
            raise ValueError(f"config.json: field {key} must be a finite number")
        try:
            value = float(cfg[key])
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"config.json: field {key} must be a finite number")
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"config.json: field {key} must be a finite number")
        pulled = clamp_param(key, value)
        if pulled != value:
            print(f"[main] config.json: {key} {value:g} is outside the "
                  f"measured range {param_range(key)}; using {pulled:g} - "
                  f"the same picture the old value gave",
                  file=sys.stderr)
        cfg[key] = pulled
    # work_scale: 0.1..1.0 - the NGX processing resolution relative to the output
    try:
        scale = float(cfg.get("work_scale", 1.0))
    except (TypeError, ValueError, OverflowError):
        # A word or a dict here used to raise straight out of load_config, so
        # the launch died before a window existed (audit H5).
        print(f"[main] config.json: work_scale {cfg.get('work_scale')!r} is "
              f"not a number; using 1.0", file=sys.stderr)
        scale = 1.0
    if scale != scale or scale in (float("inf"), float("-inf")):
        scale = 1.0
    cfg["work_scale"] = min(WORK_SCALE_MAX, max(WORK_SCALE_MIN, scale))
    # lang: the language of the HUD/alerts/menu (en/ru, DEFAULT_LANG by default)
    lang = str(cfg.get("lang", DEFAULT_LANG))
    if lang not in UI_STRINGS:
        lang = DEFAULT_LANG
    cfg["lang"] = lang
    from motion_backend import normalize_backend
    cfg["motion_backend"] = normalize_backend(cfg.get("motion_backend"))
    cfg["frame_generation"] = bool(cfg.get("frame_generation", False))
    try:
        cfg["frame_multiplier"] = min(4, max(2, int(cfg.get("frame_multiplier", 2))))
    except (ValueError, TypeError, OverflowError):
        cfg["frame_multiplier"] = 2
    mode = str(cfg.get("frame_limit_mode", "unlimited"))
    cfg["frame_limit_mode"] = mode if mode in FRAME_LIMIT_MODES else "unlimited"
    try:
        custom = int(cfg.get("frame_limit_custom", 90))
    except (ValueError, TypeError, OverflowError):
        custom = 90
    cfg["frame_limit_custom"] = min(
        FRAME_LIMIT_CUSTOM_MAX, max(FRAME_LIMIT_CUSTOM_MIN, custom))
    for directory_key in ("recording_dir", "screenshot_dir", "convert_dir"):
        value = cfg.get(directory_key, "")
        cfg[directory_key] = value if isinstance(value, str) else ""
    screenshot_mode = str(cfg.get("screenshot_mode", "ask"))
    cfg["screenshot_mode"] = (screenshot_mode
                              if screenshot_mode in ("ask", "auto") else "ask")
    screenshot_format = str(cfg.get("screenshot_format", "png")).lower()
    cfg["screenshot_format"] = (screenshot_format
                                if screenshot_format in ("png", "jpg") else "png")
    # The conversion page. An unknown value is the first choice of its list -
    # the same default the page shows - not an error: these are output
    # preferences, and a hand-edited typo must not stop the program.
    for key, allowed in (("convert_dest", CONVERT_DESTS),
                         ("convert_codec", CONVERT_CODECS),
                         ("convert_quality", CONVERT_QUALITIES),
                         ("convert_image_format", CONVERT_IMAGE_FORMATS)):
        value = str(cfg.get(key, allowed[0])).lower()
        cfg[key] = value if value in allowed else allowed[0]
    # On unless the config really says off: a video that comes back silent
    # because a string was falsy would read as a broken converter.
    cfg["convert_audio"] = cfg.get("convert_audio", True) is not False

    # --- Values startup reads with a bare int()/float()/attribute access ----
    # (audit H5). The rule is the one this validator already uses for a stale
    # profile and a shrunken parameter range: a value that cannot be used
    # falls back to the default and says so. Refusing to start is reserved for
    # a file that is not a config at all - configure() runs before any window
    # exists, so a raise here is a modal box with a Python message in it and
    # not one word about which field is wrong, and the program does not start
    # until the user hand-edits the file again.
    def _fallback(key: str, value, default, why: str) -> None:
        print(f"[main] config.json: {key} {value!r} {why}; using {default!r}",
              file=sys.stderr)
        cfg[key] = default

    def _as_int(key: str, value, default, *, minimum=None):
        """The value as an int, or the default when it cannot be one."""
        if value is None:
            return default
        if isinstance(value, bool) or isinstance(value, (dict, list, tuple)):
            _fallback(key, value, default, "is not a number")
            return default
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            _fallback(key, value, default, "is not a number")
            return default
        if minimum is not None and number < minimum:
            _fallback(key, value, default, f"is below {minimum}")
            return default
        return number

    # monitor: the DXGI devicename (a str, resolved by startup) or an output
    # index. Anything else reached `int(monitor_cfg)` and took the launch down.
    monitor = cfg.get("monitor", 0)
    if isinstance(monitor, str):
        pass                                  # resolve_output_idx handles it
    elif monitor is None or isinstance(monitor, (bool, dict, list, tuple)) or \
            not isinstance(monitor, int):
        try:
            monitor = int(monitor)            # a numeric string is fine
        except (TypeError, ValueError, OverflowError):
            _fallback("monitor", cfg.get("monitor"), 0,
                      "is neither a devicename nor an output index")
            monitor = 0
    cfg["monitor"] = monitor

    # gpu: an adapter index, or None to let the worker choose. The worker's
    # NS_GPU string is built with str(int(gpu)).
    gpu = cfg.get("gpu")
    if gpu is not None:
        if isinstance(gpu, bool) or isinstance(gpu, (dict, list, tuple)):
            _fallback("gpu", gpu, None,
                      "is not an adapter index (use null to let the worker "
                      "choose)")
        else:
            try:
                cfg["gpu"] = int(gpu)
            except (TypeError, ValueError, OverflowError):
                _fallback("gpu", gpu, None,
                          "is not an adapter index (use null to let the "
                          "worker choose)")

    # menu_offset: the saved [x, y] pair, read as [int(...), int(...)].
    offset = cfg.get("menu_offset")
    if offset is not None:
        ok = (isinstance(offset, (list, tuple)) and len(offset) == 2
              and not any(isinstance(v, (dict, list, tuple, bool))
                          for v in offset))
        if ok:
            try:
                cfg["menu_offset"] = [int(offset[0]), int(offset[1])]
            except (TypeError, ValueError, OverflowError):
                ok = False
        if not ok:
            _fallback("menu_offset", offset, [0, 0],
                      "is not an [x, y] pair - the panel is placed at the "
                      "default position")

    # menu_scale / menu_height: the panel's own size, read with float()/int().
    menu_scale = cfg.get("menu_scale", 1.0)
    if isinstance(menu_scale, bool) or isinstance(menu_scale, (dict, list, tuple)):
        _fallback("menu_scale", menu_scale, 1.0, "is not a number")
        menu_scale = 1.0
    else:
        try:
            menu_scale = float(menu_scale)
        except (TypeError, ValueError, OverflowError):
            _fallback("menu_scale", menu_scale, 1.0, "is not a number")
            menu_scale = 1.0
    if menu_scale != menu_scale or menu_scale in (float("inf"), float("-inf")):
        menu_scale = 1.0
    cfg["menu_scale"] = min(3.0, max(0.5, menu_scale))
    # Whether menu_scale is still the automatic choice. True until the user
    # picks a step; from then on their size is theirs and the fit never runs
    # again. Anything that is not a real boolean is treated as "already
    # chosen": guessing "please resize my interface" from a malformed value is
    # the worse mistake of the two.
    cfg["menu_scale_auto"] = cfg.get("menu_scale_auto") is True
    # The NR cascade: how many passes run over one frame. An experiment, off
    # (that is, one pass) unless asked for - each extra pass is another full
    # evaluation, so two passes cost about half the frame rate.
    try:
        passes = int(cfg.get("nr_passes", 1))
    except (TypeError, ValueError, OverflowError):
        # OverflowError too: 1e999 in JSON is an infinite float, and int() of
        # it raised past this handler and out of the launch.
        _fallback("nr_passes", cfg.get("nr_passes"), 1, "is not a whole number")
        passes = 1
    if passes < 1 or passes > 4:
        _fallback("nr_passes", cfg.get("nr_passes"), 1, "is not 1-4")
        passes = 1
    cfg["nr_passes"] = passes
    # What passes 2..N should use, if the user gave them their own set. A
    # missing key means "the main set for every pass", which is what every
    # build before this one did - so an untouched config is untouched.
    # Validated here as well as read at startup: a hand-edited config with a
    # string where a float belongs would otherwise reach the wire, where a
    # malformed struct is a desync, not a wrong value.
    if cfg.get("nr_pass_params") is not None:
        clean = clean_per_pass(cfg["nr_pass_params"])
        if clean is None:
            _fallback("nr_pass_params", cfg["nr_pass_params"], None,
                      "is not a usable parameter set")
            cfg.pop("nr_pass_params", None)
        else:
            cfg["nr_pass_params"] = clean
    # #93: what the taskbar's minimise and close buttons mean. Booleans, and
    # False unless the config really says otherwise - a program that vanishes
    # into the tray because a string was truthy would look like a crash.
    cfg["tray_on_minimise"] = cfg.get("tray_on_minimise") is True
    cfg["tray_on_close"] = cfg.get("tray_on_close") is True
    if cfg.get("fps_overlay") not in ("off", "tl", "tr", "bl", "br"):
        _fallback("fps_overlay", cfg.get("fps_overlay"), "off",
                  "is not one of off/tl/tr/bl/br")
        cfg["fps_overlay"] = "off"

    menu_height = cfg.get("menu_height")
    if menu_height is not None:
        cfg["menu_height"] = _as_int("menu_height", menu_height, None,
                                     minimum=1)

    # menu_mini: a plain switch. menu_mini_rows: the keys mini mode keeps,
    # and an empty list is a real answer (a panel of nothing but the action
    # strip), so only a non-list is refused.
    mini = cfg.get("menu_mini")
    if mini is not None and not isinstance(mini, bool):
        _fallback("menu_mini", mini, None, "is not true or false")
    rows = cfg.get("menu_mini_rows")
    if rows is not None:
        if not isinstance(rows, list) or not all(isinstance(r, str)
                                                 for r in rows):
            _fallback("menu_mini_rows", rows, None, "is not a list of names")

    # theme: one of THEME_NAMES, anything else is the default.
    theme = cfg.get("theme")
    if theme is not None and theme not in THEME_NAMES:
        _fallback("theme", theme, None, "is not a known theme")

    # hotkeys: a {command: "Ctrl+Alt+Q"} mapping, read by build_bindings with
    # .get() per value and .strip() on each. A list or a bare string used to
    # reach it and raise an AttributeError out of startup.
    hotkeys = cfg.get("hotkeys")
    if hotkeys is not None:
        clean: dict = {}
        usable = isinstance(hotkeys, dict)
        if usable:
            for command, binding in hotkeys.items():
                if not isinstance(command, str) or not isinstance(binding, str):
                    usable = False
                    break
                clean[command] = binding
        if usable:
            if clean != hotkeys:
                cfg["hotkeys"] = clean
        else:
            _fallback("hotkeys", hotkeys, {},
                      "is not a {command: combination} mapping - the default "
                      "bindings are used")

    # The wipe position. Never validated before: startup reads it with
    # float(), and a null or a word there took the launch down.
    split = cfg.get("split", 0.0)
    try:
        split = float(split)
    except (TypeError, ValueError, OverflowError):
        _fallback("split", cfg.get("split"), 0.0, "is not a number")
        split = 0.0
    if split != split:
        split = 0.0
    cfg["split"] = min(1.0, max(0.0, split))

    # The switches the program reads with bool(). The string "false" is
    # truthy, so a hand-edited "hdr": "false" turned HDR ON. A string that
    # spells a boolean is read as one; anything else that is not a boolean or
    # a number goes back to the shipped default. (tray_on_* and
    # menu_scale_auto are read with `is True` and need none of this.)
    for key in _BOOL_KEYS:
        value = cfg.get(key)
        if value is None or isinstance(value, (bool, int, float)):
            continue
        if isinstance(value, str):
            word = value.strip().lower()
            if word in ("true", "1", "yes", "on"):
                cfg[key] = True
                continue
            if word in ("false", "0", "no", "off", ""):
                cfg[key] = False
                continue
        _fallback(key, value, _shipped_default(key, False), "is not true or false")
    return cfg


#: The config switches read as booleans (config.default.json holds each one).
_BOOL_KEYS = (
    "fullscreen", "worker_present", "motion_on_gpu", "capture_in_worker",
    "pixels_in_shm", "nr_small", "nr_direct", "frame_generation",
    "record_audio", "rec_indicator", "gpu_record", "convert_audio", "spout",
    "hdr", "open_menu_on_start",
)


def _shipped_default(key: str, fallback):
    """One value of config.default.json, or `fallback` when it cannot be read."""
    try:
        return _read_config_object(DEFAULT_CONFIG_PATH,
                                   "config.default.json").get(key, fallback)
    except Exception:
        return fallback


def load_config(path: Path) -> dict:
    """Load, safely migrate, validate, and if needed create config.json.

    Migration is persisted only after the complete candidate validates.  The
    atomic writer therefore leaves an invalid, future-version, or interrupted
    user config byte-for-byte intact.
    """
    path = Path(path)
    defaults = _load_default_config()
    normalized_defaults = _validate_config(deepcopy(defaults))
    if normalized_defaults != defaults:
        raise ValueError(
            "config.default.json: values must already be in canonical form")

    existed = path.is_file()
    raw = (_read_config_object(path, "config.json") if existed else {})
    migrated, changed = _migrate_config(raw, defaults)
    validated = _validate_config(deepcopy(migrated))
    if not existed or changed:
        _atomic_write_json(path, migrated)
    return validated


def resolve_params(cfg: dict) -> dict:
    """Profile + custom NR parameters from the config (null = use the profile).

    A user preset is a full params snapshot and wins over the built-in
    profile it was saved from; the per-key overrides below still apply on
    top (they are the live slider values).
    """
    if cfg["profile"] in PROFILES:
        params = dict(PROFILES[cfg["profile"]])
        # A built-in profile never picks the model any more (user rule
        # 15.09): profiles move the four sliders, the model is its own
        # control. A fresh config with no style key gets Natural; a saved
        # one keeps whatever the user chose (the override below).
        params["style"] = 1
    else:
        params = dict(load_presets(cfg).get(cfg["profile"], PROFILES["Natural"]))
        # A user preset DOES carry the model it was saved with - that is
        # what "save preset" promises.
    # The live style overrides either source: it is the value the user
    # last set in the menu, and it is saved on every menu close.
    style = cfg.get("style")
    if isinstance(style, int) and not isinstance(style, bool) and 0 <= style <= 2:
        params["style"] = style
    for key in ("intensity", "local_tone", "local_structure", "skin_structure"):
        # Saved presets are written by whatever build the user had, so they
        # are pulled into range here as well - validate_config only sees the
        # live slider values, not the presets behind them.
        if key in params:
            params[key] = clamp_param(key, params[key])
        value = cfg.get(key)
        if value is not None:
            params[key] = clamp_param(key, value)
    return params


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write data to path atomically: a temp file in the same directory,
    flushed and fsynced, then os.replace() over the target.

    A crash mid-write used to truncate config.json in place and the program
    lost the user's settings. The temp file lives next to the target so the
    replace is a rename within one volume - atomic on Windows. On failure the
    temp file is removed and the original is left untouched.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _autostart_enabled() -> bool:
    """Is autostart on FOR THIS COPY? (HKCU Run, the NeuralScreen value).

    The value names the folder it was switched on from. Asking only whether
    it exists said "on" in a copy unpacked somewhere else, while the OLD copy
    was the one that started at logon (and a second instance then just
    exits) - or a Windows Script Host error came up once the old folder was
    gone. A value that points at another folder reads as off here, and
    switching it on points it at this one.
    """
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_READ)
        try:
            value, _kind = winreg.QueryValueEx(key, "NeuralScreen")
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False
    ours = os.path.normcase(str(BASE_DIR / "NeuralScreen.vbs"))
    return ours in os.path.normcase(str(value))


def _menu_layout_payload(cfg: dict, params: dict, monitor: int, lang: str,
                         work_scale: float, split_pos: float,
                         startup_menu: bool, nr_small: bool, menu) -> dict:
    """The settings _save_menu_layout persists into config.json.

    Everything the user can change in the menu: the panel geometry, the
    processing settings and the NR parameters. profile/params/monitor are
    included because the menu changes them in memory only (cfg/params are
    updated live) - without this save they would be lost on the next launch.

    monitor is saved as the DXGI devicename (e.g. '\\\\.\\DISPLAY1') so the
    saved monitor keeps pointing at the same physical display when the
    arrangement changes; old configs with a positional int still load.
    """
    monitor_name = devicename_for_output_idx(int(monitor))
    return {
        "menu_scale": round(menu.user_scale, 2),
        # Written from the config, not from the menu: the menu has no opinion
        # about whether its size was chosen or fitted.
        "menu_scale_auto": bool(cfg.get("menu_scale_auto", False)),
        "menu_height": (None if menu.user_height is None
                        else int(menu.user_height)),
        "open_menu_on_start": startup_menu,
        "split": round(split_pos, 2),
        "nr_small": bool(nr_small),
        "work_scale": round(work_scale, 2),
        "theme": menu.state.get("theme", "light"),
        # Mini mode and the rows it keeps. The CHOOSING state is not saved:
        # it is a thing you are doing, not a thing you have set.
        "menu_mini": bool(getattr(menu, "mini", False)),
        "menu_mini_rows": sorted(getattr(menu, "mini_rows", ()) or ()),
        "lang": lang,
        "menu_offset": [int(menu.offset[0]), int(menu.offset[1])],
        "profile": cfg["profile"],
        "intensity": params["intensity"],
        "local_tone": params["local_tone"],
        "local_structure": params["local_structure"],
        "skin_structure": params["skin_structure"],
        # Style is a user choice now, not a property of the profile, so it
        # has to survive a restart like the four sliders do.
        "style": int(params.get("style", 1)),
        "monitor": monitor_name if monitor_name is not None else int(monitor),
        "rec_indicator": bool(cfg.get("rec_indicator", True)),
        "gpu_record": cfg.get("gpu_record", True) is not False,
        "fps_overlay": str(cfg.get("fps_overlay", "off")),
        "nr_passes": int(cfg.get("nr_passes", 1)),
        # What passes 2..N use, only when the user actually set it. Absent from
        # the file means "every pass uses the main set" - writing a default set
        # here would turn "I never touched this" into a set that pins the
        # second pass to whatever the sliders happened to be at that moment.
        **({"nr_pass_params": clean} if (
            clean := clean_per_pass(cfg.get("nr_pass_params"))) else {}),
        "tray_on_minimise": bool(cfg.get("tray_on_minimise", False)),
        "tray_on_close": bool(cfg.get("tray_on_close", False)),
        "recording_dir": cfg.get("recording_dir") or "",
        "screenshot_dir": cfg.get("screenshot_dir") or "",
        "screenshot_mode": (str(cfg.get("screenshot_mode", "ask"))
                            if str(cfg.get("screenshot_mode", "ask"))
                            in ("ask", "auto") else "ask"),
        "screenshot_format": (str(cfg.get("screenshot_format", "png"))
                              if str(cfg.get("screenshot_format", "png"))
                              in ("png", "jpg") else "png"),
        # The conversion page's output choices: a preference, like the
        # screenshot format, and just as annoying to set again every launch.
        "convert_dest": _choice(cfg, "convert_dest", CONVERT_DESTS),
        "convert_dir": cfg.get("convert_dir") or "",
        "convert_codec": _choice(cfg, "convert_codec", CONVERT_CODECS),
        "convert_quality": _choice(cfg, "convert_quality", CONVERT_QUALITIES),
        "convert_image_format": _choice(cfg, "convert_image_format",
                                        CONVERT_IMAGE_FORMATS),
        "convert_audio": cfg.get("convert_audio", True) is not False,
        # The Spout2 bridge choice must survive a restart: the worker
        # reads NS_SPOUT at startup, and main sets it from this flag.
        "spout": bool(cfg.get("spout", False)),
        # HDR compatibility, the same hand-off: the worker reads NS_HDR at
        # startup and main sets it from this flag. Experimental, off.
        "hdr": bool(cfg.get("hdr", False)),
        "motion_backend": cfg.get("motion_backend", "nvofa"),
        # Which card runs the network and the capture. An index, as
        # DXGI enumerates adapters - the same number the worker takes
        # in NS_GPU and prints in its "[host] adapter N" lines.
        # null is a value of its own - "let the worker choose" - and the
        # validator also turns an unreadable value into it. int(None) made
        # every save raise, so the layout, the theme and the presets were
        # lost on each close (the error was swallowed below).
        "gpu": _optional_index(cfg.get("gpu")),
        # Adapters whose worker could not bring the neural pass up. Kept so
        # the picker can mark them after a restart too; cleared per adapter
        # as soon as one of them works (issue #33).
        "gpu_no_nr": _index_list(cfg.get("gpu_no_nr")),
        "frame_generation": bool(cfg.get("frame_generation", False)),
        "frame_multiplier": min(4, max(2, int(cfg.get("frame_multiplier", 2)))),
        "frame_limit_mode": (str(cfg.get("frame_limit_mode", "unlimited"))
                             if str(cfg.get("frame_limit_mode", "unlimited"))
                             in FRAME_LIMIT_MODES else "unlimited"),
        "frame_limit_custom": min(
            FRAME_LIMIT_CUSTOM_MAX,
            max(FRAME_LIMIT_CUSTOM_MIN, int(cfg.get("frame_limit_custom", 90)))),
        # The user's saved presets. Without this key "Save preset" wrote
        # everything EXCEPT the preset: the menu said "Preset saved", the
        # save really did succeed, and the preset was gone on the next
        # launch - the code's own "it will not survive a restart" branch
        # could never fire, because nothing had failed (audit F1).
        "presets": (dict(cfg["presets"]) if isinstance(cfg.get("presets"), dict)
                    else {}),
    }


def _optional_index(value) -> int | None:
    """An adapter index, or None for "the worker chooses" (and for junk)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _index_list(value) -> list:
    """The integer entries of a list; anything else is dropped, not fatal."""
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        index = _optional_index(item)
        if index is not None:
            out.append(index)
    return out




def work_scale_cap(st) -> float:
    """The scale above which the work size just hits the NGX cap.

    Rounded down to the slider's own step so the value is reachable:
    a cap the slider cannot land on exactly would leave the top of the
    range doing nothing, which is the whole thing being fixed here.
    """
    raw = min(1.0, WORK_MAX_W / max(1, st.width), WORK_MAX_H / max(1, st.height))
    return max(0.35, int(raw / 0.05) * 0.05)


#: What the worker says about the neural pass, in its own words. ONE set,
#: read by both places that ask: settings_io.refresh_gpu_ok (which drives
#: the dot in the menu and the alert) and pipeline.gpu_came_up (which
#: decides whether a GPU switch is kept or reverted). They used to carry a
#: token list each, and the lists had already drifted apart - a rename in
#: the worker would have blinded one of them and left the other working,
#: which is the worst shape for a bug like this to take.
NR_VERDICT_OK = ("feature 18 ready",)
NR_VERDICT_FAIL = ("feature 18 create failed",   # [pure], the direct refusal
                   "NR feature unavailable",     # [video], SAFE PASSTHROUGH
                   "NGX unavailable",            # [host], nothing came up
                   "no NVIDIA adapter found")    # [host], nothing to run on


def nr_verdict(lines):
    """True / False / None from the worker's log lines, NEWEST FIRST.

    None means the worker has not said yet - which is not a failure. A card
    that takes its time still works, and treating silence as a refusal
    would be worse than the bug the callers guard against.
    """
    for line in lines:
        if any(token in line for token in NR_VERDICT_OK):
            return True
        if any(token in line for token in NR_VERDICT_FAIL):
            return False
    return None


def refresh_gpu_ok(st) -> None:
    """Whether NR works - from the worker's answer, not the architecture.

    Only the worker knows for sure: it calls CreateFeature and gets
    the NGX code back. The architecture only tells us what NVIDIA
    promises. Once decided, the answer is not revisited - worker
    restarts add lines but the verdict does not change.
    """
    if st.gpu_ok is not None:
        return
    # The refusal lines are named once, in nr_verdict: the real one from the
    # worker is "[pure] direct feature 18 create failed" ("Unsupported GPU
    # architecture" lives inside nvngx_dlssnr.dll and never reaches its
    # stderr), and SAFE PASSTHROUGH is the same verdict - the worker stays
    # alive and shows the raw frame, so: no feature, no NR.
    verdict = nr_verdict(reversed(st.worker_logs[-80:]))
    if verdict is None:
        return
    st.gpu_ok = verdict
    if not verdict:
        # The red dot alone was not enough: in issue #29 the user picked a
        # card that cannot run the pass and nothing on screen said so. One
        # alert per verdict - a fresh worker clears gpu_ok and the alert can
        # speak again.
        if not st.gpu_alerted:
            st.gpu_alerted = True
            # Eight seconds, not the usual two and a half. This is not a
            # notification that something was applied - it is the reason the
            # picture will look untouched for the rest of the session, and
            # the people who reported it as a black screen had not seen it
            # at all. The standing version of the same fact is in the menu's
            # status line, for whoever looks later.
            st.display.alert(UI_STRINGS[st.lang].get(
                "gpu_nr_fail",
                "This GPU cannot run the neural pass - the picture stays "
                "unprocessed"), 8.0)


# FG's own refusal lines, the same shape as NR's: the worker says what the
# runtime answered. Init_Ext logs its HRESULT on every attempt, so success
# must be excluded by VALUE (NVSDK_NGX_Result_Success is 0x00000001).
FG_VERDICT_FAIL = ("[fg] CreateFeature failed",
                   "[fg] nvngx_dlssg.dll load failed",
                   "[fg] presenter failed")


def fg_verdict(lines):
    """True / False / None from the worker's FG lines, NEWEST FIRST.

    Only lines NEWER than the last "[fg] UI: on" marker count: a refusal
    from an earlier, already-handled attempt must not flip the switch
    again. Success is the presenter's own "[fg] Nx enabled at ..." line.
    None means the runtime has not answered yet.

    A REFUSED MULTIPLIER is not a refusal of the feature. The worker steps
    the multiplier down and rebuilds at the lower step (2x is the floor), so
    the "Nx refused ... stepping down" and "CreateFeature failed" lines that
    precede a working build must not turn the switch off - the switch would
    go dark on a card that runs Frame Generation perfectly well. The worker
    logs "stepping down" exactly for that case, and the step it lands on
    still prints its own "enabled at" line, which wins because it is newer.
    """
    for line in lines:
        if "[fg] UI: on" in line:
            return None            # just enabled - no answer yet
        if "[fg] UI: off" in line:
            return None            # disabled - nothing to judge
        if "enabled at" in line and "[fg]" in line:
            return True            # "[fg] 2x enabled at 3840x2160"
        if "stepping down to" in line:
            return None            # a retry is in flight, not a verdict
        if "[fg] Init_Ext -> 0x" in line and "0x00000001" not in line:
            return False           # the FG runtime itself refused
        if any(token in line for token in FG_VERDICT_FAIL):
            return False
    return None


def refresh_fg_ok(st) -> None:
    """The FG switch reflects reality (issue #76).

    When the FG runtime refuses on this card the switch used to stay ON
    while nothing interpolated - no rate split in the header, no reason
    anywhere on screen. The worker knows (it logs the refusal), so on a
    refusal the switch goes back off with an alert naming the reason.
    Once per attempt: flipping the switch back on arms the alert again.
    """
    if not bool(st.cfg.get("frame_generation", False)):
        return
    if getattr(st, "fg_alerted", False):
        return
    verdict = fg_verdict(reversed(st.worker_logs[-200:]))
    if verdict is not False:
        return
    st.fg_alerted = True
    st.cfg["frame_generation"] = False
    save_menu_layout(st)
    # Eight seconds like the NR refusal: this is the reason frame
    # generation will not appear at all, not a notification that something
    # was applied.
    st.display.alert(UI_STRINGS[st.lang].get(
        "fg_fail",
        "Frame Generation could not start on this GPU - the switch is "
        "back off"), 8.0)


def warn_hdr(st) -> None:
    """Warn only when an HDR display actually uses the SDR capture path.

    Display discovery precedes the first frame. Wait for its capture format:
    FP16 scRGB uses an SDR neural proxy and preserves the original HDR signal.
    """
    if st.hdr_alerted:
        return
    capture_sdr = None
    for line in reversed(st.worker_logs):
        if capture_sdr is None and "[hdr] capture=" in line:
            capture_sdr = "capture=SDR;" in line
        if "[dda] output colour space " in line:
            if "HDR IS ON for the captured display" not in line or capture_sdr is not True:
                return
            st.hdr_alerted = True
            st.display.alert(UI_STRINGS[st.lang].get(
                "hdr_on",
                "HDR display is using SDR capture. HDR brightness and colours "
                "are not preserved."), duration=6.0)
            print("[main] HDR display is using SDR capture; HDR is not preserved")
            return


def _no_nr(st) -> set:
    """Adapter indices whose worker could not bring the neural pass up."""
    try:
        return {int(i) for i in (st.cfg.get("gpu_no_nr") or [])}
    except (TypeError, ValueError):
        return set()


def _gpu_label(index) -> str:
    """"<dxgi index>: <name>" for the picker - the card that will really run.

    The value in the config is a DXGI index and it can name something that
    is not an NVIDIA card (a hybrid laptop's integrated GPU sits at 0, which
    is the shipped default) or nothing at all. The worker treats the index
    as a wish and falls back to the first usable card; the menu has to agree
    with it, or the picker shows an empty field on the machines where the
    setting matters most (issue #34).
    """
    adapters = list_adapters()
    if not adapters:
        return ""
    try:
        wanted = int(index)
    except (TypeError, ValueError):
        wanted = None
    for i, name in adapters:
        if i == wanted:
            return f"{i}: {name}"
    i, name = adapters[0]
    return f"{i}: {name}"



def _fg_displayed_fps(st) -> float | None:
    """The frame rate the presenter actually shows (real + generated).

    The worker reports it every two seconds - "[fg] displayed 87.1 FPS
    (real + generated, 2x)". The pipeline counter stays the honest network
    rate; this is what the screen really shows with Frame Generation on.
    None while FG is off.
    """
    for line in reversed(st.worker_logs[-200:]):
        if "[fg] displayed" in line:
            try:
                return float(line.split("displayed ", 1)[1].split(" FPS", 1)[0])
            except (ValueError, IndexError):
                return None
        # A marker line for FG-off resets the reading - the toggle logs one.
        if "[fg] UI: off" in line:
            return None
    return None


def _fg_active_multiplier(st) -> int | None:
    """The multiplier the presenter is REALLY running, or None while unknown.

    A card whose runtime stops at 2x answers a request for 3x/4x with a
    refusal, and the worker steps the multiplier down instead of failing
    (issue #100). The panel then showed the user's pick while a lower step
    ran, so the only way to notice was the FPS counter. The presenter names
    the step it runs in the same line as the rate, which makes this the
    honest source - it reports what happened, not what was requested.
    """
    for line in reversed(st.worker_logs[-200:]):
        if "[fg] displayed" in line:
            try:
                tail = line.split("real + generated", 1)[1]
                value = tail.split(",", 1)[1].split("x", 1)[0].strip()
                return int(value)
            except (ValueError, IndexError):
                return None
        if "[fg] UI: off" in line:
            return None
    return None



def _window_menu_state(windows: list[tuple[int, str]],
                       current_hwnd: int | None) -> tuple[list[dict], dict | None]:
    """Build the window-picker payload without mixing identity into labels.

    HWND remains an integer through hover and selection.  Titles are display
    text only, so duplicate titles and titles containing colons are safe.
    """
    # The size travels with the row: it is what decides whether a pick makes
    # sense (a 640x480 window upscaled to 4K is a different proposition from a
    # fullscreen game), and the menu cannot ask Windows for it later without
    # re-reading the list - which is the thing the freeze exists to prevent.
    entries = []
    for hwnd, title in windows:
        size = ""
        try:
            rect = window_frame_rect(int(hwnd))
            if rect is not None:
                size = f"{rect[2]}\u00d7{rect[3]}"
        except Exception:
            # A window that closed between enumerating and measuring keeps its
            # row and loses only the number.
            size = ""
        entries.append({"hwnd": int(hwnd), "title": str(title), "size": size})
    current = next((dict(entry) for entry in entries
                    if entry["hwnd"] == current_hwnd), None)
    return entries, current


def _choice(cfg: dict, key: str, allowed: tuple) -> str:
    """The config's value for one of the page's fixed choices, or the first."""
    value = str(cfg.get(key, allowed[0]))
    return value if value in allowed else allowed[0]


def _convert_rows(st) -> list:
    """The conversion queue's rows, ready to draw, or none without a queue.

    Each row also carries its second line (`line`, `tone`) and the one
    button it offers (`action`), worded in the panel's language here - so
    the menu draws them without importing the queue (convert_jobs pulls in
    the converter and the pipeline, and the menu is a leaf).
    """
    queue = getattr(st, "convert_queue", None)
    if queue is None:
        return []
    import convert_jobs   # here, not at the top: it imports pipeline, which imports us
    strings = UI_STRINGS.get(getattr(st, "lang", DEFAULT_LANG),
                             UI_STRINGS[DEFAULT_LANG])
    try:
        rows = queue.rows()
    except Exception:
        return []
    for row in rows:
        row["line"], row["tone"] = convert_jobs.status_line(row, strings)
        row["action"] = convert_jobs.row_action(row)
    return rows


def menu_payload(st) -> dict:
    """The current state for the menu - a single source of truth."""
    refresh_gpu_ok(st)
    # The list is FROZEN while the page that shows it is open, and sorted by
    # title rather than by z-order.
    #
    # EnumWindows answers in z-order, this payload is rebuilt on every frame
    # the menu is up, and z-order changes whenever anything takes the focus -
    # including the window the pointer is travelling towards. So the rows
    # re-ordered under the cursor between the hover and the click, and the
    # click landed on whatever had moved into that position: picked
    # WireGuard, got Claude (user, 13.09).
    #
    # Frozen means frozen: a window that appears or closes while the list is
    # up does not shuffle the rows either. Closing the page and opening it
    # again takes a fresh reading - which is the only way to take one, and
    # enough: the picker is a few clicks, not a live monitor.
    _menu = getattr(getattr(st, "display", None), "menu", None)
    if getattr(_menu, "page", "") == "windows" and getattr(st, "window_list", None):
        wins = st.window_list
    else:
        wins = sorted(list_capturable_windows(),
                      key=lambda hw: (str(hw[1]).casefold(), hw[0]))
        st.window_list = wins
    window_entries, current_window = _window_menu_state(
        wins, getattr(st, "window_hwnd", None))
    # The devicename is the stable identity: the menu hands it back
    # on a switch, so a reorder cannot redirect the capture.
    monitor_entries = [f"{i}: {w}x{h} ({dev})"
                       for i, w, h, dev in list_monitors()]
    active_recorder = (getattr(st, "recorder", None)
                       or getattr(st, "recording_finalizer", None))
    last_recording = dict(getattr(st, "last_recording", None) or {})
    if active_recorder is not None:
        rec_status = getattr(active_recorder, "status", "recording")
        last_recording = {
            "container": "MP4",
            "codec": str(getattr(active_recorder, "codec", "unknown")),
            "fps": float(getattr(active_recorder, "fps", 0.0)),
            "audio": bool(getattr(active_recorder, "audio_enabled", False)),
            "path": str(getattr(active_recorder, "result_path", None)
                        or getattr(active_recorder, "path", "")),
            "status": str(getattr(rec_status, "value", rec_status)),
        }
    rec_detail = ""
    if last_recording:
        rec_detail = (f"{last_recording.get('container', 'MP4')} · "
                      f"{last_recording.get('codec', 'unknown')} · "
                      f"{float(last_recording.get('fps', 0)):g} fps · "
                      f"{'AAC' if last_recording.get('audio') else 'no audio'}")
    compatibility = getattr(st, "compatibility_result", None)
    compatibility_status = (
        str(getattr(getattr(compatibility, "status", None), "value", "not_run"))
        if compatibility is not None else "not_run"
    )
    compatibility_score = ""
    if compatibility is not None:
        compatibility_score = (
            f"{int(compatibility.passed)}/{int(compatibility.expected)}"
            if compatibility.is_pass
            else f"{int(compatibility.passed)}/{int(compatibility.attempted)} "
                 f"(expected {int(compatibility.expected)})"
        )
    return {
        "nr": not st.paused,
        "work_scale": st.work_scale,
        # Where the work size hits the 2560x1440 cap. Everything above
        # it lands on the same resolution, so the slider puts "the whole
        # screen" there instead of a dead stretch.
        "work_scale_cap": work_scale_cap(st),
        "work_scale_min": WORK_SCALE_MIN,
        "nr_small": st.nr_small,
        "screen_size": f"{st.width}x{st.height}",
        "profile": st.cfg["profile"],
        "profiles": list(PROFILES) + list(st.presets),
        "preset_active": st.cfg["profile"] in st.presets,
        "params": {k: st.params[k] for k in
                   ("intensity", "local_tone",
                    "local_structure", "skin_structure")},
        # What the CURRENT profile puts each parameter at. The menu draws it
        # as a tick under the slider, so "how far have I moved this from
        # Natural" is visible instead of remembered.
        # The range each slider draws. It lives here, next to the
        # measurement that set it, rather than being a second copy of the
        # numbers inside the menu.
        "param_ranges": {k: list(v) for k, v in PARAM_RANGE.items()},
        # Which of the three looks is live - its own control since the
        # measurement showed it is the strongest lever we have.
        "style": int(st.params.get("style", 1)),
        # The second set for passes 2..N: `{}` when there is none, which is
        # what the menu draws as "off". Its ticks are the main sliders' live
        # values, so the set reads as "this far from the main set" rather than
        # against a profile the user may have since changed.
        "nr_pass_params": dict(getattr(st, "nr_pass_params", None) or {}),
        "pass_param_defaults": {
            k: float(st.params.get(k, 0.0)) for k in
            ("intensity", "local_tone", "local_structure", "skin_structure")},
        "pass_param_ranges": {k: list(v) for k, v in PARAM_RANGE.items()},
        "param_defaults": {
            k: float(v) for k, v in
            (PROFILES.get(st.cfg["profile"])
             or st.presets.get(st.cfg["profile"]) or {}).items()
            if k in ("intensity", "local_tone", "local_structure",
                     "skin_structure")},
        "lang": st.lang,
        "recording": st.recorder is not None,
        "recording_finalizing": getattr(st, "recording_finalizer", None) is not None,
        "recording_status": str(last_recording.get("status", "")),
        "recording_details": rec_detail,
        "recording_path": str(last_recording.get("path", "")),
        "work_size": f"{st.work_w}x{st.work_h}",
        "rec_seconds": ((active_recorder.duration_ms / 1000.0)
                        if active_recorder else 0.0),
        "rec_indicator": bool(st.cfg.get("rec_indicator", True)),
        "gpu_record": st.cfg.get("gpu_record", True) is not False,
        "fps_overlay": str(st.cfg.get("fps_overlay", "off")),
        "nr_passes": int(st.cfg.get("nr_passes", 1)),
        "convert_busy": bool(getattr(st, "convert_busy", False)),
        "convert_status": str(getattr(st, "convert_status", "")),
        # The queue's rows, copied under its lock - see convert_jobs.
        "convert_jobs": _convert_rows(st),
        # How far the running file is, for the main page's Convert cell;
        # None while nothing converts.
        "convert_progress": getattr(st, "convert_progress", None),
        "convert_dest": _choice(st.cfg, "convert_dest", CONVERT_DESTS),
        # The folder the files WILL go to: with none chosen yet (the picker
        # was cancelled) that is the default one, and the row says so rather
        # than standing empty while files land somewhere unnamed.
        "convert_dir": st.cfg.get("convert_dir") or str(BASE_DIR / "converted"),
        "convert_codec": _choice(st.cfg, "convert_codec", CONVERT_CODECS),
        "convert_quality": _choice(st.cfg, "convert_quality",
                                   CONVERT_QUALITIES),
        "convert_image_format": _choice(st.cfg, "convert_image_format",
                                        CONVERT_IMAGE_FORMATS),
        "convert_audio": st.cfg.get("convert_audio", True) is not False,
        "tray_on_minimise": bool(st.cfg.get("tray_on_minimise", False)),
        "tray_on_close": bool(st.cfg.get("tray_on_close", False)),
        "recording_dir": st.cfg.get("recording_dir") or "",
        "screenshot_dir": st.cfg.get("screenshot_dir") or "",
        "screenshot_mode": str(st.cfg.get("screenshot_mode", "ask")),
        "screenshot_format": str(st.cfg.get("screenshot_format", "png")),
        "spout": bool(st.cfg.get("spout", False)),
        "hdr": bool(st.cfg.get("hdr", False)),
        "motion_backend": st.cfg.get("motion_backend", "nvofa"),
        "frame_generation": bool(st.cfg.get("frame_generation", False)),
        "frame_multiplier": min(4, max(2, int(st.cfg.get("frame_multiplier", 2)))),
        # The step the presenter is REALLY running, when the runtime refused
        # the requested one and the worker stepped down (issue #100). None
        # until the worker says so; the panel marks the difference so the
        # user is not left comparing FPS numbers.
        "frame_multiplier_active": _fg_active_multiplier(st),
        "frame_limit_mode": (str(st.cfg.get("frame_limit_mode", "unlimited"))
                             if str(st.cfg.get("frame_limit_mode", "unlimited"))
                             in FRAME_LIMIT_MODES else "unlimited"),
        "frame_limit_custom": frame_limit_fps(
            dict(st.cfg, frame_limit_mode="custom")),
        # What the presenter actually shows while FG interpolates; the
        # HUD pairs it with the network rate as "42 / 84 fps".
        "display_fps": _fg_displayed_fps(st),
        # The list, with a note on any adapter whose worker could not bring
        # the neural pass up. DXGI reports some cards twice (one user has a
        # single 5080 listed as adapters 0 and 2) and the two entries are
        # indistinguishable by name - so the menu offered a choice between
        # two identical-looking lines, one of which kills the pipeline
        # (issue #33). The note is what we actually know: it was tried and
        # it did not work. The entry stays selectable.
        "gpus": [f"{i}: {name}" + (f" - {UI_STRINGS[st.lang].get('gpu_no_nr', 'no neural pass')}"
                                   if i in _no_nr(st) else "")
                 for i, name in list_adapters()],
        # The saved index may name no NVIDIA card at all. On a hybrid laptop
        # adapter 0 is the integrated GPU and "gpu": 0 is what the program
        # ships with, so the picker came up EMPTY on exactly the machines
        # where the setting matters most (issue #34). The worker already
        # falls back to the first usable card in that case - the menu says
        # the same thing now instead of showing a blank.
        "gpu": _gpu_label(st.cfg.get("gpu")),
        "open_on_start": st.startup_menu,
        "autostart": _autostart_enabled(),
        "split": st.split_pos,
        "gpu_text": st.gpu_text,
        # The four facts the log header carries, for the About block. A
        # reporter can read them off the menu instead of being asked which
        # version and which driver - which is the first exchange on almost
        # every issue.
        "about": dict(getattr(st, "environment", None) or {},
                      gpu=st.gpu_text or ""),
        "compatibility_status": compatibility_status,
        "compatibility_score": compatibility_score,
        "gpu_ok": st.gpu_ok,
        "window_mode": st.window_hwnd is not None,
        "monitor_devicename": st.capture.devicename,
        "monitors": monitor_entries,
        "monitor": next(
            (m for m in monitor_entries
             if m.startswith(f"{st.monitor}: ")),
            str(st.monitor)),
        "windows": window_entries,
        "window_current": current_window,
        "version": APP_VERSION,
        "channel": CHANNEL_LABEL,
    }


def save_menu_layout(st) -> bool:
    """Remember the panel size and position in config.json.

    We write on menu close and on exit rather than on every mouse
    move: dragging would otherwise hammer the file dozens of times
    per second. Returns False when the write failed - the callers
    that promise the user something (presets, hotkeys) show an
    alert then.
    """
    try:
        data = json.loads(st.cfg_path.read_text(encoding="utf-8-sig"))
        data.update(_menu_layout_payload(
            st.cfg, st.params, st.monitor, st.lang, st.work_scale, st.split_pos,
            st.startup_menu, st.nr_small, st.display.menu))
        _atomic_write_json(st.cfg_path, data)
        return True
    except Exception as exc:
        print(f"[main] could not save the menu layout: {exc}", file=sys.stderr)
        return False


def save_hotkeys(st, mapping: dict) -> bool:
    """Write the assignments into config.json.

    Separate from _save_menu_layout: that one runs on menu close,
    while the user expects a key to be saved right away. Returns
    False when the write failed - the caller shows an alert.
    """
    try:
        data = json.loads(st.cfg_path.read_text(encoding="utf-8-sig"))
        data["hotkeys"] = dict(mapping)
        _atomic_write_json(st.cfg_path, data)
        return True
    except Exception as exc:
        print(f"[main] could not save the hotkeys: {exc}",
              file=sys.stderr)
        return False
