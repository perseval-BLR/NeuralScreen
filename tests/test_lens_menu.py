r"""The fisheye lens from the menu: a switch, an angle, a command, a saved setting.

The main page gets one row for the lens (closing the "effect" block) and, only
while it is on, a field-of-view slider in whole degrees. The client sends the
worker a LENS command at once - nothing is rebuilt - and the setting is saved
like every other menu choice.

Checked without launching anything:

* the menu: with the lens off there is a switch and no slider; on, an angle
  slider in degrees and a webcam-noise slider in percent, and its hint fits the panel in all 12
  languages; a click on the switch asks for ("toggle", "lens"), the keyboard
  moves the angle 5 degrees at a time;
* the handlers (commands.py): the switch and the slider update the config
  and put exactly one LENS command on the worker's pipe - on/off and the
  angle, rounded and clamped; with no running worker nothing is written;
* the config: lens/lens_fov are validated (a word, NaN or 500 degrees fall
  back or clamp) and saved by save_menu_layout.

Run:  runtime\python.exe tests\test_lens_menu.py
"""
import io
import json
import os
import struct
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import pygame  # noqa: E402


def _menu(lang: str, **state):
    import fonts
    import overlay_ui as ui
    menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
    menu.visible = True
    menu.page = "main"
    menu.set_state(dict({"nr": True, "gpu_ok": True, "profile": "Natural",
                         "profiles": ["Natural"], "lang": lang}, **state))
    menu.layout(1920, 1080)
    return menu


def _item(menu, key):
    return next((it for it in menu.items if it.key == key), None)


def _check_menu(failures: list) -> None:
    from i18n import STRINGS
    menu = _menu("en")
    if _item(menu, "lens") is None:
        failures.append("the main page has no lens switch")
    if _item(menu, "lens_fov") is not None or _item(menu, "lens_noise") is not None:
        failures.append("the angle or noise slider is on screen while the lens is off")
    for lang in sorted(STRINGS):
        menu = _menu(lang, lens=True, lens_fov=125.0)
        slider = _item(menu, "lens_fov")
        if slider is None:
            failures.append(f"{lang}: the lens is on and there is no angle slider")
            continue
        if slider.extra.get("value_text") != "125°":
            failures.append(f"{lang}: the slider says {slider.extra.get('value_text')!r}")
        noise = _item(menu, "lens_noise")
        if noise is None or noise.extra.get("value_text") != "30%":
            failures.append(f"{lang}: the noise slider is missing or wrong: "
                            f"{noise and noise.extra.get('value_text')!r}")
        hint = str(slider.extra.get("hint") or "")
        width = menu._small_font.size(hint)[0]
        if not hint or width > slider.rect.w:
            failures.append(f"{lang}: the angle hint ({width} px) does not fit the "
                            f"{slider.rect.w} px row: {hint!r}")
    menu = _menu("en", lens=True, lens_fov=110.0)
    switch = _item(menu, "lens")
    actions = menu._activate_item(switch)
    if actions != [("toggle", "lens")]:
        failures.append(f"activating the lens switch asked for {actions}")
    slider = _item(menu, "lens_fov")
    stepped = menu._step_slider(slider, +1)
    if stepped != [("lens_fov", 115.0)]:
        failures.append(f"one keyboard step from 110 gave {stepped}")
    print(f"    menu: switch present, slider only while on, hint fits in "
          f"{len(STRINGS)} languages, a step is 5 degrees")


def _check_commands(failures: list) -> None:
    import commands
    import protocol
    import settings_io

    class Worker:
        pid = 4242

        def __init__(self):
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    worker = Worker()
    st = types.SimpleNamespace(
        cfg={"lens": False, "lens_fov": 110.0, "lens_noise": 0.3}, worker=worker, worker_failed=False,
        display=types.SimpleNamespace(menu=types.SimpleNamespace(set_state=lambda d: None)))
    commands.apply_menu_action(st, ("toggle", "lens"))
    commands.apply_menu_action(st, ("lens_fov", 133.6))
    commands.apply_menu_action(st, ("lens_fov", 900))
    commands.apply_menu_action(st, ("lens_noise", 0.55))
    commands.apply_menu_action(st, ("toggle", "lens"))
    size = struct.calcsize(protocol.LENS_FMT)
    data = worker.stdin.getvalue()
    sent = [struct.unpack(protocol.LENS_FMT, data[i:i + size])
            for i in range(0, len(data), size)]
    got = [(flags & protocol.LENS_FLAG_ON, round(fov), round(noise, 2))
           for _m, flags, fov, noise, _p in sent]
    print(f"    commands sent: {got}")
    top = int(settings_io.LENS_FOV_MAX)
    want = [(1, 110, 0.3), (1, 134, 0.3), (1, top, 0.3), (1, top, 0.55), (0, top, 0.55)]
    if len(data) % size or got != want or any(m != protocol.LENS_MAGIC for m, *_ in sent):
        failures.append(f"the LENS commands on the pipe were {got}, expected {want}")
    if st.cfg != {"lens": False, "lens_fov": settings_io.LENS_FOV_MAX, "lens_noise": 0.55}:
        failures.append(f"the config after the clicks is {st.cfg}")
    st.worker = None
    commands.apply_menu_action(st, ("toggle", "lens"))   # no worker: no write, no raise
    if st.cfg.get("lens") is not True:
        failures.append("the switch did not change the setting while no worker runs")


def _check_config(failures: list) -> None:
    import settings_io
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        for raw, want in (('"abc"', 110.0), ("NaN", 110.0), ("500", 170.0),
                          ("12", 60.0), ("127.4", 127.0)):
            path.write_text('{"lens": true, "lens_fov": %s}' % raw, encoding="utf-8")
            cfg = settings_io.load_config(path)
            if cfg.get("lens") is not True or cfg.get("lens_fov") != want:
                failures.append(f"lens_fov {raw} loaded as {cfg.get('lens')}, "
                                f"{cfg.get('lens_fov')} (expected {want})")
        for raw, want in (('"x"', 0.3), ("NaN", 0.3), ("-1", 0.0), ("7", 1.0), ("0.42", 0.4)):
            path.write_text('{"lens_noise": %s}' % raw, encoding="utf-8")
            got = settings_io.load_config(path).get("lens_noise")
            if got != want:
                failures.append(f"lens_noise {raw} loaded as {got} (expected {want})")
        path.write_text("{}", encoding="utf-8")
        menu = types.SimpleNamespace(user_scale=1.0, user_height=None, state={}, mini=False,
                                     mini_rows=(), offset=(0, 0))
        st = types.SimpleNamespace(
            cfg_path=path, cfg={"profile": "Natural", "lens": True, "lens_fov": 140.0},
            params=dict(settings_io.PROFILES["Natural"], style=1), monitor=0, lang="en",
            work_scale=1.0, split_pos=0.0, startup_menu=False, nr_small=True,
            display=types.SimpleNamespace(menu=menu))
        settings_io.save_menu_layout(st)
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("lens") is not True or saved.get("lens_fov") != 140.0:
            failures.append(f"the lens was saved as {saved.get('lens')}, {saved.get('lens_fov')}")


def main() -> int:
    failures: list = []
    pygame.init()
    pygame.display.set_mode((64, 64))
    try:
        _check_menu(failures)
    finally:
        pygame.quit()
    _check_commands(failures)
    _check_config(failures)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the lens switch and angle reach the worker as one command each and are saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
