r"""The worker's gray map covers the whole frame at any size.

The gray channel (GRAY) is the worker's luminance downscale of every captured
frame: NVOFA reads it, the CPU optical flow reads it, the scene score and the
adaptive exposure read it. The AREA kernel sized its blocks as
ceil(source / gray) and started them at id * block, so whenever the source is
not an exact multiple of the gray size, the last blocks start PAST the source:
their loop runs zero times and the cell is written 0. 1366x768 -> 320x180 left
46 columns and 26 rows black; #140's 799x1010 window -> 320x404 left 53 and
67. Optical flow then saw a black wall at the right and bottom edges - motion
pinned to zero there, or torn against it.

Checked on the worker with a captured window painted in four bars (the right
one mid-gray), at two sizes that do not divide: the right-most columns and
the bottom rows of the gray map must carry the picture, not black. Nothing
shows: the overlay is far off every monitor and the target is a ghost.

Run:  runtime\python.exe tests\test_gray_area.py
"""
import ctypes
import os
import sys
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from offscreen_target import Target  # noqa: E402
from paths import WORKER_EXE  # noqa: E402

#: (window, gray) pairs that do not divide - the client picks the gray size
#: as 320 wide and the height in proportion (guides.py).
CASES = (((1366, 768), (320, 180)), ((799, 1010), (320, 404)))


def _run(size, gray, failures: list) -> None:
    from pipeline import shutdown_worker, start_worker
    from protocol import (SharedFrameBuffer, send_frame, send_gray,
                          send_motion_size, send_wgc, send_window)
    from settings_io import PROFILES

    (w, h), (gw, gh) = size, gray
    target = Target(w, h, name=f"NsGrayArea{w}", ghost=True)
    shm = SharedFrameBuffer(w, h)
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), w, h, 2,
                                              0, 0, shm)
    try:
        send_wgc(worker, target.hwnd)
        got = reader.wait_wgak(15)
        if tuple(got) != (w, h):
            failures.append(f"{w}x{h}: the capture came back {got}")
            return
        shm.open_gray(gw, gh)
        send_gray(worker, gw, gh, shm.gray_name)
        reader.wait_gak(10)
        send_motion_size(worker, gw, gh)
        reader.wait_mack(10)
        send_window(worker, w, h)
        reader.wait_wack(10)
        motion = np.zeros((gh, gw, 2), np.float16)
        target.animate = True
        for i in range(20):
            send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                       motion_small=True)
            reader.recv(i, timeout=10.0)
        g = shm.read_gray().reshape(gh, gw).astype(np.int32)
    finally:
        shutdown_worker(worker, stop)
        shm.close()
        target.close()
    # The right bar is mid-gray (0x80) and every bar is far from black, so no
    # column or row of the map may be: a zero edge is the block that started
    # past the frame.
    right = g[:, -8:]
    bottom = g[-8:, :]
    print(f"    {w}x{h} -> {gw}x{gh}: right edge min {right.min()}, "
          f"bottom edge min {bottom.min()}, map min {g.min()}")
    dark_cols = int((g.max(axis=0) < 16).sum())
    dark_rows = int((g.max(axis=1) < 16).sum())
    if dark_cols or dark_rows:
        failures.append(f"{w}x{h} -> {gw}x{gh}: {dark_cols} black columns and "
                        f"{dark_rows} black rows in the gray map")


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    os.environ["NS_WINDOW_POS"] = "-30000,-30000"
    os.environ["NS_NR_SMALL"] = "0"
    os.environ["NS_HDR"] = "0"
    failures: list = []
    for size, gray in CASES:
        try:
            _run(size, gray, failures)
        except Exception as exc:
            failures.append(f"{size}: the run raised {type(exc).__name__}: {exc}")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the gray map covers the whole frame at sizes that do not divide")
    return 0


if __name__ == "__main__":
    sys.exit(main())
