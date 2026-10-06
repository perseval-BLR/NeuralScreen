r"""A failed Frame Generation start leaves nothing half-initialised behind.

EnsureFg kept the runtime module loaded whatever happened after LoadLibrary:
an entry point missing, AllocateParameters or Init_Ext refused. The module
was the only "initialised" marker, so the next FG switch-on skipped the
initialisation and called CreateFeature on a runtime never initialised -
through parameters that could be null (pre-release audit). FG failing once
is a supported outcome (an old driver, a card below Ada, a BYO runtime);
switching it on again must make a clean second attempt.

Checked on the worker: Init_Ext is made to fail once
(NS_TEST_FAIL_STAGE=fg-init), FG is switched off and on again, and the
second attempt must initialise the runtime afresh, and the worker must keep
answering every frame. Nothing shows. Needs an RTX GPU with DLSS-G.

Run:  runtime\python.exe tests\test_fg_init_retry.py
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


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    os.environ["NS_WINDOW_POS"] = "-30000,-30000"
    os.environ["NS_NR_SMALL"] = "0"
    os.environ["NS_HDR"] = "0"
    os.environ["NS_TEST_FAIL_STAGE"] = "fg-init"
    from pipeline import shutdown_worker, start_worker
    from protocol import send_frame, send_motion_size, send_wgc, send_window
    from settings_io import PROFILES

    failures: list = []
    target = Target(W, H, name="NsFgInitRetry", ghost=True)
    target.animate_interval = 1.0 / 30.0
    target.animate = True
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2,
                                              0, 0, None)
    os.environ.pop("NS_TEST_FAIL_STAGE", None)
    state = {"index": 0}
    motion = np.zeros((GH, GW, 2), np.float16)

    def run(seconds: float, fg: bool) -> None:
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            i = state["index"]
            state["index"] += 1
            send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                       motion_small=True, frame_generation=fg, frame_multiplier=2)
            reader.recv(i, timeout=10.0)

    try:
        send_wgc(worker, target.hwnd)
        if reader.wait_wgak(15) != (W, H):
            failures.append("the capture target is not the expected size")
        send_motion_size(worker, GW, GH)
        reader.wait_mack(10)
        send_window(worker, W, H)
        reader.wait_wack(10)
        run(2.0, True)       # the first start fails at Init_Ext
        run(1.0, False)      # switched off ...
        run(3.0, True)       # ... and on: a clean second attempt
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        shutdown_worker(worker, stop)
        target.close()

    lines = list(logs)
    if not any("injecting failure at stage=fg-init" in ln for ln in lines):
        unavailable = any(("[fg]" in ln and "load failed" in ln) for ln in lines)
        if unavailable and not failures:
            print("SKIP: Frame Generation did not run here (DLSS-G unavailable)")
            return 0
        failures.append("the FG runtime was never initialised")
    inits = [ln for ln in lines if "[fg] Init_Ext ->" in ln]
    if len(inits) < 2:
        failures.append(f"Init_Ext ran {len(inits)} time(s): the second switch-on "
                        f"reused a runtime whose initialisation had failed")
    elif "0x00000001" not in inits[-1]:
        failures.append(f"the second initialisation did not succeed: {inits[-1].strip()}")
    if worker.poll() not in (None, 0):
        failures.append(f"the worker exited with {worker.poll()}")

    if failures:
        for f in failures:
            print("FAIL:", f)
        print("worker log (tail):", *lines[-15:], sep="\n  ")
        return 1
    print("OK: a failed FG start is undone, and switching FG on again "
          "initialises it afresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
