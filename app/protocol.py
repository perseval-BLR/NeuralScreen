"""The worker protocol: what the two processes say to each other.

Moved out of main.py unchanged. One subject in one file - the magics, the
struct formats, the senders and the reader thread that turns the worker's
replies into a queue. It needs nothing from main.py, which is why it could
leave: the commands are self-contained by design.

The sizes here are not free-form. Every struct format has a static_assert
behind it in native/dlss5-feed-host64.cpp, and tests/test_protocol_sizes.py
checks the two sides against each other - a field added on one side and not
the other is a build error now, not a runtime desync.
"""
from __future__ import annotations

import collections
import mmap
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass

import numpy as np

from paths import BASE_DIR  # noqa: F401




# NGX feature 18 goes silent at 3840x2160 (verified in isolation: the worker
# hangs on frame 0 with work=4K, both in legacy and in upscale mode).
# We cap the work resolution at 2560x1440 - that is known to work.
WORK_MAX_W = 2560


WORK_MAX_H = 1440


#: How many destination buffers read_out() rotates through, at most. A frame
#: that comes back is handed on by REFERENCE and outlives the call: the
#: recorder queues up to QUEUE_DEPTH (4) of them for its encoder thread and
#: the main loop keeps the newest as st.output_rgba for a screenshot, so the
#: ring has to be longer than everything that can be in flight at once.
#:
#: A cap, not an allocation - slots are created on demand, and only when
#: pixels actually come back (a recording or a screenshot asked for them).
#: An idle session allocates none of it.
#:
#: Not imported from recorder: protocol.py is a leaf module, and
#: tests/test_module_layers.py is what keeps it one.
OUT_RING_SLOTS = 6


class SharedFrameBuffer:
    """Shared memory for the worker's input frame (the SHMI command).

    The layout is fixed and does NOT depend on work_scale:
        [0 .. color_capacity)                - RGBA8 full-res
        [color_capacity .. +motion_capacity) - motion float16 work-res
    The motion offset is constant, so a resolution change (RNSZ) needs no
    renegotiation of SHMI - only the used length changes.

    INVARIANT: there is one slot. Frame N+1 must not be placed until the
    worker has returned the result for frame N, otherwise we overwrite the
    pixels under its hands. The main loop is strictly paired (send -> recv),
    so the invariant holds. Add pipelining and a second slot will be needed.
    """

    def __init__(self, full_w: int, full_h: int,
                 max_work_w: int = WORK_MAX_W, max_work_h: int = WORK_MAX_H):
        self.color_capacity = full_w * full_h * 4
        self.motion_capacity = max_work_w * max_work_h * 4
        self.size = self.color_capacity + self.motion_capacity
        # The section name: ASCII, unique per process - the worker opens it
        # through OpenFileMappingA in the same Windows session.
        self.name = f"NeuralScreen_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self._mm = mmap.mmap(-1, self.size, tagname=self.name)
        self._buf = np.ndarray((self.size,), dtype=np.uint8, buffer=self._mm)
        self.negotiated = False  # set by start_worker after SACK

        # --- Reverse channel: gray (luminance) for guides in DDA mode ---
        # The worker writes a downsample of the screen here (320x180 = the
        # flow size) and Python reads it instead of the dxcam grab for
        # DISOpticalFlow.
        self.gray_w, self.gray_h = 0, 0
        self.gray_bytes = 0
        self.gray_name = f"NeuralScreenGray_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        self._gray_mm: mmap.mmap | None = None
        self._gray_buf: np.ndarray | None = None  # (gray_bytes,) uint8

        # --- Reverse channel: the result pixels (recording/screenshot) ---
        self.out_w, self.out_h = 0, 0
        self.out_bytes = 0
        self.out_name = ""
        self._out_mm: mmap.mmap | None = None
        self._out_buf: np.ndarray | None = None  # (h, w, 4) uint8
        # read_out()'s destinations, reused instead of freshly allocated.
        # Grown on demand (see _next_out_slot) rather than here: the channel
        # is negotiated for every worker, while pixels only travel back when
        # something asks for them.
        self._out_ring: list[np.ndarray] = []
        self._out_slot = 0

    def open_gray(self, w: int, h: int) -> None:
        """Open a gray section of w*h bytes (create it if there was none).

        On a size change the section name CHANGES: the worker holds the old
        handle and CreateFileMapping with the same name would return the old
        section - a larger mmap would fail and the channel would die quietly
        (audit H2). send_gray() passes the fresh name to the worker after
        open_gray().
        """
        if self._gray_mm is not None and self.gray_w == w and self.gray_h == h:
            return
        self.close_gray()
        self.gray_w, self.gray_h = w, h
        self.gray_bytes = w * h
        self.gray_name = f"NeuralScreenGray_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        self._gray_mm = mmap.mmap(-1, self.gray_bytes, tagname=self.gray_name)
        self._gray_buf = np.ndarray((self.gray_bytes,), dtype=np.uint8, buffer=self._gray_mm)

    def open_out(self, w: int, h: int) -> None:
        """Open the section for the returned pixels (RGBA8 w*h).

        The name changes on every open - just like gray: the worker holds the
        old handle and CreateFileMapping with the same name would return the
        old section, at its old size. The first 8 bytes are a seqlock written
        by the worker (odd while writing, even when done).
        """
        if self._out_mm is not None and self.out_w == w and self.out_h == h:
            return
        self.close_out()
        self.out_w, self.out_h = w, h
        self.out_bytes = w * h * 4 + 8  # + seqlock
        self.out_name = f"NeuralScreenOut_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        self._out_mm = mmap.mmap(-1, self.out_bytes, tagname=self.out_name)
        self._out_buf = np.ndarray((h, w, 4), dtype=np.uint8, buffer=self._out_mm, offset=8)

    def _next_out_slot(self) -> np.ndarray:
        """A destination buffer nobody else is still holding.

        The ring is SCANNED rather than simply advanced, because a frame that
        comes back is handed on by reference and lives as long as its
        consumer needs it: the recorder queues it for the encoder thread,
        the main loop keeps the newest one for a screenshot. Writing into a
        slot that is still queued would rewrite a frame the encoder has not
        read yet - a torn picture in the file, which is a worse bug than the
        allocation this ring exists to remove.

        The refcount is what answers the question. A slot nobody else holds
        is referenced twice here - once by the ring list, once by the local
        `buf` - and getrefcount adds its own argument on top, so 3 means
        free and 4 or more means in flight. That threshold was measured, not
        assumed.

        When every slot is busy the answer is a fresh array: slower for that
        one frame, and always correct.
        """
        n = len(self._out_ring)
        for _ in range(n):
            buf = self._out_ring[self._out_slot % n]
            self._out_slot = (self._out_slot + 1) % n
            if sys.getrefcount(buf) <= 3:
                return buf
        if n < OUT_RING_SLOTS:
            buf = np.empty_like(self._out_buf)
            self._out_ring.append(buf)
            return buf
        return np.empty_like(self._out_buf)

    def read_out(self) -> np.ndarray | None:
        """A copy of the frame from the section, guarded by the seqlock.

        The copy is mandatory: there is one slot, the worker overwrites it
        with the next frame, and the frame outlives that - it goes into the
        encoder queue. The seqlock (first 8 bytes) detects a torn frame: if
        the worker is mid-write (odd) or the sequence changed while we
        copied, we retry a few times and then fall back to None (the caller
        skips the frame).

        The destination comes from a REUSED ring, not from a fresh
        allocation. A 4K frame is 33 MB and `.copy()` mapped a new one every
        time: measured with four frames held alive (the recorder's queue
        depth), 12.0 ms per frame against 2.6 ms into a pre-allocated buffer
        - 2.8 GB/s against 12.7 GB/s. The difference is page faults on
        freshly mapped memory, not the memcpy. And it runs on the reader
        thread INSIDE the recv the main loop is blocked on, so it was ~9 ms
        of every recorded frame: the "recv 17.5 -> 24.5 ms while recording"
        left over in TECHNICAL.md after the pipe copy was removed is this.
        """
        if self._out_buf is None:
            return None
        buf = self._next_out_slot()
        for _ in range(4):
            seq1 = int.from_bytes(self._out_mm[0:8], "little")
            if seq1 & 1:
                continue  # worker is writing - not ready yet
            np.copyto(buf, self._out_buf)
            seq2 = int.from_bytes(self._out_mm[0:8], "little")
            if seq1 == seq2:
                return buf
        return None  # torn after retries - caller skips the frame

    def close_out(self) -> None:
        if self._out_buf is not None:
            self._out_buf = None
        if self._out_mm is not None:
            try:
                self._out_mm.close()
            except Exception:
                pass
            self._out_mm = None
        self.out_bytes = 0
        self.out_w = self.out_h = 0
        # The ring is shaped like the section that just closed - a new one
        # means new dimensions, so the slots go with it.
        self._out_ring = []
        self._out_slot = 0

    def read_gray(self) -> np.ndarray | None:
        """Return a copy of the gray frame (320x180 uint8), or None if it is
        not open.

        The worker writes with memcpy and no shared barrier - a tear is
        theoretically possible. At 320x180 that is microseconds; one torn
        optical-flow frame is not critical (guides survive it and the next
        frame fixes it). An accepted risk - a seqlock would be overengineering.
        """
        if self._gray_buf is None:
            return None
        return self._gray_buf.copy()

    def close_gray(self) -> None:
        if self._gray_buf is not None:
            self._gray_buf = None
        if self._gray_mm is not None:
            try:
                self._gray_mm.close()
            except Exception:
                pass
            self._gray_mm = None

    def put(self, rgba: np.ndarray, motion: np.ndarray) -> None:
        """Put the frame and motion into the mapping (one memcpy each)."""
        if self._buf is None:
            # The buffer was closed. Say so: this used to fall through to a
            # bare "NoneType is not subscriptable" from the copyto below, which
            # says nothing about what actually happened (audit H3).
            raise ValueError("the shared frame buffer is closed")
        color = rgba.reshape(-1)
        if color.nbytes > self.color_capacity:
            raise ValueError(f"a frame of {color.nbytes} B does not fit into "
                             f"{self.color_capacity} B of shared memory")
        mv = motion.reshape(-1).view(np.uint8)
        if mv.nbytes > self.motion_capacity:
            raise ValueError(f"motion of {mv.nbytes} B does not fit into "
                             f"{self.motion_capacity} B of shared memory")
        np.copyto(self._buf[:color.nbytes], color)
        off = self.color_capacity
        np.copyto(self._buf[off:off + mv.nbytes], mv)

    def close(self) -> None:
        self.negotiated = False
        # Every section this object opened, in one place (audit H3). close()
        # used to release the input and the gray channel only, and the reverse
        # pixel channel (ON by default, opened once per pipeline build) stayed
        # mapped: one 33 MB named section plus its mapping per rebuild, ~264 MB
        # after eight monitor/window switches, none of it readable or
        # releasable. close_out() already did the right thing - it just had no
        # caller in the program.
        self.close_out()
        self.close_gray()
        self._buf = None  # numpy holds the buffer: without the reset mmap.close() raises BufferError
        try:
            self._mm.close()
        except Exception as exc:
            print(f"[main] could not close the shared memory: {exc}", file=sys.stderr)


