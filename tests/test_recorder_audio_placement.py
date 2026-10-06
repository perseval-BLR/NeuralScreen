"""The first sound after a quiet stretch lands at its own time in both recorders.

WASAPI loopback hands out nothing while nothing plays, so both recorders pad
quiet stretches with silence - but only up to AUDIO_LAG_S short of the clock,
to leave room for late packets. The first real packet after the quiet then
went where the padding stopped: a click heard at 1.000 s was written at
0.80-0.90 s, the sound 100-200 ms ahead of the picture. The endpoint stamps
every packet with the performance counter (pu64QPCPosition, 100 ns units);
a packet that starts a run is now placed by that stamp.

Both recorders are driven on a fake clock with a fake loopback: 1 s of
nothing, then a click with a known stamp delivered 10 ms later, then a packet
that continues it (with 2 ms of stamp jitter, which must NOT cut a hole into
continuous sound), then a packet after lost data (the discontinuity flag).
No NVENC, no worker, no playback device.
"""
from __future__ import annotations

import ctypes
import sys
import tempfile
import threading
import types
from pathlib import Path

import av
import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import recorder  # noqa: E402
from protocol import REC_CODEC_AUTO  # noqa: E402

RATE = 48_000
QPF = 10_000_000          # the performance counter at 10 MHz, as on Windows
START = 100.0             # the clock when the recording starts, in seconds
CLICK_AT = 1.0            # seconds into the recording
STEP = 0.02               # how often the recorder pumps
PACKET = 480              # 10 ms device packets
TOLERANCE_MS = 20.0


class Clock:
    def __init__(self):
        self.t = START

    def perf_counter(self) -> float:
        return self.t

    def qpc(self) -> int:
        return int(round(self.t * QPF))


def stamp(seconds: float) -> int:
    """A packet's pu64QPCPosition for a moment `seconds` into the recording."""
    return int(round((START + seconds) * 1e7))


class FakeLoopback:
    sample_rate = RATE
    error = None

    def __init__(self):
        self.pending: list = []

    def start(self) -> bool:
        return True

    def discard(self) -> None:
        self.pending.clear()

    def read(self):
        blocks = [p[1] for p in self.read_packets()]
        return np.concatenate(blocks) if blocks else None

    def read_packets(self):
        out, self.pending = self.pending, []
        return out

    def close(self) -> None:
        pass


def click(level: float) -> np.ndarray:
    block = np.zeros((PACKET, 2), np.float32)
    block[:48] = level        # 1 ms of signal at the very start of the packet
    return block


def scenario(clock: Clock, cap: FakeLoopback, pump) -> list:
    """Drive one recorder through the timeline; return the expected samples."""
    while clock.t + STEP <= START + CLICK_AT + 1e-9:
        clock.t += STEP
        pump()                                  # the quiet: nothing arrives
    expected = []
    # The click, heard at CLICK_AT and handed over a packet later.
    clock.t = START + CLICK_AT + 0.010
    cap.pending.append((stamp(CLICK_AT), click(0.8), False))
    expected.append(int(CLICK_AT * RATE))
    # The packet that continues it, stamped 2 ms late: still the same run.
    cap.pending.append((stamp(CLICK_AT + 0.010 + 0.002), click(0.6), False))
    expected.append(int(CLICK_AT * RATE) + PACKET)
    pump()
    # Data lost after it: the next packet says so, 4 ms past where the run
    # would continue - inside the join window, so only the flag places it.
    lost_at = CLICK_AT + 0.020 + 0.004
    clock.t = START + lost_at + 0.010
    cap.pending.append((stamp(lost_at), click(0.4), True))
    expected.append(int(round(lost_at * RATE)))
    pump()
    for _ in range(10):                         # quiet again, to the end
        clock.t += STEP
        pump()
    return expected


def find_clicks(samples: np.ndarray) -> list:
    """Sample index where each click starts (rising edges above 0.2)."""
    loud = np.abs(samples) > 0.2
    edges = np.nonzero(loud & ~np.concatenate(([False], loud[:-1])))[0]
    return [int(e) for e in edges]


def check(label: str, expected: list, found: list, failures: list) -> None:
    print(f"{label}: clicks expected at {expected}, found at {found}")
    if len(found) != len(expected):
        failures.append(f"{label}: {len(found)} clicks in the track, "
                        f"{len(expected)} expected")
        return
    for i, (want, got) in enumerate(zip(expected, found)):
        off_ms = (got - want) * 1000.0 / RATE
        # The packet after lost data is only 4 ms off if the flag is ignored.
        if abs(off_ms) > (TOLERANCE_MS if i < 2 else 1.0):
            failures.append(f"{label}: click {i} is {off_ms:+.0f} ms from its "
                            f"time (the sound {'leads' if off_ms < 0 else 'trails'})")
    # The continuing packet must follow the click directly: no silence was
    # cut into a continuous run for 2 ms of stamp jitter.
    if found[1] - found[0] != PACKET:
        failures.append(f"{label}: {found[1] - found[0] - PACKET} samples of "
                        f"silence were cut into continuous sound")


# -- VideoRecorder ----------------------------------------------------------

class FakeAudioStream:
    def __init__(self, sink: list):
        self.sink = sink
        self.codec_context = types.SimpleNamespace(frame_size=1024)
        self.rate = RATE

    def encode(self, frame):
        if frame is not None:
            self.sink.append(frame.to_ndarray()[0].copy())
        return []


