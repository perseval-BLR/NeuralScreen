r"""The fisheye lens bends the output by the game's field of view, and only when asked.

The lens (native/lens.inl, the LENS command) re-projects the picture the viewer
sees from a rectilinear frame with a known horizontal field of view into an
equidistant fisheye: equal angles take equal distances from the centre, the
edges a wide game frame stretches are compressed, and the corners stay where
they are, so there is no black border.

Checked on the real worker:

* Geometry, in the pixel channel with NR off (the worker hands back the
  capture itself, so the lens is the only thing between input and output). The
  input encodes its own coordinates in red and green; at every probe point of
  the output those must be the source coordinates the equidistant mapping
  predicts for a 120-degree frame - to within the 8-bit ramp's precision. The
  centre stays the centre, the corners stay the corners, no pixel is black,
  and a point halfway to the right edge comes from much nearer the centre.
* Webcam noise over it: unbiased, clearly stronger in the shadows than in the
  light, within what a webcam shows, and renewed over time.
* Off means off: before the first LENS and after LENS off, the output is the
  input bit for bit.
* On the presented paths - the worker's own window, with and without Frame
  Generation - frames keep being answered with the lens on, the worker says
  it is on, and nothing fails.

Run:  runtime\python.exe tests\test_lens.py
"""
import ctypes
import math
import os
import struct
import subprocess
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

from paths import NATIVE_DIR, WORKER_EXE  # noqa: E402
from protocol import (FRAME_FLAG_BYPASS, FRAME_FLAG_WANT_PIXELS, FRAME_FMT,  # noqa: E402
                      FRAME_MAGIC, HEADER_FMT, LENS_ACK_FMT, LENS_ACK_MAGIC,
                      LENS_FLAG_ON, LENS_FMT, LENS_MAGIC, OUT_FMT, OUT_MAGIC,
                      VIDEO_MAGIC)
from worker_reply import read_exact, read_reply  # noqa: E402

W, H = 640, 360
FOV = 120.0


def _ramp() -> np.ndarray:
    yy, xx = np.indices((H, W), dtype=np.float64)
    rgba = np.empty((H, W, 4), np.uint8)
    rgba[..., 0] = np.round(xx * 255.0 / (W - 1))
    rgba[..., 1] = np.round(yy * 255.0 / (H - 1))
    rgba[..., 2] = 128
    rgba[..., 3] = 255
    return rgba


def _expected_source(x: float, y: float, fov: float) -> tuple:
    """Where the output pixel centre (x, y) takes its colour from."""
    cx, cy = W / 2.0, H / 2.0
    dx, dy = x + 0.5 - cx, y + 0.5 - cy
    r = math.hypot(dx, dy)
    f_rect = (W / 2.0) / math.tan(math.radians(fov) / 2.0)
    half_diag = 0.5 * math.hypot(W, H)
    f_fish = half_diag / math.atan(half_diag / f_rect)
    if r < 1e-6:
        return cx, cy
    s = f_rect * math.tan(r / f_fish) / r
    return cx + dx * s, cy + dy * s


