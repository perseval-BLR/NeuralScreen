"""A CPU recording plays at real speed when pixels come back on every frame.

main.py asks for pixels only when needs_frame() says a slot is open, but it
writes whatever pixels come back - and without a present window (the pygame
fallback) they come back on every frame. Those extra frames reached write()
without a reservation and were handed the next counter value: a 60 FPS
pipeline put two frames into each 30 fps slot, and three seconds of screen
became a six-second file in slow motion.

The recorder runs on a fake container and a fake clock, so the test needs no
NVENC and no sleeping: the pts the encoder receives ARE the file's timeline.
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import recorder  # noqa: E402

W, H = 8, 4
FPS = 30.0
PIPELINE_HZ = 60.0
SECONDS = 3.0


class Clock:
    def __init__(self):
        self.t = 100.0

    def perf_counter(self) -> float:
        return self.t


class FakeStream:
    def __init__(self, pts: list):
        self.pts = pts

    def encode(self, frame):
        if frame is None:
            return []
        self.pts.append(int(frame.pts))
        return [object()]


class FakeContainer:
    def __init__(self, path: str, pts: list):
        self.path = Path(path)
        self.pts = pts
        self.path.write_bytes(b"partial\n")

    def add_stream(self, _name, rate=None):
        return FakeStream(self.pts)

    def mux(self, _packet) -> None:
        pass

    def close(self) -> None:
        pass


class Harness(recorder.VideoRecorder):
    def _open_video_stream(self, width, height, fps):
        self.codec = "fake"
        return self._container.add_stream("fake", rate=int(round(fps)))

    def _verify_partial(self) -> None:
        if self.written <= 0:
            raise RuntimeError("recording contains no video frames")


def record(out: Path, ask_first: bool) -> tuple[list, Harness, float]:
    """SECONDS of a PIPELINE_HZ main loop that writes pixels on every frame."""
    pts: list = []
    clock = Clock()
    real_time, real_open = recorder.time, recorder.av.open
    recorder.time = types.SimpleNamespace(perf_counter=clock.perf_counter)
    recorder.av.open = lambda file, mode=None, format=None, **kw: (  # noqa: ARG005
        FakeContainer(str(file), pts))
    try:
        rec = Harness(str(out), W, H, fps=FPS, audio=False)
        frame = np.zeros((H, W, 4), dtype=np.uint8)
        start = clock.t
        while clock.t - start < SECONDS:
            if ask_first:
                rec.needs_frame()          # gates want_pixels in main.py ...
            rec.write(frame)               # ... but pixels come back anyway
            clock.t += 1.0 / PIPELINE_HZ
        wall = clock.t - start
        result = rec.close(timeout=10.0)
    finally:
        recorder.time, recorder.av.open = real_time, real_open
    if result.status is not recorder.RecordingStatus.PUBLISHED:
        raise RuntimeError(f"recording ended as {result.status}")
    return pts, rec, wall


def main() -> int:
    failures: list[str] = []
    out = Path(tempfile.gettempdir()) / "ns-test-recorder-unreserved.mp4"
    for ask_first, label in ((True, "needs_frame()+write()"),
                             (False, "write() alone")):
        out.unlink(missing_ok=True)
        Path(f"{out}.partial").unlink(missing_ok=True)
        pts, rec, wall = record(out, ask_first)
        duration = (max(pts) + 1) / FPS if pts else 0.0
        print(f"{label}: {len(pts)} frames, last pts {max(pts, default=-1)}, "
              f"file {duration:.2f} s for {wall:.2f} s of wall time, "
              f"skipped {getattr(rec, 'skipped', '-')}")
        if abs(duration - wall) > 0.10 * wall:
            failures.append(f"{label}: the file lasts {duration:.2f} s for "
                            f"{wall:.2f} s of screen")
        if pts != sorted(set(pts)):
            failures.append(f"{label}: pts are not strictly increasing")
        if len(pts) > wall * FPS + 1:
            failures.append(f"{label}: {len(pts)} frames in a {FPS:g} fps "
                            f"stream of {wall:.2f} s")
        out.unlink(missing_ok=True)
        Path(f"{out}.partial").unlink(missing_ok=True)
    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: frames written on every pipeline frame keep the file at real speed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
