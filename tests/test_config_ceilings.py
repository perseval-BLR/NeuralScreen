"""width, height and warmup have a ceiling, not only a floor.

_validate_config refused a non-positive or non-integer width, height or
warmup with a message naming the field (tests/test_config.py pins that), and
let any positive integer through. "warmup": 4294967296 then reached
struct.pack of the worker header and the launch died with "failed to start"
and a struct.error, not one word about the field; a 100000-pixel width is a
shared-memory frame of gigabytes, and the worker refuses anything over
7680x4320 anyway.

Checked through load_config, the path the program takes: a value over the
ceiling loads, is pulled to the ceiling, and the worker header packs; a value
inside the range is untouched; zero still raises with the field named.

Run:  runtime\\python.exe tests\\test_config_ceilings.py
"""
import json
import struct
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

from protocol import HEADER_FMT  # noqa: E402
from settings_io import CONFIG_SCHEMA_VERSION, load_config  # noqa: E402

GOOD = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "monitor": 0, "width": 3840, "height": 2160, "fullscreen": True,
    "warmup": 120, "work_scale": 0.65, "lang": "en", "profile": "Natural",
    "intensity": None, "local_tone": None, "local_structure": None,
    "skin_structure": None,
}
CEILINGS = {"width": 7680, "height": 4320, "warmup": 240}


def load(work: Path, **changes):
    path = work / "config.json"
    path.write_text(json.dumps(dict(GOOD, **changes)), encoding="utf-8")
    return load_config(path)


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for key, ceiling in CEILINGS.items():
            for value in (ceiling + 1, 100_000, 2 ** 32, 10 ** 30):
                try:
                    cfg = load(work, **{key: value})
                except Exception as exc:          # noqa: BLE001
                    failures.append(f"{key}={value}: the config did not load: "
                                    f"{type(exc).__name__}: {exc}")
                    continue
                if cfg[key] != ceiling:
                    failures.append(f"{key}={value} loaded as {cfg[key]}, "
                                    f"want the ceiling {ceiling}")
                    continue
                # What startup does with it: the header the worker reads.
                try:
                    struct.pack(HEADER_FMT, 0, cfg["width"], cfg["height"],
                                cfg["warmup"], 0, 0, 0, 1, 1, 0,
                                1.0, 1.0, 1.0, 1.0, 0, 0)
                except struct.error as exc:
                    failures.append(f"{key}={value}: the worker header does "
                                    f"not pack: {exc}")
            if load(work, **{key: ceiling})[key] != ceiling:
                failures.append(f"{key} at its ceiling was changed")
        if load(work, warmup=4)["warmup"] != 4:
            failures.append("an in-range warmup was changed")
        for key in CEILINGS:
            try:
                load(work, **{key: 0})
                failures.append(f"{key}=0 did not raise")
            except ValueError as exc:
                if key not in str(exc):
                    failures.append(f"{key}=0 error does not name the field: {exc}")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: width, height and warmup over their ceiling are pulled to it, "
          "and the worker header packs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
