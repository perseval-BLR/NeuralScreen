r"""Window capture keeps up with a window that draws faster than 60 times a second.

The Windows Graphics Capture session was created without a minimum update
interval, and on current Windows 11 that default throttles delivery to about
60 frames a second whatever the window draws: one-window mode lost ~40% of
its throughput against desktop capture of the same game (#155, 81.6 -> 49.1
NR frames/s; the reporter's MinUpdateInterval(4 ms) build gave 63.3).

Checked on the worker in one-window mode against a ghost target repainting
~240 times a second on a monitor faster than 60 Hz: the fresh frames the
worker reports (NS_PHASE=1, `fresh-source`) must clearly exceed 60 a second.
On a 60 Hz monitor the compositor itself stops at 60 and the test skips.

Run:  runtime\\python.exe tests\\test_wgc_update_rate.py
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
RUN_S = 6.0


def _refresh_hz() -> int:
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    dc = user32.GetDC(0)
    try:
        return int(gdi32.GetDeviceCaps(dc, 116))   # VREFRESH
    finally:
        user32.ReleaseDC(0, dc)


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    hz = _refresh_hz()
    if hz < 90:
        print(f"SKIP: the monitor runs at {hz} Hz - the compositor itself stops "
              f"at that rate, so a 60-a-second cap cannot be told apart")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    os.environ["NS_WINDOW_POS"] = "-30000,-30000"
    os.environ["NS_NR_SMALL"] = "0"
    os.environ["NS_HDR"] = "0"
    os.environ["NS_PHASE"] = "1"
    from pipeline import shutdown_worker, start_worker
    from protocol import send_frame, send_motion_size, send_wgc, send_window
    from settings_io import PROFILES

    failures: list = []
    target = Target(W, H, name="NsWgcRate", ghost=True)
    target.animate_interval = 1.0 / 240.0
    target.animate = True
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2,
                                              0, 0, None)
    os.environ.pop("NS_PHASE", None)
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

    try:
        send_wgc(worker, target.hwnd)
        reader.wait_wgak(15)
        send_motion_size(worker, GW, GH)
        reader.wait_mack(10)
        send_window(worker, W, H)
        reader.wait_wack(10)
        pump = threading.Thread(target=frames, daemon=True)
        pump.start()
        time.sleep(RUN_S)
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        running.clear()
        time.sleep(0.3)
        shutdown_worker(worker, stop)
        target.close()
    if errors:
        failures.append(f"the frame loop failed: {errors[0]!r}")
    rates = []
    for line in logs:
        m = re.search(r"activity ([0-9.]+)s: .*?fresh-source=(\d+)", line)
        if m and float(m.group(1)) > 0:
            rates.append(int(m.group(2)) / float(m.group(1)))
    # The first report covers the start-up; judge the steady ones.
    steady = rates[1:] if len(rates) > 1 else rates
    if not steady:
        failures.append("the worker reported no activity (NS_PHASE) - nothing to judge")
    else:
        best = max(steady)
        print(f"    monitor {hz} Hz; fresh window frames per second: "
              f"{', '.join(f'{r:.1f}' for r in steady)}")
        if best < 75.0:
            failures.append(f"window capture delivered at most {best:.1f} fresh frames "
                            f"a second from a window drawing ~240 on a {hz} Hz monitor")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: window capture keeps up with a window drawing faster than 60 a second")
    return 0


if __name__ == "__main__":
    sys.exit(main())
