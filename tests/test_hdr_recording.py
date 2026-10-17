"""HDR10 recording through a real worker: the file holds what the display gets.

With HDR on, the worker composes each frame in FP16 scRGB (or 10-bit PQ for
DLSS-G) and used to hand the recorder its SDR proxy - an HDR session recorded
as SDR. Now an HDR10 recording takes the composite itself as 10-bit PQ
BT.2020: with Frame Generation the composite already is that; without it,
the same composite is dispatched a second time with the PQ encode on. The
recorder converts it with its own shaders (the video processor on this
driver converts within BT.709 only) and encodes AV1 or HEVC Main10.

Checked with Frame Generation off and on, NR off - the raw capture, so the
values are the desktop's own, with no network in between:

* the recorder is told HDR10 and names it ("AV1 HDR10" or "HEVC HDR10");
* the published file is 10-bit and tagged BT.2020 / PQ / BT.2020;
* the captured window's white, grey and black come out in that order, white
  at the PQ level of an SDR white on an HDR desktop (80-480 nits, whatever
  the brightness slider says) - not SDR's code 235 and not PQ's 10 000-nit
  top - and black at the floor;
* with Frame Generation on (a PQ composite) and the colour's exposure at +1
  (the camera off), the grey carries twice the light of the plain run's grey -
  the colour works in light, not on PQ code values, where a stop came out as
  several (#164).

A light test: 960x540, frames paced to 60 fps, 2 s of recording per run. It
needs an HDR display nearest the capture target and SKIPs without one. The
target is the invisible ghost of tests/offscreen_target.py.

Run:  runtime\\python.exe tests\\test_hdr_recording.py
"""
import ctypes
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
sys.path.insert(0, str(Path(__file__).resolve().parent))

import av  # noqa: E402
import numpy as np  # noqa: E402

from offscreen_target import Target  # noqa: E402
from paths import WORKER_EXE  # noqa: E402

W, H = 960, 540
MAX_FPS = 60              # a light test: never an uncapped loop on the GPU
RECORD_S = 2.0
# White, mid grey, black, white - GDI colours on an HDR desktop.
BARS = (0xFFFFFF, 0x808080, 0x000000, 0xFFFFFF)
HDR_TAGS = (9, 16, 9)     # AVCOL_PRI_BT2020, AVCOL_TRC_SMPTE2084, AVCOL_SPC_BT2020_NCL


def luma10(frame, x: int, y: int) -> int:
    return int(frame.to_ndarray(format="yuv420p10le")[y, x])


def from_pq(n: float) -> float:
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    p = max(0.0, min(1.0, n)) ** (1 / m2)
    return (max(p - c1, 0.0) / (c2 - c3 * p)) ** (1 / m1)


def to_pq(lin: float) -> float:
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    y = max(0.0, min(1.0, lin)) ** m1
    return ((c1 + c2 * y) / (1 + c3 * y)) ** m2


RESULTS: dict = {}


