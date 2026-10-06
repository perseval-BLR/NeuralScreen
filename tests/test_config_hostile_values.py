"""A hand-edited config.json must not abort the launch (audit H5).

_validate_config checked width, height, warmup, the four parameters,
work_scale, lang, motion_backend, frame_generation, frame_multiplier,
frame_limit_*, the two directories, screenshot_* and presets. It never looked
at monitor, gpu, menu_offset, menu_scale, menu_height, theme or hotkeys - and
startup reads all of them with a bare int()/float()/attribute access:

    startup: int(monitor_cfg)              monitor = {"a": 1}  -> TypeError
    startup: [int(saved_offset[0]), ...]   ["left", "top"]    -> ValueError
    startup: str(int(gpu))                 gpu = {"a": 1}     -> TypeError
    startup: build_bindings(cfg["hotkeys"]) hotkeys = [..]    -> AttributeError
    startup: float(cfg.get("menu_scale"))  menu_scale = "big" -> ValueError

configure() raises before any window exists, so the user gets a modal box
reading "NeuralScreen failed to start: invalid literal for int() with base 10:
'left'" - not one word about which field is wrong - and the program does not
start at all until they hand-edit the file again.

The rule this test pins is the one the validator already follows for a stale
profile and a shrunken parameter range: a value that cannot be used falls back
to the default and says so in the log. Refusing to start is reserved for a
file that is not a config at all.

Checked here through the real load_config, then through the reads startup
performs on the result - the second half is what makes this a launch test and
not a validator test.

Run:  runtime\\python.exe tests\\test_config_hostile_values.py
"""
import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import settings_io  # noqa: E402
from hotkeys import build_bindings  # noqa: E402

DEFAULTS = json.loads((BASE / "config.default.json").read_text(encoding="utf-8"))

# Every value below is something a text editor or a half-broken export
# produces. None of them may abort the launch.
HOSTILE = {
    "menu_offset non-numeric": {"menu_offset": ["left", "top"]},
    "menu_offset nested": {"menu_offset": [[1], [2]]},
    "menu_offset a string": {"menu_offset": "middle"},
    "menu_offset wrong length": {"menu_offset": [1, 2, 3]},
    "monitor as a dict": {"monitor": {"a": 1}},
    "monitor as a list": {"monitor": [1]},
    "monitor a word": {"monitor": "first"},
    "gpu as a dict": {"gpu": {"a": 1}},
    "gpu a word": {"gpu": "first"},
    "gpu as a list": {"gpu": [0]},
    "menu_scale a word": {"menu_scale": "big"},
    "menu_scale null": {"menu_scale": None},
    "menu_height a word": {"menu_height": "tall"},
    "menu_height negative": {"menu_height": -5},
    "theme unknown": {"theme": "purple"},
    "theme a dict": {"theme": {"name": "dark"}},
    "hotkeys as a list": {"hotkeys": ["toggle"]},
    "hotkeys value not a string": {"hotkeys": {"toggle": 5}},
    "hotkeys as a string": {"hotkeys": "toggle"},
    "presets as a list": {"presets": ["a"]},
    "presets entry as a string": {"presets": {"P1": "x"}},
    "recording_dir as a dict": {"recording_dir": {"p": 1}},
    "screenshot_dir as a list": {"screenshot_dir": [1]},
    "work_scale a word": {"work_scale": "half"},
    "lang as a dict": {"lang": {"code": "en"}},
    "motion_backend a number": {"motion_backend": 7},
}
# Deliberately NOT in the list above: width / height / warmup. Those are
# required to raise with a field-specific message - the file names it, and
# tests/test_config.py pins that. The difference is the point of this test:
# a raise is right when the message names the field, and wrong when it is a
# bare int() traceback for a field the validator never looked at.


