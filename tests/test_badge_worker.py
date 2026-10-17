r"""The worker draws the rate badge into what it presents, and only there (v2.6.0).

The counter used to live on the HUD layer, whose every flip costs the
compositor a frame (#166). The client now hands the worker a bitmap
(app/badge.py, BADG) and the worker blends it over the presented picture
(native/badge.inl) - after the export, so Spout, recordings and screenshots
never carry it.

Checked on the real worker, its window off every monitor, a window captured
through WGC:

* presented frames, plain and with Frame Generation, are answered with the
  badge named and set - the worker says it draws it, and nothing fails;
* the pixels handed back for a screenshot do not carry it: the corner the
  badge sits in is the captured picture, not the badge's solid colour.

Needs an RTX GPU (DLSS-G for the FG half).

Run:  runtime\python.exe tests\test_badge_worker.py
"""
import ctypes
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from paths import WORKER_EXE  # noqa: E402

W, H = 640, 360
BW, BH = 120, 30
COLOUR = (255, 0, 255)


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    import badge
    from offscreen_target import Target
    from pipeline import shutdown_worker, start_worker
    from protocol import send_badge, send_frame, send_motion_size, send_wgc, send_window
    from settings_io import PROFILES

    failures = []
    os.environ["NS_WINDOW_POS"] = "-30000,-30000"
    os.environ["NS_NR_SMALL"] = "0"
    os.environ["NS_HDR"] = "0"
    gw, gh = 160, 90
    channel = badge.BadgeChannel()
    solid = np.empty((BH, BW, 4), np.uint8)
    solid[..., :3] = COLOUR
    solid[..., 3] = 255
    channel.publish(solid.tobytes(), BW, BH, "tr", 10, 0)
    target = Target(W, H, name="NsBadge", ghost=True)
    target.animate_interval = 1.0 / 30.0
    target.animate = True
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2, 0, 0, None)
    motion = np.zeros((gh, gw, 2), np.float16)
    answered = {"plain": 0, "fg": 0}
    corner_pixels = []
    try:
        send_wgc(worker, target.hwnd)
        reader.wait_wgak(15)
        send_motion_size(worker, gw, gh)
        reader.wait_mack(10)
        send_window(worker, W, H)
        reader.wait_wack(10)
        send_badge(worker, channel.name)
        i = 0
        for phase, fg in (("plain", None), ("fg", True)):
            end = time.perf_counter() + 2.5
            while time.perf_counter() < end:
                want = i % 20 == 5
                send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                           motion_small=True, frame_generation=fg, frame_multiplier=2,
                           want_pixels=want)
                out = reader.recv(i, timeout=10.0)
                if want and out is not None:
                    px = np.asarray(out).reshape(H, W, 4)
                    corner_pixels.append(px[10 + BH // 2, W - 10 - BW // 2, :3].copy())
                answered[phase] += 1
                i += 1
        if worker.poll() is not None:
            failures.append(f"the worker ended with the badge on (code {worker.returncode})")
    except Exception as exc:  # noqa: BLE001 - reported
        failures.append(f"the presented run failed: {type(exc).__name__}: {exc}")
    finally:
        shutdown_worker(worker, stop)
        target.close()
        channel.close()
    text = "\n".join(str(line) for line in logs)
    print(f"    presented frames with the badge set: {answered['plain']} plain, "
          f"{answered['fg']} with Frame Generation; screenshot corners {len(corner_pixels)}")
    if "[badge] the rate badge is drawn by the worker" not in text:
        failures.append("the worker did not open the badge's mapping")
    if "[badge] drawing the rate badge into the presented picture" not in text:
        failures.append("the worker never drew the badge into a presented frame")
    for bad in ("[badge] shader failed", "[badge] pipeline failed", "[badge] root signature",
                "device removed", "fence-timeout"):
        if bad in text:
            failures.append(f"the log says {bad!r}")
    if answered["plain"] < 20 or answered["fg"] < 20:
        failures.append(f"too few frames were answered: {answered}")
    if not corner_pixels:
        failures.append("no screenshot pixels came back to check the export")
    for px in corner_pixels:
        if tuple(int(v) for v in px) == COLOUR:
            failures.append("a screenshot carries the badge - it must stay on the screen only")
            break
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the worker draws the badge into what it presents, and not into the pixels it hands out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
