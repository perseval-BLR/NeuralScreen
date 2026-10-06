r"""An old NVIDIA driver is named as the reason, not reported as "failed" (#145).

The report (mc65934374, #145, RTX 5080 on driver 576.88): the compatibility
check stopped at the create stage with an access violation, and the dialog
said only "failed; stage create; passed 0/0". The log had the real answer one
line earlier - the runtime's requirements query returned 0xBAD0000C,
FAIL_OutOfDate - and the closed tickets show the same sequence on 576.80
(#51) and 596.36 (#83): an old driver, and NVIDIA's runtime faulting inside
the create instead of refusing.

What this locks:

* the real worker, with the #145 sequence injected (OutOfDate from the query,
  then a fault in the create), answers CACK category 3 and the preflight
  runner turns it into DRIVER_OUT_OF_DATE - not ERROR;
* the preflight files that as an UNSUPPORTED verdict for this key (the key
  carries the driver version, so an updated driver is checked afresh), with
  the reason kept;
* the dialog the user sees leads with "the driver is too old", naming it.

Run:  runtime\python.exe tests\test_driver_out_of_date.py
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import compatibility_runtime as runtime  # noqa: E402
from compatibility import (CompatibilityKey, CompatibilityResult,  # noqa: E402
                           CompatibilityStatus, CreateRequest, StageStatus)
from paths import WORKER_EXE  # noqa: E402

KEY = CompatibilityKey(
    "2.1.9", "a" * 64, "b" * 64,
    {"index": 0}, "576.88", {"width": 640, "height": 360},
)
PARAMS = {"style": 1, "auto_mask": 0, "intensity": 1.0, "local_tone": 0.5,
          "local_structure": 1.0, "skin_structure": -1.0}


def check_worker(failures: list) -> None:
    os.environ["NS_TEST_FAIL_STAGE"] = "create-old-driver"
    try:
        runner = runtime.NativeSelfTestRunner(PARAMS)
        try:
            outcome = runner.create(CreateRequest(KEY, 640, 360))
        finally:
            runner.close()
    finally:
        os.environ.pop("NS_TEST_FAIL_STAGE", None)
    status = getattr(outcome.status, "value", outcome.status)
    if status != StageStatus.DRIVER_OUT_OF_DATE.value:
        failures.append(f"an OutOfDate driver whose create faults came back "
                        f"as {status!r}, not driver_out_of_date - the user is "
                        f"told only 'failed'")
    if not any("stage=create-old-driver" in ln for ln in runner.logs):
        failures.append("the injection did not run - the check proves nothing")


def check_verdict(failures: list) -> None:
    """The preflight's own mapping, through the real CompatibilityPreflight."""
    from compatibility import CompatibilityPreflight

    class _Cache:
        def get(self, key):
            return None

        def put(self, key, result):
            self.result = result

        def reset(self, key):
            pass

    class _Runner:
        def create(self, request):
            from compatibility import StageOutcome
            return StageOutcome(StageStatus.DRIVER_OUT_OF_DATE)

        def close(self):
            pass

    cache = _Cache()
    result = CompatibilityPreflight(cache, _Runner).run(KEY)
    if result.status is not CompatibilityStatus.UNSUPPORTED:
        failures.append(f"an out-of-date driver was filed as {result.status}, "
                        f"not UNSUPPORTED for this driver")
    if result.reason != StageStatus.DRIVER_OUT_OF_DATE.value:
        failures.append(f"the verdict lost its reason: {result.reason!r}")

    # Without a known driver version the key cannot change on an update, so
    # a cached "unsupported" would outlive the fix: it must stay a failure.
    unknown = CompatibilityKey("2.1.9", "a" * 64, "b" * 64, {"index": 0},
                               "unknown", {"width": 640, "height": 360})
    result = CompatibilityPreflight(_Cache(), _Runner).run(unknown)
    if result.status is CompatibilityStatus.UNSUPPORTED:
        failures.append("with the driver version unknown, 'out of date' was "
                        "cached as unsupported - an update would not clear it")


def check_dialog(failures: list) -> None:
    result = CompatibilityResult(KEY.digest, CompatibilityStatus.UNSUPPORTED,
                                 "create", 0, 0, 3,
                                 StageStatus.DRIVER_OUT_OF_DATE.value)
    st = SimpleNamespace(lang="en", cfg={}, environment={"driver": "576.88"})
    shown = []

    def box(_hwnd, text, _title, _flags):
        shown.append(text)
        return 2  # IDCANCEL

    with mock.patch.object(runtime, "run_preflight", return_value=result), \
            mock.patch.object(runtime, "create_support_bundle",
                              return_value=Path("bundle.zip")), \
            mock.patch.object(runtime.ctypes.windll.user32, "MessageBoxW", box):
        runtime.startup_gate(st)
    if not shown:
        failures.append("the gate showed no dialog")
        return
    text = shown[0]
    if "576.88" not in text or "too old" not in text:
        failures.append(f"the dialog does not say the driver is too old: "
                        f"{text[:160]!r}")


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    failures: list = []
    check_worker(failures)
    print("the worker reports the old driver as such: checked")
    check_verdict(failures)
    print("the preflight files it as unsupported for this driver: checked")
    check_dialog(failures)
    print("the dialog leads with the driver: checked")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: an old NVIDIA driver is named as the reason (#145)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
