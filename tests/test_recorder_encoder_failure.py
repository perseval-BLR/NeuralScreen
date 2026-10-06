"""A CPU recording whose encoder fails mid-way is noticed, finalized and reported.

VideoRecorder.stopped_elsewhere() always said False, so the main loop learnt
of a dead encoder only through write() - and write() is reached only when the
worker returns pixels, which needs_frame() stops asking for once the encoder
has failed. The recording stayed "recording" with nothing finalizing it and
nothing on screen. Now stopped_elsewhere() says True as soon as the encoder
fails; the main loop finalizes the recording, and a file that closed cleanly
and reads back is published as "cut short", the way a GPU recording that the
worker ended is.

Runs on a fake container (no NVENC) and a fake clock for the frame slots.
"""
from __future__ import annotations

import os
import queue
import sys
import tempfile
import time
import types
from pathlib import Path

import numpy as np

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import commands  # noqa: E402
import recorder  # noqa: E402

W, H = 8, 4
FPS = 30.0


class Clock:
    def __init__(self):
        self.t = 100.0

    def perf_counter(self) -> float:
        return self.t


class FakeStream:
    def encode(self, frame):
        return [] if frame is None else [object()]


class FakeContainer:
    close_error: BaseException | None = None

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.write_bytes(b"partial\n")

    def add_stream(self, _name, rate=None):
        return FakeStream()

    def mux(self, _packet) -> None:
        with self.path.open("ab") as out:
            out.write(b"packet\n")

    def close(self) -> None:
        if FakeContainer.close_error is not None:
            raise FakeContainer.close_error


class Harness(recorder.VideoRecorder):
    def _open_video_stream(self, width, height, fps):
        self.codec = "fake"
        return self._container.add_stream("fake", rate=int(round(fps)))

    def _verify_partial(self) -> None:
        if self.written <= 0:
            raise RuntimeError("recording contains no video frames")
        if not Path(self.partial_path).is_file():
            raise RuntimeError("partial MP4 is missing")


def state():
    alerts: list[str] = []
    return types.SimpleNamespace(
        recorder=None, recording_finalizer=None,
        recording_finalize_deadline=0.0, last_recording={}, lang="en",
        display=types.SimpleNamespace(
            alert=lambda message, *args: alerts.append(str(message))),
        alerts=alerts)


def failing_recording(out: Path, failures: list, label: str,
                      close_error: BaseException | None = None):
    """Three frames encode, the fourth raises; return (rec, seconds to notice)."""
    for p in (out, Path(f"{out}.partial")):
        p.unlink(missing_ok=True)
    clock = Clock()
    real_time, real_open = recorder.time, recorder.av.open
    recorder.time = types.SimpleNamespace(perf_counter=clock.perf_counter)
    recorder.av.open = lambda file, mode=None, format=None, **kw: (  # noqa: ARG005
        FakeContainer(str(file)))
    FakeContainer.close_error = close_error
    try:
        rec = Harness(str(out), W, H, fps=FPS, audio=False)
        real_encode = rec._encode_one
        calls = {"n": 0}

        def encode(pts, rgba):
            calls["n"] += 1
            if calls["n"] > 3:
                raise OSError(5, "NVENC lost the device")
            real_encode(pts, rgba)

        rec._encode_one = encode
        frame = np.zeros((H, W, 4), np.uint8)
        for _ in range(3):
            clock.t += 1.05 / FPS
            rec.write(frame)
        deadline = time.monotonic() + 2.0
        while rec.written < 3 and time.monotonic() < deadline:
            time.sleep(0.005)
        if rec.stopped_elsewhere():
            failures.append(f"{label}: stopped_elsewhere() True while healthy")
        clock.t += 1.05 / FPS
        rec.write(frame)                       # the frame the encoder fails on
        failed_at = time.monotonic()
        noticed = None
        while time.monotonic() - failed_at < 2.0:
            if rec.stopped_elsewhere():
                noticed = time.monotonic() - failed_at
                break
            time.sleep(0.005)
        # What the main loop does on stopped_elsewhere(), and the finalizer
        # poll that follows it.
        st = state()
        st.recorder = rec
        if noticed is not None:
            commands.begin_recording_finalization(st)
        rec.wait(5.0)
        commands.poll_recording_finalizer(st)
    finally:
        recorder.time, recorder.av.open = real_time, real_open
        FakeContainer.close_error = None
    return rec, noticed, st


def main() -> int:
    failures: list[str] = []
    base = Path(tempfile.gettempdir())

    out = base / "ns-test-encoder-failure.mp4"
    rec, noticed, st = failing_recording(out, failures, "clean close")
    print(f"clean close: noticed after "
          f"{'never' if noticed is None else f'{noticed * 1000:.0f} ms'}, "
          f"result {rec.result}, cut short {getattr(rec, 'cut_short', None)}, "
          f"alerts {st.alerts}")
    if noticed is None or noticed > 0.2:
        failures.append("the main loop was not told the encoder failed "
                        "within 0.2 s")
    result = rec.result
    if result is None or result.status is not recorder.RecordingStatus.PUBLISHED:
        failures.append(f"a cleanly closed file after an encoder failure was "
                        f"not published: {result}")
    else:
        if result.path != str(out) or not out.is_file():
            failures.append(f"published at {result.path}, file there: "
                            f"{out.is_file()}")
        if not getattr(rec, "cut_short", False):
            failures.append("the published file is not marked cut short")
        if not any("cut short" in a for a in st.alerts):
            failures.append(f"the user was not told it was cut short: {st.alerts}")
    out.unlink(missing_ok=True)
    Path(f"{out}.partial").unlink(missing_ok=True)

    out = base / "ns-test-encoder-failure-close.mp4"
    rec, noticed, st = failing_recording(out, failures, "failed close",
                                         OSError(28, "No space left on device"))
    print(f"failed close: result {rec.result}, alerts {st.alerts}")
    result = rec.result
    if noticed is None:
        failures.append("failed close: the encoder failure was not noticed")
    if result is None or result.status is not recorder.RecordingStatus.FAILED:
        failures.append(f"a file that did not close was published: {result}")
    elif getattr(result.error, "stage", None) != "encode" or out.exists():
        failures.append(f"failed close: stage {getattr(result.error, 'stage', None)}"
                        f", final file exists {out.exists()}")
    if not any("encode" in a for a in st.alerts):
        failures.append(f"failed close: no failure alert: {st.alerts}")
    out.unlink(missing_ok=True)
    Path(f"{out}.partial").unlink(missing_ok=True)

    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: a failed CPU encoder is noticed at once and reported, "
          "its clean file published as cut short")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
