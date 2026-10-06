"""GPU recording loose ends: a late start, and a recording the worker ended.

1. The client waits START_TIMEOUT_S for the worker's RSAK, then sends RECE and
   records on the CPU instead. A worker that was only slow still opened
   `<name>.mp4.gpu.partial` and closed it on that RECE - a file nobody
   published, reported or removed. It is removed now once the worker has
   closed it.
2. A recording the worker closed by itself - an unasked REAK, which the
   worker sends with ok=1 when, say, an HDR session leaves HDR - was published
   as "Recording saved", although it ends before the user stopped it. It is
   "cut short" now, as a worker that died (a restart mid-recording) already
   was.

Fakes stand in for the worker, the reader and the file check: no worker, no
GPU, no sound device.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import commands  # noqa: E402
import recorder  # noqa: E402
from protocol import REC_CODEC_AUTO  # noqa: E402


class FakeReader:
    def __init__(self):
        self.alive = True
        self.rec_started = threading.Event()
        self.rec_done = threading.Event()
        self.rec_start_reply = None
        self.rec_done_reply = None


def start_reply():
    return types.SimpleNamespace(ok=True, codec=REC_CODEC_AUTO, hresult=0,
                                 width=8, height=4, fps=60, audio=False)


def done_reply(ok=True, written=120):
    return types.SimpleNamespace(ok=ok, written=written, dropped=0, hresult=0)


class Harness(recorder.GpuRecorder):
    START_TIMEOUT_S = 0.2

    def _verify_partial(self, count: bool = False) -> int:
        if not Path(self.partial_path).is_file():
            raise RuntimeError("the recording file is missing")
        return 100 if count else 0


def patched(start, stop):
    real = (recorder.send_rec_start, recorder.send_rec_stop)
    recorder.send_rec_start, recorder.send_rec_stop = start, stop
    return real


def restore(real) -> None:
    recorder.send_rec_start, recorder.send_rec_stop = real


def test_late_start(folder: Path, failures: list) -> None:
    path = folder / "late.mp4"
    partial = Path(f"{path}.gpu.partial")
    reader = FakeReader()

    def late_worker() -> None:
        # The RECS is answered after the client gave up; the RECE queued
        # behind it closes the file at once.
        time.sleep(0.3)
        partial.write_bytes(b"a few frames\n")
        reader.rec_start_reply = start_reply()
        reader.rec_started.set()
        time.sleep(0.1)
        reader.rec_done_reply = done_reply(written=3)
        reader.rec_done.set()

    real = patched(lambda *a, **k: threading.Thread(target=late_worker,
                                                    daemon=True).start(),
                   lambda *a, **k: None)
    try:
        try:
            Harness(object(), reader, str(path), audio=False)
            failures.append("late start: the start did not time out")
            return
        except recorder.RecordingError as exc:
            if exc.stage != "start":
                failures.append(f"late start: failed at {exc.stage}")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if reader.rec_done.is_set() and not partial.exists():
                break
            time.sleep(0.05)
        print(f"late start: worker answered {reader.rec_started.is_set()}, "
              f"closed {reader.rec_done.is_set()}, file left: {partial.exists()}")
        if not reader.rec_done.is_set():
            failures.append("late start: the fake worker never closed the file")
        elif partial.exists():
            failures.append(f"late start: {partial.name} was left on the disk")
    finally:
        restore(real)
        partial.unlink(missing_ok=True)


def state():
    alerts: list[str] = []
    return types.SimpleNamespace(
        recorder=None, recording_finalizer=None,
        recording_finalize_deadline=0.0, last_recording={}, lang="en",
        display=types.SimpleNamespace(
            alert=lambda message, *args: alerts.append(str(message))),
        alerts=alerts)


def ended_by_worker(folder: Path, failures: list, label: str,
                    reply, alive: bool) -> None:
    path = folder / f"{label.replace(' ', '_')}.mp4"
    reader = FakeReader()
    stops = []

    def start(*_a, **_k):
        Path(f"{path}.gpu.partial").write_bytes(b"fragments\n")
        reader.rec_start_reply = start_reply()
        reader.rec_started.set()

    real = patched(start, lambda *a, **k: stops.append(1))
    try:
        rec = Harness(object(), reader, str(path), audio=False)
        st = state()
        st.recorder = rec
        if rec.stopped_elsewhere():
            failures.append(f"{label}: stopped_elsewhere() before anything happened")
        # The worker ends the recording by itself.
        reader.rec_done_reply = reply
        reader.alive = alive
        reader.rec_done.set()
        if not rec.stopped_elsewhere():
            failures.append(f"{label}: stopped_elsewhere() did not notice")
            return
        commands.begin_recording_finalization(st)   # what the main loop does
        if rec.wait(5.0) is None:
            failures.append(f"{label}: the recording did not finalize")
            return
        commands.poll_recording_finalizer(st)
        print(f"{label}: {rec.result.status.value}, cut short {rec.cut_short}, "
              f"RECE sent {len(stops)}, alerts {st.alerts}")
        if rec.result.status is not recorder.RecordingStatus.PUBLISHED:
            failures.append(f"{label}: {rec.result}")
        if not rec.cut_short or not any("cut short" in a for a in st.alerts):
            failures.append(f"{label}: reported as a whole recording: {st.alerts}")
    finally:
        restore(real)
        path.unlink(missing_ok=True)
        Path(f"{path}.gpu.partial").unlink(missing_ok=True)


def test_user_stop_is_whole(folder: Path, failures: list) -> None:
    """The control: a recording the user stopped is not cut short."""
    path = folder / "user_stop.mp4"
    reader = FakeReader()

    def start(*_a, **_k):
        Path(f"{path}.gpu.partial").write_bytes(b"fragments\n")
        reader.rec_start_reply = start_reply()
        reader.rec_started.set()

    def stop(*_a, **_k):
        reader.rec_done_reply = done_reply()
        reader.rec_done.set()

    real = patched(start, stop)
    try:
        rec = Harness(object(), reader, str(path), audio=False)
        result = rec.close(timeout=5.0)
        print(f"user stop: {result.status.value}, cut short {rec.cut_short}")
        if result.status is not recorder.RecordingStatus.PUBLISHED or rec.cut_short:
            failures.append(f"user stop: {result}, cut short {rec.cut_short}")
    finally:
        restore(real)
        path.unlink(missing_ok=True)
        Path(f"{path}.gpu.partial").unlink(missing_ok=True)


def main() -> int:
    failures: list[str] = []
    folder = Path(tempfile.mkdtemp(prefix="ns-grec-ends-"))
    test_late_start(folder, failures)
    ended_by_worker(folder, failures, "unasked clean REAK", done_reply(), True)
    ended_by_worker(folder, failures, "worker restarted", None, False)
    test_user_stop_is_whole(folder, failures)
    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: a late GPU start leaves no file; a recording the worker ended "
          "is reported cut short")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