def _negotiate_shm(worker: subprocess.Popen, reader: "WorkerReader",
                   shm: SharedFrameBuffer, timeout: float = 10.0) -> None:
    """Hand the shared memory name to the worker (SHMI) and wait for SACK.

    A refusal is not fatal: if the worker could not open the mapping we stay
    on sending the frame down the pipe - that path is still there and works.
    """
    shm.negotiated = False
    try:
        worker.stdin.write(struct.pack(
            SHM_FMT, SHM_MAGIC, shm.color_capacity, shm.motion_capacity, 0, 0,
            shm.name.encode("ascii")))
        worker.stdin.flush()
        reader.wait_sack(timeout)
        shm.negotiated = True
        print(f"[main] shared memory agreed: {shm.size / 1e6:.1f} MB, "
              f"the frame does not go through the pipe")
    except Exception as exc:
        print(f"[main] shared memory unavailable ({exc}) - frames through the pipe",
              file=sys.stderr)


# --- Worker protocol (matches dlss5_converter/core.py) -------------------
# v3 (magic D5V3): a header with full_w/full_h - the worker resizes the frames
# on the GPU itself (NGX Upscaling), Python does not resize on the CPU.
VIDEO_MAGIC = 0x33563544  # 'DV5' v3
CAPTURE_MAGIC = 0x31504143  # CAP1: prepare capture before calculating motion
FRAME_FLAG_PREPARED = 0x1000

FRAME_MAGIC = 0x314D5246  # 'FMR1'
OUT_MAGIC = 0x3154554F    # 'OUT1'
OUT_STATUS_OK = 0x1
# A FRAME_FLAG_WORKER_SCENE frame's reply: the worker's scene score rides in
# bits 16-31 (x65535), and SCENE_CUT says the frame was reset on it.
OUT_STATUS_SCENE = 0x4
OUT_STATUS_SCENE_CUT = 0x8

# CACK: the worker's explicit verdict for the CreateFeature performed from
# the initial VIDEO header.  A full RGBA frame is not enough evidence: on an
# unsupported GPU the worker deliberately stays alive in SAFE PASSTHROUGH.
CREATE_ACK_MAGIC = 0x4B434143
CREATE_ACK_FMT = "<4Iq"  # magic, ok, ngx_result, category, pts
CREATE_CATEGORY_NONE = 0
CREATE_CATEGORY_UNSUPPORTED = 1
CREATE_CATEGORY_FAILED = 2

