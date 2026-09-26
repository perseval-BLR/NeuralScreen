"""Recording on the GPU: the encoder alone, then through a real worker.

Everything here runs on this machine's card and none of it opens a window.

1. gpu_recorder_check.exe - the recorder with no worker: the codecs the
   driver offers, the colour conversion (BT.709 studio range, measured on
   four bars), a mid-recording resize (letterboxed, not stretched), the A/V
   sync of the file (white flashes against tone bursts at the same
   instants), and HDR10 - 10-bit PQ bars in, their exact BT.2020 code values
   and the HDR tags out, in AV1 and in HEVC Main10 through a resize.
2. A worker in the converter's shape (no capture, no window) recording what
   it processes, driven by GpuRecorder - the real client class, the real
   RECS/RECE, the real loopback ring. The file must publish, run at the
   stream's rate, carry sound, and show the frames that were sent.
3. The same worker failing: a write error injected after 30 frames must end
   the recording by itself (an unasked REAK), and a worker that exits in the
   middle must still leave a file that plays.

A canonical SKIP without the worker binary, and stage 1 alone skips without
a compiler.

Run:  runtime\\python.exe tests\\test_gpu_recorder.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import av  # noqa: E402
import numpy as np  # noqa: E402

from paths import WORKER_EXE  # noqa: E402

NATIVE = BASE / "native"
WORK = BASE / "_work" / "grec"

#: BT.709 studio range of the four bars (red, green, blue, grey 128).
BARS_YUV = {"red": (63, 102, 240), "green": (173, 42, 26),
            "blue": (32, 240, 118), "grey": (126, 128, 128)}
#: HDR10 (the check's hdr 1): PQ code values in, 10-bit Y'CbCr out - BT.2020
#: non-constant luminance, studio range. The greys carry the curve, the PQ
#: red the matrix: BT.709's would put its luma at 157, not 179.
HDR_BARS = {"PQ grey 0.25": (283, 512, 512), "PQ grey 0.5": (502, 512, 512),
            "PQ grey 0.75": (721, 512, 512), "PQ red 0.5": (179, 449, 736)}
#: AVCOL_PRI_BT2020, AVCOL_TRC_SMPTE2084, AVCOL_SPC_BT2020_NCL.
HDR_TAGS = (9, 16, 9)


def build_check(failures: list) -> Path | None:
    """Build the check; None with a SKIP only when there is no compiler."""
    exe = WORK / "gpu_recorder_check.exe"
    WORK.mkdir(parents=True, exist_ok=True)
    # One string through cmd /s: a list would be quoted by Python and then
    # re-read by cmd's own quote rule, which breaks on a space in the path
    # (the profile folder here has one).
    bat = NATIVE / "build-gpu-recorder-check.bat"
    # The build script is cmd/bat: its own messages come out in the console
    # codepage (cp866 here), while the child gets PYTHONIOENCODING=utf-8 from
    # the test runner. Decoding the compiler's output as utf-8 then raises
    # UnicodeDecodeError inside subprocess's reader thread - in a thread, so
    # the exception never reaches the test: it surfaced only as a stray
    # traceback in the middle of a run. Read it as bytes and decode leniently.
    proc = subprocess.run(f'cmd /s /c ""{bat}" "{WORK}""', capture_output=True,
                          timeout=300)
    output = (proc.stdout or b"").decode("utf-8", errors="replace") + \
             (proc.stderr or b"").decode("utf-8", errors="replace")
    if proc.returncode == 0 and exe.is_file():
        return exe
    if "NO_COMPILER" in output:
        print("SKIP: stage 1 - no Visual Studio C++ tools to build "
              "gpu_recorder_check.exe")
    else:
        failures.append("gpu_recorder_check.exe did not build:\n" + output[-2000:])
    return None


def run_check(exe: Path, out: Path, *args) -> dict:
    # The recorder prints in the console codepage (cp866); decoding its output
    # as utf-8 dies the same way the build script's does.
    proc = subprocess.run([str(exe), str(out), *map(str, args)],
                          capture_output=True, timeout=120)
    out_text = (proc.stdout or b"").decode("utf-8", errors="replace")
    err_text = (proc.stderr or b"").decode("utf-8", errors="replace")
    lines = [ln for ln in out_text.splitlines() if ln.startswith("{")]
    stats = json.loads(lines[-1]) if lines else {}
    stats["exit"] = proc.returncode
    stats["log"] = err_text
    return stats


def yuv_at(frame, x: int, y: int) -> tuple:
    yuv = frame.reformat(format="yuv444p").to_ndarray()
    return tuple(int(yuv[c, y, x]) for c in range(3))


def yuv10_at(frame, x: int, y: int) -> tuple:
    """10-bit Y, Cb, Cr at a luma position of a 4:2:0 frame."""
    a = frame.to_ndarray(format="yuv420p10le")
    h, w = frame.height, frame.width
    u = a[h:h + h // 4].reshape(h // 2, w // 2)
    v = a[h + h // 4:].reshape(h // 2, w // 2)
    return int(a[y, x]), int(u[y // 2, x // 2]), int(v[y // 2, x // 2])


def stage_hdr(exe: Path, failures: list) -> None:
    """HDR10 through the recorder alone: 640x360, 2 s at the 60 fps clock."""
    for name, codec, resize in (("HDR10", 0, 0), ("HDR10 HEVC, resized", 2, 1)):
        out = WORK / f"hdr10_{codec}.mp4"
        st = run_check(exe, out, 2, 640, 360, codec, 60, resize, 0, 0, 1)
        if st["exit"] != 0 or not st.get("hdr"):
            failures.append(f"{name}: no HDR10 recording ({st}); log:\n{st['log'][-800:]}")
            continue
        early = late = None
        count, fmt = 0, ""
        with av.open(str(out)) as c:
            vs = c.streams.video[0]
            cc = vs.codec_context
            tags = (cc.color_primaries, cc.color_trc, cc.colorspace)
            for f in c.decode(vs):
                count += 1
                t = float(f.pts * vs.time_base)
                if early is None and t >= 0.5:
                    fmt = f.format.name
                    early = {k: yuv10_at(f, int(640 * (i + 0.5) / 4), 90)
                             for i, k in enumerate(HDR_BARS)}
                if resize and late is None and t >= 1.6:
                    # 320x240 fitted into 640x360: 480 wide, from x = 80.
                    late = {k: yuv10_at(f, int(80 + 480 * (i + 0.5) / 4), 90)
                            for i, k in enumerate(HDR_BARS)}
                    late["letterbox"] = yuv10_at(f, 8, 180)
        print(f"    {name}: codec {st['codec']}, {fmt}, tags {tags}, {count} frames, "
              f"{st['dropped']} dropped")
        if tags != HDR_TAGS:
            failures.append(f"{name}: tagged {tags}, not BT.2020 / PQ / BT.2020 {HDR_TAGS}")
        if "10" not in fmt:
            failures.append(f"{name}: the frames decode as {fmt!r}, not 10-bit")
        if abs(count - 120) > 6:
            failures.append(f"{name}: {count} frames in a 2 s recording at 60 fps")
        want = dict(HDR_BARS, letterbox=(64, 512, 512))
        for when, got in (("", early), (" after the resize", late)):
            if got is None:
                if when == "" or resize:
                    failures.append(f"{name}: no frame to measure{when}")
                continue
            for k, have in got.items():
                if max(abs(a - b) for a, b in zip(have, want[k])) > 2:
                    failures.append(f"{name}: {k}{when} is {have}, expected {want[k]}")


def stage_native(failures: list) -> None:
    exe = build_check(failures)
    if exe is None:
        return

    # Every codec: each must record, or fall back to one that does.
    for codec, name in ((1, "H.264"), (2, "HEVC"), (3, "AV1")):
        out = WORK / f"codec{codec}.mp4"
        st = run_check(exe, out, 2, 1280, 720, codec, 60, 0)
        if st["exit"] != 0 or st.get("written", 0) < 100:
            failures.append(f"{name}: the check failed ({st}); log:\n{st['log'][-800:]}")
            continue
        with av.open(str(out)) as c:
            frames = sum(1 for _ in c.decode(video=0))
        print(f"    {name:6} asked, codec {st['codec']} used: {frames} frames "
              f"in 2 s, {st['dropped']} dropped")
        if abs(frames - 120) > 6:
            failures.append(f"{name}: {frames} frames in a 2 s recording at 60 fps")

    # Colour, resize and the frame clock, on the automatic choice.
    out = WORK / "auto.mp4"
    st = run_check(exe, out, 3, 1920, 1080, 0, 60, 1)
    if st["exit"] != 0:
        failures.append(f"auto: the check failed ({st}); log:\n{st['log'][-800:]}")
        return
    with av.open(str(out)) as c:
        vs = c.streams.video[0]
        pts, early, late = [], None, None
        for f in c.decode(vs):
            t = float(f.pts * vs.time_base)
            pts.append(t)
            if early is None and t >= 1.0:
                early = {n: yuv_at(f, int(1920 * (i + 0.5) / 4), 270)
                         for i, n in enumerate(BARS_YUV)}
                size = (f.width, f.height)
            if late is None and t >= 2.6:
                # 960x720 fitted into 1920x1080: 1440 wide, from x = 240.
                late = {n: yuv_at(f, int(240 + 1440 * (i + 0.5) / 4), 270)
                        for i, n in enumerate(BARS_YUV)}
                late["edge"] = yuv_at(f, 8, 540)
        has_audio = bool(c.streams.audio)
    if size != (1920, 1080):
        failures.append(f"auto: the frames are {size}, not 1920x1080")
    gaps = np.diff(pts) * 1000.0
    print(f"    auto: {len(pts)} frames, gap {gaps.min():.2f}..{gaps.max():.2f} ms, "
          f"audio {has_audio}")
    if gaps.max() > 17.0 or gaps.min() < 16.3:
        failures.append(f"auto: the frame clock is uneven ({gaps.min():.2f}.."
                        f"{gaps.max():.2f} ms at 60 fps)")
    if not has_audio:
        failures.append("auto: the file has no audio track")
    for when, got in (("before the resize", early), ("after the resize", late)):
        for name, want in BARS_YUV.items():
            have = got[name]
            if max(abs(a - b) for a, b in zip(have, want)) > 3:
                failures.append(f"{name} bar {when}: YUV {have}, expected {want} "
                                f"(BT.709 studio range)")
    if late["edge"][0] > 18:
        failures.append(f"the letterbox is not black: Y={late['edge'][0]}")

    # A/V sync: flashes and bursts at the same instants - with a frame in
    # every slot, then at an uneven ~30 fps the way a live pipeline slower
    # than the recording's clock delivers them. The second is the case that
    # used to drift: each fragment of the MP4 took the last frame's duration
    # as 1/60 s whatever the real gap, and the picture ran ahead of the sound
    # by ~7% of the recording.
    check_sync(exe, failures, "sync", 4, 0, tolerance=(-2.0, 2.0))
    check_sync(exe, failures, "sync at ~30 fps", 9, 30, tolerance=(-5.0, 55.0))
    stage_hdr(exe, failures)


def check_sync(exe: Path, failures: list, name: str, seconds: float,
               pace: float, tolerance: tuple) -> None:
    out = WORK / f"{name.replace(' ', '_').replace('~', '')}.mp4"
    st = run_check(exe, out, seconds, 1280, 720, 0, 60, 0, 1, pace)
    if st["exit"] != 0:
        failures.append(f"{name}: the check failed ({st})")
        return
    with av.open(str(out)) as c:
        vs = c.streams.video[0]
        flashes, prev = [], False
        for f in c.decode(vs):
            white = f.to_ndarray(format="gray")[::16, ::16].mean() > 200
            if white and not prev:
                flashes.append(float(f.pts * vs.time_base))
            prev = white
    with av.open(str(out)) as c:
        a = c.streams.audio[0]
        rate = a.codec_context.sample_rate
        start, chunks = None, []
        for f in c.decode(a):
            if start is None:
                start = float(f.pts * a.time_base)
            arr = f.to_ndarray()
            chunks.append(arr[0] if arr.ndim == 2 else arr)
    env = np.abs(np.concatenate(chunks))
    env = env[: len(env) // (rate // 1000) * (rate // 1000)]
    env = env.reshape(-1, rate // 1000).max(axis=1)
    bursts, on = [], False
    for i, v in enumerate(env):
        if v > 0.05 and not on:
            bursts.append(start + i / 1000.0)
            on = True
        elif v < 0.01:
            on = False
    # Picture minus sound: positive when the flash comes later. At an uneven
    # ~30 fps a flash can land up to one frame interval after its burst.
    offsets = [round((v - b) * 1000.0, 1) for v, b in zip(flashes, bursts)]
    print(f"    {name}: {len(flashes)} flashes, picture - sound {offsets} ms")
    if len(offsets) < seconds - 1:
        failures.append(f"{name}: {len(flashes)} flashes, {len(bursts)} bursts")
    elif not all(tolerance[0] <= o <= tolerance[1] for o in offsets):
        failures.append(f"{name}: picture and sound drift apart: {offsets} ms "
                        f"(allowed {tolerance[0]:g}..{tolerance[1]:g})")


def pattern(i: int, w: int, h: int) -> np.ndarray:
    """Four bars on top, a white square moving across black below."""
    img = np.zeros((h, w, 4), np.uint8)
    img[..., 3] = 255
    bars = ((230, 30, 30), (30, 200, 30), (30, 30, 230), (128, 128, 128))
    for k, rgb in enumerate(bars):
        img[: h // 2, k * w // 4:(k + 1) * w // 4, :3] = rgb
    box = h // 8
    x = (i * 6) % (w - box)
    img[h * 3 // 4 - box // 2: h * 3 // 4 + box // 2, x:x + box, :3] = 255
    return img


def record_through_worker(folder: Path, *, seconds: float, ending: str,
                          fail_stage: str = "") -> tuple:
    """Drive a worker as the converter does and record it with GpuRecorder.

    ending: "rece" (the user stops), "auto" (the worker stops by itself),
    or "eof" (the worker goes away mid-recording).
    """
    from pipeline import shutdown_worker, start_worker
    from protocol import send_frame
    from recorder import GpuRecorder
    from settings_io import PROFILES

    params = dict(PROFILES["Natural"])
    params["style"] = 1
    w, h = 640, 360
    old = os.environ.get("NS_TEST_FAIL_STAGE")
    if fail_stage:
        os.environ["NS_TEST_FAIL_STAGE"] = fail_stage
    try:
        worker, logs, reader, stop = start_worker(params, w, h, 2)
    finally:
        if old is None:
            os.environ.pop("NS_TEST_FAIL_STAGE", None)
        else:
            os.environ["NS_TEST_FAIL_STAGE"] = old
    rec = None
    sent = 0
    try:
        motion = np.zeros((h, w, 2), np.float16)
        # One frame before the recording: the worker's first frame runs the
        # warm-up, which is not what is being measured.
        send_frame(worker, 0, pattern(0, w, h), motion, True, 0,
                   shm=None, want_pixels=True)
        reader.recv(0, timeout=60.0)
        rec = GpuRecorder(worker, reader, str(folder / f"{ending}.mp4"),
                          fps=30, audio=True)
        t0 = time.perf_counter()
        i = 1
        while time.perf_counter() - t0 < seconds:
            send_frame(worker, i, pattern(i, w, h), motion, False, i,
                       shm=None, want_pixels=True)
            reader.recv(i, timeout=30.0)
            sent += 1
            i += 1
            if ending == "auto" and rec.stopped_elsewhere():
                break
            # Paced like a 60 fps screen: the recorder takes every other one.
            time.sleep(max(0.0, t0 + i / 60.0 - time.perf_counter()))
        if ending == "eof":
            shutdown_worker(worker, stop)
            deadline = time.monotonic() + 10
            while not rec.stopped_elsewhere() and time.monotonic() < deadline:
                time.sleep(0.05)
        if ending == "auto":
            deadline = time.monotonic() + 10
            while not rec.stopped_elsewhere() and time.monotonic() < deadline:
                time.sleep(0.05)
        stopped_elsewhere = rec.stopped_elsewhere()
        rec.finish()
        result = rec.wait(40.0)
        return rec, result, stopped_elsewhere, sent, logs
    finally:
        shutdown_worker(worker, stop)


def probe(path: Path) -> dict:
    info = {"frames": 0, "audio": False, "codec": "", "size": None,
            "duration": 0.0, "top": None}
    with av.open(str(path)) as c:
        info["audio"] = bool(c.streams.audio)
        vs = c.streams.video[0]
        info["codec"] = vs.codec_context.name
        last = 0.0
        for f in c.decode(vs):
            info["frames"] += 1
            info["size"] = (f.width, f.height)
            last = float(f.pts * vs.time_base)
            if info["top"] is None and info["frames"] == 10:
                rgb = f.to_ndarray(format="rgb24").astype(int)
                hh, ww = rgb.shape[:2]
                info["top"] = [rgb[hh // 4, int(ww * (k + 0.5) / 4)].tolist()
                               for k in range(4)]
        info["duration"] = last
    return info


def stage_worker(failures: list) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)

        # 2. The user presses stop.
        rec, result, _, sent, logs = record_through_worker(
            folder, seconds=3.0, ending="rece")
        status = getattr(result, "status", None)
        if status is None or status.value != "published":
            failures.append(f"stop: the recording did not publish ({result}); "
                            f"worker log:\n" + "\n".join(logs[-15:]))
        else:
            info = probe(Path(result.path))
            print(f"    stop: {rec.codec} {info['size']}, {info['frames']} frames "
                  f"over {info['duration']:.2f} s ({sent} sent), audio "
                  f"{info['audio']}, dropped {rec.dropped}, bars {info['top']}")
            if list(folder.glob("*.partial")):
                failures.append("stop: a .partial file was left behind")
            if info["size"] != (640, 360):
                failures.append(f"stop: the frames are {info['size']}, not 640x360")
            if not 80 <= info["frames"] <= 100:
                failures.append(f"stop: {info['frames']} frames in a 3 s "
                                f"recording at 30 fps")
            if not info["audio"]:
                failures.append("stop: the file has no audio track")
            red, green, blue, grey = info["top"] or [[0, 0, 0]] * 4
            if not (red[0] > red[1] + 60 and green[1] > green[0] + 60
                    and blue[2] > blue[0] + 60 and abs(grey[0] - grey[2]) < 40):
                failures.append(f"stop: the recorded bars are not the sent "
                                f"ones: {info['top']}")
            if rec.cut_short:
                failures.append("stop: an ordinary stop was reported as cut short")

        # 3a. The encoder fails after 30 frames: the worker stops by itself.
        rec, result, by_itself, sent, logs = record_through_worker(
            folder, seconds=6.0, ending="auto", fail_stage="grec-write")
        status = getattr(result, "status", None)
        if not by_itself:
            failures.append("write failure: the worker did not end the "
                            "recording by itself (no unasked REAK)")
        if status is None or status.value != "published":
            failures.append(f"write failure: the 30 frames were not published "
                            f"({result})")
        else:
            info = probe(Path(result.path))
            print(f"    write failure: stopped by itself {by_itself}, cut short "
                  f"{rec.cut_short}, {info['frames']} frames ({sent} sent)")
            if not rec.cut_short:
                failures.append("write failure: not reported as cut short")
            if not 25 <= info["frames"] <= 35:
                failures.append(f"write failure: {info['frames']} frames kept, "
                                f"expected about 30")
        if not any("[grec] injecting" in line for line in logs):
            failures.append("write failure: the injection never ran")

        # 3b. The worker goes away mid-recording.
        rec, result, lost, sent, logs = record_through_worker(
            folder, seconds=2.0, ending="eof")
        status = getattr(result, "status", None)
        if not lost:
            failures.append("worker gone: the recorder did not notice")
        if status is None or status.value != "published":
            failures.append(f"worker gone: nothing was kept ({result})")
        else:
            info = probe(Path(result.path))
            print(f"    worker gone: noticed {lost}, cut short {rec.cut_short}, "
                  f"{info['frames']} frames ({rec.written} counted)")
            if info["frames"] < 40:
                failures.append(f"worker gone: only {info['frames']} frames kept")


def main() -> int:
    failures: list = []
    print("  stage 1: the recorder alone")
    stage_native(failures)
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - stages 2 and 3 cannot run")
    else:
        print("  stages 2-3: through a worker")
        try:
            stage_worker(failures)
        except Exception as exc:
            failures.append(f"the worker stages raised {type(exc).__name__}: {exc}")
    if failures:
        print("=" * 60)
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: GPU recording - codecs, colour, resize, sync, HDR10, and the "
          "worker's stop, failure and loss")
    return 0


if __name__ == "__main__":
    sys.exit(main())