def run(fg: bool, folder: Path, failures: list, exposure: float = 0.0) -> bool:
    """One worker, one HDR10 recording. False when HDR capture is not on."""
    from pipeline import shutdown_worker, start_worker
    from protocol import (SharedFrameBuffer, send_frame, send_gray, send_lens,
                          send_motion_size, send_resize, send_wgc, send_window)
    from recorder import GpuRecorder
    from settings_io import PROFILES

    label = f"FG {'on' if fg else 'off'}" + (f", exposure {exposure:+g}" if exposure else "")
    target = Target(W, H, name=f"NsHdrRecording{int(fg)}", ghost=True)
    target.set_bars(BARS)
    params = dict(PROFILES["Natural"])
    params["style"] = 1
    shm = SharedFrameBuffer(W, H)
    worker, logs, reader, stop = start_worker(params, W, H, 2, 0, 0, None)
    state = {"index": 0, "due": 0.0}
    motion = np.zeros((90, 160, 2), np.float16)

    def frame():
        wait = state["due"] - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        state["due"] = max(state["due"], time.perf_counter()) + 1.0 / MAX_FPS
        i = state["index"]
        state["index"] += 1
        send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                   motion_small=True, bypass=True,
                   frame_generation=fg, frame_multiplier=2)
        reader.recv(i, timeout=10.0)

    rec = result = None
    try:
        send_wgc(worker, target.hwnd)
        if reader.wait_wgak(15) != (W, H):
            failures.append(f"{label}: the capture target is not {W}x{H}")
        shm.open_gray(160, 90)
        send_gray(worker, 160, 90, shm.gray_name)
        reader.wait_gak(10)
        send_motion_size(worker, 160, 90)
        reader.wait_mack(10)
        send_window(worker, W, H)
        reader.wait_wack(10)
        send_resize(worker, params, W, H, 2, 0, 0, False, False, 1)
        reader.wait_rack(60)
        if exposure:
            send_lens(worker, False, 110.0, 0.0, exposure=exposure)   # the camera off
        for _ in range(30):
            frame()
        if not any("capture=FP16" in ln for ln in logs):
            print(f"    {label}: no FP16 capture here (no HDR display nearest "
                  f"the target) - nothing to check")
            return False
        rec = GpuRecorder(worker, reader, str(folder / f"hdr_fg{int(fg)}.mp4"),
                          fps=60, audio=False, hdr=True)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < RECORD_S:
            frame()
        rec.finish()
        result = rec.wait(30.0)
    except Exception as exc:
        failures.append(f"{label}: the run raised {type(exc).__name__}: {exc}")
    finally:
        shutdown_worker(worker, stop)
        shm.close()
        target.close()
    if rec is None or result is None:
        return True

    print(f"    {label}: recorder says {rec.codec!r}, hdr={rec.hdr}, "
          f"{rec.written} frames, {rec.dropped} dropped")
    if not rec.hdr or not rec.codec.endswith("HDR10"):
        failures.append(f"{label}: the recording is not HDR10 ({rec.codec!r})")
    if result.path is None or not result.path.endswith(".mp4") or not Path(result.path).is_file():
        failures.append(f"{label}: nothing was published ({result})")
        return True
    with av.open(result.path) as c:
        vs = c.streams.video[0]
        cc = vs.codec_context
        tags = (cc.color_primaries, cc.color_trc, cc.colorspace)
        count, fmt, bars = 0, "", None
        for f in c.decode(vs):
            count += 1
            if bars is None and float(f.pts * vs.time_base) >= 1.0:
                fmt = f.format.name
                bars = [luma10(f, int(W * (k + 0.5) / 4), H // 2) for k in range(4)]
    print(f"    {label}: {Path(result.path).name}: {cc.name}, {fmt}, tags {tags}, "
          f"{count} frames, luma of white/grey/black/white {bars}")
    if tags != HDR_TAGS:
        failures.append(f"{label}: tagged {tags}, not BT.2020 / PQ / BT.2020")
    if "10" not in fmt:
        failures.append(f"{label}: the frames decode as {fmt!r}, not 10-bit")
    if count < RECORD_S * 60 * 0.8:
        failures.append(f"{label}: {count} frames in {RECORD_S:g} s at 60 fps")
    if bars is None:
        failures.append(f"{label}: no frame to measure")
    elif exposure:
        plain = RESULTS.get(fg)
        if plain is not None:
            # 10-bit studio range: 64 + 876 * PQ.
            want = 64 + 876 * to_pq(2 ** exposure * from_pq((plain[1] - 64) / 876))
            print(f"    {label}: grey {bars[1]}, the plain run's {plain[1]}, "
                  f"a stop more light is {want:.0f}")
            if abs(bars[1] - want) > 12:
                failures.append(f"{label}: grey came out at {bars[1]}, not {want:.0f} "
                                f"(twice the light of {plain[1]})")
    else:
        RESULTS[fg] = bars
        white, grey, black = bars[0], bars[1], bars[2]
        # PQ code of an SDR white at 80-480 nits, in 10-bit studio range.
        if not 440 <= white <= 700:
            failures.append(f"{label}: white came out at {white}, not at an SDR "
                            f"white's PQ level (440-700)")
        if not black <= 70 or not black < grey < white - 30:
            failures.append(f"{label}: white/grey/black out of order: {bars}")
    if not any("[grec]" in ln and "HDR10" in ln for ln in logs):
        failures.append(f"{label}: the worker never said it records HDR10")
    return True


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    os.environ["NS_WINDOW_POS"] = "-30000,-30000"
    os.environ["NS_HDR"] = "1"
    os.environ["NS_NR_SMALL"] = "0"
    os.environ["NS_MOTION_BACKEND"] = "nvofa"
    failures: list = []
    with tempfile.TemporaryDirectory() as tmp:
        ran = [run(fg, Path(tmp), failures) for fg in (False, True)]
        if ran[1]:
            run(True, Path(tmp), failures, exposure=1.0)
    if not any(ran):
        print("SKIP: no HDR display to capture from")
        return 0
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: with HDR on, the recording is HDR10 - tagged, 10-bit, and the "
          "desktop's own levels - with Frame Generation off and on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