def _pipe_run(failures: list) -> None:
    profile = dict(style=1, auto_mask=1, intensity=1.0, local_tone=0.5,
                   local_structure=1.0, skin_structure=-1.0)
    header = struct.pack(HEADER_FMT, VIDEO_MAGIC, W, H, 2, 0, 0, 0,
                         profile["style"], profile["auto_mask"], 0,
                         profile["intensity"], profile["local_tone"],
                         profile["local_structure"], profile["skin_structure"], W, H)
    env = dict(os.environ, NS_NR_SMALL="0", NS_HDR="0")
    proc = subprocess.Popen([str(WORKER_EXE), "--live"], cwd=str(NATIVE_DIR), env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    logs: list = []
    threading.Thread(target=lambda: logs.extend(iter(proc.stderr.readline, b"")),
                     daemon=True).start()
    watchdog = threading.Timer(120, proc.kill)
    watchdog.start()
    rgba = _ramp()
    motion = np.zeros((H, W, 2), np.float16)
    index = [0]

    def frame() -> np.ndarray:
        i = index[0]
        index[0] += 1
        proc.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, i, 1 if i == 0 else 0,
                                     FRAME_FLAG_WANT_PIXELS | FRAME_FLAG_BYPASS, i)
                         + rgba.tobytes() + motion.tobytes())
        proc.stdin.flush()
        magic, idx, ok, nbytes, _ngx, _pts = struct.unpack(
            OUT_FMT, read_reply(proc.stdout, struct.calcsize(OUT_FMT)))
        if magic != OUT_MAGIC or not ok or idx != i or not nbytes:
            raise RuntimeError(f"bad reply to frame {i}: ok={ok} bytes={nbytes}")
        return np.frombuffer(read_exact(proc.stdout, nbytes), np.uint8).reshape(H, W, 4).copy()

    def lens(on: bool, fov: float, noise: float = 0.0) -> None:
        proc.stdin.write(struct.pack(LENS_FMT, LENS_MAGIC, LENS_FLAG_ON if on else 0,
                                     fov, noise, 0))
        proc.stdin.flush()
        magic, ok, _r0, _r1, _pts = struct.unpack(
            LENS_ACK_FMT, read_reply(proc.stdout, struct.calcsize(LENS_ACK_FMT)))
        if magic != LENS_ACK_MAGIC or not ok:
            raise RuntimeError(f"LENS was not acknowledged (0x{magic:08X}, ok={ok})")

    bent = noisy = noisy_later = None
    try:
        proc.stdin.write(header)
        proc.stdin.flush()
        before = frame()
        if not np.array_equal(before[..., :3], rgba[..., :3]):
            failures.append("with no lens asked for, the output is not the input")
        lens(True, FOV)
        bent = frame()
        lens(True, FOV, 1.0)
        noisy = frame()
        time.sleep(0.12)              # the noise is renewed 30 times a second
        noisy_later = frame()
        lens(False, FOV)
        after = frame()
        if not np.array_equal(after[..., :3], rgba[..., :3]):
            failures.append("after LENS off, the output is not the input")
    except Exception as exc:  # noqa: BLE001 - reported
        failures.append(f"the pixel-channel run failed: {type(exc).__name__}: {exc}")
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        watchdog.cancel()
    text = b"".join(logs).decode("utf-8", "replace")
    for line in text.splitlines():
        if "[lens] shader failed" in line or "[lens] pipeline failed" in line:
            failures.append("the lens pipeline did not build: " + line.strip())
    if bent is None:
        return
    if noisy is not None and noisy_later is not None:
        diff = noisy[..., :3].astype(np.float64) - bent[..., :3].astype(np.float64)
        dark = diff[: H // 3, : W // 3].std()      # the ramp is darkest top-left
        bright = diff[2 * H // 3:, 2 * W // 3:].std()
        moved = np.abs(noisy_later[..., :3].astype(int) - noisy[..., :3].astype(int)).mean()
        print(f"    webcam noise at 100%: mean shift {diff.mean():+.2f}, spread "
              f"{dark:.1f} in the shadows vs {bright:.1f} in the light, "
              f"{moved:.1f} codes of change 120 ms later")
        if abs(diff.mean()) > 1.0:
            failures.append(f"the noise shifts the picture ({diff.mean():+.2f} codes)")
        if not 1.5 <= dark <= 20.0:
            failures.append(f"the noise in the shadows is {dark:.1f} codes - not a webcam's")
        if not dark > 1.5 * bright:
            failures.append(f"the noise is not stronger in the shadows ({dark:.1f} vs {bright:.1f})")
        if moved < 0.5:
            failures.append("the noise does not change over time")
    if (bent[..., 2] < 64).any():
        failures.append(f"{int((bent[..., 2] < 64).sum())} output pixels are black - "
                        "the lens should fill the frame")
    worst = 0.0
    for y in range(8, H - 8, 16):
        for x in range(8, W - 8, 16):
            sx, sy = _expected_source(x, y, FOV)
            got_x = float(bent[y, x, 0]) * (W - 1) / 255.0
            got_y = float(bent[y, x, 1]) * (H - 1) / 255.0
            worst = max(worst, abs(got_x - sx), abs(got_y - sy))
    print(f"    {FOV:.0f} degrees: worst source-position error {worst:.2f} px "
          "over the probe grid (8-bit ramp: ~1.3 px a code in x)")
    if worst > 4.0:
        failures.append(f"the bend does not follow the equidistant mapping "
                        f"(worst error {worst:.1f} px)")
    sx, _ = _expected_source(W * 0.75, H / 2 - 0.5, FOV)
    got = float(bent[H // 2, int(W * 0.75), 0]) * (W - 1) / 255.0
    print(f"    the point halfway to the right edge comes from x = {got:.0f} "
          f"(expected {sx:.0f}, unbent {W * 0.75:.0f})")
    if not got < W * 0.75 - 30:
        failures.append(f"halfway to the edge is not drawn from nearer the centre ({got:.0f})")
    # The frame's own corner maps onto itself; the pixels next to it come from
    # a few pixels inward (tan is steep there) - no farther than the mapping says.
    for (x, y) in ((0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1)):
        sx, sy = _expected_source(x, y, FOV)
        gx = float(bent[y, x, 0]) * (W - 1) / 255.0
        gy = float(bent[y, x, 1]) * (H - 1) / 255.0
        if abs(gx - sx) > 4 or abs(gy - sy) > 4 or abs(sx - x) > 8 or abs(sy - y) > 8:
            failures.append(f"the corner ({x},{y}) is drawn from ({gx:.0f},{gy:.0f}), "
                            f"expected ({sx:.0f},{sy:.0f})")


def _present_run(failures: list) -> None:
    from offscreen_target import Target
    from pipeline import shutdown_worker, start_worker
    from protocol import send_frame, send_lens, send_motion_size, send_wgc, send_window
    from settings_io import PROFILES

    os.environ["NS_WINDOW_POS"] = "-30000,-30000"
    os.environ["NS_NR_SMALL"] = "0"
    os.environ["NS_HDR"] = "0"
    gw, gh = 160, 90
    target = Target(W, H, name="NsLens", ghost=True)
    target.animate_interval = 1.0 / 30.0
    target.animate = True
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2, 0, 0, None)
    motion = np.zeros((gh, gw, 2), np.float16)
    answered = {"plain": 0, "fg": 0}
    try:
        send_wgc(worker, target.hwnd)
        reader.wait_wgak(15)
        send_motion_size(worker, gw, gh)
        reader.wait_mack(10)
        send_window(worker, W, H)
        reader.wait_wack(10)
        send_lens(worker, True, 110.0)
        i = 0
        for phase, fg in (("plain", None), ("fg", True)):
            end = time.perf_counter() + 2.5
            while time.perf_counter() < end:
                send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                           motion_small=True, frame_generation=fg, frame_multiplier=2,
                           want_pixels=(i % 20 == 5))
                reader.recv(i, timeout=10.0)
                answered[phase] += 1
                i += 1
        if worker.poll() is not None:
            failures.append(f"the worker ended with the lens on (code {worker.returncode})")
    except Exception as exc:  # noqa: BLE001 - reported
        failures.append(f"the presented run failed: {type(exc).__name__}: {exc}")
    finally:
        shutdown_worker(worker, stop)
        target.close()
    text = "\n".join(str(line) for line in logs)
    print(f"    presented frames with the lens on: {answered['plain']} plain, "
          f"{answered['fg']} with Frame Generation")
    if "[lens] on" not in text:
        failures.append("the worker never said the lens is on")
    for bad in ("[lens] target", "[lens] shader failed", "[lens] pipeline failed",
                "device removed", "fence-timeout"):
        if bad in text:
            failures.append(f"the log says {bad!r}")
    if answered["plain"] < 20 or answered["fg"] < 20:
        failures.append(f"too few frames were answered: {answered}")


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    failures: list = []
    _pipe_run(failures)
    _present_run(failures)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the lens bends the output by the field of view on every path, and off is off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
