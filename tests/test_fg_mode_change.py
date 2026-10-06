r"""A display mode change met by the FG presenter rebuilds the window.

Every ordinary present path reads DXGI_STATUS_MODE_CHANGED as "this chain is
left behind" and builds the window again before the next frame (#58: an
output switched from 8 to 10 bits per colour went black). The Frame
Generation presenter only logged it - on every present - and carried on
presenting into the old chain. A colour-depth change brings no resize, so
nothing else rebuilt it: with FG on the picture stayed frozen or black until
FG was toggled (native audit, 06.10).

Checked on the worker with FG 2x: the presenter is made to meet one mode
change (NS_TEST_FAIL_STAGE=fg-mode-changed), and then the window must be
built again, FG must keep displaying after it, and the change must be said
once, not per present. Nothing shows: the overlay is far off every monitor
and the target is a ghost. Needs an RTX GPU with DLSS-G.

Run:  runtime\python.exe tests\test_fg_mode_change.py
"""
import ctypes
import os
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
    os.environ["NS_TEST_FAIL_STAGE"] = "fg-mode-changed"
    from pipeline import shutdown_worker, start_worker
    from protocol import send_frame, send_motion_size, send_wgc, send_window
    from settings_io import PROFILES

    failures: list = []
    target = Target(W, H, name="NsFgModeChange", ghost=True)
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
        run(6.0, True)
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        shutdown_worker(worker, stop)
        target.close()

    lines = list(logs)
    if not any("injecting failure at stage=fg-mode-changed" in ln for ln in lines):
        if any("[fg]" in ln and "displayed" in ln for ln in lines):
            failures.append("FG presented but never met the injected mode change")
        else:
            print("SKIP: Frame Generation did not run here (DLSS-G unavailable)")
            return 0
    said = [i for i, ln in enumerate(lines) if "mode change" in ln and "[fg]" in ln]
    if len(said) != 1:
        failures.append(f"the mode change was said {len(said)} times, not once")
    ready = [i for i, ln in enumerate(lines) if "[present] overlay" in ln and "ready" in ln]
    if not said or not any(i > said[0] for i in ready):
        failures.append("the window was not built again after the mode change - "
                        "FG kept presenting into the old chain")
    elif not any("[fg] displayed" in ln for ln in lines[said[0]:]):
        failures.append("FG did not display anything after the rebuild")

    if failures:
        for f in failures:
            print("FAIL:", f)
        print("worker log (tail):", *lines[-15:], sep="\n  ")
        return 1
    print("OK: a mode change met by the FG presenter rebuilds the window once, "
          "and FG carries on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
