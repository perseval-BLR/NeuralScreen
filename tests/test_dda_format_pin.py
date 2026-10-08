r"""A desktop duplication that keeps changing format is pinned after the first change.

The legacy DuplicateOutput is used for an SDR display because it converts
the desktop to BGRA8 - but one driver alternated FP16 and BGRA8 through it
anyway on a display that reports 8 bits and no HDR: 307 bridge rebuilds in
two minutes, each one losing a frame (#151). The FP16 pin through
DuplicateOutput1 that stops this (#89) was only taken for displays that
report HDR or more than 8 bits.

Checked on the worker in desktop mode with NS_TEST_FAIL_STAGE=dda-format-flip:
the legacy duplication hands every 8th frame over in the other format, as
that driver did. The worker must notice the first change, reopen the
capture with the format pinned, stop rebuilding, and keep answering frames.
On a machine whose display already takes the pinned path there is no legacy
duplication to flip, and the test says so and skips.

Run:  runtime\\python.exe tests\\test_dda_format_pin.py
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
FRAMES = 160


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    os.environ["NS_TEST_FAIL_STAGE"] = "dda-format-flip"
    os.environ["NS_HDR"] = "0"
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
        end = time.perf_counter() + 30.0
        for i in range(FRAMES):
            if time.perf_counter() > end:
                break
            pipe.prepare_capture(worker, reader, i, i)
            pipe.send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                            motion_small=True, prepared=True)
            reader.recv(i, 10.0)
            answered += 1
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        alive = worker.poll() is None
        shutdown_worker(worker, stop)
    text = "\n".join(logs)
    if "legacy duplication" not in text:
        print("SKIP: this display already takes the pinned FP16 duplication - "
              "there is no legacy path to flip")
        return 0
    flips = text.count("[test] the legacy duplication changed format")
    rebuilds = text.count("rebuilding the bridge") + flips
    print(f"    {answered} frames answered, {flips} injected format change(s)")
    if flips == 0:
        failures.append("no format change was injected - the check proves nothing")
    if flips > 1:
        failures.append(f"the capture kept changing format: {flips} changes in "
                        f"{answered} frames, every one a bridge rebuild")
    if "duplication pinned through IDXGIOutput5" not in text:
        failures.append("the capture was not reopened with the format pinned")
    if not alive:
        failures.append("the worker ended")
    if answered < FRAMES * 0.9:
        failures.append(f"only {answered} of {FRAMES} frames were answered")
    for f in failures:
        print("FAIL:", f)
    if failures:
        print("worker log (tail):", *logs[-12:], sep="\n  ")
        return 1
    print(f"OK: one format change pinned the capture ({rebuilds} rebuild)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
