"""Redesign B: the main page as tiles, a profile and four fold-outs (v2.6.0).

The main page ran to 1356 px at 100% - past a 1080p screen - with twenty-odd
rows in one column, and the camera added seven more. It is now the four
switches a game is played with as tiles (NR, FG, Boost, the camera), the FG
multiplier, the profile and its strength, and four fold-outs that say what
they hold while closed: Network, Fine tuning, Camera, Source and comparison.

Checked without a window, on the real menu and the real command layer:

1. nothing was lost: every control of the old main page is on the page or in
   a fold-out (the interface scale moved to Settings -> Program);
2. folded, the page fits a 1080p work area at 100% with no scrolling, and
   closed folds lay out none of their controls;
3. a fold header opens and closes its block and says so for the saved
   layout; its summary names the state inside it;
4. the open folds survive: the command stores them, save_menu_layout writes
   them, restore_menu_extras gives them to a new menu, and an unknown fold
   name in the config is refused.

Fails before the change: there were no tiles and no folds.

Run:  runtime\\python.exe tests\\test_menu_folds.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))

import pygame  # noqa: E402

STATE = {
    "nr": True, "gpu_ok": True, "profile": "Natural", "profiles": ["Natural", "Faithful"],
    "params": {"intensity": 1.0, "local_tone": 0.5, "local_structure": 1.0,
               "skin_structure": -1.0},
    "param_ranges": {}, "split": 0.0, "style": 1, "frame_generation": True,
    "frame_multiplier": 3, "nr_small": True, "work_scale": 0.65, "work_scale_cap": 0.65,
    "work_scale_min": 0.1, "work_size": "2496x1404", "frame_limit_mode": "60",
    "lens": False, "monitors": [], "windows": [], "lang": "en",
}

#: Every control the main page had before v2.6.0, by (kind, key) - the tiles
#: replace the four switches.
OLD_PAGE = (
    [("tile", k) for k in ("nr", "frame_generation", "boost", "lens")]
    + [("button", f"frame_multiplier:{m}") for m in range(2, 7)]
    + [("choice", "profile"), ("slider", "intensity"), ("choice", "frame_limit_mode"),
       ("slider", "nr_res"), ("segmented", "nr_passes"), ("segmented", "style"),
       ("slider", "local_tone"), ("slider", "local_structure"),
       ("slider", "skin_structure"), ("button", "save_preset"),
       ("button", "delete_preset"), ("toggle", "lens_bend"), ("slider", "lens_fov"),
       ("slider", "lens_noise"), ("slider", "lens_vignette"),
       ("slider", "lens_aberration"), ("slider", "lens_sensor"), ("slider", "lens_soft"),
       ("segmented", "source"), ("slider", "split"), ("button", "screenshot"),
       ("button", "record"), ("button", "convert"), ("action", "exit")])
FOLDED_AWAY = {("choice", "frame_limit_mode"), ("slider", "nr_res"),
               ("segmented", "style"), ("slider", "lens_noise"), ("segmented", "source"),
               ("slider", "split"), ("button", "save_preset")}


def menu_with(folds, **state):
    import fonts
    import overlay_ui as ui
    menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
    menu.visible = True
    menu.page = "main"
    menu.set_state(dict(STATE, **state))
    menu.folds = set(folds)
    return menu


def keys(menu):
    return {(i.kind, i.key) for i in menu.items}


def main() -> int:
    failures = []
    pygame.init()
    pygame.display.set_mode((64, 64))
    try:
        import commands
        import settings_io

        # 1. Nothing lost.
        menu = menu_with(settings_io.MENU_FOLDS)
        menu.layout(1920, 1080)
        missing = [k for k in OLD_PAGE if k not in keys(menu)]
        if missing:
            failures.append(f"controls of the old main page are gone: {missing}")
        menu.page, menu.settings_tab = "settings", "app"
        menu.layout(1920, 1080)
        if ("segmented", "menu_scale") not in keys(menu):
            failures.append("the interface scale is not on Settings -> Program")
        print(f"    all folds open: {len(OLD_PAGE) - len(missing)} of {len(OLD_PAGE)} "
              f"old controls present")

        # 2. Folded: fits 1080p, holds nothing of the folds.
        menu = menu_with(())
        menu.layout(1920, 1032)
        print(f"    folded page: {menu.content_height} px, scroll {menu._max_scroll}")
        if menu._max_scroll > 0:
            failures.append(f"the folded main page needs {menu._max_scroll} px of "
                            f"scrolling on a 1080p work area at 100%")
        shown = FOLDED_AWAY & keys(menu)
        if shown:
            failures.append(f"closed folds still laid out {sorted(shown)}")
        folds = [i for i in menu.items if i.kind == "fold"]
        if [f.key for f in folds] != list(settings_io.MENU_FOLDS):
            failures.append(f"fold headers are {[f.key for f in folds]}")

        # 3. A header opens and closes its block; the summary names the state.
        net = next((f for f in folds if f.key == "net"), None)
        if net is not None:
            if "2496x1404" not in net.extra.get("summary", "") \
                    or "60" not in net.extra.get("summary", ""):
                failures.append(f"the Network summary is {net.extra.get('summary')!r}")
            out = menu._activate_item(net)
            if out != [("folds", ["net"])] or ("choice", "frame_limit_mode") not in keys(menu):
                failures.append(f"opening Network gave {out} and no frame limit")
            again = next(i for i in menu.items if i.kind == "fold" and i.key == "net")
            out = menu._activate_item(again)
            if out != [("folds", [])] or ("choice", "frame_limit_mode") in keys(menu):
                failures.append(f"closing Network gave {out}")

        # 4. Persistence.
        st = types.SimpleNamespace(cfg={})
        with contextlib.redirect_stdout(io.StringIO()):
            commands.apply_menu_action(st, ("folds", ["camera", "net", "bogus"]))
        if st.cfg.get("menu_folds") != ["camera", "net"]:
            failures.append(f"the command stored {st.cfg.get('menu_folds')}")
        fresh = menu_with(())
        settings_io.restore_menu_extras(fresh, {"menu_folds": ["tune"], "menu_mini": True,
                                                "menu_bar_offset": [40, 5]})
        if fresh.folds != {"tune"} or not fresh.mini or fresh.bar_offset != [40, 5]:
            failures.append(f"restore gave folds={fresh.folds} mini={fresh.mini} "
                            f"bar={fresh.bar_offset}")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"menu_folds": ["net", "nonsense"], "menu_bar_offset": "x"}',
                            encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                cfg = settings_io.load_config(path)
            if cfg.get("menu_folds") is not None or cfg.get("menu_bar_offset") is not None:
                failures.append(f"bad saved folds/bar loaded as {cfg.get('menu_folds')}, "
                                f"{cfg.get('menu_bar_offset')}")
            path.write_text("{}", encoding="utf-8")
            saved_menu = types.SimpleNamespace(
                user_scale=1.0, user_height=None, state={}, mini=False,
                folds={"source", "net"}, bar_offset=[3, 4], offset=(0, 0))
            st = types.SimpleNamespace(
                cfg_path=path, cfg={"profile": "Natural"},
                params=dict(settings_io.PROFILES["Natural"], style=1), monitor=0, lang="en",
                work_scale=1.0, split_pos=0.0, startup_menu=False, nr_small=True,
                display=types.SimpleNamespace(menu=saved_menu))
            settings_io.save_menu_layout(st)
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("menu_folds") != ["net", "source"] or data.get("menu_bar_offset") != [3, 4]:
                failures.append(f"save_menu_layout wrote folds={data.get('menu_folds')} "
                                f"bar={data.get('menu_bar_offset')}")
    finally:
        pygame.quit()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the main page is tiles and fold-outs, nothing was lost, and it fits 1080p")
    return 0


if __name__ == "__main__":
    sys.exit(main())
