r"""A fault inside NVIDIA's Frame Generation runtime does not end the worker.

The neural renderer's create and evaluate have always run under an SEH guard:
an old driver faults inside NVIDIA's runtime instead of refusing (#51, #83,
#145), and the guard turns that into a failure line and a clean "unsupported".
DLSS-G's create and evaluate had no guard, so the same fault with Frame
Generation on took the whole worker down - no failure line, a restart, and
the same again on the next frame with FG (pre-release audit).

Checked on the worker, once per call: a fault is raised inside the guarded
create (NS_TEST_FAIL_STAGE=fg-create-fault) and inside the guarded evaluate
(fg-evaluate-fault) with FG on. The worker must catch it, say so, turn FG off
and keep answering every frame. Nothing shows. Needs an RTX GPU with DLSS-G.

Run:  runtime\python.exe tests\test_fg_fault.py
"""
import ctypes
import os
import re
import sys
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
GW, GH = 160, 90


def _run(stage: str, failures: list) -> None:
    from pipeline import shutdown_worker, start_worker
    from protocol import send_frame, send_motion_size, send_wgc, send_window
    from settings_io import PROFILES

    os.environ["NS_TEST_FAIL_STAGE"] = stage
    target = Target(W, H, name=f"NsFgFault{stage}", ghost=True)
    target.animate_interval = 1.0 / 30.0
    target.animate = True
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2,
                                              0, 0, None)
    os.environ.pop("NS_TEST_FAIL_STAGE", None)
    motion = np.zeros((GH, GW, 2), np.float16)
    alive = False
    try:
        send_wgc(worker, target.hwnd)
        reader.wait_wgak(15)
        send_motion_size(worker, GW, GH)
        reader.wait_mack(10)
        send_window(worker, W, H)
        reader.wait_wack(10)
        end = time.perf_counter() + 3.0
        i = 0
        while time.perf_counter() < end:
            send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                       motion_small=True, frame_generation=True, frame_multiplier=2)
            reader.recv(i, timeout=10.0)
            i += 1
        alive = worker.poll() is None
    except Exception as exc:
        failures.append(f"{stage}: the run raised {type(exc).__name__}: {exc}")
    finally:
        shutdown_worker(worker, stop)
        target.close()
    text = "\n".join(logs)
    if f"injecting failure at stage={stage}" not in text:
        if "[fg]" in text and "load failed" in text and not failures:
            print(f"SKIP: Frame Generation did not run here (DLSS-G unavailable)")
            return
        failures.append(f"{stage}: the fault was never raised")
    elif "(caught)" not in text:
        failures.append(f"{stage}: the fault was not caught")
    if not alive:
        failures.append(f"{stage}: the worker did not survive the fault")


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    os.environ["NS_WINDOW_POS"] = "-30000,-30000"
    os.environ["NS_NR_SMALL"] = "0"
    os.environ["NS_HDR"] = "0"
    failures: list = []
    for stage in ("fg-create-fault", "fg-evaluate-fault"):
        _run(stage, failures)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a fault inside DLSS-G is caught, FG goes off, and the worker "
          "keeps answering")
    return 0


if __name__ == "__main__":
    sys.exit(main())
