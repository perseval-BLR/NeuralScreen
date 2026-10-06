"""VideoRecorder - writes NR overlay frames into an MP4 (NVENC + AAC).

Records the frames Python receives from the worker (output_rgba) while
recording is on (Num0). Frames arrive full-res RGBA8 every ~30 ms; PyAV
converts them to yuv420p and encodes them through NVENC (AV1, or HEVC/H.264
on GPUs without an AV1 encoder - the first codec that opens wins).

System audio comes from WASAPI loopback (audio.LoopbackCapture) as a second
track. It is best-effort: a machine without a playback endpoint still records
video, it just gets no sound.

Recording does not depend on ShadowPlay/OBS: the overlay is excluded from
external capture (WDA_EXCLUDEFROMCAPTURE), so the video is written from the
inside - exactly the NR result that is on screen.
"""

from __future__ import annotations

import ctypes
import mmap
import os
import queue
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from audio import LoopbackCapture
from protocol import (AUDIO_RING_FMT, AUDIO_RING_MAGIC, REC_CODEC_AUTO,
                      REC_CODEC_HDR10, REC_CODEC_NAMES, send_rec_start,
                      send_rec_stop)


class RecordingStatus(str, Enum):
    """Observable lifecycle states for a recording."""

    RECORDING = "recording"
    FINALIZING = "finalizing"
    PUBLISHED = "published"
    FAILED = "failed"


class RecordingError(RuntimeError):
    """A fatal recorder error with the lifecycle stage that produced it."""

    def __init__(self, stage: str, cause: BaseException):
        self.stage = stage
        self.cause = cause
        super().__init__(f"{stage}: {cause}")


@dataclass(frozen=True)
class RecordingResult:
    """Terminal outcome returned by wait()/close().

    ``path`` is the file that actually remains: the published MP4 on success,
    or the recoverable ``.partial`` file on failure (``None`` if no file was
    created). ``error`` is never discarded or converted to a log-only string.
    """

    status: RecordingStatus
    path: str | None
    error: BaseException | None