HEADER_FMT = "<10I4f2I"   # magic, w, h, warmup, frame_count, profile, preset,
                          # style, auto_mask, ui_correction, intensity,
                          # local_tone, local_structure, skin_structure,
                          # full_w, full_h
FRAME_FMT = "<4Iq"        # magic, index, reset, reserved, pts
OUT_FMT = "<5Iq"          # magic, index, ok, bytes, ngx_result, pts


@dataclass(frozen=True)
class FrameReply:
    """One OUT1 reply, including work that intentionally did not run."""

    pixels: np.ndarray | None
    ngx_result: int
    # The worker's scene score for a FRAME_FLAG_WORKER_SCENE frame that
    # captured something; None otherwise. scene_cut: it reset on it.
    scene: float | None = None
    scene_cut: bool = False

# SHMI: the frame travels through shared memory and only the FRM1 header with
# the FRAME_FLAG_SHM flag goes down the pipe. The worker loads the pixels into
# a texture straight from the mapping - two 33 MB copies disappear (the write
# into the pipe and the read out of it).
SHM_MAGIC = 0x494D4853      # 'SHMI'
SHM_ACK_MAGIC = 0x4B434153  # 'SACK'
SHM_FMT = "<4Iq64s"         # magic, color_bytes, motion_bytes, flags, pts, name (88 bytes)
SHM_ACK_FMT = "<4Iq"        # magic, ok, reserved0, reserved1, pts (24 bytes)
FRAME_FLAG_SHM = 0x1         # a bit in the reserved field of the frame header
FRAME_FLAG_WANT_PIXELS = 0x2  # return the pixels even in window mode (for a screenshot)
FRAME_FLAG_MOTION_SMALL = 0x4  # motion field at flow resolution, upscaled by the worker
FRAME_FLAG_SPLIT = 0x20        # before/after wipe; position in the high 16 bits of reserved
# Answer once the frame is on the GPU queue rather than once it is presented:
# the loop's own work (the HUD, commands, the next capture request) then runs
# while the GPU finishes the frame. The worker honours it only where it is
# safe - a processed frame presented by the worker with no pixels coming back.
FRAME_FLAG_EARLY_REPLY = 0x2000

# Leave the scene cut to the worker. With NVOFA the only use the loop had for
# the capture before sending a frame was the scene score - mean(|gray -
# previous|)/255 > 0.24 - and fetching it (CAP1 -> Python -> FRM1) left the
# GPU idle ~2 ms a frame. The worker scores the same gray as it captures and
# sets the reset itself; the reply brings the score back for the status line.
FRAME_FLAG_WORKER_SCENE = 0x4000

# MOTS: the motion field arrives at the optical-flow resolution (~320x180) and
# the worker upscales it to the work resolution on the GPU. The CPU is spared
# a resize and the conversion of 6 million values - ~8 ms per frame measured.
MOTION_MAGIC = 0x53544F4D      # 'MOTS'
MOTION_ACK_MAGIC = 0x4B43414D  # 'MACK'
MOTION_FMT = "<4Iq"            # magic, width, height, flags, pts (24 bytes)
MOTION_ACK_FMT = "<4Iq"


# WNDO: the worker shows the result itself, in its own window above the
# screen. While that window is up OUT1 arrives with bytes=0 - no pixels come
# back to Python at all, and the worker's readback, the reverse pipe and the
# pygame blit all disappear.
WINDOW_MAGIC = 0x4F444E57      # 'WNDO'
WINDOW_ACK_MAGIC = 0x4B434157  # 'WACK'
WINDOW_FMT = "<4Iq"            # magic, width, height, flags, pts (24 bytes)
WINDOW_ACK_FMT = "<4Iq"        # magic, ok, reserved0, reserved1, pts
WINDOW_FLAG_CAPTURABLE = 0x1   # debug: do NOT hide the window from screen capture
WINDOW_FLAG_DISABLE = 0x2      # close the window, go back to sending pixels

# PPRM: per-pass NR parameters. The cascade runs the network over one frame
# more than once, and until now every pass got the SAME numbers - `send_resize`
# carries one set and the worker memcpy's it into g_video_options, which both
# passes read. So a second pass repeated the first exactly: the picture gained
# nothing and the frame rate paid for it (the second pass costs about a third
# of the frame rate, measured).
#
# This is a separate message rather than more fields on RNSZ on purpose.
# RESIZE_FMT mirrors HEADER_FMT field for field, and seven tests build it by
# POSITION - widening it would have to move in step with the worker's
# VideoResizeCmd struct and every one of those tests at once. A new command
# costs none of that.
#
# A worker that never receives PPRM keeps exactly today's behaviour: one set
# of parameters for every pass (pass 2+ falls back to the main set). That is
# also what makes the feature additive - an older client stays correct.
#
# The set applies to passes 2..N; pass 1 always uses the main parameters,
# because that is the pass the user's profile describes.
PER_PASS_MAGIC = 0x4D525050     # 'PPRM'
PER_PASS_ACK_MAGIC = 0x50414150  # 'PAAP' - worker -> client reply to PPRM
# magic, flags, style, auto_mask (4 x uint32 = 16) + the four strengths
# (4 x float = 16) + pts (int64 = 8) = 40 bytes. The field order is NOT
# arbitrary: the worker's struct holds an int64_t, so the 4-byte fields
# before it must add up to a multiple of 8 or C++ inserts four bytes of
# padding the Python format knows nothing about, and every struct.pack of
# this command lands a frame's worth of fields out of step.
PER_PASS_FMT = "<4I4fq"
PER_PASS_ACK_FMT = "<4Iq"       # magic, ok, reserved0, reserved1, pts
PER_PASS_FLAG_ENABLED = 0x1     # passes 2+ use this set; clear = fall back to main

# RNSZ: change the work resolution on the fly (without restarting the worker
# process). The worker recreates the NGX feature at the new sizes and answers
# RACK.
RESIZE_MAGIC = 0x5A534E52  # 'RNSZ'
RESIZE_ACK_MAGIC = 0x4B434152  # 'RACK'
RESIZE_FMT = "<10I4f2I"   # the same layout as HEADER_FMT (magic instead of VIDEO_MAGIC)
# The slot the header keeps frame_count in carries flags in a resize.
RESIZE_FLAG_NR_SMALL = 0x1   # run the network at the work size, scale the result back
# Show the network's own output, stretched, instead of composing its delta
# onto the native frame. Only means anything with NR_SMALL on. Travels with
# the resize so that flipping it costs no feature - it is an A/B switch.
RESIZE_FLAG_NR_DIRECT = 0x2
#: How many NR passes run over one frame, 1-4, in bits 2-4 of the same flags
#: word. Zero reads as one pass on the worker, so the field can be absent.
#: Each pass is its own NGX feature with its own temporal history - one
#: feature called twice gets two evaluations with no motion between them.
RESIZE_FLAG_NR_PASSES_SHIFT = 2
NR_MAX_PASSES = 4
RACK_FMT = "<4Iq"         # magic, ok, ngx_result, reserved, pts (24 bytes)

