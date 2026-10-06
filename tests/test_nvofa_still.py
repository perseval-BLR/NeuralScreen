r"""NVOFA gives no motion to a frame that brought nothing new (#141).

#141 (Mnilionic, RTX 3070): the picture "jiggles like jelly" - in window mode
in patches, in fullscreen more - on a static photo; a commenter with another
3070 found that switching Motion Estimation from NVOFA to CPU removes it, and
#95 said the same. NR runs on every frame, including the ones where the
capture brought nothing new (a window that did not redraw, a desktop that
answered WAIT_TIMEOUT). The CPU path answers zero motion for those; NVOFA ran
optical flow on two identical gray frames with the temporal hint carrying the
last real flow forward, and was free to answer non-zero vectors - 2-4.6 px
measured on #141's static frames - which NR then warped its history by.

Checked on the worker with NVOFA and a captured window: while the window
does not change, every motion field the worker builds is exactly zero and
no optical flow is run for it; once it animates, flow is measured again.
Nothing shows: the overlay is far off every monitor and the target is a
ghost. Needs an RTX GPU with the Optical Flow Accelerator.

Run:  runtime\python.exe tests\test_nvofa_still.py
"""
import ctypes
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from offscreen_target import Target  # noqa: E402
from paths import WORKER_EXE  # noqa: E402

W, H = 640, 360
GW, GH = 320, 180


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    dump = Path(tempfile.mkdtemp(prefix="ns-nvofa-still-"))
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
    target = Target(W, H, name="NsNvofaStill", ghost=True)
    shm = SharedFrameBuffer(W, H)
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2,
                                              0, 0, shm)
    for key in ("NS_MOTION_BACKEND", "NS_NVOFA_DUMP"):
        os.environ.pop(key, None)
    state = {"index": 0}
    motion = np.zeros((GH, GW, 2), np.float16)
    phases: dict = {}

    def run(frames: int, label: str) -> None:
        first = state["index"]
        for _ in range(frames):
            i = state["index"]
            state["index"] += 1
            send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                       motion_small=True)
            reader.recv(i, timeout=10.0)
        phases[label] = (first, state["index"])

    try:
        send_wgc(worker, target.hwnd)
        if reader.wait_wgak(15) != (W, H):
            failures.append("the capture target is not the expected size")
        shm.open_gray(GW, GH)
        send_gray(worker, GW, GH, shm.gray_name)
        reader.wait_gak(10)
        send_motion_size(worker, GW, GH)
        reader.wait_mack(10)
        send_window(worker, W, H)
        reader.wait_wack(10)
        target.animate_interval = 1.0 / 30.0
        target.animate = True
        run(30, "warm")                  # the flow becomes valid on real motion
        target.animate = False
        time.sleep(0.3)                  # the last repaint drains
        run(25, "still")
        target.animate = True
        run(30, "moving")
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        shutdown_worker(worker, stop)
        shm.close()
        target.close()

    if not any("[nvofa] active" in ln for ln in logs):
        print("SKIP: NVOFA did not run here (no Optical Flow Accelerator)")
        shutil.rmtree(dump, ignore_errors=True)
        return 0
    motions = sorted(dump.glob("motion-*.bin"))
    flows = {p.name.split("-")[1] for p in dump.glob("flow-*.bin")}
    if not motions:
        failures.append("the worker dumped no motion fields")
    still_files = motions[phases.get("still", (0, 0))[0] + 3:
                          phases.get("still", (0, 0))[1]] if motions else []
    moving_vectors = 0
    for path in still_files:
        field = np.fromfile(path, np.float16)
        if np.any(field != 0):
            failures.append(f"{path.name}: a frame of an unchanged window carried "
                            f"motion (max {float(np.abs(field).max()):.2f} px)")
            break
    still_flows = [p for p in still_files if p.name.split("-")[1] in flows]
    if len(still_flows) > 2:
        failures.append(f"optical flow ran on {len(still_flows)} of "
                        f"{len(still_files)} frames of an unchanged window")
    lo, hi = phases.get("moving", (0, 0))
    for path in motions[lo:hi]:
        if np.any(np.fromfile(path, np.float16) != 0):
            moving_vectors += 1
    if motions and moving_vectors == 0:
        failures.append("no motion was measured once the window moved again")
    print(f"    {len(motions)} motion fields, {len(flows)} flows, "
          f"{len(still_files)} checked still, {moving_vectors} moving with vectors")
    shutil.rmtree(dump, ignore_errors=True)

    if failures:
        for f in failures:
            print("FAIL:", f)
        print("worker log (tail):", *logs[-12:], sep="\n  ")
        return 1
    print("OK: an unchanged frame gets zero motion and no optical flow; real "
          "motion is still measured (#141)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