class VideoRecorder:
    """Writes frames into an MP4 (NVENC). Created when recording starts,
    closed on Num0/exit. write()/close() are called from the main loop only.

    Encoding runs in its own thread. A 4K measurement showed a synchronous
    write() cost 19.9 ms per frame - the RGBA->yuv420p conversion and the
    hand-off to nvenc, both on the CPU - and dropped the pipeline from 56 to
    21 FPS. Bitrate had nothing to do with it: the time went into the colour
    conversion, not the encoder.

    The frame is handed to the thread by reference, without a copy: the worker
    sends every frame in a fresh buffer (WorkerReader.recv -> np.frombuffer
    over new bytes) and the main loop never mutates it - it only reads it for
    display and screenshots.
    """

    #: main.py hands this recorder the pixels of every frame it wants (and
    #: bakes the open menu into them first); GpuRecorder never needs them.
    takes_pixels = True
    #: How many frames wait for the encoder. More means more memory (33 MB per
    #: frame at 4K), less means we start dropping frames earlier on spikes.
    QUEUE_DEPTH = 4
    #: How long write() waits for room before dropping a frame. Stalling the
    #: pipeline for the sake of the recording is not acceptable: the user
    #: looks at the screen, not at the file. A dropped frame does not affect
    #: timing - pts comes from the clock.
    #:
    #: ONE frame period, not the quarter second above. write() runs on the
    #: main loop, so this wait IS a freeze of the picture: 0.25 s is about 14
    #: frames at 55 FPS, a visible stutter spent protecting a file whose
    #: duration the clock-based pts keeps correct with or without that frame.
    #: The queue only fills when the encoder has stalled, and a stalled
    #: encoder is exactly when the screen must not be held hostage to it.
    FRAME_PUT_TIMEOUT_S = 1.0 / 60.0
    #: Bounded compatibility wait used by close(). New callers can use
    #: finish() + wait() and never block the UI thread.
    FINISH_TIMEOUT_S = 30.0

    #: NVENC codecs, best first. AV1 is the newest and most efficient, but the
    #: RTX 30 series has no AV1 encoder at all - on those cards the first
    #: add_stream() succeeds and the failure only surfaces when the encoder is
    #: opened. The probe below opens each codec for real and keeps the first
    #: one that works.
    CODEC_CHAIN = ("av1_nvenc", "hevc_nvenc", "h264_nvenc")

    #: Bitrate and encoder parameters live as class attributes so they can be
    #: changed without touching the constructor (measurements, experiments).
    BIT_RATE = 120_000_000
    ENCODER_OPTIONS = {
        "preset": "p6",     # p1 fast ... p7 high quality
        "tune": "hq",
        "rc": "vbr",        # not a fixed bitrate: on fast motion the encoder
                            # must be allowed to spend more
        "cq": "16",         # target quality; bitrate is a ceiling, not a goal
        "maxrate": "250M",
        "bufsize": "500M",
    }

    #: Audio bitrate. 192 kbit/s of AAC is transparent enough for game sound and
    #: speech, and next to a 120 Mbit/s video track its size does not matter.
    AUDIO_BIT_RATE = 192_000
    #: AAC cannot encode every rate that a Windows playback device accepts:
    #: 192 kHz loopback, for example, makes avcodec_open2 fail after the MP4
    #: already has its audio stream. Keep the file format predictable and
    #: resample every endpoint to the broadly supported AAC rate instead.
    AAC_SAMPLE_RATE = 48_000
    #: How far the audio track may fall behind the clock before we pad it with
    #: silence, and how much lag we leave after padding. WASAPI loopback hands
    #: back nothing at all while the device is idle, so without padding a quiet
    #: passage would shorten the track and pull everything after it out of sync.
    #: The remaining lag is deliberate: real samples that are merely late must
    #: not land after silence we already wrote for their slot.
    AUDIO_GAP_S = 0.20
    AUDIO_LAG_S = 0.10
    #: A loopback packet that starts within this of where the previous one
    #: ended continues it; one that starts later follows a gap.
    AUDIO_JOIN_S = 0.005

    def __init__(self, path: str, width: int, height: int, fps: float = 60.0,
                 audio: bool = True):
        # ``path`` remains the requested public destination for compatibility
        # with commands.py. Bytes are written next to it under a .partial name
        # and only published after close + read-back verification succeeds.
        self.path = str(Path(path))
        self.partial_path = f"{self.path}.partial"
        self.width = width
        self.height = height
        self.fps = fps
        self.dropped = 0
        self._queue: queue.Queue = queue.Queue(maxsize=self.QUEUE_DEPTH)
        self._thread: threading.Thread | None = None
        self._encode_error: BaseException | None = None
        self._state_lock = threading.RLock()
        self._finish_requested = threading.Event()
        self._abort_publish = threading.Event()
        self._done = threading.Event()
        self._status = RecordingStatus.RECORDING
        self._result: RecordingResult | None = None
        self._stopped_at: float | None = None
        self._container = None
        self._stream = None
        # Audio fields are initialised before opening the container so cleanup
        # after a constructor failure is deterministic.
        self._audio: LoopbackCapture | None = None
        self._astream = None
        self._fifo: av.AudioFifo | None = None
        self._resampler: av.AudioResampler | None = None
        self._audio_input_samples = 0  # source-rate clock for the resampler
        self._audio_samples = 0      # frames handed to the fifo, our audio clock
        self.audio_padded = 0        # frames of silence inserted into gaps
        self._audio_next_qpc: int | None = None  # see _packet_after_gap()
        self._frame_idx = 0
        self._reserved = False  # needs_frame() reserved the next slot
        self.written = 0
        #: Frames that came before their slot on the clock (not an encoder
        #: that could not keep up - that is `dropped`).
        self.skipped = 0
        self._started = 0.0
        try:
            # The suffix no longer identifies the format, so be explicit.
            # PyAV may not create anything until the first packet; touching the
            # staging path now guarantees that even an early encoder stall has
            # an exact, inspectable path in its terminal result.
            Path(self.partial_path).touch()
            self._container = av.open(self.partial_path, mode="w", format="mp4")
            self._stream = self._open_video_stream(width, height, fps)
            self._stream.width = width
            # An odd height is rounded up by the encoder (yuv420p needs even
            # dimensions) and the last row comes out duplicated. One-window
            # mode makes odd sizes normal, and duplication is better than crop.
            self._stream.height = height
            self._stream.pix_fmt = "yuv420p"
            self._stream.time_base = Fraction(1, int(round(fps)))
            # Desktop capture is sRGB full range. Keep conversion and stream
            # metadata aligned or players visibly change contrast/colour.
            try:
                self._stream.color_range = 2
                self._stream.colorspace = 1
                self._stream.color_primaries = 1
                self._stream.color_trc = 13
            except Exception as exc:
                print(f"[record] color metadata failed: {exc}", file=sys.stderr)
            try:
                self._stream.bit_rate = self.BIT_RATE
                self._stream.gop_size = max(30, int(round(fps)) * 2)
                self._stream.max_b_frames = 0
            except Exception as exc:
                print(f"[record] encoder params failed: {exc}", file=sys.stderr)
            if self.ENCODER_OPTIONS:
                try:
                    self._stream.options = dict(self.ENCODER_OPTIONS)
                except Exception as exc:
                    print(f"[record] encoder options failed: {exc}",
                          file=sys.stderr)
            if audio:
                self._open_audio()
        except BaseException:
            # A half-constructed object cannot expose its result API. Do not
            # leave a misleading final MP4 or an orphaned staging file behind.
            try:
                if self._container is not None:
                    self._container.close()
            except Exception:
                pass
            self._container = None
            try:
                os.unlink(self.partial_path)
            except FileNotFoundError:
                pass
            raise
        # Codec/audio setup time is not recording time. This also preserves
        # needs_frame()'s contract that slot zero is closed at construction.
        self._started = time.perf_counter()

    def _open_video_stream(self, width: int, height: int, fps: float):
        """Create the video stream with the first NVENC codec that opens.

        add_stream() alone is not a probe: PyAV opens the encoder lazily, at
        the first mux() (start_encoding -> avcodec_open2). On an RTX 30 card
        add_stream("av1_nvenc") succeeds and the recording dies mid-way with
        "no NVENC capable devices found". So each candidate is opened for
        real - on a throwaway null-muxer container, because a stream cannot
        be removed from a container and start_encoding() would re-open a
        codec context that is still closed. The chosen codec is stored on
        self.codec for the caller (and the tests).
        """
        rate = int(round(fps))
        for name in self.CODEC_CHAIN:
            try:
                with av.open("null", mode="w", format="null") as probe:
                    stream = probe.add_stream(name, rate=rate)
                    stream.width = width
                    stream.height = height
                    stream.pix_fmt = "yuv420p"
                    stream.time_base = Fraction(1, rate)
                    stream.bit_rate = self.BIT_RATE
                    stream.gop_size = max(30, rate * 2)
                    stream.max_b_frames = 0
                    if self.ENCODER_OPTIONS:
                        stream.options = dict(self.ENCODER_OPTIONS)
                    stream.codec_context.open()
            except Exception as exc:                      # noqa: BLE001
                print(f"[record] {name} unavailable ({exc}) - trying the "
                      f"next codec", file=sys.stderr)
                continue
            print(f"[record] video codec: {name}")
            self.codec = name
            return self._container.add_stream(name, rate=rate)
        raise RuntimeError(
            "no NVENC encoder available (tried "
            + ", ".join(self.CODEC_CHAIN) + ")")

    def _probe_aac_encoder(self) -> bool:
        """Open the exact AAC configuration before it can poison an MP4.

        PyAV opens streams lazily when the first packet starts the container.
        At that point a bad audio rate does not merely lose audio: the whole
        muxer rejects the video too. The null muxer is the same eager probe
        used for NVENC above, and lets recording fall back to video-only.
        """
        try:
            with av.open("null", mode="w", format="null") as probe:
                stream = probe.add_stream("aac", rate=self.AAC_SAMPLE_RATE)
                stream.bit_rate = self.AUDIO_BIT_RATE
                stream.layout = "stereo"
                stream.format = "fltp"
                stream.time_base = Fraction(1, self.AAC_SAMPLE_RATE)
                stream.codec_context.open()
        except Exception as exc:                      # noqa: BLE001
            print(f"[record] AAC unavailable ({exc}) - recording video without audio",
                  file=sys.stderr)
            return False
        return True

    def _open_audio(self) -> None:
        """Start loopback and a resampled AAC track; audio stays optional."""
        cap = LoopbackCapture()
        try:
            if not cap.start():
                print(f"[record] no audio: {cap.error or 'endpoint unavailable'}",
                      file=sys.stderr)
                cap.close()
                return
            if not self._probe_aac_encoder():
                cap.close()
                return
            # The endpoint may have captured a few samples while spinning up
            # (client.Start() -> the recorder's clock). They are earlier than
            # the video PTS=0 - drop them so the audio does not lead the
            # picture (audit #3, D2).
            cap.discard()
            self._astream = self._container.add_stream("aac",
                                                        rate=self.AAC_SAMPLE_RATE)
            self._astream.bit_rate = self.AUDIO_BIT_RATE
            self._astream.layout = "stereo"
            self._astream.format = "fltp"
            # The stream time base is one sample, so a pts is simply the index
            # of the sample - no rounding anywhere between the clock and the
            # container.
            self._astream.time_base = Fraction(1, self.AAC_SAMPLE_RATE)
            # AAC encodes fixed 1024-sample frames while the loopback hands out
            # whatever the device period gives. The fifo does the regrouping.
            self._fifo = av.AudioFifo()
            self._resampler = av.AudioResampler(format="fltp", layout="stereo",
                                                 rate=self.AAC_SAMPLE_RATE)
            self._audio = cap
            print(f"[record] audio: WASAPI loopback {cap.sample_rate} Hz -> "
                  f"AAC {self.AAC_SAMPLE_RATE} Hz stereo")
        except Exception as exc:                      # noqa: BLE001
            print(f"[record] audio track not created: {exc}", file=sys.stderr)
            cap.close()
            self._audio = None
            self._astream = None
            self._fifo = None
            self._resampler = None

    def _encode_loop(self) -> None:
        """The sole owner of the container while recording is running.

        Audio is pumped from here rather than from the main loop for the same
        reason video is: the container must be touched from one thread only.
        The wait on the video queue is bounded so that audio keeps flowing even
        while the pipeline is between frames.

        finish() first prevents new writes, then sets _finish_requested. The
        loop exits only after a timed queue read proves that every accepted
        frame has been consumed. This avoids the old stop-event race, where
        close() could set the event while queued frames were still pending.
        """
        fatal: BaseException | None = None
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.05)
                except queue.Empty:
                    if self._finish_requested.is_set():
                        break
                    self._pump_audio()
                    continue
                pts, rgba = item
                try:
                    self._pump_audio()
                    self._encode_one(pts, rgba)
                except BaseException as exc:   # noqa: BLE001 - report back to main
                    self._encode_error = exc
                    fatal = RecordingError("encode", exc)
                    print(f"[record] encoding aborted: {exc}", file=sys.stderr)
                    break
                finally:
                    self._queue.task_done()
        finally:
            self._finalize_recording(fatal)

    def _pump_audio(self) -> None:
        """Move captured samples into the container; pad gaps with silence.

        Audio failures never stop the recording: the video is the point, the
        sound is a bonus. On an error the track simply stops growing.
        """
        if self._audio is None or self._fifo is None:
            return
        try:
            for qpc, block, discontinuity in self._audio.read_packets():
                if len(block):
                    self._place_audio(qpc, len(block), discontinuity)
                    self._push_audio(block)
            self._pad_audio()
            self._drain_fifo()
        except Exception as exc:                      # noqa: BLE001
            print(f"[record] audio stopped: {exc}", file=sys.stderr)
            try:
                self._audio.close()
            except Exception:
                pass
            self._audio = None

    def _push_audio(self, chunk: np.ndarray) -> None:
        """Resample float32 loopback audio and append it to the AAC fifo."""
        # 'fltp' is planar: PyAV wants (channels, samples), and contiguous -
        # a transposed view is neither.
        planar = np.ascontiguousarray(chunk.T)
        frame = av.AudioFrame.from_ndarray(planar, format="fltp", layout="stereo")
        frame.sample_rate = self._audio.sample_rate
        frame.time_base = Fraction(1, self._audio.sample_rate)
        frame.pts = self._audio_input_samples
        self._audio_input_samples += planar.shape[1]
        for converted in self._resampler.resample(frame):
            self._append_audio_frame(converted)

    def _append_audio_frame(self, frame: av.AudioFrame) -> None:
        """Append a target-rate frame on one contiguous output clock."""
        if frame is None or frame.samples <= 0:
            return
        frame.sample_rate = self.AAC_SAMPLE_RATE
        frame.time_base = self._astream.time_base
        frame.pts = self._audio_samples
        self._audio_samples += frame.samples
        self._fifo.write(frame)

    def _push_silence(self, samples: int) -> None:
        """Pad the AAC clock directly, without resampling source-rate zeros."""
        if samples <= 0:
            return
        planar = np.zeros((2, samples), dtype=np.float32)
        frame = av.AudioFrame.from_ndarray(planar, format="fltp", layout="stereo")
        self._append_audio_frame(frame)

    def _place_audio(self, qpc: int, frames: int, discontinuity: bool) -> None:
        """Pad up to the moment a packet that follows a gap was heard.

        _pad_audio() stops AUDIO_LAG_S short of the clock, so the first sound
        after a quiet stretch used to be written where that padding ended -
        100-200 ms before its time, the sound ahead of the picture that made
        it. A packet that starts a run (the first one, one after a gap or
        after lost data) goes where its own time says. One that continues the
        previous packet is appended as it is: the device's clock and the
        counter drift apart a little, and following that would cut holes into
        continuous sound. `qpc` is in the 100 ns units of the counter that
        time.perf_counter() reads on Windows, so it compares with _started;
        a time past the clock is not believed beyond the clock.
        """
        after_gap, self._audio_next_qpc = _packet_after_gap(
            self._audio_next_qpc, qpc, frames, self._audio.sample_rate,
            discontinuity, self.AUDIO_JOIN_S)
        if not after_gap:
            return
        heard = min(qpc / 1e7, time.perf_counter())
        due = int((heard - self._started) * self.AAC_SAMPLE_RATE)
        need = due - self._audio_samples
        if need > 0:
            self._push_silence(need)
            self.audio_padded += need

    def _pad_audio(self) -> None:
        """Insert silence when the track has fallen behind the wall clock.

        Only when the gap is real (AUDIO_GAP_S), and never all the way up to
        the clock: samples that are merely late must still have room ahead of
        them, otherwise they would be written after silence covering their own
        slot and the track would drift forward.
        """
        rate = self.AAC_SAMPLE_RATE
        elapsed = time.perf_counter() - self._started
        deficit = int(elapsed * rate) - self._audio_samples
        if deficit < int(self.AUDIO_GAP_S * rate):
            return
        need = deficit - int(self.AUDIO_LAG_S * rate)
        if need <= 0:
            return
        self._push_silence(need)
        self.audio_padded += need

    def _drain_fifo(self, flush: bool = False) -> None:
        """Encode whole AAC frames out of the fifo and mux them."""
        size = self._astream.codec_context.frame_size or 1024
        while True:
            frame = self._fifo.read(size, partial=flush)
            if frame is None:
                return
            for packet in self._astream.encode(frame):
                self._container.mux(packet)

    def needs_frame(self) -> bool:
        """Whether the recorder wants the next frame's pixels.

        Gates FRAME_FLAG_WANT_PIXELS in main.py: the flag is expensive (a
        full 33 MB round-trip from the worker per frame), so it must be
        requested only when the recording can actually use a frame. The
        stream runs at 30 fps - the pipeline usually delivers more - so the
        demand is throttled to one frame per stream slot. The test mirrors
        write()'s acceptance (pts > _frame_idx): a frame is wanted exactly
        when write() would keep it, and the first slot (pts 0) is dropped by
        write() anyway.
        """
        with self._state_lock:
            if (self._status is not RecordingStatus.RECORDING
                    or self._encode_error is not None):
                return False
            elapsed = time.perf_counter() - self._started
            slot = int(elapsed * self.fps)
            if slot > self._frame_idx:
                # Reserve the slot: write() will put the frame into it. The
                # reservation is what keeps the file at the real duration -
                # recomputing the slot in write() (after the frame's round-trip
                # through the worker) skips every second slot at a ~27 fps
                # pipeline (73 frames / 4.8 s instead of ~150).
                self._frame_idx = slot
                self._reserved = True
                return True
            return False

    def write(self, rgba: np.ndarray) -> None:
        """Queue a frame for the encoder (RGBA8 full-res, 4 channels).

        PTS is built from the REAL recording time, not from a frame counter:
        frames arrive at whatever rate the pipeline manages, and the container
        must reflect the real duration - otherwise the video plays back at the
        wrong speed. We compute it HERE, when the frame arrives: inside the
        thread it would reflect the moment of encoding, i.e. it would be off by
        the whole queue depth.

        Frames that arrive faster than the stream's rate are dropped. A small
        window runs the pipeline at ~140 FPS, and a 60 fps stream has no slot
        for the extra ones; the old code handed them the next free counter
        value instead, which turned five seconds of screen into an 11.8-second
        file in slow motion.
        """
        if rgba.shape[0] != self.height or rgba.shape[1] != self.width:
            # The display mode changed - frames have a different shape. Skipping
            # them silently is not an option: the recording would "quietly"
            # write nothing. The exception stops the recording (main.py:
            # recorder.close() + recorder = None).
            raise ValueError(
                f"display mode changed: frame {rgba.shape[1]}x{rgba.shape[0]} "
                f"!= recorder {self.width}x{self.height}")
        # The lifecycle lock covers both the state check and the bounded put.
        # Therefore finish() cannot observe an empty queue while a previously
        # accepted writer is still about to enqueue its frame.
        with self._state_lock:
            if self._encode_error is not None:
                raise RecordingError("encode", self._encode_error)
            if self._status is not RecordingStatus.RECORDING:
                if self._result is not None and self._result.error is not None:
                    raise self._result.error
                raise RuntimeError("recording is already finalizing")
            if self._thread is None:
                self._start_thread_locked()
            # needs_frame() reserved the next free slot when it said yes; the
            # elapsed clock has moved on since (the frame spent a round-trip
            # in the worker, ~36 ms at 27 fps), so recomputing pts here would
            # skip the reserved slot and drop every second frame.
            if not self._reserved:
                # A frame nobody reserved: main.py writes whatever pixels come
                # back, and without a present window they come back on every
                # frame, not only when needs_frame() asked. It takes its own
                # slot on the clock, and is dropped when that slot is not
                # newer than the last one. Handing it the next counter value
                # instead put two frames into each 30 fps slot of a 60 FPS
                # pipeline: three seconds of screen became a six-second file.
                slot = int((time.perf_counter() - self._started) * self.fps)
                if slot <= self._frame_idx:
                    self.skipped += 1
                    return
                self._frame_idx = slot
            self._reserved = False
            pts = self._frame_idx
            try:
                self._queue.put((pts, rgba), timeout=self.FRAME_PUT_TIMEOUT_S)
            except queue.Full:
                # The encoder cannot keep up. Dropping the frame is more honest
                # than holding up the main loop.
                self.dropped += 1

    def _start_thread_locked(self) -> None:
        """Start the sole container-owning thread while _state_lock is held."""
        self._thread = threading.Thread(target=self._encode_loop,
                                        name="nr-encode", daemon=True)
        self._thread.start()

    def _encode_one(self, pts: int, rgba: np.ndarray) -> None:
        """The encoding proper - only from the _encode_loop thread."""
        frame = av.VideoFrame.from_ndarray(rgba, format="rgba")
        # The colour tags are MANDATORY on the frame, not only on the stream:
        # when converting RGBA->yuv420p swscale takes the matrix from the frame,
        # while the player interprets the result by the stream tags. That
        # mismatch (an untagged frame -> swscale default, a tagged stream) is
        # what produces the "contrast".
        try:
            frame.color_range = 2        # AVCOL_RANGE_JPEG = full (sRGB)
            frame.colorspace = 1         # AVCOL_SPC_BT709
            frame.color_primaries = 1    # AVCOL_PRI_BT709
            frame.color_trc = 13         # AVCOL_TRC_IEC61966_2_1 = sRGB
        except Exception as exc:
            print(f"[record] frame color tags failed: {exc}", file=sys.stderr)
        frame.pts = pts
        for packet in self._stream.encode(frame):
            self._container.mux(packet)
        self.written += 1

    def finish(self) -> bool:
        """Request a non-blocking, queue-draining finalization.

        Returns True only for the call that changes RECORDING -> FINALIZING.
        No accepted frame can appear after this transition: write() performs
        its state check and queue put under the same lock. Use wait() to poll
        or await the immutable RecordingResult.
        """
        with self._state_lock:
            if self._status is not RecordingStatus.RECORDING:
                return False
            self._status = RecordingStatus.FINALIZING
            self._stopped_at = time.perf_counter()
            self._reserved = False
            self._finish_requested.set()
            if self._thread is None:
                # Even an empty recording is finalized on the worker, so this
                # method never performs codec or filesystem work itself.
                self._start_thread_locked()
            return True

    def wait(self, timeout: float | None = None) -> RecordingResult | None:
        """Return the terminal result, or None when *timeout* expires."""
        if not self._done.wait(timeout):
            return None
        with self._state_lock:
            return self._result

    def close(self, timeout: float | None = FINISH_TIMEOUT_S) -> RecordingResult:
        """Compatibility wrapper: finish(), wait, then return or raise.

        Existing no-argument callers remain synchronous. New UI code can use
        finish() + wait(0) to keep its event loop responsive. A failed close
        raises the stored RecordingError; commands.py/main.py already guard
        close() with try/except, while callers needing detail can inspect
        status/result/error/result_path afterwards.
        """
        self.finish()
        result = self.wait(timeout)
        if result is None:
            result = self._fail_after_timeout(timeout)
        if result.status is RecordingStatus.FAILED:
            error = result.error or RecordingError(
                "finalize", RuntimeError("recording failed without an error"))
            raise error
        return result

    def _fail_after_timeout(self, timeout: float | None) -> RecordingResult:
        """Freeze a timeout as the terminal result and forbid late publish."""
        seconds = self.FINISH_TIMEOUT_S if timeout is None else timeout
        error = RecordingError(
            "timeout",
            TimeoutError(f"encoder did not finish within {seconds:g} s"),
        )
        with self._state_lock:
            if self._result is not None:
                return self._result
            self._abort_publish.set()
            self._status = RecordingStatus.FAILED
            self._result = RecordingResult(
                RecordingStatus.FAILED, self._remaining_partial_path(), error)
            self._done.set()
            print(f"[record] {error}; partial file was not published",
                  file=sys.stderr)
            return self._result

    def _finalize_recording(self, fatal: BaseException | None) -> None:
        """Close, verify, and atomically publish; called by the worker only."""
        with self._state_lock:
            if self._status is RecordingStatus.RECORDING:
                # An asynchronous encoder failure can arrive before finish().
                self._status = RecordingStatus.FINALIZING
                self._stopped_at = time.perf_counter()
                self._finish_requested.set()

        error = fatal
        if error is None and self._encode_error is not None:
            error = RecordingError("encode", self._encode_error)

        try:
            if self._container is None:
                raise RuntimeError("container is already closed")
            self._close_audio()
            for packet in self._stream.encode(None):
                self._container.mux(packet)
            self._container.close()
        except BaseException as exc:                 # noqa: BLE001
            close_error = RecordingError("close", exc)
            if error is None:
                error = close_error
            else:
                print(f"[record] secondary {close_error}", file=sys.stderr)
            print(f"[record] close failed: {exc}", file=sys.stderr)
        finally:
            self._container = None

        if error is None and not self._abort_publish.is_set():
            try:
                self._verify_partial()
            except BaseException as exc:             # noqa: BLE001
                error = RecordingError("verify", exc)
                print(f"[record] verification failed: {exc}", file=sys.stderr)

        # Timeout and publish contend on this lock. Whichever wins defines the
        # immutable result: a timeout can never be followed by a late MP4 that
        # contradicts FAILED, and a completed replace cannot become a timeout.
        with self._state_lock:
            if self._result is None:
                if error is None and not self._abort_publish.is_set():
                    try:
                        os.replace(self.partial_path, self.path)
                    except BaseException as exc:     # noqa: BLE001
                        error = RecordingError("publish", exc)
                        print(f"[record] publish failed: {exc}", file=sys.stderr)
                if error is None:
                    self._status = RecordingStatus.PUBLISHED
                    self._result = RecordingResult(
                        RecordingStatus.PUBLISHED, self.path, None)
                else:
                    self._status = RecordingStatus.FAILED
                    self._result = RecordingResult(
                        RecordingStatus.FAILED,
                        self._remaining_partial_path(), error)
                self._done.set()
            self._thread = None

        if self.dropped:
            print(f"[record] frames dropped: {self.dropped} "
                  f"(encoder could not keep up)", file=sys.stderr)
        if error is not None:
            print(f"[record] recording failed: {error}", file=sys.stderr)

    def _verify_partial(self) -> None:
        """Read back enough of the closed MP4 to reject broken/empty output."""
        partial = Path(self.partial_path)
        if self.written <= 0:
            raise RuntimeError("recording contains no video frames")
        if not partial.is_file() or partial.stat().st_size <= 0:
            raise RuntimeError("partial MP4 is missing or empty")
        with av.open(str(partial), mode="r", format="mp4") as container:
            if not container.streams.video:
                raise RuntimeError("partial MP4 has no video stream")
            if next(container.decode(video=0), None) is None:
                raise RuntimeError("partial MP4 has no decodable video frame")

    def _remaining_partial_path(self) -> str | None:
        return self.partial_path if Path(self.partial_path).is_file() else None

    @property
    def status(self) -> RecordingStatus:
        with self._state_lock:
            return self._status

    @property
    def result(self) -> RecordingResult | None:
        with self._state_lock:
            return self._result

    @property
    def error(self) -> BaseException | None:
        with self._state_lock:
            if self._result is not None:
                return self._result.error
            return self._encode_error

    @property
    def result_path(self) -> str | None:
        with self._state_lock:
            return self._result.path if self._result is not None else None

    @property
    def audio_enabled(self) -> bool:
        """Whether this MP4 actually has an AAC stream, not merely a request."""
        with self._state_lock:
            return self._astream is not None

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def _close_audio(self) -> None:
        """Stop the capture, write what is left and flush the AAC encoder.

        Keyed on the stream, not on the capture: a loopback that died mid-way
        sets _audio to None, and the frames already encoded still have to be
        flushed - otherwise the tail of the track is lost along with it.
        """
        if self._astream is None:
            return
        try:
            if self._audio is not None:
                self._pump_audio()      # whatever arrived after the last frame
                self._audio.close()
                self._audio = None
            if self._resampler is not None:
                for frame in self._resampler.resample(None):
                    self._append_audio_frame(frame)
                self._resampler = None
            self._drain_fifo(flush=True)
            for packet in self._astream.encode(None):
                self._container.mux(packet)
            secs = self._audio_samples / max(1, self._astream.rate)
            padded = self.audio_padded / max(1, self._astream.rate)
            print(f"[record] audio: {secs:.1f} s written"
                  + (f", {padded:.1f} s of it silence in gaps" if padded > 0.05 else ""))
        except Exception as exc:                      # noqa: BLE001
            print(f"[record] audio flush failed: {exc}", file=sys.stderr)
        finally:
            self._audio = None
            self._resampler = None

    def stopped_elsewhere(self) -> bool:
        """Never: this recorder stops only when told (see GpuRecorder)."""
        return False

    @property
    def duration_ms(self) -> float:
        end = self._stopped_at if self._stopped_at is not None else time.perf_counter()
        return (end - self._started) * 1000.0