# DDA1: the worker captures the screen itself (Desktop Duplication) - the
# colour goes straight into a GPU texture and Python no longer ships 33 MB per
# frame. FRM1 frames go out with FRAME_FLAG_NO_COLOR: motion only, no colour.
DDA_MAGIC = 0x31414444  # 'DDA1'
WGC_MAGIC = 0x57434757      # 'WGCW' - capture ONE window instead of the desktop
WGC_ACK_MAGIC = 0x4B414757  # 'WGAK' - its acknowledgement, with the real capture size
WGC_FMT = "<4IqQ"           # magic, width, height, flags, pts, hwnd
WGC_ACK_FMT = "<4Iq"        # magic, ok, width, height, pts
DDA_ACK_MAGIC = 0x4B434144  # 'DACK'
DDA_FMT = "<4Iq"        # magic, width, height, flags, pts (24 bytes)
DDA_ACK_FMT = "<4Iq"    # magic, ok, reserved0, reserved1, pts
FRAME_FLAG_NO_COLOR = 0x8  # in DDA mode: we send no colour (the worker takes it)
FRAME_FLAG_BYPASS = 0x10  # NR OFF: skip NGX, show the raw capture

# GRAY: the worker writes luminance (a downsample of the screen, ~320x180)
# into a reverse mapping for Python - for the guides' optical flow. In DDA
# mode this replaces the dxcam grab: the gray frame comes straight off the GPU.
# OUTS: the reverse channel for PIXELS. A 4K recorded frame weighs 33 MB, and
# through the pipe that is ~7 ms per frame (measured: recv 17.4 -> 31.6 ms
# when recording is switched on). Through shared memory those bytes never
# travel down the pipe.
OUTS_MAGIC = 0x5354554F      # 'OUTS'
OUTS_ACK_MAGIC = 0x324B414F  # 'OAK2'
OUTS_FMT = "<4Iq64s"         # like GRAY_FMT: magic, w, h, flags, pts, name
OUTS_ACK_FMT = "<4Iq"
# VideoResultHeader.bytes: the pixels are in the OUTS section, not in the pipe.
OUT_BYTES_IN_SHM = 0xFFFFFFFF

GRAY_MAGIC = 0x59415247  # 'GRAY'
GRAY_ACK_MAGIC = 0x4B434147  # 'GAK'
GRAY_FMT = "<4Iq64s"    # magic, width, height, flags, pts, name (88 bytes)
GRAY_ACK_FMT = "<4Iq"   # magic, ok, reserved0, reserved1, pts

# GPU recording (native/gpu_recorder.cpp): the worker encodes the frame the
# viewer sees on the card, so no recorded pixel crosses the pipe. The client
# supplies only the sound, as 16-bit PCM in a ring it names in RECS.
REC_START_MAGIC = 0x53434552      # 'RECS'
REC_START_ACK_MAGIC = 0x4B415352  # 'RSAK'
REC_STOP_MAGIC = 0x45434552       # 'RECE'
REC_DONE_MAGIC = 0x4B414552       # 'REAK' - also sent unasked when the encoder fails
# magic, fps, codec, bitrate, pts, start_qpc, flags, reserved, ring name,
# UTF-8 path (1128)
REC_START_FMT = "<4Iqq2I64s1024s"
# magic, ok, codec, hresult, pts, origin_qpc, width, height, fps, audio (48)
REC_START_ACK_FMT = "<4Iqq4I"
REC_STOP_FMT = "<4Iq"             # magic, reserved x3, pts (24)
# magic, ok, written, dropped, pts, hresult, duration_ms, audio_frames, codec (40)
REC_DONE_FMT = "<4Iq4I"
REC_CODEC_AUTO, REC_CODEC_H264, REC_CODEC_HEVC, REC_CODEC_AV1 = 0, 1, 2, 3
# RECS flags: record HDR10 where the worker's frames are HDR.
REC_FLAG_HDR = 0x1
# Or-ed into RSAK's and REAK's codec when the file is HDR10 (10-bit, BT.2020,
# PQ): the name is REC_CODEC_NAMES[codec & 0xFF].
REC_CODEC_HDR10 = 0x100
REC_CODEC_NAMES = {REC_CODEC_H264: "H.264", REC_CODEC_HEVC: "HEVC",
                   REC_CODEC_AV1: "AV1"}
# The PCM ring's header (GpuRecAudioRing): magic, rate, channels, capacity,
# frames written ever, reserved. The samples follow it, int16 interleaved.
AUDIO_RING_MAGIC = 0x474E5241     # 'ARNG'
AUDIO_RING_FMT = "<4Iqq"


def _read_exact(stream, size: int) -> bytes:
    """Read exactly size bytes from the stream (the worker may give fewer)."""
    chunks = bytearray()
    while len(chunks) < size:
        block = stream.read(size - len(chunks))
        if not block:
            raise EOFError(f"the worker stopped after {len(chunks)} of {size} reply bytes")
        chunks.extend(block)
    return bytes(chunks)



def prepare_capture(worker, reader, index: int, pts: int) -> None:
    """Latch capture and gray together; FRM1 will consume that exact capture."""
    worker.stdin.write(struct.pack(FRAME_FMT, CAPTURE_MAGIC, index, 0, 0, pts))
    worker.stdin.flush()
    reader.recv(index, timeout=5.0)


