"""Multipass at native resolution, on the worker: four of four passes build.

The GPU half of test_cascade_work_size (the report in #110). A 1280x720 frame
processed at native with four NR passes asked for:

* at the work size `_work_size` gives it (1278x718, the residual path), the
  worker builds all four - `NR cascade built: asked=4 effective=4` - and a
  frame goes through the whole cascade and comes back full size;
* at 1:1, what the slider at native used to produce, it builds ONE and the
  same line says why ("the network runs at 1:1 ...") instead of saying
  nothing while the panel shows four;
* with Boost off it builds one too, and the reason names Boost - at 1:1 and
  with a work size below the frame alike. The second is what a user's log
  showed: Boost off, four passes saved, and a line blaming "1:1" and advising
  a resolution slider the panel hides without Boost.

Headless, in the converter's shape: no capture, no window.

Run:  runtime\\python.exe tests\\test_cascade_native.py
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import numpy as np  # noqa: E402

from paths import WORKER_EXE  # noqa: E402

W, H = 1280, 720
PASSES = 4
BUILT = re.compile(r"NR cascade built: asked=(\d+) effective=(\d+) allocated=(\d+)(.*)")


def run(work_w: int, work_h: int, boost: bool = True) -> tuple[list, object]:
    """One worker at this work size for a 1280x720 frame, four passes asked."""
    from pipeline import shutdown_worker, start_worker
    from protocol import send_frame, send_resize
    from settings_io import PROFILES

    params = dict(PROFILES["Natural"])
    params["style"] = 1
    upscale = (work_w, work_h) != (W, H)
    full_w, full_h = (W, H) if upscale else (0, 0)
    worker, logs, reader, stop = start_worker(params, work_w, work_h, 2,
                                              full_w, full_h, None)
    try:
        # The pass count reaches the worker only on a resize - the stream
        # header has no field for it (test_nr_passes_wire).
        send_resize(worker, params, work_w, work_h, 2, full_w, full_h,
                    boost, False, PASSES)
        reader.wait_rack(timeout=120.0)
        reader.set_output_size(W, H)
        yy, xx = np.mgrid[0:H, 0:W]
        frame = np.zeros((H, W, 4), np.uint8)
        frame[..., 0] = (xx * 3) & 0xFF
        frame[..., 1] = (yy * 5) & 0xFF
        frame[..., 2] = ((xx + yy) * 2) & 0xFF
        frame[..., 3] = 255
        motion = np.zeros((work_h, work_w, 2), np.float16)
        send_frame(worker, 0, frame, motion, True, 0, shm=None,
                   want_pixels=True)
        pixels = reader.recv(0, timeout=120.0)
        return list(logs), pixels
    finally:
        shutdown_worker(worker, stop)


def built(logs: list):
    lines = [BUILT.search(line) for line in logs]
    lines = [m for m in lines if m]
    return lines[-1] if lines else None


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    from settings_io import _work_size

    failures = []
    work = _work_size(W, H, 1.0, PASSES)
    logs, pixels = run(*work)
    line = built(logs)
    if line is None:
        failures.append(f"at {work[0]}x{work[1]} the worker never logged the "
                        f"cascade build:\n  " + "\n  ".join(logs[-12:]))
    else:
        asked, effective, allocated = (int(line.group(i)) for i in (1, 2, 3))
        print(f"    native + {PASSES} passes -> work {work[0]}x{work[1]}: asked "
              f"{asked}, effective {effective}, allocated {allocated}")
        if (asked, effective, allocated) != (PASSES,) * 3:
            failures.append(f"at {work[0]}x{work[1]} the cascade built "
                            f"{effective}/{allocated} of {asked}")
    if pixels is None or getattr(pixels, "shape", None) != (H, W, 4):
        failures.append(f"a frame through the {PASSES}-pass cascade came back "
                        f"as {getattr(pixels, 'shape', pixels)}")

    logs, _ = run(W, H)
    line = built(logs)
    if line is None:
        failures.append("at 1:1 the worker never logged the cascade build")
    else:
        asked, effective = int(line.group(1)), int(line.group(2))
        reason = line.group(4).strip()
        print(f"    1:1 for comparison: asked {asked}, effective {effective} "
              f"{reason}")
        if effective != 1:
            failures.append(f"1:1 built {effective} passes - the premise of the "
                            f"fix changed; re-check _work_size's step")
        if "1:1" not in reason:
            failures.append("at 1:1 the build line does not say why the "
                            "cascade runs one pass")

    # Boost off: one pass, and the reason is the switch - never "1:1" and a
    # slider that is not on screen.
    for label, size in (("1:1", (W, H)), ("below the frame", work)):
        logs, _ = run(*size, boost=False)
        line = built(logs)
        if line is None:
            failures.append(f"Boost off, {label}: the worker never logged the "
                            f"cascade build")
            continue
        effective, reason = int(line.group(2)), line.group(4).strip()
        print(f"    Boost off, {label} ({size[0]}x{size[1]}): effective "
              f"{effective} {reason}")
        if effective != 1:
            failures.append(f"Boost off, {label}: {effective} passes built")
        if "Boost is off" not in reason or "1:1" in reason:
            failures.append(f"Boost off, {label}: the reason is {reason!r}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print(f"OK: native with {PASSES} passes builds and runs {PASSES} of {PASSES}; "
          f"1:1 and Boost off build one and say why")
    return 0


if __name__ == "__main__":
    sys.exit(main())
