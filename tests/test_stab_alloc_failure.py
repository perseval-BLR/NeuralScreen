r"""Stabilizer textures that do not fit are not tried again on every frame.

The edit stabilizer (native/stabilizer.inl) makes five work-size textures the
first time it runs after a resize. When video memory is short - a large Boost
work size, cascade features holding the rest - one of them fails, and the
pass stays off. It used to stay off by trying again on every frame: allocate,
free, wait on the fence, and log one more "[stab] textures ... could not be
created" line, all session long.

Checked on the worker in Boost with the allocation forced to fail
(NS_TEST_FAIL_STAGE=stab-alloc): over 24 frames of a pan the failure is
logged once, and every frame is still answered with pixels - the picture
goes out unsteadied, as it does with NS_STAB=0.

Run:  runtime\python.exe tests\test_stab_alloc_failure.py
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import nr_motion_harness as hz  # noqa: E402
from paths import WORKER_EXE  # noqa: E402

FRAMES = 24


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    scene = hz.make_scene("pan", FRAMES, seed=1)
    run = hz.run_worker(scene, "gt", FRAMES, env={"NS_TEST_FAIL_STAGE": "stab-alloc"})
    failures = []
    if not run.ok:
        failures.append(f"the worker run failed: {run.error}")
    if len(run.outputs) != FRAMES:
        failures.append(f"{len(run.outputs)} of {FRAMES} frames came back")
    lines = [line for line in run.log.splitlines()
             if "[stab] textures" in line and "could not be created" in line]
    print(f"    {len(lines)} allocation-failure line(s) over {len(run.outputs)} frames")
    if not lines:
        failures.append("the forced failure was never reached - the check proves nothing")
    elif len(lines) > 1:
        failures.append(f"the failed allocation was retried and logged {len(lines)} times")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a stabilizer that does not fit is tried once per size, not once per frame")
    return 0


if __name__ == "__main__":
    sys.exit(main())
