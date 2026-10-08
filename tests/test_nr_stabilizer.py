r"""The NR edit is steadied over time without losing the effect or ghosting.

The network's edit shimmers from frame to frame - fed the same frame it moves
by ~0.26 of 255 on average and up to 3 every frame - and moving content adds
the motion field's error on top ("jelly", #151/#141/#95). The stabilizer
(native/stabilizer.inl) filters only the edit, at the work resolution, along
the motion, with a per-pixel trust test; the native picture underneath is
never delayed.

Checked on the real worker in Boost with known motion (tests/nr_motion_harness.py),
the stabilizer on (the default) against NS_STAB=0, on the same frames:
* a pan and a moving object: the edit's frame-to-frame instability falls by
  at least a quarter (measured: -43% / -45%), the still background around the
  object too, and the strength of the edit stays within 5%;
* NVOFA-like coarse vectors: the halo around the object is not made worse;
* zero vectors on a moving object (a motion field that is simply wrong): the
  output's own warp error does not grow - no ghosts when the vectors lie.

Run:  runtime\python.exe tests\test_nr_stabilizer.py
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


def _metrics(scene_name: str, variant: str, stab: str) -> dict:
    scene = hz.make_scene(scene_name, FRAMES, seed=1)
    run = hz.run_worker(scene, variant, FRAMES, env={"NS_STAB": stab})
    if not run.ok:
        raise RuntimeError(f"{scene_name}/{variant} NS_STAB={stab}: {run.error}")
    return hz.stability_metrics(scene, run.outputs, skip=4)


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    failures = []
    for scene_name, variant in (("pan", "gt"), ("object", "gt"), ("object", "coarse"),
                                ("object", "zero")):
        off = _metrics(scene_name, variant, "0")
        on = _metrics(scene_name, variant, "1")
        label = f"{scene_name}/{variant}"
        print(f"    {label:14s} instability moving {off['instab_mov']:.3f} -> "
              f"{on['instab_mov']:.3f}, still {off['instab_sta']:.3f} -> "
              f"{on['instab_sta']:.3f}, halo {off['instab_halo']:.3f} -> "
              f"{on['instab_halo']:.3f}, edit {off['edit_mov']:.2f} -> {on['edit_mov']:.2f}, "
              f"output warp {off['warp_out_mov']:.3f} -> {on['warp_out_mov']:.3f}")
        if variant == "gt":
            if not on["instab_mov"] <= 0.75 * off["instab_mov"]:
                failures.append(f"{label}: the moving edit was not steadied "
                                f"({off['instab_mov']:.3f} -> {on['instab_mov']:.3f})")
            if abs(on["edit_mov"] - off["edit_mov"]) > 0.05 * off["edit_mov"]:
                failures.append(f"{label}: the edit's strength changed "
                                f"({off['edit_mov']:.2f} -> {on['edit_mov']:.2f})")
        if scene_name == "object" and variant == "gt":
            if not on["instab_sta"] <= 0.75 * off["instab_sta"]:
                failures.append(f"{label}: the still background was not steadied "
                                f"({off['instab_sta']:.3f} -> {on['instab_sta']:.3f})")
        if variant == "coarse" and on["instab_halo"] > off["instab_halo"] * 1.05:
            failures.append(f"{label}: the halo around the object got worse "
                            f"({off['instab_halo']:.3f} -> {on['instab_halo']:.3f})")
        if variant == "zero" and on["warp_out_mov"] > off["warp_out_mov"] * 1.05:
            failures.append(f"{label}: with wrong (zero) vectors the output ghosts "
                            f"({off['warp_out_mov']:.3f} -> {on['warp_out_mov']:.3f})")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the NR edit is steadied on motion and still content, keeps its "
          "strength, and wrong vectors do not ghost")
    return 0


if __name__ == "__main__":
    sys.exit(main())
