"""A rebuild can leave the desktop in view instead of dimming it (#157).

A pipeline rebuild (a monitor, a resolution, a window switch) freezes the
last frame under a grey veil with the assembling mark until the new worker
shows its first frame. A reporter running automatic rebuilds asked for a way
to keep the desktop visible instead. Settings -> Program now has "Dim while
rebuilding", on by default (the behaviour as it was).

Checked on a real Display (SDL's dummy driver) and the command layer:

1. on, a rebuild raises the veil, as before;
2. off, it does not - no switch state at all, so nothing waits for a fade;
3. the switch is on the Program tab, its command flips the config and the
   live display, and the config key is a real boolean.

Fails before the change: there was no setting and every rebuild dimmed.

Run:  runtime\\python.exe tests\\test_dim_on_rebuild.py
"""
import contextlib
import io
import os
import sys
import types

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))

import pygame  # noqa: E402


def main() -> int:
    failures = []
    pygame.init()
    import commands
    import display as display_mod
    import settings_io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        disp = display_mod.Display(640, 360)
        disp.enter_switch_mode(None)
        on = disp.is_switch_active()
        disp.exit_switch_mode()
        disp.dim_on_rebuild = False
        disp._switch_active = False
        disp.enter_switch_mode(None)
        off = disp.is_switch_active()
    if not on:
        failures.append("with dimming on a rebuild raised no veil")
    if off:
        failures.append("with dimming off a rebuild still raised the veil")
    if "switch overlay skipped" not in out.getvalue():
        failures.append("the skipped veil was not said in the log")

    menu = disp.menu
    menu.visible = True
    menu.page, menu.settings_tab = "settings", "app"
    menu.set_state({"dim_on_rebuild": True})
    menu.layout(1920, 1080)
    sw = next((i for i in menu.items if i.kind == "toggle" and i.key == "dim_on_rebuild"), None)
    if sw is None or menu._activate_item(sw) != [("toggle", "dim_on_rebuild")]:
        failures.append("the Program tab has no dim-while-rebuilding switch")

    st = types.SimpleNamespace(cfg={}, display=disp)
    disp.dim_on_rebuild = True
    real_save = settings_io.save_menu_layout
    settings_io.save_menu_layout = lambda st: True   # no config.json written here
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            commands.apply_menu_action(st, ("toggle", "dim_on_rebuild"))
    finally:
        settings_io.save_menu_layout = real_save
    if st.cfg.get("dim_on_rebuild") is not False or disp.dim_on_rebuild is not False:
        failures.append(f"the command left cfg={st.cfg} display={disp.dim_on_rebuild}")
    if "dim_on_rebuild" not in settings_io._BOOL_KEYS:
        failures.append("dim_on_rebuild is not validated as a boolean")
    pygame.quit()

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
