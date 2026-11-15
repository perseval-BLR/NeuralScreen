"""Skin structure's "off" is not zero, and the row now says so (#193).

settings_io.PARAM_RANGE puts skin_structure at (-1.0, 2.5): -1.0 is off and
0.0 is a real middle value. A reporter set the slider to 0, read "0.00" in the
value cell and reported the face filter "still active" - the filter WAS active,
and nothing on the row said where off was.

Checked on the real menu, laid out without a window, in English and Russian:

1. at -1.0 the cell reads the interface language's own "Off" (tile_off);
2. at 0.0 and 2.5 the cell reads the number - the value is real, and the row
   must not pretend it is off.

There is deliberately no hint line here: on a 4K work area with every fold
open the main page is already at its height limit, and one more line pushed
the source segment off the screen (test_ui_buttons caught it).

Fails before: the cell was always f"{value:.2f}".
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import pygame  # noqa: E402

from i18n import STRINGS  # noqa: E402


def state(lang: str, skin: float) -> dict:
    import settings_io
    return {
        "nr": True, "gpu_ok": True, "profile": "Natural",
        "profiles": ["Natural", "Faithful"],
        "params": {"intensity": 1.0, "local_tone": 0.5,
                   "local_structure": 1.0, "skin_structure": skin},
        "param_ranges": {key: settings_io.PARAM_RANGE[key] for key in (
            "intensity", "local_tone", "local_structure", "skin_structure")},
        "split": 0.0, "style": 1, "frame_generation": False,
        "frame_multiplier": 2, "nr_small": True, "work_scale": 0.65,
        "work_scale_cap": 0.65, "work_scale_min": 0.1, "work_size": "2496x1404",
        "frame_limit_mode": "60", "lens": False, "monitors": [], "windows": [],
        "lang": lang,
    }


def slider(menu):
    return next((i for i in menu.items
                 if i.kind == "slider" and i.key == "skin_structure"), None)


def main() -> int:
    failures = []
    pygame.init()
    pygame.display.set_mode((64, 64))
    try:
        import fonts
        import overlay_ui as ui
        import settings_io

        def menu_with(lang: str, skin: float):
            menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False:
                                  fonts.load(size, mono=mono))
            menu.visible = True
            menu.page = "main"
            menu.set_state(state(lang, skin))
            menu.folds = set(settings_io.MENU_FOLDS)  # the slider lives in one
            menu.layout(1920, 1080)
            return menu

        # 1. The left end says what it is, in the user's own language.
        for lang in ("en", "ru"):
            menu = menu_with(lang, -1.0)
            item = slider(menu)
            if item is None:
                failures.append(f"{lang}: the skin_structure slider is not laid out")
                continue
            want = STRINGS[lang].get("tile_off")
            got = item.extra.get("value_text")
            if got != want:
                failures.append(f"{lang}: at -1.0 the cell reads {got!r}, "
                                f"expected the language's off {want!r}")

        # 2. A value above off is a real one and must read as a number.
        for skin in (0.0, 2.5):
            menu = menu_with("en", skin)
            item = slider(menu)
            if item is None:
                failures.append("the skin_structure slider is not laid out")
                continue
            got = item.extra.get("value_text")
            if got != f"{skin:.2f}":
                failures.append(f"at {skin} the cell reads {got!r}, expected "
                                f"{skin:.2f} - a real value was shown as off")
    finally:
        pygame.quit()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: skin structure reads 'off' at its left end and a number above it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
