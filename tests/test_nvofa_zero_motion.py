r"""NVOFA keeps a moving object's vector on the object, not on the still background.

NVOFA hands back one vector per 4x4 block of a 320x180 grey frame, and the
worker stretches that field over the picture - so a moving object's vector
spread onto the still background around it, and the driver answers small
vectors on still pixels by itself too. Measured on the worker with a supplied
field of that shape: the background next to a moving object shimmered 45-70%
more than with no vectors at all ("jelly", #151, #141, #95). A vector is now
kept only where it explains the pixel's neighbourhood better than no motion.

Checked on the worker with NVOFA and a captured window: a textured square
moves across a textured still background. In the motion fields the worker
builds:
* the ring of still background around the square carries (almost) no motion;
* the square itself still carries its motion, pointing the right way.
Nothing shows: the overlay is far off every monitor and the target is a
ghost. Needs an RTX GPU with the Optical Flow Accelerator.

Run:  runtime\python.exe tests\test_nvofa_zero_motion.py
"""
import ctypes
import os
import shutil
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

import offscreen_target as ot  # noqa: E402
from paths import WORKER_EXE  # noqa: E402

W, H = 640, 360
GW, GH = 320, 180
SQ = 64             # the square's side
STEP = 4            # px per paint, along x
Y0 = 140


class MovingSquare(ot.Target):
    """A ghost target: still stripes everywhere, a checkered square moving right."""

    def __init__(self):
        self.x = 120
        super().__init__(W, H, name="NsZeroMotion", ghost=True)

    def _paint(self):
        dc = ot.user32.GetDC(self.hwnd)
        for k in range(0, W, 8):       # still background: vertical stripes
            colour = 0x00202020 + ((k * 37) % 160) * 0x010101
            brush = ot.gdi32.CreateSolidBrush(colour)
            r = wintypes.RECT(k, 0, k + 8, H)
            ot.user32.FillRect(dc, ctypes.byref(r), brush)
            ot.gdi32.DeleteObject(brush)
        for by in range(0, SQ, 8):     # the moving checkered square
            for bx in range(0, SQ, 8):
                colour = 0x00F0F0F0 if ((bx + by) // 8) % 2 == 0 else 0x00101010
                brush = ot.gdi32.CreateSolidBrush(colour)
                r = wintypes.RECT(self.x + bx, Y0 + by, self.x + bx + 8, Y0 + by + 8)
                ot.user32.FillRect(dc, ctypes.byref(r), brush)
                ot.gdi32.DeleteObject(brush)
        ot.user32.ReleaseDC(self.hwnd, dc)
        if self.animate:
            self.x = 120 + (self.x - 120 + STEP) % 360


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    dump = Path(tempfile.mkdtemp(prefix="ns-nvofa-zero-"))
    os.environ["NS_WINDOW_POS"] = "-30000,-30000"
    os.environ["NS_NR_SMALL"] = "0"
    os.environ["NS_HDR"] = "0"
    os.environ["NS_MOTION_BACKEND"] = "nvofa"
    os.environ["NS_NVOFA_DUMP"] = str(dump)
    from pipeline import shutdown_worker, start_worker
    from protocol import (SharedFrameBuffer, send_frame, send_gray,
                          send_motion_size, send_wgc, send_window)
    from settings_io import PROFILES

    failures: list = []
    target = MovingSquare()
    shm = SharedFrameBuffer(W, H)
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2,
                                              0, 0, shm)
    for key in ("NS_MOTION_BACKEND", "NS_NVOFA_DUMP"):
        os.environ.pop(key, None)
    motion = np.zeros((GH, GW, 2), np.float16)
    positions = []
    try:
        send_wgc(worker, target.hwnd)
        reader.wait_wgak(15)
        shm.open_gray(GW, GH)
        send_gray(worker, GW, GH, shm.gray_name)
        reader.wait_gak(10)
        send_motion_size(worker, GW, GH)
        reader.wait_mack(10)
        send_window(worker, W, H)
        reader.wait_wack(10)
        target.animate_interval = 1.0 / 30.0
        target.animate = True
        for i in range(70):
            send_frame(worker, i, None, motion, i == 0, i, no_color=True, motion_small=True)
            reader.recv(i, timeout=10.0)
            positions.append(target.x)
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        target.animate = False
        shutdown_worker(worker, stop)
        shm.close()
        target.close()

    if not any("[nvofa] active" in ln for ln in logs):
        if any("[nvofa] unavailable" in ln for ln in logs) and not failures:
            print("SKIP: NVOFA did not run here (no Optical Flow Accelerator)")
            shutil.rmtree(dump, ignore_errors=True)
            return 0
        failures.append("NVOFA never became active")
    flows = {p.name.split("-")[1] for p in dump.glob("flow-*.bin")}
    fields = [p for p in sorted(dump.glob("motion-*.bin"))
              if p.name.split("-")[1] in flows][10:]      # measured fields, warm
    ring_moving = inside_right = inside_total = 0
    ring_total = 0
    for path in fields:
        field = np.fromfile(path, np.float16).astype(np.float32)
        if field.size != W * H * 2:
            continue
        field = field.reshape(H, W, 2)
        mag = np.hypot(field[..., 0], field[..., 1])
        moving = mag > 0
        # Where is the square? The columns whose band rows move the most.
        band = moving[Y0 + 8:Y0 + SQ - 8]
        cols = np.where(band.mean(axis=0) > 0.3)[0]
        rows = np.zeros(H, bool)
        rows[Y0:Y0 + SQ] = True
        if cols.size:
            x0, x1 = int(cols.min()), int(cols.max())
        else:
            x0 = x1 = None
        # Still background ring: 8..40 px around the square's rows, plus the
        # same rows outside a generous box around wherever the square could be.
        ring = np.zeros((H, W), bool)
        ring[max(0, Y0 - 40):Y0 - 8, :] = True
        ring[Y0 + SQ + 8:min(H, Y0 + SQ + 40), :] = True
        ring_total += int(ring.sum())
        ring_moving += int((moving & ring).sum())
        if x0 is not None:
            inner = field[Y0 + 8:Y0 + SQ - 8, x0:x1 + 1, 0]
            inside_total += inner.size
            # current -> previous: the square moved right, so it came from the left.
            inside_right += int((inner < -0.5).sum())
    shutil.rmtree(dump, ignore_errors=True)
    if not fields:
        failures.append("the worker dumped no measured motion fields")
    else:
        ring_frac = ring_moving / max(1, ring_total)
        inside_frac = inside_right / max(1, inside_total)
        print(f"    {len(fields)} fields: still ring with motion {100 * ring_frac:.2f}%, "
              f"square with the right motion {100 * inside_frac:.1f}%")
        if ring_frac > 0.02:
            failures.append(f"{100 * ring_frac:.2f}% of the still background around the "
                            f"moving square carried motion")
        if inside_total == 0 or inside_frac < 0.4:
            failures.append(f"the moving square lost its motion "
                            f"({100 * inside_frac:.1f}% of it points the right way)")
    for f in failures:
        print("FAIL:", f)
    if failures:
        print("worker log (tail):", *logs[-8:], sep="\n  ")
        return 1
    print("OK: motion stays on what moves; the still background around it gets none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