def send_frame(worker: subprocess.Popen, index: int, rgba: np.ndarray,
               motion: np.ndarray, reset: bool, pts: int,
               shm: "SharedFrameBuffer | None" = None,
               want_pixels: bool = False, motion_small: bool = False,
               no_color: bool = False, bypass: bool = False,
               split: float = 0.0,
               frame_generation: bool | None = None, frame_multiplier: int = 2,
               prepared: bool = False, early_reply: bool = False,
               worker_scene: bool = False) -> None:
    """Send a frame to the worker.

    With shared memory agreed, only the 24-byte header with the
    FRAME_FLAG_SHM flag goes down the pipe and the pixels are placed into the
    mapping. Otherwise it is the old path: header + RGBA8 + motion float16
    sent inline through the pipe.

    no_color (DDA mode): the worker takes the colour itself from Desktop
    Duplication - only motion goes down the pipe, rgba is ignored.
    bypass (NR OFF): the worker skips NGX and shows the raw capture - the
    overlay (window, HUD) stays alive while the effect is off.
    split (0..1): the share of the frame on the left the worker leaves
    unprocessed - the before/after wipe. 0 means off.
    """
    flags = (FRAME_FLAG_WANT_PIXELS if want_pixels else 0) | \
            (FRAME_FLAG_MOTION_SMALL if motion_small else 0) | \
            (FRAME_FLAG_NO_COLOR if no_color else 0) | \
            (FRAME_FLAG_BYPASS if bypass else 0)
    if prepared:
        flags |= FRAME_FLAG_PREPARED
    if early_reply:
        flags |= FRAME_FLAG_EARLY_REPLY
    if worker_scene:
        flags |= FRAME_FLAG_WORKER_SCENE
    if frame_generation is not None:
        # Bits 8-11: enabled, multiplier minus two, explicit UI override.
        flags |= 0x800 | (0x100 if frame_generation else 0)
        flags |= (min(4, max(2, int(frame_multiplier))) - 2) << 9
    if split > 0.0:
        # The wipe position rides in the high 16 bits of the same flags field:
        # there is no dedicated field in the header, and widening it for a
        # single number would mean changing the protocol on both sides.
        frac = min(0xFFFF, max(0, int(round(min(1.0, split) * 0xFFFF))))
        flags |= FRAME_FLAG_SPLIT | (frac << 16)
    if no_color:
        # DDA mode: motion only, no colour (SHM is not used for colour)
        worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index, int(reset), flags, pts))
        worker.stdin.write(motion.tobytes())
        worker.stdin.flush()
        return
    if shm is not None and shm.negotiated:
        shm.put(rgba, motion)
        worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index, int(reset),
                                       FRAME_FLAG_SHM | flags, pts))
        worker.stdin.flush()
        return
    worker.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, index, int(reset), flags, pts))
    worker.stdin.write(rgba.tobytes())
    worker.stdin.write(motion.tobytes())
    worker.stdin.flush()


def send_resize(worker: subprocess.Popen, params: dict, width: int, height: int,
                warmup: int, full_w: int = 0, full_h: int = 0,
                nr_small: bool = False, nr_direct: bool = False,
                nr_passes: int = 1) -> None:
    """Send RNSZ - change the work resolution/parameters on the fly.

    The worker recreates the NGX feature at the new sizes (ReleaseFeature ->
    CreateFeature inside the same process) and answers RACK. A process restart
    is not needed - a restart was exactly what caused the hangs and crashes
    (exit 127).
    """
    worker.stdin.write(struct.pack(
        RESIZE_FMT,
        RESIZE_MAGIC, width, height, int(warmup),
        (RESIZE_FLAG_NR_SMALL if nr_small else 0)
        | (RESIZE_FLAG_NR_DIRECT if nr_direct else 0)
        | (min(NR_MAX_PASSES, max(1, int(nr_passes)))
           << RESIZE_FLAG_NR_PASSES_SHIFT),
        # profile, preset and ui_correction: sent, and sent as zero. All
        # three are dead in the 310.8.0 runtime - every value gives a
        # byte-identical frame - so they are not carried in the profiles
        # any more. The wire keeps its shape because the resize command
        # shares this layout and a hundred tests build it by position.
        0, 0, params["style"],
        params["auto_mask"], 0,
        params["intensity"], params["local_tone"],
        params["local_structure"], params["skin_structure"],
        int(full_w), int(full_h),
    ))
    worker.stdin.flush()


def send_per_pass(worker: subprocess.Popen, params: dict | None,
                  enabled: bool = True, pts: int = 0) -> None:
    """PPRM: give passes 2..N their own NR parameters.

    `params` is a whole parameter set, shaped like a profile (style,
    auto_mask, intensity, local_tone, local_structure, skin_structure).
    `None` or enabled=False clears it: every pass goes back to the main set,
    which is also what a worker that never hears PPRM does.

    Pass 1 is not affected - it stays on the profile the user picked.

    No feature is recreated: style and the strengths are read at EVALUATE
    time (CreateFeature only takes sizes and the preset hint), so this
    command is a parameter change and nothing else.
    """
    p = params or {}
    flags = PER_PASS_FLAG_ENABLED if (enabled and params) else 0
    worker.stdin.write(struct.pack(
        PER_PASS_FMT,
        PER_PASS_MAGIC, int(flags), int(p.get("style", 0)),
        int(p.get("auto_mask", 1)),
        float(p.get("intensity", 1.0)), float(p.get("local_tone", 0.0)),
        float(p.get("local_structure", 1.0)), float(p.get("skin_structure", -1.0)),
        int(pts),
    ))
    worker.stdin.flush()


def send_motion_size(worker: subprocess.Popen, width: int, height: int,
                     flags: int = 0, pts: int = 0) -> None:
    """MOTS: at what resolution the motion field will arrive.

    0x0 turns it off: the field goes back to the work resolution.
    """
    worker.stdin.write(struct.pack(MOTION_FMT, MOTION_MAGIC, int(width), int(height),
                                   int(flags), int(pts)))
    worker.stdin.flush()


def send_window(worker: subprocess.Popen, width: int, height: int,
                flags: int = 0, pts: int = 0) -> None:
    """WNDO: ask the worker to raise its own output window (or close it).

    width=height=0 or the WINDOW_FLAG_DISABLE flag closes the window and goes
    back to sending pixels through the pipe.
    """
    worker.stdin.write(struct.pack(WINDOW_FMT, WINDOW_MAGIC, int(width), int(height),
                                   int(flags), int(pts)))
    worker.stdin.flush()


def send_dda(worker: subprocess.Popen, width: int, height: int,
             flags: int = 0, pts: int = 0) -> None:
    """DDA1: ask the worker to capture the screen itself (Desktop Duplication).

    width=height=0 turns the capture off and goes back to sending the frame
    from Python. While it is active FRM1 frames carry FRAME_FLAG_NO_COLOR
    (motion only).
    """
    worker.stdin.write(struct.pack(DDA_FMT, DDA_MAGIC, int(width), int(height),
                                   int(flags), int(pts)))
    worker.stdin.flush()


def send_wgc(worker: subprocess.Popen, hwnd: int, width: int = 0,
             height: int = 0, pts: int = 0) -> None:
    """WGCW: ask the worker to capture ONE window instead of the desktop.

    Windows Graphics Capture of a single window is unaffected by whatever is
    drawn on top of it, so there is no self-capture loop - which is the whole
    reason for this mode: the overlay no longer has to hide from screen
    capture, and an outside recorder can see it. hwnd = 0 turns it off.
    """
    worker.stdin.write(struct.pack(WGC_FMT, WGC_MAGIC, int(width), int(height),
                                   0, int(pts), int(hwnd)))
    worker.stdin.flush()


