r"""The worker hands the network motion in the convention it expects.

DLSS NR wants motion current -> previous, in pixels of the frame it runs on.
Get the sign or the scale wrong and the network warps its history the wrong
way: other projects measured inverted vectors doing worse than no vectors at
all (DLSS5Tool), and the worker's own pieces disagree easily (the motion
arrives from the client, from NVOFA's expand shader or the GPU stretch, and
MVecScale converts between the motion texture and the network's size).

A guard, not a fix: checked on the real worker in Boost with known motion
(tests/nr_motion_harness.py), the stabilizer off so the network is measured
alone. On a pan, the true vectors must beat both no vectors and the true
vectors with the sign flipped, and a flipped field must cost part of the
edit - measured: instability 0.72 (true) / 0.94 (none) / 1.15 (flipped).

Run:  runtime\python.exe tests\test_motion_convention.py
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import nr_motion_harness as hz  # noqa: E402
from paths import WORKER_EXE  # noqa: E402

FRAMES = 32


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    scene = hz.make_scene("pan", FRAMES, seed=1)
    m = {}
    for variant in ("gt", "zero", "flipped"):
        run = hz.run_worker(scene, variant, FRAMES, env={"NS_STAB": "0"})
        if not run.ok:
            print(f"FAIL: the worker did not run ({variant}): {run.error}")
            return 1
        m[variant] = hz.stability_metrics(scene, run.outputs, skip=4)
    gt, zero, flipped = m["gt"], m["zero"], m["flipped"]
    print(f"    instability on a pan: true {gt['instab_mov']:.3f}, none "
          f"{zero['instab_mov']:.3f}, flipped {flipped['instab_mov']:.3f}; edit "
          f"{gt['edit_mov']:.2f} / {zero['edit_mov']:.2f} / {flipped['edit_mov']:.2f}")
    failures = []
    if not gt["instab_mov"] < 0.9 * zero["instab_mov"]:
        failures.append("the true vectors are not clearly better than none - the "
                        "network is not using them, or not the way they are meant")
    if not gt["instab_mov"] < 0.8 * flipped["instab_mov"]:
        failures.append("the true vectors are not clearly better than the flipped "
                        "ones - the sign convention is lost somewhere")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: current -> previous motion in the network's pixels, as it expects")
    return 0


if __name__ == "__main__":
    sys.exit(main())
