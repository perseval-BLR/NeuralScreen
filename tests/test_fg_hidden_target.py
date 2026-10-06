r"""Frame Generation stands still while its window is hidden, and stops at once.

H4: when the captured window is minimised the picture window is hidden, but
FG kept queueing a slot every 100 ms; the presenter then stood in the
compositor's latency waitable (2 s timeouts - a hidden flip chain's presents
are dropped and DXGI_STATUS_OCCLUDED never comes) and presented into a chain
nobody showed. #138's residual: StopFgPresentation woke the presenter only
through its condition variable, so a presenter in that 2 s wait (and the
copy wait after it) held every caller - a resize, ClosePresent, the FG
toggle - for seconds.

Checked on the worker with FG 2x: the target is minimised for 2 s while
frames keep coming; no waitable timeout may be logged, and closing the
picture window (WNDO off, which stops the presenter) must be answered
promptly. Nothing shows. Needs an RTX GPU with DLSS-G.

Run:  runtime\python.exe tests\test_fg_hidden_target.py
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
    from pipeline import shutdown_worker, start_worker
    from protocol import send_frame, send_motion_size, send_wgc, send_window
    from settings_io import PROFILES

    failures: list = []
    target = Target(W, H, name="NsFgHiddenTarget", ghost=True)
    target.animate_interval = 1.0 / 30.0
    target.animate = True
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2,
                                              0, 0, None)
    state = {"index": 0}
    motion = np.zeros((GH, GW, 2), np.float16)
    hidden_lines: list = []
    close_s = 0.0

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
        run(2.0, True)
        ctypes.windll.user32.ShowWindow(target.hwnd, 6)      # SW_MINIMIZE
        hidden_from = len(logs)
        run(2.5, True)
        hidden_lines = list(logs)[hidden_from:]
        t = time.perf_counter()
        send_window(worker, 0, 0)          # closes the window: stops FG first
        reader.wait_wack(15)
        close_s = time.perf_counter() - t
        ctypes.windll.user32.ShowWindow(target.hwnd, 9)      # SW_RESTORE
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        shutdown_worker(worker, stop)
        target.close()

    lines = list(logs)
    if not any("[fg]" in ln and "enabled at" in ln for ln in lines):
        unavailable = any(("[fg]" in ln and "load failed" in ln) for ln in lines)
        if unavailable and not failures:
            print("SKIP: Frame Generation did not run here (DLSS-G unavailable)")
            return 0
        failures.append("Frame Generation never started")
    timeouts = [ln for ln in hidden_lines if "waitable timeout" in ln]
    if timeouts:
        failures.append(f"the presenter waited out the compositor {len(timeouts)} "
                        f"time(s) while the window was hidden")
    print(f"    closing the window took {close_s * 1000:.0f} ms")
    if close_s > 0.5:
        failures.append(f"stopping FG held the close for {close_s:.1f} s")

    if failures:
        for f in failures:
            print("FAIL:", f)
        print("worker log (tail):", *lines[-15:], sep="\n  ")
        return 1
    print("OK: FG stands still while its window is hidden and stops at once")
    return 0


if __name__ == "__main__":
    sys.exit(main())
