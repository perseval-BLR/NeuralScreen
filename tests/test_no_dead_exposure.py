r"""The worker does not claim an exposure stage that the network ignores.

An "adaptive exposure" mapped the frame's mean luminance into
DLSS.Exposure.Scale to brighten dark scenes, on by default, and the log said
`[pw] adaptive exposure on` in every desktop session. The NR runtime never
reads that parameter: its DLL carries no "Exposure" string at all, and
outputs with Exposure.Scale and Pre.Exposure forced to 0.3, 1.0 and 3.0 were
byte for byte the same (scan and A/B, 2026-10-08). The stage cost a pass over
the grey frame per capture and put a false line in every support log - one
more thing a ticket reply had to rule out.

Checked on the worker in desktop mode, where the stage ran: the session
works and nothing in the log announces an exposure stage.

Run:  runtime\\python.exe tests\\test_no_dead_exposure.py
"""
import ctypes
import os
import sys
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
FRAMES = 40


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    os.environ["NS_HDR"] = "0"
    import protocol as pipe
    from pipeline import shutdown_worker, start_worker
    from settings_io import PROFILES

    screen_w = ctypes.windll.user32.GetSystemMetrics(0)
    screen_h = ctypes.windll.user32.GetSystemMetrics(1)
    failures = []
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2,
                                              0, 0, None)
    motion = np.zeros((GH, GW, 2), dtype=np.float16)
    answered = 0
    try:
        pipe.send_dda(worker, screen_w, screen_h)
        reader.wait_dack(15)
        pipe.send_motion_size(worker, GW, GH)
        reader.wait_mack(10)
        for i in range(FRAMES):
            pipe.prepare_capture(worker, reader, i, i)
            pipe.send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                            motion_small=True, prepared=True)
            reader.recv(i, 10.0)
            answered += 1
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        shutdown_worker(worker, stop)
    text = "\n".join(logs)
    if "capture" not in text or answered < FRAMES:
        failures.append(f"the desktop session did not run ({answered} of {FRAMES} frames)")
    claims = [line for line in logs if "[pw]" in line or "exposure on" in line.lower()]
    if claims:
        failures.append(f"the log announces an exposure stage the network ignores: "
                        f"{claims[0].strip()!r}")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: no exposure stage is claimed; the desktop session runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
