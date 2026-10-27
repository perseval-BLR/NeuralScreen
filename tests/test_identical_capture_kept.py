r"""A window that presents the same picture again is held like a still one (#183).

The worker evaluates an unchanged capture a few times and then keeps the last
result on screen. "Unchanged" meant: the capture handed over no new frame. A
program that draws through a swap chain - Chromium, Photos, Photoshop -
presents again and again over a still picture, the window capture hands every
present over as a new frame, and the network ran on all of them: an RTX 3070
stayed busy on a still image in window mode (#183). The worker now compares
the frame's luminance map with the previous one; byte for byte the same is
not new.

Checked on the worker in one-window mode with the luminance map on and
NS_PHASE=1 counts, against a window that presents on every vsync
(identical_presenter.py), the overlay far off every monitor:
* while it presents the same picture, almost no frame is evaluated, and the
  log counts the frames that arrived identical;
* while its picture changes (one bar steps on every present - a video),
  frames are new and evaluated again.

Fails before the change: every present of the same picture was new.

Run:  runtime\\python.exe tests\\test_identical_capture_kept.py
"""
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))

import numpy as np  # noqa: E402

from paths import WORKER_EXE  # noqa: E402

W, H = 640, 360
GW, GH = 160, 90
PRESENTER = Path(__file__).resolve().parent / "identical_presenter.py"


def _counts(logs, start):
    """(evaluated, processed, fresh, identical) summed over activity reports."""
    ev = pr = fr = ident = 0
    for line in logs[start:]:
        m = re.search(r"activity [0-9.]+s: .*?fresh-source=(\d+) evaluated=(\d+) .*?"
                      r"processed=(\d+)(?: .*?identical=(\d+))?", line)
        if m:
            fr += int(m.group(1))
            ev += int(m.group(2))
            pr += int(m.group(3))
            ident += int(m.group(4) or 0)
    return ev, pr, fr, ident


def _presenter(title: str, vary: bool):
    args = [sys.executable, str(PRESENTER), title, str(W), str(H), "60"] + (["vary"] if vary else [])
    # A real window: this test's own SDL runs on the dummy driver.
    env = {k: v for k, v in os.environ.items() if k != "SDL_VIDEODRIVER"}
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            env=env)
    line = proc.stdout.readline()
    m = re.match(r"hwnd (\d+)", line or "")
    if not m:
        proc.kill()
        raise RuntimeError(f"the presenter did not start: {line!r}")
    return proc, int(m.group(1))


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
    from protocol import (SharedFrameBuffer, send_frame, send_gray, send_motion_size,
                          send_wgc, send_window)
    from settings_io import PROFILES

    failures: list = []
    phases = {}
    procs = []
    shm = SharedFrameBuffer(W, H)
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2, 0, 0, shm)
    os.environ.pop("NS_PHASE", None)
    motion = np.zeros((GH, GW, 2), np.float16)
    running = threading.Event()
    running.set()
    errors: list = []
    lock = threading.Lock()

    def frames():
        i = 0
        try:
            while running.is_set():
                with lock:
                    send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                               motion_small=True)
                reader.recv(i, timeout=10.0)
                i += 1
                time.sleep(0.008)
        except Exception as exc:
            errors.append(exc)

    try:
        same, hwnd = _presenter("NsIdenticalSame", vary=False)
        procs.append(same)
        with lock:
            send_wgc(worker, hwnd)
            reader.wait_wgak(15)
            shm.open_gray(GW, GH)
            send_gray(worker, GW, GH, shm.gray_name)
            reader.wait_gak(10)
            send_motion_size(worker, GW, GH)
            reader.wait_mack(10)
            send_window(worker, W, H)
            reader.wait_wack(10)
        pump = threading.Thread(target=frames, daemon=True)
        pump.start()
        time.sleep(2.3)                            # settle past the first report
        start = len(logs)
        time.sleep(8.0)
        phases["same"] = _counts(logs, start)
        moving, hwnd2 = _presenter("NsIdenticalMoving", vary=True)
        procs.append(moving)
        with lock:
            send_wgc(worker, hwnd2)
            reader.wait_wgak(15)
        time.sleep(2.3)
        start = len(logs)
        time.sleep(4.5)
        phases["moving"] = _counts(logs, start)
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        running.clear()
        time.sleep(0.3)
        shutdown_worker(worker, stop)
        shm.close()
        for proc in procs:
            proc.kill()
    if errors:
        failures.append(f"the frame loop failed: {errors[0]!r}")
    for name, (ev, pr, fr, ident) in phases.items():
        print(f"    {name:7s} evaluated {ev} of {pr} processed frames "
              f"(new {fr}, identical {ident})")
    ev, pr, fr, ident = phases.get("same", (0, 0, 0, 0))
    if pr < 40:
        failures.append(f"too few frames while the picture stayed the same ({pr})")
    elif ev > pr * 0.2:
        failures.append(f"a picture presented again and again was evaluated {ev} times "
                        f"in {pr} frames")
    ev, pr, fr, ident = phases.get("moving", (0, 0, 0, 0))
    if pr < 20 or ev < pr * 0.5:
        failures.append(f"a changing picture was evaluated only {ev} times in {pr} frames")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the same picture presented again is held; a changing one is evaluated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