class FakeVideoStream:
    def encode(self, frame):
        return []


class FakeContainer:
    def __init__(self, path: str, sink: list):
        Path(path).write_bytes(b"partial\n")
        self.sink = sink

    def add_stream(self, name, rate=None):
        return FakeAudioStream(self.sink) if name == "aac" else FakeVideoStream()

    def mux(self, _packet) -> None:
        pass

    def close(self) -> None:
        pass


class CpuHarness(recorder.VideoRecorder):
    def _open_video_stream(self, width, height, fps):
        self.codec = "fake"
        return self._container.add_stream("fake", rate=int(round(fps)))


def run_cpu(failures: list) -> None:
    clock = Clock()
    cap = FakeLoopback()
    sink: list = []
    out = Path(tempfile.gettempdir()) / "ns-test-audio-placement.mp4"
    real = (recorder.time, recorder.av.open, recorder.LoopbackCapture)
    real_open = av.open

    def fake_open(file, mode=None, format=None, **kw):
        if format == "null":                    # the AAC probe stays real
            return real_open(file, mode=mode, format=format, **kw)
        return FakeContainer(str(file), sink)

    recorder.time = types.SimpleNamespace(perf_counter=clock.perf_counter)
    recorder.av.open = fake_open
    recorder.LoopbackCapture = lambda: cap
    try:
        rec = CpuHarness(str(out), 8, 4, fps=30.0, audio=True)
        if rec._audio is not cap:
            failures.append("CPU: the fake loopback was not taken")
            return
        expected = scenario(clock, cap, rec._pump_audio)
        rec._close_audio()
    finally:
        recorder.time, recorder.av.open, recorder.LoopbackCapture = real
        Path(f"{out}.partial").unlink(missing_ok=True)
    track = np.concatenate(sink) if sink else np.zeros(0, np.float32)
    check("CPU recorder", expected, find_clicks(track), failures)


# -- GpuRecorder ------------------------------------------------------------

class FakeReader:
    alive = True

    def __init__(self):
        self.rec_started = threading.Event()
        self.rec_done = threading.Event()
        self.rec_start_reply = None
        self.rec_done_reply = None


class GpuHarness(recorder.GpuRecorder):
    AUDIO_PUMP_S = 3600.0     # the test pumps, not the thread


def run_gpu(failures: list) -> None:
    clock = Clock()
    cap = FakeLoopback()
    reader = FakeReader()
    real = (recorder._qpc, recorder._qpf, recorder.LoopbackCapture,
            recorder.send_rec_start)

    def fake_start(worker, path, **kw):
        reader.rec_start_reply = types.SimpleNamespace(
            ok=True, codec=REC_CODEC_AUTO, hresult=0, width=8, height=4,
            fps=60, audio=True)
        reader.rec_started.set()

    recorder._qpc = clock.qpc
    recorder._qpf = lambda: QPF
    recorder.LoopbackCapture = lambda: cap
    recorder.send_rec_start = fake_start
    rec = None
    try:
        rec = GpuHarness(object(), reader, str(Path(tempfile.gettempdir())
                                               / "ns-test-gpu-placement.mp4"))
        if rec._ring is None:
            failures.append("GPU: the audio ring was not set up")
            return
        expected = scenario(clock, cap, rec._pump_audio)
        ring = rec._ring
        track = ring._samples[:ring.written, 0].astype(np.float32) / 32768.0
    finally:
        if rec is not None:
            rec._close_audio()
        recorder._qpc, recorder._qpf, recorder.LoopbackCapture, \
            recorder.send_rec_start = real
    check("GPU recorder", expected, find_clicks(track), failures)


# -- LoopbackCapture --------------------------------------------------------

class FakeCaptureClient:
    """IAudioCaptureClient stand-in: hands out the packets, then stops."""

    def __init__(self, cap, packets):
        self.cap = cap
        self.packets = list(packets)
        self.buffers = []

    def GetNextPacketSize(self):
        if not self.packets:
            self.cap._stop.set()
            return 0
        return len(self.packets[0][0])

    def GetBuffer(self):
        block, flags, qpc = self.packets.pop(0)
        buf = ctypes.create_string_buffer(block.tobytes())
        self.buffers.append(buf)
        return ctypes.addressof(buf), len(block), flags, 0, qpc

    def ReleaseBuffer(self, _frames):
        pass


def run_capture(failures: list) -> None:
    from audio import LoopbackCapture
    cap = LoopbackCapture()
    block = np.full((PACKET, 2), 0.25, np.float32)
    client = FakeCaptureClient(cap, [
        (block, 0, stamp(0.5)),
        (block, 0x1, stamp(0.6)),           # DATA_DISCONTINUITY
        (block, 0x4, stamp(0.7)),           # TIMESTAMP_ERROR: no usable time
    ])
    fmt = {"dtype": np.float32, "scale": 1.0, "src_channels": 2,
           "rate": RATE, "block": 8}
    cap._pump(client, fmt)
    got = [(qpc, len(b), disc) for qpc, b, disc in cap.read_packets()]
    want = [(stamp(0.5), PACKET, False), (stamp(0.6), PACKET, True),
            (0, PACKET, False)]
    print(f"loopback packets: {got}")
    if got != want:
        failures.append(f"loopback packets carry {got}, expected {want}")


def main() -> int:
    failures: list = []
    run_capture(failures)
    run_cpu(failures)
    run_gpu(failures)
    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: the first sound after a quiet stretch lands at its own time")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