def _packet_after_gap(expected: int | None, qpc: int, frames: int, rate: int,
                      discontinuity: bool, join_s: float):
    """Whether a loopback packet starts a new run, and where the next one
    that continues it would start - both in the packet's 100 ns QPC units.

    A packet without a time (qpc 0) is appended where the track stands; the
    next one with a time starts a run again.
    """
    if qpc <= 0 or rate <= 0:
        return False, None
    after_gap = (discontinuity or expected is None
                 or qpc - expected > join_s * 10_000_000)
    return after_gap, qpc + frames * 10_000_000 // rate


def _qpc() -> int:
    """QueryPerformanceCounter, raw: the clock the worker's recorder runs on."""
    value = ctypes.c_int64()
    ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(value))
    return value.value


def _qpf() -> int:
    value = ctypes.c_int64()
    ctypes.windll.kernel32.QueryPerformanceFrequency(ctypes.byref(value))
    return value.value


class AudioRing:
    """The PCM ring a GPU recording takes its sound from (GpuRecAudioRing).

    A named section the worker maps read-only: the header (AUDIO_RING_FMT),
    then `capacity` frames of int16, interleaved. This side is the only
    writer and keeps one order - the samples first, the running count after
    them - which is all the worker's reader relies on.
    """

    def __init__(self, rate: int = 48_000, channels: int = 2,
                 seconds: float = 4.0):
        self.rate = int(rate)
        self.channels = int(channels)
        self.capacity = int(self.rate * seconds)
        header = struct.calcsize(AUDIO_RING_FMT)
        # ASCII and unique per process, like the other sections: the worker
        # opens it with OpenFileMappingA in the same session.
        self.name = f"NeuralScreenRec_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        self._mm = mmap.mmap(-1, header + self.capacity * self.channels * 2,
                             tagname=self.name)
        struct.pack_into(AUDIO_RING_FMT, self._mm, 0, AUDIO_RING_MAGIC,
                         self.rate, self.channels, self.capacity, 0, 0)
        self._samples: np.ndarray | None = np.ndarray(
            (self.capacity, self.channels), dtype=np.int16, buffer=self._mm,
            offset=header)
        # The count as one aligned 8-byte store: the worker reads it while
        # this side writes it, and a torn value would be a jump in time.
        self._count: ctypes.c_int64 | None = ctypes.c_int64.from_buffer(self._mm, 16)
        self.written = 0

    def write(self, frames: np.ndarray) -> None:
        """Append (n, channels) int16 frames, then publish the new count."""
        n = int(frames.shape[0])
        if n <= 0 or self._samples is None:
            return
        if n > self.capacity:
            # More than the ring holds: only the newest part can survive.
            self.written += n - self.capacity
            frames = frames[n - self.capacity:]
            n = self.capacity
        start = self.written % self.capacity
        first = min(n, self.capacity - start)
        self._samples[start:start + first] = frames[:first]
        if first < n:
            self._samples[:n - first] = frames[first:]
        self._publish(n)

    def silence(self, n: int) -> None:
        """Append n frames of silence."""
        n = int(n)
        if n <= 0 or self._samples is None:
            return
        if n >= self.capacity:
            self._samples[:] = 0
        else:
            start = self.written % self.capacity
            first = min(n, self.capacity - start)
            self._samples[start:start + first] = 0
            if first < n:
                self._samples[:n - first] = 0
        self._publish(n)

    def _publish(self, n: int) -> None:
        self.written += n
        self._count.value = self.written

    def close(self) -> None:
        # The two views export the buffer, and mmap refuses to close under
        # an export. The worker keeps its own view: closing here never pulls
        # the samples out from under it.
        self._samples = None
        self._count = None
        try:
            self._mm.close()
        except (BufferError, ValueError):
            pass