def send_gray(worker: subprocess.Popen, width: int, height: int,
              name: str, flags: int = 0, pts: int = 0) -> None:
    """GRAY: give the worker the name of the reverse mapping for luminance.

    In DDA mode the worker writes a downsample of the screen there (width x
    height, usually 320x180 = the flow field size) and Python reads it for
    guides. width=height=0 turns the reverse channel off.
    """
    if len(name) >= 64:
        raise ValueError("the gray section name is longer than 63 characters")
    worker.stdin.write(struct.pack(GRAY_FMT, GRAY_MAGIC, int(width), int(height),
                                   int(flags), int(pts), name.encode("ascii")))
    worker.stdin.flush()


def send_rec_start(worker: subprocess.Popen, path: str, *, fps: int,
                   codec: int = REC_CODEC_AUTO, bitrate: int = 0,
                   start_qpc: int = 0, audio_ring: str = "",
                   pts: int = 0, hdr: bool = False) -> None:
    """RECS: record the frame the viewer sees into `path`, on the GPU.

    The worker answers with RSAK (WorkerReader.rec_started). `audio_ring` is
    the name of an AudioRing section, "" for a silent file; `start_qpc` is
    the QueryPerformanceCounter reading that the ring's frame 0 belongs to.
    `hdr` allows HDR10: the worker records it when its frames are HDR, and
    says so in RSAK (REC_CODEC_HDR10).
    """
    raw_path = str(path).encode("utf-8")
    if len(raw_path) >= 1024:
        raise ValueError("the recording path is longer than 1023 bytes")
    if len(audio_ring) >= 64:
        raise ValueError("the audio ring name is longer than 63 characters")
    worker.stdin.write(struct.pack(
        REC_START_FMT, REC_START_MAGIC, int(fps), int(codec), int(bitrate),
        int(pts), int(start_qpc), REC_FLAG_HDR if hdr else 0, 0,
        audio_ring.encode("ascii"), raw_path))
    worker.stdin.flush()


def send_rec_stop(worker: subprocess.Popen, pts: int = 0) -> None:
    """RECE: stop the GPU recording; the worker answers with REAK."""
    worker.stdin.write(struct.pack(REC_STOP_FMT, REC_STOP_MAGIC, 0, 0, 0,
                                   int(pts)))
    worker.stdin.flush()


@dataclass(frozen=True)
class RecStartReply:
    """RSAK: whether the worker is recording, and how."""

    ok: bool
    codec: int
    hresult: int
    origin_qpc: int
    width: int
    height: int
    fps: int
    audio: bool


@dataclass(frozen=True)
class RecDoneReply:
    """REAK: the file is closed - what went into it."""

    ok: bool
    written: int
    dropped: int
    hresult: int
    duration_ms: int
    audio_frames: int
    codec: int


def send_out(worker: subprocess.Popen, width: int, height: int,
             name: str, flags: int = 0, pts: int = 0) -> None:
    """OUTS: give the worker the name of the section for the result pixels.

    width=height=0 turns the channel off and the pixels travel inline through
    the pipe again.
    """
    if len(name) >= 64:
        raise ValueError("the out section name is longer than 63 characters")
    worker.stdin.write(struct.pack(OUTS_FMT, OUTS_MAGIC, int(width), int(height),
                                   int(flags), int(pts), name.encode("ascii")))
    worker.stdin.flush()


