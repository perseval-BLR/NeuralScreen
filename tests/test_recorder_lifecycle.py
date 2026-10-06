"""Recorder publication lifecycle: staging, drain, verify, and exact result.

Uses a deterministic in-memory-ish encoder/container stand-in so failure and
timeout paths do not depend on NVENC. The existing recorder tests still cover
real PyAV/NVENC output and read-back.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import recorder  # noqa: E402


class FakeBackend:
    def __init__(self, *, gated: bool = False,
                 close_error: BaseException | None = None,
                 verify_error: BaseException | None = None):
        self.gate = threading.Event()
        if not gated:
            self.gate.set()
        self.encode_started = threading.Event()
        self.close_error = close_error
        self.verify_error = verify_error
        self.opened_path: Path | None = None


class FakeStream:
    def __init__(self, backend: FakeBackend):
        self.backend = backend

    def encode(self, frame):
        if frame is None:
            return []
        self.backend.encode_started.set()
        self.backend.gate.wait()
        return [object()]


class FakeContainer:
    def __init__(self, path: str, backend: FakeBackend):
        self.path = Path(path)
        self.backend = backend
        self.backend.opened_path = self.path
        self.path.write_bytes(b"partial-mp4\n")

    def add_stream(self, _name, rate=None):
        return FakeStream(self.backend)

    def mux(self, _packet) -> None:
        with self.path.open("ab") as output:
            output.write(b"packet\n")

    def close(self) -> None:
        if self.backend.close_error is not None:
            raise self.backend.close_error
        with self.path.open("ab") as output:
            output.write(b"closed\n")


class HarnessRecorder(recorder.VideoRecorder):
    QUEUE_DEPTH = 8

    def __init__(self, path: str, backend: FakeBackend):
        self._test_backend = backend
        super().__init__(path, 8, 4, fps=30.0, audio=False)

    def _open_video_stream(self, width: int, height: int, fps: float):
        self.codec = "fake"
        return self._container.add_stream("fake", rate=int(round(fps)))

    def _verify_partial(self) -> None:
        if self._test_backend.verify_error is not None:
            raise self._test_backend.verify_error
        partial = Path(self.partial_path)
        if self.written <= 0:
            raise RuntimeError("recording contains no video frames")
        if not partial.is_file() or partial.stat().st_size <= 0:
            raise RuntimeError("partial MP4 is missing or empty")


def make_recorder(path: Path, backend: FakeBackend) -> HarnessRecorder:
    real_open = recorder.av.open
    recorder.av.open = lambda file, mode=None, format=None, **kwargs: (  # noqa: ARG005
        FakeContainer(str(file), backend)
    )
    try:
        return HarnessRecorder(str(path), backend)
    finally:
        recorder.av.open = real_open


def frame(value: int = 0) -> np.ndarray:
    pixels = np.zeros((4, 8, 4), dtype=np.uint8)
    pixels[..., 0] = value
    pixels[..., 3] = 255
    return pixels


def next_slot(rec: HarnessRecorder) -> None:
    """Let one stream period pass before a write.

    write() keeps one frame per slot of the recording clock and drops a frame
    whose slot is already taken (slot 0 is closed at construction), so frames
    written back to back would not all reach the encoder.
    """
    time.sleep(1.05 / rec.fps)


def cleanup(path: Path) -> None:
    path.unlink(missing_ok=True)
    Path(f"{path}.partial").unlink(missing_ok=True)


def expect_failure(rec: HarnessRecorder, stage: str,
                   failures: list[str], timeout: float = 2.0) -> None:
    try:
        rec.close(timeout=timeout)
    except recorder.RecordingError as exc:
        if exc.stage != stage:
            failures.append(f"expected {stage} failure, got {exc.stage}: {exc}")
    else:
        failures.append(f"{stage} failure did not make close() raise")


def test_nonblocking_drain(out: Path, failures: list[str]) -> None:
    cleanup(out)
    backend = FakeBackend(gated=True)
    rec = make_recorder(out, backend)
    partial = Path(rec.partial_path)
    try:
        for i in range(5):
            next_slot(rec)
            rec.write(frame(i))
        if not backend.encode_started.wait(1.0):
            failures.append("encoder did not start")
            backend.gate.set()
            return
        if backend.opened_path != partial or not partial.is_file():
            failures.append("recorder did not open the .partial path")
        if out.exists():
            failures.append("final MP4 exists before publication")

        started = time.perf_counter()
        changed = rec.finish()
        elapsed = time.perf_counter() - started
        if not changed:
            failures.append("first finish() did not start finalization")
        if elapsed > 0.1:
            failures.append(f"finish() blocked for {elapsed:.3f} s")
        if rec.status is not recorder.RecordingStatus.FINALIZING:
            failures.append(f"status after finish() is {rec.status}")
        if rec.wait(0) is not None:
            failures.append("wait(0) completed while encoder was gated")
        if out.exists():
            failures.append("final MP4 was published before queue drain")

        backend.gate.set()
        result = rec.wait(2.0)
        if result is None:
            failures.append("drained recording did not finish")
            return
        if result.status is not recorder.RecordingStatus.PUBLISHED:
            failures.append(f"drained recording ended as {result.status}")
        if result.path != str(out) or result.error is not None:
            failures.append(f"wrong success result: {result}")
        if rec.written != 5:
            failures.append(f"queue was not drained: wrote {rec.written} of 5")
        if partial.exists() or not out.is_file():
            failures.append("atomic publication did not replace .partial")
        if rec.close() is not result:
            failures.append("idempotent close() did not return the same result")
    finally:
        backend.gate.set()
        cleanup(out)


def test_close_failure(out: Path, failures: list[str]) -> None:
    cleanup(out)
    backend = FakeBackend(close_error=OSError("trailer write failed"))
    rec = make_recorder(out, backend)
    try:
        next_slot(rec)
        rec.write(frame())
        expect_failure(rec, "close", failures)
        if rec.status is not recorder.RecordingStatus.FAILED:
            failures.append(f"close failure status is {rec.status}")
        if rec.result is None or rec.result.path != rec.partial_path:
            failures.append(f"close failure lost partial path: {rec.result}")
        if out.exists() or not Path(rec.partial_path).is_file():
            failures.append("close failure published final MP4 or lost .partial")
    finally:
        cleanup(out)


def test_verify_failure(out: Path, failures: list[str]) -> None:
    cleanup(out)
    backend = FakeBackend(verify_error=ValueError("bad moov"))
    rec = make_recorder(out, backend)
    try:
        next_slot(rec)
        rec.write(frame())
        expect_failure(rec, "verify", failures)
        if rec.result is None or rec.result.error is not rec.error:
            failures.append("verification error was not preserved in result")
        if out.exists() or not Path(rec.partial_path).is_file():
            failures.append("unverified MP4 was published or .partial was lost")
    finally:
        cleanup(out)


def test_timeout_forbids_late_publish(out: Path, failures: list[str]) -> None:
    cleanup(out)
    backend = FakeBackend(gated=True)
    rec = make_recorder(out, backend)
    next_slot(rec)
    rec.write(frame())
    if not backend.encode_started.wait(1.0):
        failures.append("timeout test encoder did not enter encode()")
        backend.gate.set()
        cleanup(out)
        return
    worker = rec._thread
    started = time.perf_counter()
    expect_failure(rec, "timeout", failures, timeout=0.02)
    elapsed = time.perf_counter() - started
    try:
        if elapsed > 0.5:
            failures.append(f"timed close blocked for {elapsed:.3f} s")
        timed_out_result = rec.result
        if timed_out_result is None or timed_out_result.path != rec.partial_path:
            failures.append(f"timeout lost exact result path: {timed_out_result}")
        backend.gate.set()
        if worker is not None:
            worker.join(2.0)
            if worker.is_alive():
                failures.append("worker did not exit after test gate opened")
        if rec.result is not timed_out_result:
            failures.append("late worker completion replaced timeout result")
        if out.exists() or not Path(rec.partial_path).is_file():
            failures.append("worker published after terminal timeout")
    finally:
        backend.gate.set()
        cleanup(out)


def main() -> int:
    failures: list[str] = []
    base = Path(tempfile.gettempdir())
    test_nonblocking_drain(base / "ns-test-recorder-lifecycle.mp4", failures)
    test_close_failure(base / "ns-test-recorder-close-fail.mp4", failures)
    test_verify_failure(base / "ns-test-recorder-verify-fail.mp4", failures)
    test_timeout_forbids_late_publish(
        base / "ns-test-recorder-timeout.mp4", failures)
    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: .partial staging, queue drain, verified atomic publish, exact failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
