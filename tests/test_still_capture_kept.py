r"""A capture that has not changed is not re-evaluated frame after frame.

On identical input the network's output never settles: it moves by ~0.26 of
255 on average and up to 3 on every frame (measured 2026-10-08). A still
desktop or window was evaluated anyway, every frame - it shimmered and kept
the GPU busy for nothing. Now an unchanged capture is evaluated a few times,
until the history has it, and then the last result stays on screen.

Checked on the worker in one-window mode with a still ghost target and
NS_PHASE=1 counts (`evaluated` against `processed`):
* while the window stays still, almost no frame is evaluated;
* a repaint of the window brings the evaluation back;
* a parameter change (RNSZ) brings it back too - the new numbers must reach
  the picture even though the window did not change.

Run:  runtime\\python.exe tests\\test_still_capture_kept.py
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
    from protocol import send_frame, send_motion_size, send_resize, send_wgc, send_window
    from settings_io import PROFILES

    failures: list = []
    params = dict(PROFILES["Natural"])
    target = Target(W, H, name="NsStillKept", ghost=True)
    worker, logs, reader, stop = start_worker(params, W, H, 2, 0, 0, None)
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

    def report_mark():
        time.sleep(2.3)              # one full NS_PHASE report window
        return len(logs)

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
        start = report_mark()                      # settle past the first report
        time.sleep(9.0)
        phases["still"] = _counts(logs, start)
        start = len(logs)
        for _ in range(8):                         # the window changes
            target.repaint()
            time.sleep(0.25)
        time.sleep(2.3)
        phases["repaint"] = _counts(logs, start)
        time.sleep(3.0)                            # still again
        start = len(logs)
        with lock:                                 # a parameter change, nothing else
            send_resize(worker, dict(params, intensity=0.5), W, H, 2)
        time.sleep(2.5)
        phases["params"] = _counts(logs, start)
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
        failures.append(f"a still window was evaluated {ev} times in {pr} frames")
    ev, pr = phases.get("repaint", (0, 0))
    if ev < 8:
        failures.append(f"repainting the window brought only {ev} evaluations")
    ev, pr = phases.get("params", (0, 0))
    if ev < 1:
        failures.append("a parameter change on a still window was never evaluated")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a still capture keeps its result; a change or new parameters bring "
          "the evaluation back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
