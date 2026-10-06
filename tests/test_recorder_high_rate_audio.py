"""A 192 kHz WASAPI endpoint must not abort the MP4 recorder.

Issue #86 supplied a precise failure: the endpoint runs at 192 kHz, while
AAC refuses that rate. The former code added the AAC stream anyway, so its
lazy avcodec_open2 failure stopped the video encoder and left no usable file.

This uses a fake loopback at that exact rate, but the real PyAV muxer and
NVENC chain. The output must contain a video track and a resampled 48 kHz AAC
track; if AAC cannot be opened, VideoRecorder must choose video-only instead
of letting it poison the container.

Run: runtime\\python.exe tests\\test_recorder_high_rate_audio.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import av
import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import recorder  # noqa: E402


class _HighRateLoopback:
    """A deterministic 192 kHz stereo endpoint, with one short packet."""

    sample_rate = 192_000
    error = None

    def __init__(self):
        self._gave_packet = False

    def start(self) -> bool:
        return True

    def discard(self) -> None:
        pass

    def read(self):
        if self._gave_packet:
            return None
        self._gave_packet = True
        # 100 ms of a quiet non-zero signal: enough to exercise resampling
        # without inventing a real WASAPI device for the test.
        return np.full((19_200, 2), 0.1, dtype=np.float32)

    def read_packets(self):
        # The same packet, without a time (qpc 0): appended where the track
        # stands, as the recorders do for an endpoint that gives none.
        block = self.read()
        return [] if block is None else [(0, block, False)]

    def close(self) -> None:
        pass


def main() -> int:
    failures = []
    out = Path(tempfile.gettempdir()) / "ns-test-recorder-192khz.mp4"
    out.unlink(missing_ok=True)
    original_loopback = recorder.LoopbackCapture
    rec = None
    recorder.LoopbackCapture = _HighRateLoopback
    try:
        rec = recorder.VideoRecorder(str(out), 320, 180, fps=30.0, audio=True)
        if rec._astream is None:
            failures.append("192 kHz loopback disabled audio instead of creating a valid AAC stream")
        elif rec._astream.rate != recorder.VideoRecorder.AAC_SAMPLE_RATE:
            failures.append(f"AAC stream rate is {rec._astream.rate}, expected "
                            f"{recorder.VideoRecorder.AAC_SAMPLE_RATE}")
        frame = np.zeros((180, 320, 4), dtype=np.uint8)
        frame[:, :, 0] = 55
        frame[:, :, 1] = 110
        frame[:, :, 3] = 255
        for _ in range(10):
            rec.write(frame)
            time.sleep(1.0 / 30.0)
        # Give the background encoder a chance to consume its queue before
        # close() posts its bounded stop signal.
        time.sleep(0.25)
        rec.close()
        if rec._encode_error is not None:
            failures.append(f"the encoder died after resampling: {rec._encode_error}")
        if rec.written <= 0:
            failures.append("the 192 kHz recording encoded no video frames")
        if not out.is_file() or out.stat().st_size == 0:
            failures.append("the 192 kHz recording produced no MP4")
        else:
            with av.open(str(out)) as container:
                if not container.streams.video:
                    failures.append("the MP4 has no video stream")
                if not container.streams.audio:
                    failures.append("the MP4 has no audio stream")
                else:
                    stream = container.streams.audio[0]
                    print(f"audio: {stream.codec_context.name}, "
                          f"{stream.codec_context.sample_rate} Hz")
                    if stream.codec_context.name != "aac":
                        failures.append(f"audio codec is {stream.codec_context.name}, not AAC")
                    if stream.codec_context.sample_rate != recorder.VideoRecorder.AAC_SAMPLE_RATE:
                        failures.append("MP4 audio stream was not resampled to "
                                        f"{recorder.VideoRecorder.AAC_SAMPLE_RATE} Hz")
            print(f"video frames written: {rec.written}, file: {out.stat().st_size} bytes")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"high-rate recording raised {exc!r}")
    finally:
        try:
            if rec is not None and rec._container is not None:
                rec.close()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"cleanup close failed: {exc!r}")
        recorder.LoopbackCapture = original_loopback
        out.unlink(missing_ok=True)

    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: 192 kHz loopback becomes valid 48 kHz AAC; video keeps recording")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
