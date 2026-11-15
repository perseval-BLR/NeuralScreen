"""The panel opens the folder the diagnostic packages land in (#196).

A reporter attached a package to an issue by right-clicking the shortcut,
"Open file location" and hunting for support-bundles/. The panel had
"Create diagnostic package" and nothing that showed where it went.

Checked on the real menu and the real command layer:

1. the button sits on Settings -> Program, under Create diagnostic package,
   and its caption is translated in every language;
2. a press creates the folder (a fresh install has none until the first
   package) and shows it in Explorer;
3. a refusal is an alert, not a crash - the panel keeps working.

Fails before: there was no such control and no command behind it.
"""
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import pygame  # noqa: E402

from i18n import STRINGS  # noqa: E402

STATE = {
    "nr": True, "gpu_ok": True, "profile": "Natural", "profiles": ["Natural"],
    "params": {"intensity": 1.0, "local_tone": 0.5, "local_structure": 1.0,
               "skin_structure": -1.0},
    "param_ranges": {}, "split": 0.0, "style": 1, "frame_generation": False,
    "frame_multiplier": 2, "nr_small": True, "work_scale": 0.65,
    "work_scale_cap": 0.65, "work_scale_min": 0.1, "work_size": "2496x1404",
    "frame_limit_mode": "60", "lens": False, "monitors": [], "windows": [],
    "lang": "en",
    # Filled the way main's payload fills it: the page renders the block only
    # when there is something to say about the build.
    "about": {"version": "0.0-test", "channel": "test"},
}


def main() -> int:
    failures = []
    pygame.init()
    pygame.display.set_mode((64, 64))
    try:
        import commands
        import fonts
        import overlay_ui as ui

        # 1. The button is there, under the create button, with a real caption.
        menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False:
                              fonts.load(size, mono=mono))
        menu.visible = True
        menu.page, menu.settings_tab = "settings", "app"
        menu.set_state(dict(STATE))
        menu.layout(1920, 1080)
        items = [(i.kind, i.key) for i in menu.items]
        if ("button", "diagnostics_open") not in items:
            failures.append(f"Settings -> Program has no diagnostics_open "
                            f"button: {items}")
        else:
            create_at = items.index(("button", "diagnostics"))
            open_at = items.index(("button", "diagnostics_open"))
            if open_at != create_at + 1:
                failures.append("the open-folder button is not right under the "
                                "create button")
            item = next(i for i in menu.items if i.key == "diagnostics_open")
            label = str(item.extra.get("label") or "")
            if not label.strip():
                failures.append("the button has no caption")

        # 2. A press makes the folder and shows it.
        import compatibility_runtime
        real_dir = compatibility_runtime.SUPPORT_DIR
        real_startfile = getattr(os, "startfile", None)
        opened = []
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "support-bundles"
            compatibility_runtime.SUPPORT_DIR = target
            os.startfile = opened.append  # type: ignore[attr-defined]
            st = types.SimpleNamespace(cfg={}, lang="en", frame_index=0,
                                       display=types.SimpleNamespace(alert=Mock()))
            try:
                commands.apply_menu_action(st, ("button", "diagnostics_open"))
            except Exception as exc:
                failures.append(f"the press raised "
                                f"({type(exc).__name__}: {exc})")
            finally:
                compatibility_runtime.SUPPORT_DIR = real_dir
                if real_startfile is not None:
                    os.startfile = real_startfile  # type: ignore[attr-defined]
            if not target.is_dir():
                failures.append("the folder was not created before opening it - "
                                "a fresh install has none")
            if not opened:
                failures.append("Explorer was never asked to show the folder")
            elif Path(str(opened[-1])) != target:
                failures.append(f"Explorer was pointed at {opened[-1]!r}, not "
                                f"the support folder {target}")

            # 3. A refusal is an alert, not a crash.
            compatibility_runtime.SUPPORT_DIR = Path(tmp) / "refused"
            def refuse(_path):
                raise OSError("no shell")
            os.startfile = refuse  # type: ignore[attr-defined]
            st2 = types.SimpleNamespace(cfg={}, lang="en", frame_index=0,
                                        display=types.SimpleNamespace(alert=Mock()))
            try:
                commands.apply_menu_action(st2, ("button", "diagnostics_open"))
            except Exception as exc:
                failures.append(f"a refusing shell took the program down "
                                f"({type(exc).__name__}: {exc})")
            finally:
                compatibility_runtime.SUPPORT_DIR = real_dir
                if real_startfile is not None:
                    os.startfile = real_startfile  # type: ignore[attr-defined]
            if not st2.display.alert.called:
                failures.append("a refused folder open said nothing")

        # 4. The captions are translated, not the English source everywhere.
        en_label = STRINGS["en"].get("diagnostics_open", "")
        en_fail = STRINGS["en"].get("diagnostics_open_failed", "")
        if not en_label.strip() or not en_fail.strip():
            failures.append("an English caption is empty")
        for lang, table in STRINGS.items():
            for key in ("diagnostics_open", "diagnostics_open_failed"):
                value = str(table.get(key) or "")
                if not value.strip():
                    failures.append(f"{lang}: {key} is missing")
                elif lang != "en" and value == STRINGS["en"].get(key):
                    failures.append(f"{lang}: {key} is the English source")
    finally:
        pygame.quit()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the panel opens the diagnostic package folder, creates it "
          "first, and survives a refused shell")
    return 0


if __name__ == "__main__":
    sys.exit(main())
