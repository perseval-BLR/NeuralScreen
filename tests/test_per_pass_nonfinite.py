r"""A per-pass set with NaN or Infinity in config.json is dropped, not sent.

Python's json reads the NaN and Infinity literals, and the range clamp let NaN
through untouched (min/max with NaN return NaN). The main sliders and presets
already refused non-finite numbers; the per-pass set did not, so a hand-edited
"nr_pass_params" with NaN reached the worker's wire as a strength and was
written back by the next save. A half-written set is dropped whole, as the
loader does for a missing key - a repaired one would change the picture in a
way nobody asked for.

Checked through settings_io.clean_per_pass and the full config load.

Run:  runtime\python.exe tests\test_per_pass_nonfinite.py
"""
import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import settings_io  # noqa: E402

GOOD = '{"style":1,"intensity":%s,"local_tone":0,"local_structure":1,"skin_structure":-1}'


def main() -> int:
    failures = []
    for literal in ("NaN", "Infinity", "-Infinity"):
        got = settings_io.clean_per_pass(json.loads(GOOD % literal))
        print(f"    intensity {literal}: {got}")
        if got is not None:
            failures.append(f"a set with intensity {literal} was kept: {got}")
    kept = settings_io.clean_per_pass(json.loads(GOOD % "0.5"))
    if not kept or kept["intensity"] != 0.5:
        failures.append(f"an ordinary set was not kept: {kept}")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text('{"nr_passes": 2, "nr_pass_params": %s}' % (GOOD % "NaN"),
                        encoding="utf-8")
        cfg = settings_io.load_config(path)
        if cfg.get("nr_pass_params") is not None:
            failures.append(f"load_config kept {cfg.get('nr_pass_params')}")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a per-pass set with a non-finite number is dropped whole")
    return 0


if __name__ == "__main__":
    sys.exit(main())