class GpuRecorder:
    """Records on the GPU, inside the worker (RECS/RECE, gpu_recorder.cpp).

    VideoRecorder pulls every recorded frame back to Python - 33 MB at 4K,
    then an RGBA->YUV conversion on the CPU - which is why it records at 30
    fps and still costs a good part of the frame rate. Here the worker copies
    the frame the viewer sees into the encoder's ring on the card, converts it
    there and hands it to the hardware encoder (NVENC, through Media
    Foundation); the frame never leaves the GPU. Only the sound comes from
    this side, through an AudioRing. So it records at 60 fps for the price of
    one texture copy a frame.

    The lifecycle is VideoRecorder's, so commands.py, pipeline.py and main.py
    drive either one the same way: finish() starts the finalization and never
    waits, wait()/close() report the RecordingResult, and the file is written
    as `.partial` and published only once it reads back.

    What it does not record is the open menu: that is a separate window the
    worker never sees (VideoRecorder draws it onto each frame itself). The
    Media tab's "Record on the GPU" switch picks the old path for that.
    """

    #: The frame's pixels never come to Python for this recorder.
    takes_pixels = False
    FINISH_TIMEOUT_S = 30.0
    #: The worker blocks on the encoder's setup while it answers: 0.1-0.3 s
    #: measured, about half a second more for each codec refused first.
    START_TIMEOUT_S = 8.0
    AUDIO_RATE = 48_000
    AUDIO_RING_S = 4.0
    AUDIO_PUMP_S = 0.02
    AUDIO_GAP_S = VideoRecorder.AUDIO_GAP_S
    AUDIO_LAG_S = VideoRecorder.AUDIO_LAG_S
    AUDIO_JOIN_S = VideoRecorder.AUDIO_JOIN_S

    def __init__(self, worker, reader, path: str, *, fps: int = 60,
                 audio: bool = True, codec: int = REC_CODEC_AUTO,
                 hdr: bool = False):
        self.path = str(Path(path))
        # Its own staging name, not the CPU recorder's: when the start times out
        # the worker may still act on the RECS afterwards and create this file,
        # while the CPU fallback (commands.start_recorder) is already writing
        # its own - under the same `.partial` they overwrote each other.
        self.partial_path = f"{self.path}.gpu.partial"
        self.fps = float(fps)
        self.width = 0
        self.height = 0
        self.codec = "unknown"
        #: The file is HDR10 - asked for with `hdr`, given when the worker's
        #: frames are HDR and a 10-bit encoder opened.
        self.hdr = False
        self.written = 0
        self.dropped = 0
        self.audio_padded = 0
        #: The file ends where the worker stopped, not where the user did.
        self.cut_short = False
        self._worker = worker
        self._reader = reader
        self._state_lock = threading.RLock()
        self._status = RecordingStatus.RECORDING
        self._result: RecordingResult | None = None
        self._done = threading.Event()
        self._abort_publish = threading.Event()
        self._started = time.perf_counter()
        self._stopped_at: float | None = None
        self._audio: LoopbackCapture | None = None
        self._ring: AudioRing | None = None
        self._resampler: av.AudioResampler | None = None
        self._in_samples = 0
        self._audio_next_qpc: int | None = None  # see _packet_after_gap()
        self._qpf = _qpf()
        self._start_qpc = 0
        self._audio_stop = threading.Event()
        self._audio_thread: threading.Thread | None = None
        self._finalizer: threading.Thread | None = None
        self._audio_track = False

        if audio:
            self._open_audio()
        if self._start_qpc == 0:
            self._start_qpc = _qpc()
        # The answers land on the reader, not in its frame queue. Cleared
        # BEFORE the alive check: a reader that dies after it sets them again.
        reader.rec_start_reply = None
        reader.rec_started.clear()
        reader.rec_done_reply = None
        reader.rec_done.clear()
        if not reader.alive:
            self._close_audio()
            raise RecordingError("start", EOFError("the worker is not running"))
        try:
            send_rec_start(worker, self.partial_path, fps=int(round(fps)),
                           codec=codec, start_qpc=self._start_qpc,
                           audio_ring=self._ring.name if self._ring else "",
                           hdr=hdr)
        except (OSError, ValueError) as exc:
            self._close_audio()
            raise RecordingError("start", exc) from exc
        if not reader.rec_started.wait(self.START_TIMEOUT_S):
            # No answer in time. The worker may still start: make it stop.
            try:
                send_rec_stop(worker)
            except (OSError, ValueError):
                pass
            self._close_audio()
            raise RecordingError("start", TimeoutError(
                f"the worker did not answer within {self.START_TIMEOUT_S:g} s"))
        reply = reader.rec_start_reply
        if reply is None:
            self._close_audio()
            raise RecordingError("start", EOFError("the worker stopped"))
        if not reply.ok:
            self._close_audio()
            raise RecordingError("start", RuntimeError(
                f"the GPU encoder did not start "
                f"(0x{reply.hresult & 0xFFFFFFFF:08X})"))
        self.hdr = bool(reply.codec & REC_CODEC_HDR10)
        self.codec = REC_CODEC_NAMES.get(reply.codec & 0xFF, "unknown") + (
            " HDR10" if self.hdr else "")
        self.width, self.height = int(reply.width), int(reply.height)
        self.fps = float(reply.fps)
        if self._ring is not None and not reply.audio:
            # The worker could not open an AAC stream: the capture is moot.
            print("[record] the GPU recording has no sound track "
                  "(the AAC encoder was refused)", file=sys.stderr)
            self._close_audio()
        self._audio_track = self._ring is not None
        self._started = time.perf_counter()
        if self._ring is not None:
            self._audio_thread = threading.Thread(
                target=self._audio_loop, name="nr-gpu-audio", daemon=True)
            self._audio_thread.start()

    # -- sound --------------------------------------------------------------

    def _open_audio(self) -> None:
        """Start the loopback and the ring; the sound stays optional."""
        cap = LoopbackCapture()
        try:
            if not cap.start():
                print(f"[record] no audio: {cap.error or 'endpoint unavailable'}",
                      file=sys.stderr)
                cap.close()
                return
            self._resampler = av.AudioResampler(format="s16", layout="stereo",
                                                rate=self.AUDIO_RATE)
            self._ring = AudioRing(self.AUDIO_RATE, 2, self.AUDIO_RING_S)
            # The spin-up samples belong before the recording, as in
            # VideoRecorder; the ring's frame 0 is this moment.
            cap.discard()
            self._start_qpc = _qpc()
            self._audio = cap
            print(f"[record] audio: WASAPI loopback {cap.sample_rate} Hz -> "
                  f"the worker's AAC at {self.AUDIO_RATE} Hz")
        except Exception as exc:                      # noqa: BLE001
            print(f"[record] audio not set up: {exc}", file=sys.stderr)
            cap.close()
            if self._ring is not None:
                self._ring.close()
            self._ring = None
            self._audio = None
            self._resampler = None

    def _audio_loop(self) -> None:
        while not self._audio_stop.wait(self.AUDIO_PUMP_S):
            self._pump_audio()
        self._pump_audio(final=True)

    def _pump_audio(self, final: bool = False) -> None:
        """Move captured sound into the ring; pad the gaps with silence.

        The same rule as VideoRecorder._pad_audio: loopback hands out nothing
        while the device is idle, and the worker times the sound by its
        position in the ring - so a gap left unpadded would pull everything
        after it early. At the end the pad goes all the way to the clock.
        """
        if self._audio is None or self._ring is None:
            return
        try:
            for qpc, block, discontinuity in self._audio.read_packets():
                if not len(block):
                    continue
                # Each packet reaches the ring before the next is placed: the
                # placement compares a packet's time with the ring's count.
                self._place_audio(qpc, len(block), discontinuity)
                planar = np.ascontiguousarray(block.T)
                frame = av.AudioFrame.from_ndarray(planar, format="fltp",
                                                   layout="stereo")
                frame.sample_rate = self._audio.sample_rate
                frame.time_base = Fraction(1, self._audio.sample_rate)
                frame.pts = self._in_samples
                self._in_samples += planar.shape[1]
                self._write_ring(self._resampler.resample(frame))
            if final:
                self._write_ring(self._resampler.resample(None))
            rate = self.AUDIO_RATE
            due = (_qpc() - self._start_qpc) * rate // self._qpf
            deficit = int(due) - self._ring.written
            if final or deficit >= int(self.AUDIO_GAP_S * rate):
                need = deficit - (0 if final else int(self.AUDIO_LAG_S * rate))
                if need > 0:
                    self._ring.silence(need)
                    self.audio_padded += need
        except Exception as exc:                      # noqa: BLE001
            # The sound is a bonus: the picture goes on without it.
            print(f"[record] audio stopped: {exc}", file=sys.stderr)
            try:
                self._audio.close()
            except Exception:
                pass
            self._audio = None

    def _write_ring(self, frames) -> None:
        for converted in frames:
            if converted is not None and converted.samples > 0:
                # s16 is packed: one row of interleaved samples.
                self._ring.write(converted.to_ndarray().reshape(-1, 2))

    def _place_audio(self, qpc: int, frames: int, discontinuity: bool) -> None:
        """Pad the ring up to the moment a packet that follows a gap was
        heard - VideoRecorder._place_audio, on the worker's QPC clock."""
        after_gap, self._audio_next_qpc = _packet_after_gap(
            self._audio_next_qpc, qpc, frames, self._audio.sample_rate,
            discontinuity, self.AUDIO_JOIN_S)
        if not after_gap:
            return
        ticks = min(qpc * self._qpf // 10_000_000, _qpc())
        due = (ticks - self._start_qpc) * self.AUDIO_RATE // self._qpf
        need = int(due) - self._ring.written
        if need > 0:
            self._ring.silence(need)
            self.audio_padded += need

    def _stop_audio_thread(self) -> None:
        self._audio_stop.set()
        thread = self._audio_thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._audio_thread = None

    def _close_audio(self) -> None:
        self._stop_audio_thread()
        if self._audio is not None:
            try:
                self._audio.close()
            except Exception:
                pass
            self._audio = None
        if self._ring is not None:
            secs = self._ring.written / float(self.AUDIO_RATE)
            padded = self.audio_padded / float(self.AUDIO_RATE)
            if secs > 0:
                print(f"[record] audio: {secs:.1f} s handed to the worker"
                      + (f", {padded:.1f} s of it silence in gaps"
                         if padded > 0.05 else ""))
            self._ring.close()
            self._ring = None
        self._resampler = None

    # -- the VideoRecorder surface -------------------------------------------

    def needs_frame(self) -> bool:
        """Never: the worker takes the frames itself."""
        return False

    def write(self, rgba: np.ndarray) -> None:
        """Nothing to do - kept so a stray call is harmless."""
        return None

    def stopped_elsewhere(self) -> bool:
        """The worker closed the recording by itself, or is gone.

        Polled by the main loop, so a full disk or a crashed worker ends the
        recording at once - with what reached the disk - rather than when the
        user next presses the key.
        """
        with self._state_lock:
            if self._status is not RecordingStatus.RECORDING:
                return False
        return self._reader.rec_done.is_set() or not self._reader.alive

    def finish(self) -> bool:
        """Ask the worker to close the file; returns without waiting."""
        with self._state_lock:
            if self._status is not RecordingStatus.RECORDING:
                return False
            self._status = RecordingStatus.FINALIZING
            self._stopped_at = time.perf_counter()
        # The sound up to this moment reaches the ring before the worker is
        # told to drain it.
        self._stop_audio_thread()
        if not self._reader.rec_done.is_set():
            try:
                send_rec_stop(self._worker)
            except (OSError, ValueError) as exc:
                print(f"[record] could not ask the worker to stop: {exc}",
                      file=sys.stderr)
        self._finalizer = threading.Thread(target=self._finalize,
                                           name="nr-gpu-finalize", daemon=True)
        self._finalizer.start()
        return True

    def _finalize(self) -> None:
        """Wait for REAK, verify, publish; the finalizer thread only."""
        got = self._reader.rec_done.wait(self.FINISH_TIMEOUT_S)
        reply = self._reader.rec_done_reply if got else None
        self._close_audio()
        stage = "verify"
        if reply is None:
            # A worker that died mid-recording still leaves every fragment
            # it finished: that is what gets published.
            self.cut_short = True
            stage = "worker"
            why = ("the worker stopped" if got else
                   f"the worker did not answer within {self.FINISH_TIMEOUT_S:g} s")
            print(f"[record] {why} before the recording was closed - "
                  f"keeping what reached the disk", file=sys.stderr)
        else:
            self.written = int(reply.written)
            self.dropped = int(reply.dropped)
            if not reply.ok:
                stage = "encode"
                self.cut_short = reply.written > 0
                print(f"[record] the GPU recorder reported "
                      f"0x{reply.hresult & 0xFFFFFFFF:08X} after "
                      f"{reply.written} frames", file=sys.stderr)
        error: BaseException | None = None
        if not self._abort_publish.is_set():
            try:
                frames = self._verify_partial(count=reply is None)
                if reply is None:
                    self.written = frames
            except BaseException as exc:             # noqa: BLE001
                error = RecordingError(stage, exc)
                print(f"[record] the recording does not read back: {exc}",
                      file=sys.stderr)
        with self._state_lock:
            if self._result is None:
                if error is None:
                    try:
                        os.replace(self.partial_path, self.path)
                    except BaseException as exc:     # noqa: BLE001
                        error = RecordingError("publish", exc)
                        print(f"[record] publish failed: {exc}", file=sys.stderr)
                if error is None:
                    self._status = RecordingStatus.PUBLISHED
                    self._result = RecordingResult(
                        RecordingStatus.PUBLISHED, self.path, None)
                else:
                    self._status = RecordingStatus.FAILED
                    self._result = RecordingResult(
                        RecordingStatus.FAILED, self._remaining_partial_path(),
                        error)
                self._done.set()
        if self.dropped:
            print(f"[record] frames dropped: {self.dropped} "
                  f"(the encoder could not keep up)", file=sys.stderr)

    def _verify_partial(self, count: bool = False) -> int:
        """Read back enough of the file to reject a broken or empty one.

        With `count`, also the number of video frames in it - demuxed, not
        decoded - for a recording whose REAK never came. A fragmented file
        cut mid-fragment ends in a partial one; counting stops there.
        """
        partial = Path(self.partial_path)
        if not partial.is_file() or partial.stat().st_size <= 0:
            raise RuntimeError("the recording file is missing or empty")
        with av.open(str(partial), mode="r", format="mp4") as container:
            if not container.streams.video:
                raise RuntimeError("the recording has no video stream")
            if next(container.decode(video=0), None) is None:
                raise RuntimeError("the recording has no decodable video frame")
        if not count:
            return 0
        frames = 0
        try:
            with av.open(str(partial), mode="r", format="mp4") as container:
                for packet in container.demux(video=0):
                    if packet.size:
                        frames += 1
        except Exception:                            # noqa: BLE001
            pass
        return frames

    def wait(self, timeout: float | None = None) -> RecordingResult | None:
        """Return the terminal result, or None when *timeout* expires."""
        if not self._done.wait(timeout):
            return None
        with self._state_lock:
            return self._result

    def close(self, timeout: float | None = FINISH_TIMEOUT_S) -> RecordingResult:
        """finish(), wait, then return or raise - as VideoRecorder.close()."""
        self.finish()
        result = self.wait(timeout)
        if result is None:
            result = self._fail_after_timeout(timeout)
        if result.status is RecordingStatus.FAILED:
            raise result.error or RecordingError(
                "finalize", RuntimeError("recording failed without an error"))
        return result

    def _fail_after_timeout(self, timeout: float | None) -> RecordingResult:
        seconds = self.FINISH_TIMEOUT_S if timeout is None else timeout
        error = RecordingError("timeout", TimeoutError(
            f"the worker did not close the recording within {seconds:g} s"))
        with self._state_lock:
            if self._result is not None:
                return self._result
            self._abort_publish.set()
            self._status = RecordingStatus.FAILED
            self._result = RecordingResult(
                RecordingStatus.FAILED, self._remaining_partial_path(), error)
            self._done.set()
            print(f"[record] {error}; partial file was not published",
                  file=sys.stderr)
            return self._result

    def _remaining_partial_path(self) -> str | None:
        return self.partial_path if Path(self.partial_path).is_file() else None

    @property
    def status(self) -> RecordingStatus:
        with self._state_lock:
            return self._status

    @property
    def result(self) -> RecordingResult | None:
        with self._state_lock:
            return self._result

    @property
    def error(self) -> BaseException | None:
        with self._state_lock:
            return self._result.error if self._result is not None else None

    @property
    def result_path(self) -> str | None:
        with self._state_lock:
            return self._result.path if self._result is not None else None

    @property
    def audio_enabled(self) -> bool:
        """Whether the file has a sound track (the worker said so in RSAK)."""
        return self._audio_track

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def duration_ms(self) -> float:
        end = self._stopped_at if self._stopped_at is not None else time.perf_counter()
        return (end - self._started) * 1000.0