class WorkerReader:
    """The permanent reader thread for the worker's stdout (one per worker).

    Created in start_worker, it lives as long as the worker does and dies on
    EOF: shutdown_worker terminates the process -> the pipe closes -> read()
    returns b"" -> _read_exact raises EOFError -> a sentinel goes into the
    queue.

    A replacement for the old recv_frame (a thread per EVERY frame): on a
    timeout the reader thread does NOT hang on read() - it keeps reading the
    following frames, main simply did not get its answer in time. On a restart
    the old reader dies on the EOF of the old stdout and physically cannot
    read the data of the new worker (different pipes) - there is no read race.
    """

    def __init__(self, worker: subprocess.Popen, width: int, height: int,
                 shm: "SharedFrameBuffer | None" = None):
        self._worker = worker
        self._width = width
        self._height = height
        self.last_ngx_result = 0
        self.last_scene: float | None = None
        self.last_scene_cut = False
        # The pixels arrive through it once the OUTS channel is agreed.
        self._shm = shm
        self._queue: queue.Queue = queue.Queue()
        # The GPU recording's answers do not go through the queue: recv()
        # drops what it is not waiting for, and a REAK can come at any moment
        # (the worker closes a recording whose encoder failed by itself). The
        # reply is stored first and the event set after it; both events are
        # also set when the reader dies, with no reply - "the worker is gone".
        self.rec_started = threading.Event()
        self.rec_start_reply: RecStartReply | None = None
        self.rec_done = threading.Event()
        self.rec_done_reply: RecDoneReply | None = None
        # Frame replies a wait_* met while waiting for its acknowledgement.
        # Commands go out from inside the frame loop (a hotkey handled during
        # recv), so the reply of the frame in flight can arrive in the middle
        # of a probe - and dropping it left recv waiting for an answer that
        # had already come and gone: 5 s, then a worker restart counted
        # towards the three that turn NR off. recv reads these first.
        self._frames: collections.deque = collections.deque()
        # Acknowledgements whose wait timed out: when one arrives after all,
        # it belongs to that command, not to the next one of the same kind.
        # Every command goes out with pts 0, so the reply itself cannot say.
        self._orphans: dict[str, int] = {}
        # Set by the reader when it stops understanding the worker (a protocol
        # error), before it goes on draining the pipe - see _run.
        self._failed = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="worker-reader")
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                magic_raw = _read_exact(self._worker.stdout, 4)
                magic = struct.unpack("<I", magic_raw)[0]
                if magic == MOTION_ACK_MAGIC:
                    # MACK: acknowledgement of MOTS
                    rest = _read_exact(self._worker.stdout, struct.calcsize(MOTION_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(MOTION_ACK_FMT, magic_raw + rest)
                    self._queue.put(("mack", ok))
                elif magic == CREATE_ACK_MAGIC:
                    rest = _read_exact(
                        self._worker.stdout, struct.calcsize(CREATE_ACK_FMT) - 4)
                    _magic, ok, ngx_result, category, _pts = struct.unpack(
                        CREATE_ACK_FMT, magic_raw + rest)
                    self._queue.put(("cack", (ok, ngx_result, category)))
                elif magic == WINDOW_ACK_MAGIC:
                    # WACK: acknowledgement of WNDO - the window is up or closed
                    rest = _read_exact(self._worker.stdout, struct.calcsize(WINDOW_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(WINDOW_ACK_FMT, magic_raw + rest)
                    self._queue.put(("wack", ok))
                elif magic == PER_PASS_ACK_MAGIC:
                    # PAAP: acknowledgement of PPRM - passes 2+ have their own
                    # parameters now (or have gone back to the main set)
                    rest = _read_exact(self._worker.stdout, struct.calcsize(PER_PASS_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(PER_PASS_ACK_FMT, magic_raw + rest)
                    self._queue.put(("paap", ok))
                elif magic == SHM_ACK_MAGIC:
                    # SACK: acknowledgement of SHMI - the worker opened the mapping
                    rest = _read_exact(self._worker.stdout, struct.calcsize(SHM_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(SHM_ACK_FMT, magic_raw + rest)
                    self._queue.put(("sack", ok))
                elif magic == RESIZE_ACK_MAGIC:
                    # RACK (24 bytes): acknowledgement of RNSZ - we put it in
                    # the queue, main takes it via wait_rack()
                    rest = _read_exact(self._worker.stdout, struct.calcsize(RACK_FMT) - 4)
                    _magic, ok, ngx_result, _reserved, _pts = struct.unpack(RACK_FMT, magic_raw + rest)
                    self._queue.put(("rack", (ok, ngx_result)))
                elif magic == DDA_ACK_MAGIC:
                    # DACK (24 bytes): acknowledgement of DDA1 - capture moved to the worker
                    rest = _read_exact(self._worker.stdout, struct.calcsize(DDA_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(DDA_ACK_FMT, magic_raw + rest)
                    self._queue.put(("dack", ok))
                elif magic == WGC_ACK_MAGIC:
                    # WGAK (24 bytes): acknowledgement of WGCW. It carries the
                    # size the window capture really produces - physical
                    # pixels, which is what the pipeline has to be built for.
                    rest = _read_exact(self._worker.stdout, struct.calcsize(WGC_ACK_FMT) - 4)
                    _magic, ok, aw, ah, _pts = struct.unpack(WGC_ACK_FMT, magic_raw + rest)
                    self._queue.put(("wgak", (ok, aw, ah)))
                elif magic == OUTS_ACK_MAGIC:
                    rest = _read_exact(self._worker.stdout,
                                       struct.calcsize(OUTS_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(
                        OUTS_ACK_FMT, magic_raw + rest)
                    self._queue.put(("outs", ok))
                elif magic == GRAY_ACK_MAGIC:
                    # GAK: acknowledgement of GRAY - the reverse luminance channel is open
                    rest = _read_exact(self._worker.stdout, struct.calcsize(GRAY_ACK_FMT) - 4)
                    _magic, ok, _r0, _r1, _pts = struct.unpack(GRAY_ACK_FMT, magic_raw + rest)
                    self._queue.put(("gak", ok))
                elif magic == REC_START_ACK_MAGIC:
                    rest = _read_exact(self._worker.stdout,
                                       struct.calcsize(REC_START_ACK_FMT) - 4)
                    (_magic, ok, codec, hresult, _pts, origin, width, height,
                     fps, audio) = struct.unpack(REC_START_ACK_FMT, magic_raw + rest)
                    self.rec_start_reply = RecStartReply(
                        bool(ok), codec, hresult, origin, width, height, fps,
                        bool(audio))
                    self.rec_started.set()
                elif magic == REC_DONE_MAGIC:
                    rest = _read_exact(self._worker.stdout,
                                       struct.calcsize(REC_DONE_FMT) - 4)
                    (_magic, ok, written, dropped, _pts, hresult, duration_ms,
                     audio_frames, codec) = struct.unpack(REC_DONE_FMT, magic_raw + rest)
                    self.rec_done_reply = RecDoneReply(
                        bool(ok), written, dropped, hresult, duration_ms,
                        audio_frames, codec)
                    self.rec_done.set()
                elif magic == OUT_MAGIC:
                    rest = _read_exact(self._worker.stdout, struct.calcsize(OUT_FMT) - 4)
                    _magic, out_index, status, byte_count, ngx_result, _pts = struct.unpack(OUT_FMT, magic_raw + rest)
                    if not (status & OUT_STATUS_OK):
                        raise RuntimeError(
                            f"worker answered with an error for frame {out_index}: status={status}")
                    scene = ((status >> 16) / 65535.0
                             if status & OUT_STATUS_SCENE else None)
                    scene_cut = bool(status & OUT_STATUS_SCENE_CUT)
                    # The NGX result travels with every frame; the loop reads it
                    # to tell "the pass ran" from "the pass did not run". It is
                    # NOT an error channel on its own: 0 means "no evaluation
                    # this frame", which is exactly the state worth surfacing.
                    # The NGX result is not a boolean: 0x00000000 means "no
                    # frame this call" (the network skipped the evaluation -
                    # a laptop on the iGPU, a driver hiccup) and is NOT a
                    # failure. Only the 0xBAD00000 family is a real error
                    # (NVSDK_NGX_FAILED masks the top nibble). Treating
                    # 0x00000000 as a crash restarted the worker three
                    # times and then turned NR off (issue #11, kortul).
                    if (ngx_result & 0xFFF00000) == 0xBAD00000:
                        raise RuntimeError(
                            f"NGX evaluation failed on frame {out_index}: 0x{ngx_result:08X}")
                    if byte_count == 0:
                        # No pixels through the pipe: WNDO mode (the worker
                        # showed the frame in its own window). There is
                        # nothing to show - the pipeline waits for the next
                        # one.
                        self._queue.put((out_index, FrameReply(
                            None, ngx_result, scene, scene_cut)))
                        continue
                    if byte_count == OUT_BYTES_IN_SHM:
                        # The pixels are in the OUTS section. The copy is made
                        # here, in the reader thread: main is waiting for the
                        # frame anyway, and this way the copy does not pile
                        # onto its thread along with everything else.
                        if self._shm is None or self._shm._out_buf is None:
                            raise RuntimeError(
                                "the worker said the pixels are in shared "
                                "memory, but the section is not open")
                        frame = self._shm.read_out()
                        if frame is None:
                            # A torn frame (seqlock retries exhausted): skip
                            # it, but keep the protocol paired - main treats
                            # None as "frame not ready" and moves on.
                            self._queue.put((out_index, FrameReply(
                                None, ngx_result, scene, scene_cut)))
                            continue
                        self._queue.put((out_index, FrameReply(
                            frame, ngx_result, scene, scene_cut)))
                        continue
                    if byte_count != self._width * self._height * 4:
                        raise RuntimeError(
                            f"worker returned {byte_count} bytes instead of {self._width * self._height * 4}")
                    data = _read_exact(self._worker.stdout, byte_count)
                    frame = np.frombuffer(data, dtype=np.uint8).reshape(self._height, self._width, 4)
                    self._queue.put((out_index, FrameReply(
                        frame, ngx_result, scene, scene_cut)))
                else:
                    raise RuntimeError(f"invalid magic in the worker reply: 0x{magic:08X}")
        except Exception as exc:
            # EOF (the worker exited or was killed) or a protocol error - sentinel
            self._failed = True
            self._queue.put((None, exc))
            # Whoever waits on a recording answer learns it will not come.
            self.rec_started.set()
            self.rec_done.set()
            if not isinstance(exc, EOFError):
                # A protocol error stops the reading, not the worker: it may
                # be in the middle of a frame-sized payload into a pipe a few
                # kilobytes deep, and a worker blocked on that write never
                # reads the end of its stdin that tells it to exit - the
                # shutdown then waited 10 s and killed it, skipping the NGX
                # cleanup. The rest of the stream is read and thrown away.
                try:
                    while self._worker.stdout.read(65536):
                        pass
                except Exception:
                    pass

    @property
    def alive(self) -> bool:
        """Whether the reader still reads the worker (the worker is there).

        False from the first error on, even while the thread drains what the
        worker is still writing: nothing it reads counts any more.
        """
        return self._thread.is_alive() and not self._failed

    def set_output_size(self, width: int, height: int) -> None:
        """Change the expected size of the output frames (right after RNSZ)."""
        self._width = width
        self._height = height
        # Frames kept from before the change are the old size's.
        self._frames.clear()

    def _death(self, payload) -> BaseException:
        """The reader's last word, for the wait that met it - and every later one.

        It goes back into the queue: taken once, as it was, the next wait saw
        an empty queue and sat out its whole timeout. A worker dying during
        startup cost the four negotiations after the first 15 s each - about a
        minute of an unresponsive window before anything noticed.
        """
        self._queue.put((None, payload))
        return payload if isinstance(payload, Exception) else EOFError("the worker stopped")

    def _wait(self, tag: str, timeout: float, what: str, *, keep_frames: bool = True):
        """Wait for the acknowledgement `tag`; returns its payload.

        Raises TimeoutError naming `what`, or the reader's error when the
        worker is gone. Frame replies met on the way are kept for recv
        (keep_frames) or dropped (wait_rack: they are the old size's). Any
        other acknowledgement is an answer nobody waits for and is dropped,
        as it always was.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._orphans[tag] = self._orphans.get(tag, 0) + 1
                raise TimeoutError(f"the worker did not acknowledge {what} within {timeout:.0f}s")
            try:
                got, payload = self._queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if got is None:
                raise self._death(payload)
            if got == tag:
                if self._orphans.get(tag, 0) > 0:
                    # The late answer to a command whose wait already gave up.
                    self._orphans[tag] -= 1
                    continue
                return payload
            if keep_frames and isinstance(got, int):
                self._frames.append((got, payload))

    def wait_mack(self, timeout: float) -> None:
        """Wait for MACK - the acknowledgement of the motion field size (MOTS)."""
        if not self._wait("mack", timeout, "MOTS"):
            raise RuntimeError("the worker could not enable GPU motion upscaling")

    def wait_create_ack(self, timeout: float) -> tuple[int, int, int]:
        """Wait for the initial CreateFeature verdict from the VIDEO header."""
        ok, ngx_result, category = self._wait("cack", timeout, "feature creation")
        return int(ok), int(ngx_result), int(category)

    def wait_wack(self, timeout: float) -> None:
        """Wait for WACK - the acknowledgement of the WNDO command."""
        if not self._wait("wack", timeout, "WNDO"):
            raise RuntimeError("the worker could not raise the output window")

    def wait_dack(self, timeout: float) -> None:
        """Wait for DACK - the acknowledgement of DDA1 (capture in the worker)."""
        if not self._wait("dack", timeout, "DDA1"):
            raise RuntimeError("the worker could not enable screen capture")

    def wait_wgak(self, timeout: float) -> tuple:
        """Wait for WGAK - the acknowledgement of WGCW; returns the capture size."""
        ok, aw, ah = self._wait("wgak", timeout, "WGCW")
        if not ok:
            raise RuntimeError("the worker could not capture that window")
        return aw, ah

    def wait_gak(self, timeout: float) -> None:
        """Wait for GAK - the acknowledgement that the reverse gray channel is open."""
        if not self._wait("gak", timeout, "GRAY"):
            raise RuntimeError("the worker could not open the gray channel")

    def wait_oak(self, timeout: float) -> None:
        """Wait for OAK2 - the acknowledgement of the shared-memory pixel channel."""
        if not self._wait("outs", timeout, "OUTS"):
            raise RuntimeError("the worker could not open the pixel channel")

    def wait_sack(self, timeout: float) -> None:
        """Wait for SACK - the shared memory acknowledgement (SHMI)."""
        if not self._wait("sack", timeout, "SHMI"):
            raise RuntimeError("the worker could not open the shared memory")

    def wait_per_pass(self, timeout: float) -> None:
        """Wait for PAAP - the acknowledgement of a per-pass change (PPRM).

        Frames that arrive before it are kept for recv: a per-pass change does
        not change the frame, and the menu sends it from inside the frame loop.
        """
        if not self._wait("paap", timeout, "the per-pass parameters"):
            raise RuntimeError("the worker rejected the per-pass parameters")

    def wait_rack(self, timeout: float) -> None:
        """Wait for RACK - the acknowledgement of a resolution change (RNSZ).

        Frames that arrived before RACK (after a recv timeout) are skipped:
        they are the old size's. A dead worker surfaces at once, as in every
        other wait (audit F7).
        """
        self._frames.clear()
        ok, ngx_result = self._wait("rack", timeout, "the resolution change",
                                    keep_frames=False)
        if not ok:
            raise RuntimeError(f"RNSZ rejected by the worker: ngx=0x{ngx_result:08X}")

    def recv(self, index: int, timeout: float):
        """Wait for frame index; timeout > 0 guards against an NGX hang.

        Returns an np.ndarray with the pixels, or None if the worker showed
        the frame in its own window (WNDO mode) and sent no pixels.

        Replies with a foreign index (frames main no longer waits for after a
        timeout) are dropped - the protocol cannot desynchronise.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self._frames:
                # A reply a wait_* met while this frame was in flight.
                got_index, payload = self._frames.popleft()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"the worker has been silent for {timeout:.0f}s on frame {index} - NGX did not answer after the restart")
                try:
                    got_index, payload = self._queue.get(timeout=remaining)
                except queue.Empty:
                    continue  # the loop raises TimeoutError itself once the deadline passes
            if got_index is None:
                raise self._death(payload)
            if got_index == index:
                if isinstance(payload, FrameReply):
                    self.last_ngx_result = payload.ngx_result
                    self.last_scene = payload.scene
                    self.last_scene_cut = payload.scene_cut
                    return payload.pixels
                # Compatibility for tests and third-party callers that place
                # legacy payloads into the private queue.
                self.last_ngx_result = 0
                self.last_scene = None
                self.last_scene_cut = False
                return payload
