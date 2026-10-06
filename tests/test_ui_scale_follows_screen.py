r"""The interface scale follows the screen after a resolution or monitor change.

display.ui_scale and the menu's scale were set once, in Display.__init__,
from the height of the first screen. Display.resize() and
set_fullscreen_layer() - the paths a resolution change, a monitor switch
and one-window mode go through - changed the layer's size and left the
scale alone: a session started at 1440p and moved to 4K kept a 1440p-sized
panel, HUD and alerts.

And the panel was never narrower than PANEL_W at the current scale, even on
a screen narrower than that: a 1080-wide portrait monitor at the top manual
scale put the panel's right edge, with its header icons, off the screen.

Checked with the real Display (dummy SDL driver) and OverlayMenu:
* resize 2560x1440 -> 3840x2160 gives the 4K scale (ui_scale_for, 1.2 at
  2160 rows), the panel width a Display made at 4K has, and the 4K alert font;
* the same through set_fullscreen_layer;
* a 1080x1920 portrait screen with user_scale 2.0 keeps the panel inside.

Run:  runtime\python.exe tests\test_ui_scale_follows_screen.py
"""
import os
import sys
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


def _panel_w(disp, w, h):
    disp.menu.visible = True
    disp.menu.layout(w, h)
    return disp.menu.panel_rect.w


def _display(display_mod, w, h):
    disp = display_mod.Display(w, h, click_through=False)
    disp._screen_rect = lambda: None      # the dummy driver has no monitor
    return disp


def main() -> int:
    failures = []
    pygame.init()
    import display as display_mod

    want_scale = display_mod.ui_scale_for(2160)
    if want_scale <= display_mod.ui_scale_for(1440):
        print("SKIP: the scale rule no longer differs between 1440p and 4K")
        return 0

    # The reference: what a Display made at 4K looks like.
    ref = _display(display_mod, 3840, 2160)
    ref_w = _panel_w(ref, 3840, 2160)
    ref_alert = ref._alert_font.get_height()

    # One pygame session throughout: fonts.py caches faces by size, and a face
    # cached before a pygame.quit() is a dead object after the next init.
    for route in ("resize", "set_fullscreen_layer"):
        disp = _display(display_mod, 2560, 1440)
        small_w = _panel_w(disp, 2560, 1440)
        getattr(disp, route)(3840, 2160)
        if (disp.width, disp.height) != (3840, 2160):
            failures.append(f"{route}: the layer is {disp.width}x"
                            f"{disp.height}, nothing to check")
            continue
        if abs(disp.ui_scale - want_scale) > 1e-6:
            failures.append(f"{route} to 4K left ui_scale at "
                            f"{disp.ui_scale}, the rule gives {want_scale}")
        got_w = _panel_w(disp, 3840, 2160)
        if got_w != ref_w:
            failures.append(f"{route} to 4K: the panel is {got_w} px wide "
                            f"(it was {small_w} at 1440p), a 4K start "
                            f"gives {ref_w}")
        if disp._alert_font.get_height() != ref_alert:
            failures.append(f"{route} to 4K: the alert font is "
                            f"{disp._alert_font.get_height()} px, a 4K "
                            f"start gives {ref_alert}")

    # A portrait screen at the top manual scale.
    disp = _display(display_mod, 1080, 1920)
    try:
        disp.menu.set_user_scale(2.0)
        _panel_w(disp, 1080, 1920)
        r = disp.menu.panel_rect
        if r.left < 0 or r.right > 1080:
            failures.append(f"on a 1080-wide portrait screen at scale 2.0 the "
                            f"panel spans x {r.left}..{r.right}")
    finally:
        disp.close()
        pygame.quit()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the interface scale follows the screen, and the panel stays on it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
