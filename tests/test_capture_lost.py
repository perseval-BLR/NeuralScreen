r"""A desktop that goes away for a while does not end the worker.

A UAC prompt, the lock screen and Ctrl+Alt+Del put up the secure desktop:
Desktop Duplication answers ACCESS_LOST, and DuplicateOutput refuses for as
long as that desktop is up (a fullscreen game's mode switch does the same for
a moment). The worker reopened the duplication once, at once; when that was
refused the capture counted as gone, and the next CAP1 ("requires active
capture") or NO_COLOR frame ended the worker. The client restarted it - NGX
init, seconds - and three such restarts in a row turned NR off.

Checked on the worker in desktop mode: after a live stream, the desktop is
lost and refuses to reopen for 1.5 s (NS_TEST_FAIL_STAGE=dda-lost); CAP1 and
NO_COLOR frames keep coming throughout. The worker must answer every frame,
say the desktop is gone and that it is back, and keep running.

Run:  runtime\\python.exe tests\\test_capture_lost.py
"""
import ctypes
import os
import sys
import time
from pathlib import Path

import numpy as np

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paths import WORKER_EXE  # noqa: E402

W, H = 640, 360
GW, GH = 160, 90


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    os.environ["NS_TEST_FAIL_STAGE"] = "dda-lost"
    import protocol as pipe
    from pipeline import shutdown_worker, start_worker
    from settings_io import PROFILES

    screen_w = ctypes.windll.user32.GetSystemMetrics(0)
    screen_h = ctypes.windll.user32.GetSystemMetrics(1)
    failures = []
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2,
                                              0, 0, None)
    os.environ.pop("NS_TEST_FAIL_STAGE", None)
    motion = np.zeros((GH, GW, 2), dtype=np.float16)
    answered = 0
    try:
        pipe.send_dda(worker, screen_w, screen_h)
        reader.wait_dack(15)
        pipe.send_motion_size(worker, GW, GH)
        reader.wait_mack(10)
        end = time.perf_counter() + 5.0
        i = 0
        while time.perf_counter() < end:
            # CAP1 then the frame that consumes it, as the client's CPU path
            # does; both end the worker if the capture counts as gone.
            pipe.prepare_capture(worker, reader, i, i)
            pipe.send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                            motion_small=True, prepared=True)
            reader.recv(i, 10.0)
            answered += 1
            i += 1
            time.sleep(0.01)
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        alive = worker.poll() is None
        shutdown_worker(worker, stop)
    text = "\n".join(logs)
    if "injecting failure at stage=dda-lost" not in text:
        failures.append("the desktop was never lost - the check proves nothing")
    if not alive:
        failures.append("the worker ended while the desktop was away")
    if "requires active capture" in text:
        failures.append("CAP1 was refused while the desktop was away")
    if "desktop is not available" not in text:
        failures.append("the worker did not say the desktop was away")
    if "desktop is back" not in text:
        failures.append("the desktop never came back")
    print(f"    {answered} frames answered")
    for f in failures:
        print("FAIL:", f)
    if failures:
        print("worker log (tail):", *logs[-12:], sep="\n  ")
        return 1
    print("OK: a desktop that goes away for a while keeps the worker alive and "
          "comes back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
