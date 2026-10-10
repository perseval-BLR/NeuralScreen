r"""A still capture is held on the CPU motion path too, where CAP1 leads every frame.

The worker holds the last NR result once an unchanged capture has been
evaluated a few times, and steadies the edit over time - both start over on
any command, because a command may change what the network makes. CAP1 does
not: it only latches the capture the next FRM1 consumes. But on the CPU motion
path (motion_backend cpu, an NVOFA fallback, NS_WORKER_SCENE=0) the client
sends CAP1 before EVERY frame, so the count was reset on every frame: a still
desktop was evaluated on every frame, shimmered, and the stabilizer reset its
history every time and did nothing.

Checked on the worker in one-window mode with a still ghost target and
NS_PHASE=1 counts, frames sent the way the CPU motion path sends them (CAP1,
then FRM1 marked prepared):
* while the window stays still, almost no frame is evaluated;
* a repaint of the window brings the evaluation back.

Run:  runtime\\python.exe tests\\test_still_capture_cap1.py
"""
import ctypes
import os
import re
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from offscreen_target import Target  # noqa: E402
from paths import WORKER_EXE  # noqa: E402

W, H = 640, 360
GW, GH = 160, 90


def _counts(logs, start):
    """(evaluated, processed) summed over activity reports from index start."""
    ev = pr = 0
    for line in logs[start:]:
        m = re.search(r"activity [0-9.]+s: .*?evaluated=(\d+) .*?processed=(\d+)", line)
        if m:
            ev += int(m.group(1))
            pr += int(m.group(2))
    return ev, pr


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    os.environ["NS_WINDOW_POS"] = "-30000,-30000"
    os.environ["NS_NR_SMALL"] = "0"
    os.environ["NS_HDR"] = "0"
    os.environ["NS_PHASE"] = "1"
    from pipeline import shutdown_worker, start_worker
    from protocol import (prepare_capture, send_frame, send_motion_size, send_wgc,
                          send_window)
    from settings_io import PROFILES

    failures: list = []
    params = dict(PROFILES["Natural"])
    target = Target(W, H, name="NsStillCap1", ghost=True)
    worker, logs, reader, stop = start_worker(params, W, H, 2, 0, 0, None)
    os.environ.pop("NS_PHASE", None)
    motion = np.zeros((GH, GW, 2), np.float16)
    running = threading.Event()
    running.set()
    errors: list = []

    def frames():
        i = 0
        try:
            while running.is_set():
                # The CPU motion path: latch the capture, then the frame.
                prepare_capture(worker, reader, i, i)
                send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                           motion_small=True, prepared=True)
                reader.recv(i, timeout=10.0)
                i += 1
                time.sleep(0.008)
        except Exception as exc:
            errors.append(exc)

    phases = {}
    try:
        send_wgc(worker, target.hwnd)
        reader.wait_wgak(15)
        send_motion_size(worker, GW, GH)
        reader.wait_mack(10)
        send_window(worker, W, H)
        reader.wait_wack(10)
        pump = threading.Thread(target=frames, daemon=True)
        pump.start()
        time.sleep(2.3)                            # settle past the first report
        start = len(logs)
        time.sleep(9.0)
        phases["still"] = _counts(logs, start)
        start = len(logs)
        for _ in range(8):                         # the window changes
            target.repaint()
            time.sleep(0.25)
        time.sleep(2.3)
        phases["repaint"] = _counts(logs, start)
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        running.clear()
        time.sleep(0.3)
        shutdown_worker(worker, stop)
        target.close()
    if errors:
        failures.append(f"the frame loop failed: {errors[0]!r}")
    for name, (ev, pr) in phases.items():
        print(f"    {name:8s} evaluated {ev} of {pr} processed frames")
    ev, pr = phases.get("still", (0, 0))
    if pr < 40:
        failures.append(f"too few frames while still ({pr}) - the check proves nothing")
    elif ev > pr * 0.2:
        failures.append(f"a still window behind CAP1 was evaluated {ev} times in {pr} frames")
    ev, pr = phases.get("repaint", (0, 0))
    if ev < 8:
        failures.append(f"repainting the window brought only {ev} evaluations")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: CAP1 before every frame no longer resets the still-capture hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
