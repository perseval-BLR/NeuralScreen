r"""A preset at intensity 0 does not get the card blocked as unsupported.

The startup preflight runs the neural runtime on three synthetic frames and,
since v2.2.0, refuses a runtime that answers success but changes nothing
("no effect" - an unsupported card on a patched runtime). The verdict is
cached per runtime, driver and card, with no parameters in the key - but the
probe ran with the USER's parameters. Intensity 0 is inside the slider range
and a preset can hold it, and at intensity 0 the runtime returns the frame
byte for byte (measured: mean and max difference 0, Natural style). So the
next preflight - after any app or driver update - said "no effect", cached it
as UNSUPPORTED with no expiry, and startup was blocked on a card that works.

Checked on the real worker through compatibility_runtime.run_preflight with
the user's parameters at intensity 0 and a throwaway cache file (the real
compatibility-cache.json is not touched): the verdict is PASS.

Run:  runtime\python.exe tests\test_preflight_user_params.py
"""
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

from paths import WORKER_EXE  # noqa: E402


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    import compatibility_runtime as cr
    from settings_io import PROFILES, load_config

    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        cr.CACHE_PATH = Path(tmp) / "compatibility-cache.json"
        cfg = load_config(Path(tmp) / "config.json")
        st = types.SimpleNamespace(
            cfg=cfg, params=dict(PROFILES["Natural"], style=1, intensity=0.0),
            environment={"driver": "test"})
        result = cr.run_preflight(st, force=True)
        print(f"    verdict with the user at intensity 0: {result.status.value} "
              f"({result.passed}/{result.attempted}), reason={result.reason}")
        if not result.is_pass:
            failures.append(f"intensity 0 in the user's settings made the preflight "
                            f"say {result.status.value} ({result.reason})")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the preflight probes with the shipped parameters, not the user's")
    return 0


if __name__ == "__main__":
    sys.exit(main())
