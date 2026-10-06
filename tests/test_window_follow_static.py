r"""The picture keeps up with a window that is dragged without redrawing.

In window mode the picture window follows the captured window from the
worker's frame loop. A window whose content does not change gives Windows
Graphics Capture nothing to deliver, and the loop then waited 100 ms for a
frame on every pass - so while such a window was dragged, the picture moved
ten times a second and trailed behind it (native audit). The wait now runs in
slices and ends as soon as the window has moved.

Checked on the worker: a still target is moved in 40 steps; after each step
the picture window must be on it within a short settle time, in most steps.
Nothing shows: the target is a ghost (1/255 opaque) and the test restores it.

Run:  runtime\\python.exe tests\\test_window_follow_static.py
"""
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from offscreen_target import Target  # noqa: E402
from paths import WORKER_EXE  # noqa: E402
from test_fg_mode_change import _frame_rect, _overlay_rect  # noqa: E402

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

    user32 = ctypes.WinDLL("user32")
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.UINT]
    failures: list = []
    target = Target(W, H, name="NsFollowStatic", ghost=True)
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2,
                                              0, 0, None)
    motion = np.zeros((GH, GW, 2), np.float16)
    running = threading.Event()
    running.set()
    errors: list = []

    def frames():
        i = 0
        try:
            while running.is_set():
                send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                           motion_small=True)
                reader.recv(i, timeout=10.0)
                i += 1
        except Exception as exc:
            errors.append(exc)

    on_target = 0
    steps = 40
    origin = None
    try:
        send_wgc(worker, target.hwnd)
        reader.wait_wgak(15)
        send_motion_size(worker, GW, GH)
        reader.wait_mack(10)
        send_window(worker, W, H)
        reader.wait_wack(10)
        pump = threading.Thread(target=frames, daemon=True)
        pump.start()
        time.sleep(1.0)
        origin = _frame_rect(target.hwnd)
        for k in range(1, steps + 1):
            x, y = origin[0] + 6 * k, origin[1] + 3 * k
            user32.SetWindowPos(target.hwnd, None, x, y, 0, 0,
                                0x0001 | 0x0004 | 0x0010)   # NOSIZE|NOZORDER|NOACTIVATE
            time.sleep(0.035)                                # two frames at 60 Hz
            placed = _overlay_rect(worker.pid)
            frame = _frame_rect(target.hwnd)
            if placed is not None and frame is not None and placed[:2] == frame[:2]:
                on_target += 1
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        running.clear()
        time.sleep(0.3)
        shutdown_worker(worker, stop)
        target.close()
    if errors:
        failures.append(f"the frame loop failed: {errors[0]!r}")
    print(f"    the picture was on the moved window in {on_target} of {steps} steps")
    if on_target < steps * 0.7:
        failures.append(f"the picture trailed a still window being moved: on it in "
                        f"only {on_target} of {steps} steps")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the picture keeps up with a window dragged without redrawing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
