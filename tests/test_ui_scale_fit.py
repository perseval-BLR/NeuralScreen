r"""The interface scale: fitted once on a first launch, chosen after that.

At the shipped menu_scale of 1.0 the main page used to be taller than a
1080p desktop, so the panel came up scrolled with its last controls under
the taskbar - measured here, and visible in a reporter's phone video (#107).
A first launch therefore picks the largest ladder step whose page needs no
scrolling. Since redesign B (v2.6.0) the folded main page fits a 1080p desktop
at 1.0 - that is now required here - and the fit still steps down on a
smaller one (a 720p work area).

Three properties, because each one is a different way to get this wrong:

1. The fit answers from the SCREEN it is given: small desktops step down,
   a 1440p one keeps 1.0.
2. It never steps UP. A big desktop is not a request for a big panel - the
   per-monitor density is handled by display.ui_scale_for, and a fit that
   grows would fight the user's own choice on every launch.
3. It measures without deciding: the caller's scale, page and scroll are
   what they were.

Plus the flag that makes it a FIRST-launch affair: menu_scale_auto is a
real boolean or it is False, so a malformed value cannot be read as
"please resize my interface" on every start.

Run:  runtime\python.exe tests\test_ui_scale_fit.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

#: Work areas, not screen heights: the taskbar is not room the panel has.
DESKTOPS = (
    ("720p", 1280, 680, True),      # must step down
    ("1080p", 1920, 1032, False),   # fits at 1.0 since redesign B (#107)
    ("1440p", 2560, 1392, False),   # fits at 1.0, must not be touched
    ("4K", 3840, 2112, False),      # fits with room to spare, must not grow
)

STATE = {
    "nr": True, "gpu_ok": True, "profile": "Natural", "profiles": ["Natural"],
    "params": {"intensity": 1.0, "local_tone": 0.5, "local_structure": 1.0,
               "skin_structure": -1.0},
    "param_ranges": {}, "split": 0.0, "style": 1, "frame_generation": False,
    "frame_multiplier": 2, "work_scale": 0.65, "work_scale_cap": 0.65,
    "work_scale_min": 0.1, "work_size": "2496x1404",
    "monitors": [], "windows": [],
}


def main() -> int:
    failures = []
    pygame.init()
    pygame.display.set_mode((64, 64))
    try:
        import fonts
        import overlay_ui as ui

        menu = ui.OverlayMenu(
            1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
        menu.visible = True
        menu.page = "main"
        menu.set_state(dict(STATE))

        for name, w, h, must_step_down in DESKTOPS:
            step = menu.fit_user_scale(w, h)
            if step > 1.0:
                failures.append(
                    f"{name}: the fit chose {step:g} - it must never step "
                    f"above 1.0, the density of a big screen is "
                    f"display.ui_scale_for's business")
            if step not in ui.SCALE_STEPS:
                failures.append(
                    f"{name}: the fit chose {step:g}, which is not one of the "
                    f"steps the control offers {ui.SCALE_STEPS}")
            # The answer has to be true of the layout, not merely plausible.
            menu.set_user_scale(step)
            menu.layout(w, h)
            if menu._max_scroll > 0:
                failures.append(
                    f"{name}: at the fitted {step:g} the page still needs "
                    f"{menu._max_scroll} px of scrolling")
            if must_step_down and step >= 1.0:
                failures.append(
                    f"{name}: the fit kept {step:g}, but 1.0 does not fit "
                    f"this desktop - that is the case this exists for")
            if not must_step_down and step != 1.0:
                failures.append(
                    f"{name}: the fit moved to {step:g} on a desktop where "
                    f"1.0 fits")

        # It measures, it does not decide.
        menu.set_user_scale(1.15)
        menu.page, menu.scroll = "settings", 17
        menu.fit_user_scale(1920, 1032)
        if abs(menu.user_scale - 1.15) > 0.001:
            failures.append(
                f"fit_user_scale left the scale at {menu.user_scale} - it must "
                f"restore the caller's")
        if menu.page != "settings" or menu.scroll != 17:
            failures.append(
                f"fit_user_scale left page={menu.page!r} scroll={menu.scroll} - "
                f"it must restore both")
    finally:
        pygame.quit()

    # The flag: a real boolean, or False.
    from settings_io import CONFIG_SCHEMA_VERSION, load_config
    base = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "monitor": 0, "width": 3840, "height": 2160, "fullscreen": True,
        "warmup": 120, "work_scale": 0.65, "lang": "en", "profile": "Natural",
        "intensity": None, "local_tone": None, "local_structure": None,
        "skin_structure": None,
    }
    # (menu_scale_auto as written, menu_scale, what must come out).
    # The last two rows are the upgrade case: a config from before the flag
    # existed. Still at the shipped 1.0 -> never adjusted, fit it. Anything
    # else -> the user's own size, leave it alone.
    for value, scale, want in ((True, 1.0, True), (False, 1.0, False),
                               ("yes", 1.0, False), (1, 1.0, False),
                               (None, 1.0, True), (None, 0.85, False)):
        data = dict(base)
        data["menu_scale"] = scale
        if value is not None:
            data["menu_scale_auto"] = value
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                        encoding="utf-8")
        json.dump(data, f)
        f.close()
        try:
            got = load_config(Path(f.name)).get("menu_scale_auto")
        finally:
            os.unlink(f.name)
        if got is not want:
            failures.append(
                f"menu_scale_auto {value!r} with menu_scale {scale} loaded as "
                f"{got!r}, want {want!r} - "
                f"anything but a real True must read as 'already chosen', or a "
                f"malformed config re-fits the interface on every launch")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the fit steps down where it must, never up, restores what it "
          "touched, and only a real True asks for it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