def _reads_startup_does(cfg: dict, cfg_path=None) -> list:
    """Actually run startup.configure on a config path - no hand-copied reads.

    The old version re-typed the reads (`int(cfg["width"])`,
    `str(int(gpu))`, `build_bindings(...)`, ...) and never imported startup,
    so a read added to startup.configure with no validator behind it would
    not be caught here (audit: WEAK). configure() is safe to call in-process:
    it reads the config, applies the environment and enumerates monitors; it
    does not open capture or create a window.

    Returns the failures, so the caller can report them per case.
    """
    import types

    import startup

    if cfg_path is None:
        # The caller has already validated this dict; write it out so the
        # real load_config inside configure() sees exactly the same bytes.
        cfg_path = Path(tempfile.mkdtemp(prefix="ns-cfg-hostile-run-")) / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    bad = []
    st = types.SimpleNamespace(cfg_path=str(cfg_path))
    try:
        startup.configure(st)
    except Exception as exc:
        bad.append(f"configure: {type(exc).__name__}: {exc}")
        return bad

    # The values configure is expected to have produced. A read that is
    # present but silently wrong (a fallback that never fires) shows up here.
    for name, want in (("width", int), ("height", int), ("warmup", int),
                       ("work_scale", float), ("lang", str)):
        if not hasattr(st, name):
            bad.append(f"configure did not set st.{name} - a config read "
                       f"vanished")
        elif not isinstance(getattr(st, name), want):
            bad.append(f"st.{name} is {getattr(st, name)!r} "
                       f"({type(getattr(st, name)).__name__}), expected {want.__name__}")
    if hasattr(st, "monitor") and not isinstance(st.monitor, int):
        bad.append(f"st.monitor is {st.monitor!r}, expected an int")
    return bad


def main() -> int:
    failures = []
    root = Path(tempfile.mkdtemp(prefix="ns-cfg-hostile-"))

    for label, patch in HOSTILE.items():
        cfg = dict(DEFAULTS)
        cfg.update(patch)
        path = root / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        try:
            loaded = settings_io.load_config(path)
        except Exception as exc:
            failures.append(f"{label}: load_config raised "
                            f"{type(exc).__name__}: {exc} - the launch dies "
                            f"before a window exists")
            continue
        bad = _reads_startup_does(loaded, cfg_path=path)
        if bad:
            failures.append(f"{label}: startup's own reads fail on the "
                            f"validated config: {'; '.join(bad)}")

    # The defaults must still come through untouched: this validator is
    # load-bearing for config.default.json, which load_config requires to be
    # already canonical.
    path = root / "default.json"
    path.write_text(json.dumps(DEFAULTS), encoding="utf-8")
    try:
        loaded = settings_io.load_config(path)
    except Exception as exc:
        failures.append(f"the shipped defaults no longer validate: "
                        f"{type(exc).__name__}: {exc}")
    else:
        for key, want in (("monitor", 0), ("gpu", 0), ("menu_offset", [0, 0]),
                          ("menu_scale", 1.0), ("menu_height", None)):
            if loaded.get(key) != want:
                failures.append(f"the shipped default {key} changed: "
                                f"{loaded.get(key)!r} != {want!r}")

    # And a value that really is unusable still starts, on the default.
    cfg = dict(DEFAULTS)
    cfg.update({"monitor": {"a": 1}, "gpu": "first",
                "menu_offset": ["left", "top"], "hotkeys": ["toggle"]})
    path = root / "recover.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    loaded = settings_io.load_config(path)
    if loaded["monitor"] != 0:
        failures.append(f"a broken monitor did not fall back to 0: "
                        f"{loaded['monitor']!r}")
    if loaded.get("gpu") is not None and not isinstance(loaded["gpu"], int):
        failures.append(f"a broken gpu did not fall back: {loaded['gpu']!r}")
    if loaded["menu_offset"] != [0, 0]:
        failures.append(f"a broken menu_offset did not fall back to [0, 0]: "
                        f"{loaded['menu_offset']!r}")

    # A switch written as a number or null means one thing to every reader.
    # Left raw, `0` read as off through bool() and as ON through `is not
    # False` - the panel and the program disagreed about the same key.
    for key, raw, want in (("hotkeys_enabled", 0, False),
                           ("keep_speed_when_hidden", 0, False),
                           ("gpu_record", 0.0, False),
                           ("hotkeys_enabled", 1, True),
                           ("keep_speed_when_hidden", None,
                            DEFAULTS.get("keep_speed_when_hidden", True))):
        cfg = dict(DEFAULTS)
        cfg[key] = raw
        path = root / "switch.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        got = settings_io.load_config(path).get(key)
        if got is not want:
            failures.append(f"{key}={raw!r} validated to {got!r}, not {want!r} "
                            f"- the readers that use `is not False` and the "
                            f"ones that use bool() disagree on it")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: no hand-edited value aborts the launch - each falls back and "
          "says so")
    return 0


if __name__ == "__main__":
    sys.exit(main())
