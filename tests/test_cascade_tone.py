r"""Extra NR passes do not darken the picture pass after pass.

Passes 2+ of the cascade repeated the whole profile, local tone included,
and Natural's local tone darkens a little each time it runs: the frame's
mean luminance against the input measured 0.988, 0.978 and 0.970 for one,
two and three passes. Passes 2+ now run with tone 0 unless the user gave
them a set of their own.

Checked on the worker in Boost (the only mode with a cascade): the same
textured frame through 1 and 3 passes of Natural with no per-pass set. The
three-pass picture must keep the one-pass brightness (it darkened by 0.018
of the input's mean before), and the extra passes must still change it - a
cascade that does nothing would pass the brightness check trivially.

Run:  runtime\\python.exe tests\\test_cascade_tone.py
"""
import os
import sys
from pathlib import Path

import numpy as np

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paths import WORKER_EXE  # noqa: E402

FW, FH, W, H = 1280, 720, 832, 468
FRAMES = 10


def _frame() -> np.ndarray:
    rng = np.random.default_rng(5)
    yy, xx = np.mgrid[0:FH, 0:FW]
    g = (96 + 40 * np.sin(xx / 23.0) * np.cos(yy / 17.0)
         + rng.normal(0, 10, (FH, FW))).clip(0, 255).astype(np.uint8)
    f = np.zeros((FH, FW, 4), np.uint8)
    f[..., 0] = g
    f[..., 1] = (g * 0.9).astype(np.uint8)
    f[..., 2] = (g * 0.8).astype(np.uint8)
    f[..., 3] = 255
    return np.ascontiguousarray(f)


def _run(passes: int, img: np.ndarray):
    import protocol as pipe
    from pipeline import shutdown_worker, start_worker
    from settings_io import PROFILES

    prm = dict(PROFILES["Natural"])
    worker, logs, reader, stop = start_worker(prm, W, H, 2, FW, FH, None)
    motion = np.zeros((H, W, 2), np.float16)
    out = None
    try:
        pipe.send_resize(worker, prm, W, H, 2, FW, FH, nr_small=True, nr_passes=passes)
        reader.wait_rack(30)
        for i in range(FRAMES):
            pipe.send_frame(worker, i, img, motion, i == 0, i, want_pixels=True)
            got = reader.recv(i, 20)
            if got is not None:
                out = got
    finally:
        shutdown_worker(worker, stop)
    effective = [line for line in logs if "NR cascade built" in line]
    return out, effective


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    os.environ["NS_NR_SMALL"] = "1"
    os.environ["NS_HDR"] = "0"
    failures = []
    img = _frame()
    one, _ = _run(1, img)
    three, built = _run(3, img)
    if one is None or three is None:
        print("FAIL: no pixels came back")
        return 1
    if not any("effective=3" in line for line in built):
        print("SKIP: the worker could not build a three-pass cascade here")
        return 0
    m_in = float(img[..., :3].astype(np.float64).mean())
    r1 = float(one[..., :3].astype(np.float64).mean()) / m_in
    r3 = float(three[..., :3].astype(np.float64).mean()) / m_in
    change = float(np.abs(three[..., :3].astype(np.int16)
                          - one[..., :3].astype(np.int16)).mean())
    print(f"    mean luminance against the input: 1 pass {r1:.4f}, 3 passes {r3:.4f}; "
          f"3 vs 1 pass differ by {change:.2f} per channel")
    if abs(r3 - r1) > 0.006:
        failures.append(f"three passes changed the brightness: {r1:.4f} -> {r3:.4f} "
                        f"of the input's mean")
    if change < 0.5:
        failures.append(f"the extra passes changed nothing ({change:.2f}) - "
                        f"this check proves nothing")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: extra passes keep the one-pass brightness and still work on the picture")
    return 0


if __name__ == "__main__":
    sys.exit(main())
