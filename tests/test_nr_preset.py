"""The NR model preset: the runtime's default, A or B (v2.6.0).

The NR runtime 310.9.16 carries two models: preset B, new - 24% smaller and
up to twice as fast by NVIDIA's figures - and its default; preset A, the
previous model. 310.8 has one model and ignores the hint. The worker passes
NS_NR_PRESET as the feature's Hint.Render.Preset when it creates it, and
logs the runtime's version with the hint it asked for.

Checked without a GPU:

1. the setting is validated: "default", "a" or "b" in any case, anything else
   is "default"; the menu payload carries it;
2. the Fine tuning block has the three-way switch, and a pick sends it;
3. the command sets the config and NS_NR_PRESET (0 / 1 / 2) and restarts the
   worker once; picking the current value restarts nothing;
4. the bundled runtime carries the hint's name (the parameter is real);
5. #179: a runtime with one model (310.8) shows a line naming it instead of a
   switch that changes nothing; the version is read from the DLL's resource.

The worker's "[pure] NR runtime <version> ... preset hint" line is checked by
test_lens.py, which runs the worker (GPU).

Fails before the change: there was no setting, and NS_NR_PRESET was only an
environment variable for sweeps.

Run:  runtime\\python.exe tests\\test_nr_preset.py
"""
import contextlib
import io
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))


def main() -> int:
    failures = []
    import settings_io

    # 1. Validation.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        for raw, want in (('"B"', "b"), ('"a"', "a"), ('"default"', "default"),
                          ('"c"', "default"), ("3", "default"), ("null", "default")):
            path.write_text('{"nr_preset": %s}' % raw, encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                got = settings_io.load_config(path).get("nr_preset")
            if got != want:
                failures.append(f"nr_preset {raw} loaded as {got!r}, not {want!r}")
    if settings_io.NR_PRESET_HINTS != {"default": "0", "a": "1", "b": "2"}:
        failures.append(f"the hints are {settings_io.NR_PRESET_HINTS}")

    # 2. The menu.
    import pygame
    pygame.init()
    pygame.display.set_mode((64, 64))
    import fonts
    import overlay_ui as ui
    menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
    menu.visible = True
    menu.page = "main"
    menu.set_state({"nr": True, "gpu_ok": True, "profile": "Natural",
                    "profiles": ["Natural"], "nr_preset": "default"})
    menu.folds = {"tune"}
    menu.layout(1920, 1080)
    seg = next((i for i in menu.items if i.kind == "segmented" and i.key == "nr_preset"), None)
    if seg is None:
        failures.append("the Fine tuning block has no NR preset switch")
    elif menu._pick("nr_preset", "b") != [("nr_preset", "b")]:
        failures.append("picking B does not send it")
    menu.folds = set()
    menu.layout(1920, 1080)
    if any(i.key == "nr_preset" for i in menu.items):
        failures.append("the NR preset switch shows with its block closed")
    # #179: a runtime with one model gets no switch - a line says what runs.
    menu.folds = {"tune"}
    menu.set_state({"nr_presets": False, "nr_runtime_version": "310.8.0.0"})
    menu.layout(1920, 1080)
    row = next((i for i in menu.items if i.key == "nr_preset"), None)
    if row is None or row.kind != "info" or "310.8.0.0" not in str(row.extra.get("value")):
        failures.append(f"with one model the preset row is {row and (row.kind, row.extra)}")
    menu.set_state({"nr_presets": True})
    menu.layout(1920, 1080)
    if not any(i.kind == "segmented" and i.key == "nr_preset" for i in menu.items):
        failures.append("with two models the switch is gone")
    pygame.quit()

    # #179: the runtime's version, read from the DLL without loading it.
    import nr_runtime
    if not nr_runtime.has_presets((310, 9, 16, 0)) or nr_runtime.has_presets((310, 8, 0, 0))             or nr_runtime.has_presets(None):
        failures.append("has_presets does not draw the line at 310.9.16")
    dll = Path(BASE) / "native" / "nvngx_dlssnr.dll"
    if dll.is_file():
        version = nr_runtime.file_version(dll)
        print(f"    bundled runtime: {version}")
        if version is None or version[0] != 310:
            failures.append(f"the bundled runtime's version read as {version}")
    payload_keys = settings_io._nr_runtime_current()
    if not (isinstance(payload_keys, tuple) and len(payload_keys) == 2):
        failures.append(f"the payload's runtime check gave {payload_keys!r}")

    # 3. The command: config, environment, one restart.
    import commands
    import pipeline
    restarts = []
    real = (pipeline.teardown_pipeline, pipeline.rebuild_pipeline, settings_io.save_menu_layout)
    pipeline.teardown_pipeline = lambda st: restarts.append("down")
    pipeline.rebuild_pipeline = lambda st, note: restarts.append("up")
    settings_io.save_menu_layout = lambda st: True
    saved_env = os.environ.get("NS_NR_PRESET")
    try:
        st = types.SimpleNamespace(cfg={}, lang="en")
        with contextlib.redirect_stdout(io.StringIO()):
            commands.apply_menu_action(st, ("nr_preset", "b"))
            if (st.cfg.get("nr_preset"), os.environ.get("NS_NR_PRESET"), restarts) != \
                    ("b", "2", ["down", "up"]):
                failures.append(f"B gave cfg {st.cfg}, env {os.environ.get('NS_NR_PRESET')}, "
                                f"restarts {restarts}")
            restarts.clear()
            commands.apply_menu_action(st, ("nr_preset", "b"))
            commands.apply_menu_action(st, ("nr_preset", "x"))
            if restarts:
                failures.append(f"the same or a bad value restarted the worker: {restarts}")
            commands.apply_menu_action(st, ("nr_preset", "a"))
            if os.environ.get("NS_NR_PRESET") != "1" or restarts != ["down", "up"]:
                failures.append(f"A gave env {os.environ.get('NS_NR_PRESET')}, "
                                f"restarts {restarts}")
    finally:
        pipeline.teardown_pipeline, pipeline.rebuild_pipeline, settings_io.save_menu_layout = real
        if saved_env is None:
            os.environ.pop("NS_NR_PRESET", None)
        else:
            os.environ["NS_NR_PRESET"] = saved_env

    # 4. The parameter is one the runtime knows.
    dll = Path(BASE) / "native" / "nvngx_dlssnr.dll"
    if dll.is_file() and b"DLSSNR.Hint.Render.Preset" not in dll.read_bytes():
        failures.append("the bundled runtime has no DLSSNR.Hint.Render.Preset")

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
