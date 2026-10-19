"""A runtime in native\\libraries\\ that fails here falls back to the bundled one (#175).

The official NR runtime 310.9.16 passes the NVIDIA-signature gate but refuses
to create the feature on an RTX 3060 Ti (0xBAD00002, PlatformError), where the
bundled 310.8 runs. The start-up check then failed 0/0 and blocked the start
with a dialog - from the user's side, "v2.6.0 crashes with the new DLL".

Checked on the real start-up gate, with the preflight stood in:

1. the BYO runtime fails and the bundled one is there: the check runs again
   with NS_NR_NO_BYO=1, passes, no dialog, st.byo_fallback says so; the
   runtime path and the route then name the bundled runtime, so the cached
   verdict for the BYO file is not reused;
2. no bundled runtime, or a runtime named by NS_NR_DLL: no fallback, the
   dialog as before;
3. the bundled runtime failing too: the dialog, once - no loop.

Fails before the change: the first failure went straight to the dialog.

Run:  runtime\\python.exe tests\\test_byo_fallback.py
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))

import compatibility_runtime as runtime  # noqa: E402
from compatibility import (CompatibilityResult, CompatibilityStatus,  # noqa: E402
                           StageStatus)


def _result(ok: bool) -> CompatibilityResult:
    if ok:
        return CompatibilityResult("k", CompatibilityStatus.PASS, "evaluate", 3, 3, 3,
                                   StageStatus.SUCCESS.value)
    return CompatibilityResult("k", CompatibilityStatus.FAILED, "create", 0, 0, 3,
                               StageStatus.ERROR.value)


def _gate(native: Path, bundled_ok: bool, env: dict) -> tuple:
    """Run the gate; the preflight passes only on the bundled runtime when
    bundled_ok. Returns (gate result, st, dialogs shown, preflight calls)."""
    calls, shown = [], []

    def preflight(st, force=False):
        byo = runtime.runtime_path().parent.name == "libraries"
        calls.append("byo" if byo else "bundled")
        return _result(not byo and bundled_ok)

    def box(_hwnd, text, _title, _flags):
        shown.append(text)
        return 2  # IDCANCEL

    st = SimpleNamespace(lang="en", cfg={})
    saved = {k: os.environ.get(k) for k in ("NS_NR_DLL", "NS_NR_NO_BYO")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        os.environ.update(env)
        with mock.patch.object(runtime, "NATIVE_DIR", native), \
                mock.patch.object(runtime, "run_preflight", preflight), \
                mock.patch.object(runtime, "create_support_bundle",
                                  return_value=Path("bundle.zip")), \
                mock.patch.object(runtime.ctypes.windll.user32, "MessageBoxW", box):
            ok = runtime.startup_gate(st)
            path = runtime.runtime_path()
            source = runtime._runtime_route()["runtime_source"]
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return ok, st, shown, calls, path, source


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        native = Path(tmp)
        (native / "libraries").mkdir()
        (native / "libraries" / "nvngx_dlssnr.dll").write_bytes(b"byo 310.9.16")
        (native / "nvngx_dlssnr.dll").write_bytes(b"bundled 310.8")

        # 1. Falls back, passes, says so.
        ok, st, shown, calls, path, source = _gate(native, True, {})
        if not ok or shown or calls != ["byo", "bundled"]:
            failures.append(f"fallback: gate {ok}, dialogs {len(shown)}, runs {calls}")
        if not getattr(st, "byo_fallback", False):
            failures.append("the fallback was not recorded for the start-up notice")
        if path != native / "nvngx_dlssnr.dll" or source != "bundled-byo-skipped":
            failures.append(f"after the fallback the runtime is {path} ({source})")

        # 3. The bundled one fails too: one dialog, no loop.
        ok, st, shown, calls, _p, _s = _gate(native, False, {})
        if ok or len(shown) != 1 or calls != ["byo", "bundled"]:
            failures.append(f"both failing: gate {ok}, dialogs {len(shown)}, runs {calls}")

        # 2. A runtime named by NS_NR_DLL is the user's explicit choice.
        ok, st, shown, calls, _p, _s = _gate(
            native, True, {"NS_NR_DLL": str(native / "libraries" / "nvngx_dlssnr.dll")})
        if ok or len(shown) != 1 or calls != ["byo"]:
            failures.append(f"NS_NR_DLL: gate {ok}, dialogs {len(shown)}, runs {calls}")

        # 2. No bundled runtime to fall back to.
        (native / "nvngx_dlssnr.dll").unlink()
        ok, st, shown, calls, _p, _s = _gate(native, True, {})
        if ok or len(shown) != 1 or calls != ["byo"]:
            failures.append(f"no bundled runtime: gate {ok}, dialogs {len(shown)}, runs {calls}")

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
