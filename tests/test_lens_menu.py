r"""The camera from the menu: switches, an angle, amounts, a command, saved settings.

The main page gets a tile for the camera and a fold-out for what it does
(v2.6.0): the fisheye switch, a field-of-view slider in whole
degrees while the fisheye is on, and five amounts in percent: webcam noise,
vignette, chromatic aberration, the small sensor's tone and the softness towards
the corners (#168: they were one amount). The noise works without
the fisheye (#159). The client sends the worker a LENS command at once -
nothing is rebuilt - and the settings are saved like every other menu choice.

Checked without launching anything:

* the menu: the camera is a tile; its block closed shows nothing of it, open
  (on or off) the fisheye switch, an angle slider in degrees and the five amounts in
  percent, and the angle's hint fits the panel in all 12 languages; with the
  fisheye off the angle slider goes and the amounts stay; a click on a switch
  asks for ("toggle", key), the keyboard moves the angle 5 degrees at a time;
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


def _menu(lang: str, folds=("camera",), **state):
    import fonts
    import overlay_ui as ui
    menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
    menu.visible = True
    menu.page = "main"
    menu.folds = set(folds)
    menu.set_state(dict({"nr": True, "gpu_ok": True, "profile": "Natural",
                         "profiles": ["Natural"], "lang": lang}, **state))
    menu.layout(1920, 1080)
    return menu


def _item(menu, key):
    return next((it for it in menu.items if it.key == key), None)


def _check_menu(failures: list) -> None:
    from i18n import STRINGS
    # v2.6.0: the camera is a tile; what it does lives in its fold-out, on
    # screen whenever that block is open - so it can be set up before the
    # camera is turned on - and never while it is closed.
    menu = _menu("en", folds=())
    tile = _item(menu, "lens")
    if tile is None or tile.kind != "tile":
        failures.append("the main page has no camera tile")
    under = ("lens_bend", "lens_fov", "lens_noise", "lens_vignette", "lens_aberration",
             "lens_sensor", "lens_soft")
    if any(_item(menu, key) is not None for key in under):
        failures.append("a camera control is on screen while its block is closed")
    menu = _menu("en")
    if any(_item(menu, key) is None for key in under):
        failures.append("the open camera block lacks a control while the camera is off")
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
        if _item(menu, "lens_bend") is None:
            failures.append(f"{lang}: the camera is on and there is no fisheye switch")
        for key in ("lens_vignette", "lens_aberration", "lens_sensor", "lens_soft"):
            amount = _item(menu, key)
            if amount is None or amount.extra.get("value_text") != "0%":
                failures.append(f"{lang}: the {key} slider is missing or wrong: "
                                f"{amount and amount.extra.get('value_text')!r}")
        hint = str(slider.extra.get("hint") or "")
        width = menu._small_font.size(hint)[0]
        if not hint or width > slider.rect.w:
            failures.append(f"{lang}: the angle hint ({width} px) does not fit the "
                            f"{slider.rect.w} px row: {hint!r}")
    flat = _menu("en", lens=True, lens_bend=False)
    if _item(flat, "lens_fov") is not None:
        failures.append("the angle slider is on screen while the fisheye is off")
    if _item(flat, "lens_noise") is None:
        failures.append("with the fisheye off the noise slider went too (#159)")
    menu = _menu("en", lens=True, lens_fov=110.0)
    switch = _item(menu, "lens")
    actions = menu._activate_item(switch)
    if actions != [("toggle", "lens")]:
        failures.append(f"activating the lens switch asked for {actions}")
    actions = menu._activate_item(_item(menu, "lens_bend"))
    if actions != [("toggle", "lens_bend")]:
        failures.append(f"activating the fisheye switch asked for {actions}")
    stepped = menu._step_slider(_item(menu, "lens_vignette"), +1)
    if stepped != [("lens_vignette", 0.05)]:
        failures.append(f"one keyboard step of the vignette gave {stepped}")
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
    commands.apply_menu_action(st, ("toggle", "lens_bend"))
    commands.apply_menu_action(st, ("lens_vignette", 0.4))
    commands.apply_menu_action(st, ("lens_aberration", 7))
    commands.apply_menu_action(st, ("lens_sensor", 0.25))
    commands.apply_menu_action(st, ("lens_soft", 0.5))
    commands.apply_menu_action(st, ("toggle", "lens"))
    size = struct.calcsize(protocol.LENS_FMT)
    data = worker.stdin.getvalue()
    sent = [struct.unpack(protocol.LENS_FMT, data[i:i + size])
            for i in range(0, len(data), size)]
    got = [(flags & protocol.LENS_FLAG_ON, (flags & protocol.LENS_FLAG_BEND) >> 1,
            round(fov), round(noise, 2), (flags >> 8) & 255, (flags >> 16) & 255,
            (flags >> 24) & 255, (flags >> protocol.LENS_SHIFT_SOFT) & protocol.LENS_SOFT_MAX)
           for _m, flags, fov, noise, _p, _sat, _ev in sent]
    print(f"    commands sent: {got}")
    top = int(settings_io.LENS_FOV_MAX)
    # The tone and the corner softness are two amounts (#168): the sensor
    # moves only the top byte, the softness only its own six bits.
    want = [(1, 1, 110, 0.3, 0, 0, 0, 0), (1, 1, 134, 0.3, 0, 0, 0, 0),
            (1, 1, top, 0.3, 0, 0, 0, 0), (1, 1, top, 0.55, 0, 0, 0, 0),
            (1, 0, top, 0.55, 0, 0, 0, 0), (1, 0, top, 0.55, 102, 0, 0, 0),
            (1, 0, top, 0.55, 102, 255, 0, 0), (1, 0, top, 0.55, 102, 255, 64, 0),
            (1, 0, top, 0.55, 102, 255, 64, 32), (0, 0, top, 0.55, 102, 255, 64, 32)]
    if len(data) % size or got != want or any(m != protocol.LENS_MAGIC for m, *_ in sent):
        failures.append(f"the LENS commands on the pipe were {got}, expected {want}")
    if st.cfg != {"lens": False, "lens_fov": settings_io.LENS_FOV_MAX, "lens_noise": 0.55,
                  "lens_bend": False, "lens_vignette": 0.4, "lens_aberration": 1.0,
                  "lens_sensor": 0.25, "lens_soft": 0.5}:
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
        for raw, want in (('"x"', 0.0), ("NaN", 0.0), ("-1", 0.0), ("7", 1.0), ("0.42", 0.4)):
            path.write_text('{"lens_sensor": %s}' % raw, encoding="utf-8")
            got = settings_io.load_config(path).get("lens_sensor")
            if got != want:
                failures.append(f"lens_sensor {raw} loaded as {got} (expected {want})")
        # #164: the colour pair is signed, -1..1 in 0.05 steps, 0 = as it is.
        for raw, want in (('"x"', 0.0), ("NaN", 0.0), ("-7", -1.0), ("7", 1.0), ("-0.42", -0.4)):
            path.write_text('{"lens_saturation": %s, "lens_exposure": %s}' % (raw, raw),
                            encoding="utf-8")
            cfg = settings_io.load_config(path)
            if cfg.get("lens_saturation") != want or cfg.get("lens_exposure") != want:
                failures.append(f"colour {raw} loaded as {cfg.get('lens_saturation')}, "
                                f"{cfg.get('lens_exposure')} (expected {want})")
        path.write_text("{}", encoding="utf-8")
        if settings_io.load_config(path).get("lens_bend") is not True:
            failures.append("a config without lens_bend does not keep the fisheye on")
        path.write_text("{}", encoding="utf-8")
        menu = types.SimpleNamespace(user_scale=1.0, user_height=None, state={}, mini=False,
                                     mini_rows=(), offset=(0, 0))
        st = types.SimpleNamespace(
            cfg_path=path, cfg={"profile": "Natural", "lens": True, "lens_fov": 140.0,
                                "lens_bend": False, "lens_vignette": 0.35},
            params=dict(settings_io.PROFILES["Natural"], style=1), monitor=0, lang="en",
            work_scale=1.0, split_pos=0.0, startup_menu=False, nr_small=True,
            display=types.SimpleNamespace(menu=menu))
        settings_io.save_menu_layout(st)
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("lens") is not True or saved.get("lens_fov") != 140.0:
            failures.append(f"the lens was saved as {saved.get('lens')}, {saved.get('lens_fov')}")
        if saved.get("lens_bend") is not False or saved.get("lens_vignette") != 0.35:
            failures.append(f"the camera was saved as bend={saved.get('lens_bend')}, "
                            f"vignette={saved.get('lens_vignette')}")


def _check_colour(failures: list) -> None:
    """#164: saturation and exposure - centred sliders in the camera block,
    and the pair rides LENS to the worker."""
    import commands
    import protocol
    menu = _menu("en", lens_saturation=-0.2, lens_exposure=0.5)
    for key, text in (("lens_saturation", "-20%"), ("lens_exposure", "+0.50 EV")):
        item = _item(menu, key)
        if item is None or item.lo != -1.0 or item.hi != 1.0 \
                or item.extra.get("value_text") != text:
            failures.append(f"the {key} slider is missing or wrong: "
                            f"{item and (item.lo, item.hi, item.extra.get('value_text'))}")
    stepped = menu._step_slider(_item(menu, "lens_exposure"), +1)
    if stepped != [("lens_exposure", 0.55)]:
        failures.append(f"one keyboard step of the exposure gave {stepped}")

    class Worker:
        pid = 77

        def __init__(self):
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    st = types.SimpleNamespace(cfg={"lens": True}, worker=Worker(), worker_failed=False,
                               display=types.SimpleNamespace(
                                   menu=types.SimpleNamespace(set_state=lambda d: None)))
    commands.apply_menu_action(st, ("lens_saturation", -0.4))
    commands.apply_menu_action(st, ("lens_exposure", 3.0))
    size = struct.calcsize(protocol.LENS_FMT)
    data = st.worker.stdin.getvalue()
    last = struct.unpack(protocol.LENS_FMT, data[-size:]) if len(data) >= size else None
    if st.cfg.get("lens_saturation") != -0.4 or st.cfg.get("lens_exposure") != 1.0:
        failures.append(f"the colour commands stored {st.cfg}")
    if last is None or round(last[5], 2) != -0.4 or round(last[6], 2) != 1.0:
        failures.append(f"the LENS command carries {last and last[5:]}, not (-0.4, 1.0)")


def main() -> int:
    failures: list = []
    pygame.init()
    pygame.display.set_mode((64, 64))
    try:
        _check_menu(failures)
        _check_colour(failures)
    finally:
        pygame.quit()
    _check_commands(failures)
    _check_config(failures)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the camera's switches, angle and amounts reach the worker as one command "
          "each and are saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
