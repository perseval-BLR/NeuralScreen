"""Run a file through the neural pass: an image or a video, off the desktop.

The overlay exists to process what is on screen, which means the one thing
the network could always do - take a picture and give it back better - was
the one thing this program could not do to a FILE. The machinery was already
here: compatibility_runtime drives a worker with synthetic frames and no
capture and no window at all, which is a converter with the file part
missing. This module is that missing part.

What it does NOT do, on purpose:

* It does not touch the live pipeline. It starts its own worker, converts, and
  reaps it; the overlay's worker keeps running. Two workers on one card was
  measured rather than assumed (a second one created and evaluated in 1.17 s
  while the first kept answering) - they share the card, so both run slower
  while a file converts, and that is the whole cost.
* It does not interpret settings. It is handed the same `params` dict the
  overlay builds, so a conversion is the picture the sliders were showing,
  not a second tuning surface that could drift from them. The output choices
  it does take - codec, quality, image format, audio - are about the FILE,
  and the panel has no other place for them.
* It knows nothing about the menu or the queue. Progress is a callback and
  cancellation is an Event, so the engine can be driven by a test, by the
  queue in convert_jobs, or from a shell, and none of those is the "real"
  caller.

The motion field is the part worth understanding. The network is temporal:
it is handed motion vectors and a reset flag, and it accumulates across
frames. A still image is therefore ONE frame with reset=True and zero
motion - there is no history to correlate against and pretending otherwise
smears it. A video is the opposite: the frames are a real sequence, so the
guides run exactly as they do live, and the result is temporally stable for
the same reason the desktop is.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable

import numpy as np

from guides import TemporalGuideGenerator
from pipeline import shutdown_worker, start_worker
from protocol import (SharedFrameBuffer, send_frame, send_motion_size,
                      send_out, send_resize)

#: Stills the converter will open. Kept to what the bundled Pillow can both
#: read and write without extra plugins - a format that opens and then fails
#: to save is a worse experience than one that was never offered.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")

#: Containers PyAV can demux with the codecs in the bundled runtime
#: (h264/hevc/av1/vp9 are all present - verified against av.codecs_available).
VIDEO_SUFFIXES = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv")

#: Pillow's own name for each suffix we offer. Needed because the bytes are
#: written to a ".partial" file first and Pillow derives the format from the
#: extension, which that name does not have.
_PIL_FORMATS = {
    ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".bmp": "BMP",
    ".tif": "TIFF", ".tiff": "TIFF", ".webp": "WEBP",
}

#: The container name for each suffix, for the same reason as _PIL_FORMATS:
#: the bytes go to a ".partial" file first and PyAV, like Pillow, reads the
#: format off the extension - "Could not determine output format" otherwise.
_AV_FORMATS = {
    ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska",
    ".webm": "webm", ".avi": "avi", ".wmv": "asf",
}

#: NVENC encoders, best first - the same chain and the same reason as
#: recorder.VideoRecorder: the RTX 30 series has no AV1 encoder at all, and
#: add_stream() succeeds there anyway, so the failure only surfaces when the
#: encoder is opened.
CODEC_CHAIN = ("av1_nvenc", "hevc_nvenc", "h264_nvenc")

#: What the panel offers for the video codec. "auto" is the chain above; a
#: named codec starts the chain there, so a card without it still produces a
#: file (and the result says which codec it got) instead of failing the job.
CODEC_CHOICES = ("auto", "av1", "hevc", "h264")

#: Quality steps -> NVENC constant quality. 16 is what the recorder uses and
#: is visually lossless for this content; 23 is the usual "high quality web"
#: point; 30 is for when the file size matters more than the last detail.
QUALITY_CQ = {"high": 16, "balanced": 23, "small": 30}

#: The CPU encoder that ends every chain. NVENC refuses small frames outright
#: (measured on the bundled build: 128x128 fails in all three encoders,
#: 640x360 opens) and a card can be out of encoder sessions, while x264
#: opens at any size. Slower by an order of magnitude - which is why it is
#: last, and why the result says when it was used.
SOFTWARE_CODEC = "libx264"
SOFTWARE_CRF = {"high": 17, "balanced": 21, "small": 26}

#: What the panel offers for a still: the source's own format, or one of two.
IMAGE_FORMATS = ("keep", "png", "jpg")

#: Quality-targeted VBR, as the recorder uses. A conversion is not realtime,
#: so it can afford p7 where the recorder settles for p6. `cq` is replaced by
#: the chosen quality step.
ENCODER_OPTIONS = {
    "preset": "p7",
    "tune": "hq",
    "rc": "vbr",
    "cq": "16",
    "maxrate": "250M",
    "bufsize": "500M",
}
BIT_RATE = 120_000_000

#: Audio codecs the MP4 muxer takes as they are. Anything else (Vorbis, PCM,
#: WMA...) is re-encoded to AAC rather than dropped: a converted video that
#: comes back silent reads as a broken converter, whatever the reason.
MP4_AUDIO_COPY = frozenset({"aac", "mp3", "ac3", "eac3", "opus", "flac", "alac"})
AAC_SAMPLE_RATE = 48000
AAC_BIT_RATE = 192_000

#: The worker is handed one frame at a time and answers before the next is
#: sent, so this is a per-frame ceiling and not a whole-file one. A 4K frame
#: through four NR passes is the slow case that sets it.
FRAME_TIMEOUT_S = 60.0

#: Discarded evaluations before the first real frame. The live pipeline pays
#: 120 on a cold card because the frame watchdog would otherwise kill the
#: worker on frame 0; a conversion has no watchdog and no picture to keep
#: alive, so it pays the smallest warm-up that still lets NGX settle.
WARMUP_FRAMES = 8

#: How many frames may wait between two stages of a video conversion. The
#: depth is what the overlap costs in memory: two lanes hold 2 frames each,
#: and the reader's ring grows to about as many again - 8 MB a frame at
#: 1080p, four times that at 4K. Two is already enough to keep all three
#: stages busy, and a deeper lane only buys latency nobody is waiting on.
STAGE_DEPTH = 2

#: Run the three stages on their own threads. `NS_CONVERT_PIPELINE=0` puts
#: them back on one, which is the order this module had before the overlap
#: and the way to tell a pipeline bug from a conversion bug.
PIPELINE = os.environ.get("NS_CONVERT_PIPELINE", "1") != "0"


class ConversionCancelled(RuntimeError):
    """Raised inside the engine when the caller's cancel event is set."""


class ConversionError(RuntimeError):
    """A conversion that could not be completed, with the stage that failed."""

    def __init__(self, stage: str, cause: BaseException | str):
        self.stage = stage
        self.cause = cause
        super().__init__(f"{stage}: {cause}")


@dataclass
class Progress:
    """What the caller is told while a conversion runs.

    `total` is 0 when it is not knowable - a container that declares neither
    a frame count nor a duration - so a progress bar has to cope rather than
    lie about it. `stage` is one of: decoding, starting, processing, writing.
    """
    stage: str
    done: int = 0
    total: int = 0
    detail: str = ""

    @property
    def fraction(self) -> float:
        return min(1.0, self.done / self.total) if self.total > 0 else 0.0


@dataclass
class ConversionResult:
    """Where the output went and what it cost."""
    source: Path
    output: Path
    kind: str                     # "image" | "video"
    frames: int = 0
    seconds: float = 0.0
    codec: str = ""
    width: int = 0
    height: int = 0
    work_width: int = 0
    work_height: int = 0
    skipped: int = 0
    #: "copied" | "aac" | "none" (the source had none) | "off" (not asked
    #: for) | "dropped" (it could not be carried - the reason is in notes).
    audio: str = "none"
    notes: list = field(default_factory=list)


def classify(path: Path) -> str:
    """"image", "video", or "" for something this module will not open."""
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return ""


def output_suffix(source: Path, image_format: str = "keep") -> str:
    """The extension a converted file gets.

    A still keeps its own format unless PNG or JPEG was asked for. A video is
    re-encoded with NVENC (AV1/HEVC/H.264), and not every source container
    can carry that: AVI and WMV cannot hold AV1 at all, and WebM holds
    neither HEVC nor H.264. So video goes to MP4 - which every player opens -
    except from MKV and WebM, whose files often carry audio and subtitle
    codecs MP4 refuses, and which become MKV so the audio survives as it was.
    """
    source = Path(source)
    suffix = source.suffix.lower()
    if classify(source) == "image":
        if image_format == "png":
            return ".png"
        if image_format == "jpg":
            return ".jpg"
        return suffix
    return ".mkv" if suffix in (".mkv", ".webm") else ".mp4"


def default_output_path(source: Path, out_dir: Path | None = None,
                        suffix: str | None = None) -> Path:
    """`<name>-nr<suffix>`, beside the source unless a folder was chosen.

    Never the source itself: a converter that can overwrite its own input
    destroys the original on a second run, and the second run is exactly
    what someone does after changing a slider. A ".partial" left by a run
    that died counts as taken too - its name is the one a retry would use.
    """
    source = Path(source)
    folder = Path(out_dir) if out_dir else source.parent
    stem = source.stem
    suffix = suffix or source.suffix
    candidate = folder / f"{stem}-nr{suffix}"
    n = 2
    while candidate.exists() or Path(f"{candidate}.partial").exists():
        candidate = folder / f"{stem}-nr-{n}{suffix}"
        n += 1
    return candidate


def codec_chain(choice: str) -> tuple[str, ...]:
    """The NVENC encoders to try for a panel choice, best first."""
    if choice == "hevc":
        return ("hevc_nvenc", "h264_nvenc")
    if choice == "h264":
        return ("h264_nvenc",)
    return CODEC_CHAIN


def encoder_options(quality: str) -> dict:
    """ENCODER_OPTIONS with the chosen quality step."""
    cq = QUALITY_CQ.get(quality, QUALITY_CQ["high"])
    return dict(ENCODER_OPTIONS, cq=str(cq))


def audio_plan(codec_name: str, container_format: str) -> str:
    """"copy" when the output container takes the source audio as it is,
    "aac" when it has to be re-encoded to be carried at all."""
    if container_format in ("matroska", "webm"):
        return "copy"
    return "copy" if str(codec_name).lower() in MP4_AUDIO_COPY else "aac"


def processing_size(width: int, height: int, work_scale: float,
                    nr_small: bool, nr_passes: int = 1) -> tuple[int, int]:
    """The resolution the network runs at for a frame of this size.

    The same two rules the live pipeline obeys, for the same reasons: the
    NGX cap is real (feature 18 goes silent above 2560x1440), and a work
    size must never exceed the frame it came from. With Boost off the
    network is handed the whole frame and the scale is inert - which is the
    measurement in TECHNICAL.md, not a decision made here - and no cascade
    runs, so the pass count does not size anything either
    (settings_io.cascade_passes).
    """
    from settings_io import _work_size
    if not nr_small:
        work_scale, nr_passes = 1.0, 1
    return _work_size(width, height, float(work_scale), nr_passes)


def flow_size(work_w: int, work_h: int) -> tuple[int, int]:
    """The grid a motion field travels on - TemporalGuideGenerator's rule.

    Repeated here rather than imported, because a still has no guides to ask:
    its motion is zeros, and building a generator to learn the shape of them
    would allocate six work-sized buffers to say so. tests/test_media_convert
    holds the two together.
    """
    scale = min(1.0, 320 / max(1, work_w))
    return (max(64, int(round(work_w * scale / 2) * 2)),
            max(64, int(round(work_h * scale / 2) * 2)))


class _Engine:
    """One worker, held open for the frames of one file.

    The worker's three side channels are negotiated here for the reason the
    live pipeline negotiates them: a converted frame is the same 8 MB at
    1080p as a captured one, and it used to travel the same way - written
    into a pipe, read out of it, and the answer back the same way.

        SHMI  the frame goes into a mapping; only its 24-byte header travels.
        OUTS  the processed frame comes back through a second mapping.
        MOTS  the motion field travels at flow size (~320x180) and the worker
              upscales it on the GPU, instead of the CPU building it at the
              work size - 6 million values, every frame.

    Each is optional and each is asked for separately: a worker that refuses
    one keeps the old path for that one and says so, which is how channels.py
    treats the same three. Measured on the CPU alone at 1080p with the work
    size at the frame size, per frame: 13.7 ms of guides becomes 6.0, 2.7 ms
    of tobytes and 6.2 ms of pipe become 0.9 ms of memcpy, and the pixels
    coming back cost 0.4 instead of about 3.
    """

    def __init__(self, params: dict, width: int, height: int,
                 work_w: int, work_h: int, nr_passes: int = 1):
        self.params = dict(params)
        self.nr_passes = max(1, min(4, int(nr_passes or 1)))
        self.width, self.height = int(width), int(height)
        self.work_w, self.work_h = int(work_w), int(work_h)
        # Upscale mode exactly as the live pipeline decides it: the worker is
        # told the full size only when it differs from the work size, because
        # at work == full the legacy 1:1 path is the one that is known to work.
        self.upscale = (self.work_w != self.width or self.work_h != self.height)
        self.worker = None
        self.reader = None
        self.logs: list = []
        self.stop = None
        self.shm: SharedFrameBuffer | None = None
        #: The motion field travels at flow size and the worker upscales it.
        self.motion_small = False
        #: The processed frame comes back through a section, not the pipe.
        self.out_shm = False

    @property
    def motion_size(self) -> tuple[int, int]:
        """The size of the motion field this worker expects to be handed."""
        if self.motion_small:
            return flow_size(self.work_w, self.work_h)
        return self.work_w, self.work_h

    def __enter__(self) -> "_Engine":
        full_w = self.width if self.upscale else 0
        full_h = self.height if self.upscale else 0
        try:
            # The motion capacity is the WORK size, not the flow size: MOTS
            # may be refused after the mapping is made, and the full field
            # has to fit when it is.
            self.shm = SharedFrameBuffer(self.width, self.height,
                                         self.work_w, self.work_h)
        except Exception as exc:
            print(f"[convert] no shared memory ({exc}) - frames through "
                  f"the pipe", file=sys.stderr)
            self.shm = None
        # The residual strength is NOT derived here. It was, briefly, as
        # 1/passes - which quietly made a two-pass conversion apply half the
        # effect of a one-pass one, the same surprise the overlay had. It is
        # a setting now (config residual_strength, or the environment), and a
        # conversion inherits whatever the process was started with so that a
        # converted file matches what the panel is showing.
        self.worker, self.logs, self.reader, self.stop = start_worker(
            self.params, self.work_w, self.work_h, WARMUP_FRAMES,
            full_w, full_h, self.shm)
        # The cascade depth reaches the worker ONLY on a resize: the stream
        # header has no field for it (see tests/test_nr_passes_wire). A
        # converter that never sent one therefore ran a single pass whatever
        # the panel said - which is what this call fixes.
        if self.nr_passes > 1:
            send_resize(self.worker, self.params, self.work_w, self.work_h,
                        WARMUP_FRAMES, full_w, full_h,
                        True, False, self.nr_passes)
            self.reader.wait_rack(timeout=60.0)
            self.reader.set_output_size(full_w or self.work_w,
                                        full_h or self.work_h)
        self._open_channels()
        return self

    def _open_channels(self) -> None:
        """MOTS and OUTS, each on its own, each refusable.

        Both live inside the worker process, so they are asked for after it
        is up and after any resize - a feature rebuilt at another size keeps
        neither. Neither is fatal: the field is upscaled on the CPU and the
        pixels come back through the pipe, which is what this module did.
        """
        flow_w, flow_h = flow_size(self.work_w, self.work_h)
        try:
            send_motion_size(self.worker, flow_w, flow_h)
            self.reader.wait_mack(timeout=15.0)
            self.motion_small = True
        except Exception as exc:
            self.motion_small = False
            print(f"[convert] the motion field is upscaled on the CPU ({exc})",
                  file=sys.stderr)
        if self.shm is None:
            return
        try:
            self.shm.open_out(self.width, self.height)
            send_out(self.worker, self.width, self.height, self.shm.out_name)
            self.reader.wait_oak(timeout=15.0)
            self.out_shm = True
        except Exception as exc:
            self.out_shm = False
            print(f"[convert] the processed frame comes back through the "
                  f"pipe ({exc})", file=sys.stderr)

    def __exit__(self, *exc) -> None:
        if self.worker is not None:
            try:
                shutdown_worker(self.worker, self.stop)
            except Exception:
                pass
            self.worker = None
        if self.shm is not None:
            # The frames already handed out are copies in the reader's own
            # ring, not views into the section, so closing it here cannot
            # pull a frame out from under the encoder.
            try:
                self.shm.close()
            except Exception:
                pass
            self.shm = None

    def evaluate(self, index: int, rgba: np.ndarray,
                 motion: np.ndarray, reset: bool) -> np.ndarray | None:
        """One frame in, the processed frame out (or None if it was skipped)."""
        if self.worker is None or self.worker.poll() is not None:
            tail = "\n".join(self.logs[-12:]) or "(no worker output)"
            raise ConversionError("worker", f"the worker exited:\n{tail}")
        send_frame(self.worker, index, rgba, motion, reset, index,
                   shm=self.shm, want_pixels=True,
                   motion_small=self.motion_small)
        return self.reader.recv(index, timeout=FRAME_TIMEOUT_S)


def _zero_motion(work_w: int, work_h: int) -> np.ndarray:
    """The motion field the worker reads exactly work_w*work_h*4 bytes of."""
    return np.zeros((work_h, work_w, 2), dtype=np.float16)


def _as_rgba(array: np.ndarray) -> np.ndarray:
    """A contiguous HxWx4 uint8 view, whatever the decoder handed back."""
    if array.ndim == 2:
        array = np.dstack([array] * 3)
    if array.shape[2] == 3:
        alpha = np.full(array.shape[:2] + (1,), 255, dtype=np.uint8)
        array = np.concatenate([array, alpha], axis=2)
    return np.ascontiguousarray(array, dtype=np.uint8)


#: The largest frame the worker takes: its stream header refuses a width over
#: 7680 or a height over 4320 (RunVideo in dlss5-feed-host64.cpp) - a
#: landscape shape, so a portrait 4000x6000 is refused while it has fewer
#: pixels than the limit.
WORKER_MAX_W, WORKER_MAX_H = 7680, 4320

#: Pillow formats that carry a colour profile and EXIF when saving.
_META_FORMATS = ("JPEG", "PNG", "WEBP", "TIFF")


def _load_image(handle) -> tuple[np.ndarray, dict]:
    """The still as it is meant to be seen (HxWx4 uint8), and what to save back.

    Three things the pixels alone do not say:

      * The EXIF orientation. A phone stores a portrait photo as landscape
        pixels and a tag that says "turn me", every viewer honours the tag,
        and the converted file - written without it - showed the result
        sideways. exif_transpose applies it and takes the tag out of the
        EXIF it hands back, so the saved copy is not turned twice.
      * 16-bit greys. Pillow's I;16 -> RGBA conversion clips every value
        above 255 rather than scaling it: a 16-bit PNG came out 99.6% white.
      * The colour profile and the EXIF, which go back into the output: a
        Display P3 photo saved without its profile is read as sRGB and
        looks washed out.
    """
    from PIL import ImageOps

    image = ImageOps.exif_transpose(handle)
    keep = {}
    icc = image.info.get("icc_profile")
    if icc:
        keep["icc_profile"] = icc
    exif = image.info.get("exif")
    if exif:
        keep["exif"] = exif
    if image.mode.startswith("I;16") or image.mode == "I":
        grey = np.asarray(image).astype(np.int64)
        # "I" is 32-bit and holds 8-bit greys as often as 16-bit ones.
        if image.mode.startswith("I;16") or grey.max(initial=0) > 255:
            grey = (np.clip(grey, 0, 65535) * 255 + 32767) // 65535
        return _as_rgba(np.clip(grey, 0, 255).astype(np.uint8)), keep
    return _as_rgba(np.asarray(image.convert("RGBA"))), keep


def _quarter_turns_to_fit(width: int, height: int) -> int:
    """0 if the worker takes the frame as it is, 1 if it takes it turned.

    A portrait frame taller than 4320 fits the worker's landscape-shaped
    limit on its side - which is also the way a phone stored it before the
    EXIF turn - so it goes through turned and comes back upright. Only a
    frame that fits neither way is refused, and with a sentence that says so.
    """
    if width <= WORKER_MAX_W and height <= WORKER_MAX_H:
        return 0
    if height <= WORKER_MAX_W and width <= WORKER_MAX_H:
        return 1
    raise ConversionError(
        "too_large", f"{width}x{height} is larger than the "
                     f"{WORKER_MAX_W}x{WORKER_MAX_H} the worker processes")


def _display_rotation(source: Path) -> int:
    """The turn a player gives the source's picture, in degrees (0 if none).

    A phone stores a portrait video as landscape frames and a display matrix
    that says "turn me"; the frames decode as stored. The matrix belongs to
    the stream, and the converted file - a new stream - came out without it:
    a portrait clip played sideways. PyAV reports it on the decoded frame
    (counter-clockwise, -180..180, FFmpeg's convention), so the first frame
    is decoded once, from its own handle, to learn it before the output's
    header is written.
    """
    import av

    try:
        with av.open(str(source)) as probe:
            stream = next((s for s in probe.streams if s.type == "video"), None)
            if stream is None:
                return 0
            for frame in probe.decode(stream):
                return int(round(float(frame.rotation or 0)))
    except Exception as exc:
        print(f"[convert] display rotation not read ({exc})", file=sys.stderr)
    return 0


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ConversionCancelled("cancelled")


def _drop_partial(partial: Path) -> None:
    try:
        partial.unlink()
    except OSError:
        pass


def convert_image(source: Path, output: Path, params: dict, *,
                  work_scale: float = 0.65, nr_small: bool = True,
                  nr_passes: int = 1,
                  progress: Callable[[Progress], None] | None = None,
                  cancel: threading.Event | None = None) -> ConversionResult:
    """One still through the network.

    One frame, reset=True, zero motion. The network is temporal and a single
    image has no history: handing it anything but a reset asks it to
    correlate against whatever the feature was last shown, which on a fresh
    worker is nothing at all.
    """
    from PIL import Image

    source, output = Path(source), Path(output)
    started = time.perf_counter()

    def say(stage: str, done: int = 0, total: int = 1, detail: str = "") -> None:
        if progress is not None:
            progress(Progress(stage, done, total, detail))

    say("decoding", 0, 1, source.name)
    try:
        with Image.open(source) as handle:
            handle.load()
            frame, keep = _load_image(handle)
    except Exception as exc:
        raise ConversionError("decode", exc) from exc

    height, width = frame.shape[0], frame.shape[1]
    if width < 64 or height < 64:
        raise ConversionError(
            "decode", f"{width}x{height} is below the 64x64 the worker accepts")
    turns = _quarter_turns_to_fit(width, height)
    if turns:
        frame = np.ascontiguousarray(np.rot90(frame, turns))
    proc_h, proc_w = frame.shape[0], frame.shape[1]
    work_w, work_h = processing_size(proc_w, proc_h, work_scale, nr_small,
                                     nr_passes)
    _check(cancel)

    say("starting", 0, 1, f"{width}x{height}")
    with _Engine(params, proc_w, proc_h, work_w, work_h, nr_passes) as engine:
        _check(cancel)
        say("processing", 0, 1, f"{width}x{height}")
        # The field is zeros either way; its SHAPE is the worker's, which
        # depends on whether it took the MOTS channel.
        pixels = engine.evaluate(0, frame, _zero_motion(*engine.motion_size),
                                 True)
    if pixels is None:
        raise ConversionError("process", "the worker returned no pixels")
    _check(cancel)

    say("writing", 1, 1, output.name)
    partial = output.with_name(output.name + ".partial")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        out = np.ascontiguousarray(pixels)[:, :, :3]
        if turns:
            out = np.ascontiguousarray(np.rot90(out, -turns))
        image = Image.fromarray(out, mode="RGB")
        # The partial name is the recorder's rule, for the recorder's reason:
        # a file that exists is a file someone will open, and a conversion
        # that died halfway must not leave one that looks finished.
        # The format is named, never inferred: Pillow picks it from the
        # EXTENSION, and the partial name ends in ".partial", so letting it
        # guess raises "unknown file extension" after the frame has already
        # been through the network - the whole conversion lost at the last
        # step (found by the first real run).
        fmt = _PIL_FORMATS.get(output.suffix.lower(), "PNG")
        meta = keep if fmt in _META_FORMATS else {}
        if fmt == "JPEG":
            image.save(partial, format=fmt, quality=97, subsampling=0, **meta)
        else:
            image.save(partial, format=fmt, **meta)
        os.replace(partial, output)
    except Exception as exc:
        _drop_partial(partial)
        raise ConversionError("encode", exc) from exc

    return ConversionResult(
        source=source, output=output, kind="image", frames=1,
        seconds=time.perf_counter() - started,
        width=width, height=height, work_width=work_w, work_height=work_h,
        codec=output.suffix.lstrip(".").lower(), audio="none")


def _estimate_frames(container, stream, rate) -> int:
    """How many frames the video holds, or 0 when nothing says.

    The declared count first; many containers leave it at zero (MKV, WebM,
    most AVIs), and there the duration times the rate is exact enough for a
    progress bar - which is all the number is for.
    """
    declared = int(getattr(stream, "frames", 0) or 0)
    if declared > 0:
        return declared
    seconds = 0.0
    try:
        if stream.duration and stream.time_base:
            seconds = float(stream.duration * stream.time_base)
        elif container.duration:
            seconds = float(container.duration) / 1_000_000.0  # AV_TIME_BASE
    except Exception:
        seconds = 0.0
    return max(0, int(round(seconds * float(rate)))) if seconds > 0 else 0


MAX_TIMESCALE = 90_000


def _encoder_grid(stream, rate: Fraction) -> Fraction:
    """The clock the output encoder writes in.

    The encoder's tick has to be at least as fine as the source's own, or two
    frames land on the same tick and the muxer refuses the stream
    ("Application provided invalid, non monotonically increasing dts"). A
    variable-rate recording triggers it: `average_rate` is an average over the
    whole file - 53.78 fps for a 60 fps grid the recorder dropped frames from -
    so 1/rate is 0.0186 s while frames arrive every 0.0167 s, 1.12 frames per
    tick, and the sixth frame rounds onto the tick the fifth one already took.

    The source's own time_base is the grid its timestamps live on, so writing
    in it is exact for any file, variable rate or not - but it also becomes the
    mp4 timescale, and a fine one puts a 32-bit player past its limit: 2^32
    ticks of a microsecond is 71 minutes. Anything finer than 1/90000 s (the
    MPEG system clock, 13 hours in 32 bits) is therefore coarsened to it: no
    frame rate in use comes near that tick, so frames still land on ticks of
    their own, and the pts rounding keeps them in order.
    """
    grid = Fraction(getattr(stream, "time_base", None) or 0)
    if not grid:
        return Fraction(1, 1) / rate
    floor = Fraction(1, MAX_TIMESCALE)
    return grid if grid >= floor else floor


def _encoder_probe(av, name, rate, width, height, quality) -> bool:
    """Whether `name` opens for this size, tried in a throwaway container.

    Walking the chain inside the real output container would leave every
    refused candidate behind in it as a dead stream, and the muxer writes
    them all into the file header.
    """
    import io
    probe = av.open(io.BytesIO(), mode="w", format="mp4")
    try:
        stream = probe.add_stream(name, rate=rate)
        stream.width, stream.height = width, height
        stream.pix_fmt = "yuv420p"
        stream.time_base = Fraction(1, 1) / rate
        stream.options = (encoder_options(quality) if name != SOFTWARE_CODEC
                          else {"preset": "medium",
                                "crf": str(SOFTWARE_CRF.get(quality, 17))})
        stream.open()
        return True
    except Exception:
        return False
    finally:
        try:
            probe.close()
        except Exception:
            pass


def _pick_video_encoder(av, chain, rate, width, height, quality) -> str:
    """The first encoder of the chain that opens here, x264 as the last."""
    for name in tuple(chain) + (SOFTWARE_CODEC,):
        if _encoder_probe(av, name, rate, width, height, quality):
            return name
    raise ConversionError("encode", f"no video encoder opens at {width}x{height}")


def _add_video_stream(out_container, name, rate, width, height, quality,
                      grid=None):
    stream = out_container.add_stream(name, rate=rate)
    stream.width, stream.height = width, height
    stream.pix_fmt = "yuv420p"
    stream.time_base = Fraction(1, 1) / rate
    if grid is not None:
        # The encoder's clock, not the declared rate, decides where a frame
        # lands: a tick coarser than the source's own grid makes two adjacent
        # frames round onto the same one and the muxer rejects the stream.
        #
        # Both clocks are set, and the container's matters as much as the
        # encoder's: it becomes the mp4 timescale, and 1/average_rate is an
        # absurd one - 19620000 for a 53.78 fps file, which a 32-bit player
        # overflows after six minutes. The source's grid is 60000 there.
        stream.time_base = grid
        stream.codec_context.time_base = grid
    # The colour tags on the STREAM, before it opens: the encoder's colour
    # description and the mp4 `colr` box are fixed at open(), and the frames
    # alone (convert_video tags them too) never reached them. The samples are
    # full-range sRGB, and a file that says nothing is read by players as
    # limited range - crushed blacks and clipped whites in every conversion.
    # The same four values the recorder writes (recorder.py).
    try:
        context = stream.codec_context
        context.color_range = 2          # full
        context.colorspace = 1           # BT.709
        context.color_primaries = 1      # BT.709
        context.color_trc = 13           # sRGB
    except Exception as exc:
        print(f"[convert] colour metadata not set: {exc}", file=sys.stderr)
    if name == SOFTWARE_CODEC:
        stream.options = {"preset": "medium",
                          "crf": str(SOFTWARE_CRF.get(quality, 17))}
    else:
        stream.bit_rate = BIT_RATE
        stream.options = encoder_options(quality)
    stream.open()
    return stream


#: The transfer curves that mean an HDR source: SMPTE ST 2084 (PQ, HDR10) and
#: ARIB STD-B67 (HLG), as libav numbers them.
HDR_TRANSFERS = frozenset({16, 18})
#: Undecodable packets tolerated before a conversion gives up: a recording that
#: was cut short ends in one or two, and losing the whole file to them is worse
#: than losing those frames.
MAX_BAD_PACKETS = 30


def _is_hdr(stream) -> bool:
    """Whether a video stream is HDR (PQ or HLG), by its transfer tag."""
    try:
        return int(stream.codec_context.color_trc) in HDR_TRANSFERS
    except Exception:
        return False


class _AacTrack:
    """Source audio re-encoded to AAC, on one contiguous sample clock.

    The recorder's pattern (resampler -> fifo -> whole 1024-sample frames)
    for the same reason: AAC encodes fixed frames, a decoder hands out
    whatever its packets held.
    """

    def __init__(self, av, out_container):
        self.av = av
        self.stream = out_container.add_stream("aac", rate=AAC_SAMPLE_RATE)
        self.stream.bit_rate = AAC_BIT_RATE
        self.stream.layout = "stereo"
        self.stream.format = "fltp"
        self.stream.time_base = Fraction(1, AAC_SAMPLE_RATE)
        self.resampler = av.AudioResampler(format="fltp", layout="stereo",
                                           rate=AAC_SAMPLE_RATE)
        self.fifo = av.AudioFifo()
        self.samples = None

    def push(self, frame, container) -> None:
        for converted in self.resampler.resample(frame):
            self._append(converted, frame)
        self._drain(container)

    def _append(self, converted, source_frame) -> None:
        if converted is None or converted.samples <= 0:
            return
        if self.samples is None:
            # The track starts where the source's audio starts, so a file
            # whose sound begins after its picture stays in sync.
            start = getattr(source_frame, "time", None)
            self.samples = max(0, int(round(float(start) * AAC_SAMPLE_RATE))
                               ) if start is not None else 0
        converted.sample_rate = AAC_SAMPLE_RATE
        converted.time_base = self.stream.time_base
        converted.pts = self.samples
        self.samples += converted.samples
        self.fifo.write(converted)

    def _drain(self, container, flush: bool = False) -> None:
        size = self.stream.codec_context.frame_size or 1024
        while True:
            frame = self.fifo.read(size, partial=flush)
            if frame is None:
                return
            for packet in self.stream.encode(frame):
                container.mux(packet)

    def finish(self, container) -> None:
        for converted in self.resampler.resample(None):
            self._append(converted, None)
        self._drain(container, flush=True)
        for packet in self.stream.encode(None):
            container.mux(packet)


class _Lane:
    """A bounded hand-off between two stages, with one shared stop.

    put and get poll instead of blocking outright: when one stage dies, the
    others have to notice, and a thread parked forever on a queue that will
    never move again is a hung conversion with no error to show for it.
    """

    def __init__(self, depth: int, stop: threading.Event):
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, depth))
        self._stop = stop

    def put(self, item) -> bool:
        """True once the item is in; False if everything was stopped."""
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def get(self):
        """The next item, or None if everything was stopped."""
        while not self._stop.is_set():
            try:
                return self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
        return None


def _staged(stage: str, call):
    """Run a stage's callable, naming the stage if it fails.

    The stage is what the queue turns into a sentence for the user
    (convert_jobs.friendly_error), and with the work on three threads it is
    no longer a variable the loop can move - each stage carries its own.
    """
    def run(item):
        try:
            call(item)
        except (ConversionCancelled, ConversionError):
            raise
        except Exception as exc:
            raise ConversionError(stage, exc) from exc
    return run


def _tagged(items, stage: str):
    """The same, for something that is iterated rather than called."""
    try:
        for item in items:
            yield item
    except (ConversionCancelled, ConversionError):
        raise
    except Exception as exc:
        raise ConversionError(stage, exc) from exc


def _decoded_items(container, streams, video_index: int, audio_mode: str,
                   even_w: int, even_h: int, guides, cancel,
                   notes: list, bad: dict):
    """Everything the source holds, in the order it holds it.

        ("video", rgba, motion, reset, time) | ("audio", packet) | ("sound", frame)

    The guides run HERE rather than next to the worker. The optical flow
    reads two decoded frames and nothing the network produces, so it has no
    reason to wait for it - and it is the most expensive thing the CPU does
    per frame: 6.0 ms at 1080p even with the field kept at flow size.
    """
    import av

    def decoded(packet):
        """The packet's frames; a packet that will not decode is skipped."""
        try:
            return packet.decode()
        except av.error.InvalidDataError as exc:
            bad["n"] += 1
            if bad["n"] == 1:
                notes.append("some of the source could not be decoded - "
                             "those frames are left out")
            if bad["n"] > MAX_BAD_PACKETS:
                raise ConversionError("decode", exc) from exc
            return ()

    for packet in container.demux(*streams):
        _check(cancel)
        if packet.stream.index != video_index:
            if audio_mode == "copy":
                if packet.dts is None:
                    continue              # the demuxer's flush packet
                yield ("audio", packet)
            elif audio_mode == "aac":
                for audio_frame in decoded(packet):
                    yield ("sound", audio_frame)
            continue
        for frame in decoded(packet):
            _check(cancel)
            rgba = _as_rgba(frame.to_ndarray(format="rgba"))
            if rgba.shape[1] != even_w or rgba.shape[0] != even_h:
                rgba = np.ascontiguousarray(rgba[:even_h, :even_w])
            guide = guides.process(rgba)
            # The generator hands back a buffer it reuses for the next
            # frame, and with the stages overlapped this one may still be
            # on its way into the worker's mapping: 230 KB at flow size.
            yield ("video", rgba, guide.motion.copy(), guide.reset, frame.time)


class _Writer:
    """The output side: the muxer, the video encoder, and the audio track.

    One thread owns all three. The pts is carried by the item, not by a
    counter here, so a variable-rate source keeps its own spacing.
    """

    def __init__(self, av, container, stream, audio_out, aac,
                 even_w: int, even_h: int, step: int):
        self._av = av
        self._container = container
        self._stream = stream
        self._audio_out = audio_out
        self._aac = aac
        self._even_w, self._even_h = even_w, even_h
        self._time_base = stream.time_base
        #: One frame's length on the output clock, for a frame the demuxer
        #: left without a timestamp - the muxer's tick is far finer than a
        #: frame (1/12800 s for a 25 fps AVI in mp4).
        self._step = step
        self._last_pts = -1

    def write(self, item) -> None:
        kind = item[0]
        if kind == "audio":
            packet = item[1]
            packet.stream = self._audio_out
            self._container.mux(packet)
            return
        if kind == "sound":
            self._aac.push(item[1], self._container)
            return
        self._video(item[1], item[2])

    def _video(self, pixels, when) -> None:
        out = np.ascontiguousarray(pixels)[:self._even_h, :self._even_w]
        video_frame = self._av.VideoFrame.from_ndarray(out, format="rgba")
        # The colour tags belong on the FRAME as well as the stream:
        # swscale takes its matrix from the frame while the player reads
        # the stream, and that mismatch is what the recorder's own comment
        # calls "the contrast".
        video_frame.color_range = 2          # full (sRGB)
        video_frame.colorspace = 1           # BT.709
        video_frame.color_primaries = 1
        video_frame.color_trc = 13           # sRGB
        pts = (int(round(float(when) / float(self._time_base)))
               if when is not None else self._last_pts + self._step)
        pts = max(pts, self._last_pts + 1)
        self._last_pts = pts
        video_frame.pts = pts
        video_frame.time_base = self._time_base
        for out_packet in self._stream.encode(video_frame):
            self._container.mux(out_packet)


class _Overlap:
    """Decode, the network and encode on three threads instead of one.

    The middle stage stays on the calling thread, because it owns the
    worker: the input mapping has one slot, so its send and its receive
    must stay paired (protocol.SharedFrameBuffer). The two ends run ahead
    of it and behind it.

    Order is exact. Every item passes through both lanes in the order the
    demuxer produced it, so the muxer still sees audio and video
    interleaved the way the source was - the overlap moves work off the
    critical path, it does not reorder the file.

    The first exception from either end stops everything and is raised on
    the calling thread, carrying the stage it happened in.
    """

    def __init__(self, source, sink, depth: int):
        self._source = _tagged(source, "decode")
        self._sink = _staged("encode", sink)
        self.stop = threading.Event()
        self._in = _Lane(depth, self.stop)
        self._out = _Lane(depth, self.stop)
        self._error: BaseException | None = None
        self._threads: list[threading.Thread] = []

    def _produce(self) -> None:
        try:
            for item in self._source:
                if not self._in.put(("item", item)):
                    return
        except BaseException as exc:      # noqa: BLE001 - raised on the caller
            self._error = self._error or exc
        finally:
            self._in.put(("end", None))

    def _consume(self) -> None:
        while True:
            got = self._out.get()
            if got is None or got[0] == "end":
                return
            try:
                self._sink(got[1])
            except BaseException as exc:  # noqa: BLE001 - raised on the caller
                self._error = self._error or exc
                self.stop.set()
                return

    def items(self):
        """The source's items, decoded up to `depth` frames ahead."""
        while True:
            got = self._in.get()
            if got is None or got[0] == "end":
                break
            yield got[1]
        if self._error is not None:
            raise self._error

    def emit(self, item) -> None:
        if not self._out.put(("item", item)):
            raise self._error or ConversionCancelled("stopped")

    def finish(self) -> None:
        """Let the encode stage drain what is still in flight, then join."""
        self._out.put(("end", None))
        for thread in self._threads:
            thread.join(timeout=FRAME_TIMEOUT_S)
        if self._error is not None:
            raise self._error

    def __enter__(self) -> "_Overlap":
        for name, target in (("convert-decode", self._produce),
                             ("convert-encode", self._consume)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        for thread in self._threads:
            thread.join(timeout=10.0)


def convert_video(source: Path, output: Path, params: dict, *,
                  work_scale: float = 0.65, nr_small: bool = True,
                  nr_passes: int = 1, flow_preset: str = "fast",
                  copy_audio: bool = True, codec: str = "auto",
                  quality: str = "high",
                  progress: Callable[[Progress], None] | None = None,
                  cancel: threading.Event | None = None) -> ConversionResult:
    """A video through the network, frame by frame, with real motion guides.

    The guides are the live ones, at the live work size, for the reason the
    pipeline has them at all: the network accumulates across frames, and a
    sequence handed zero motion flickers where a sequence handed real
    vectors is stable. A scene cut is reported as a reset by the same
    scene-score rule the desktop uses.

    One pass over the source, video and audio packets in the order the file
    holds them, so the output is interleaved the way the source was. Audio
    is copied packet for packet when the output container takes it, and
    re-encoded to AAC when it does not - never silently dropped. (It was, in
    the first version: `add_stream(template=...)` is not an API of the
    bundled PyAV 18, the TypeError was caught as "audio not copied", and
    every converted video came back without sound.)

    Video timestamps follow the source frames rather than a frame counter,
    so a file with a variable frame rate, or with its picture starting after
    its sound, stays in sync with the audio that was carried over.
    """
    import av

    source, output = Path(source), Path(output)
    started = time.perf_counter()
    notes: list = []
    partial = output.with_name(output.name + ".partial")

    def say(stage: str, done: int, total: int, detail: str = "") -> None:
        if progress is not None:
            progress(Progress(stage, done, total, detail))

    say("decoding", 0, 0, source.name)
    try:
        container = av.open(str(source))
    except Exception as exc:
        raise ConversionError("decode", exc) from exc

    engine = None
    out_container = None
    finished = False
    stage = "decode"
    try:
        stream = next((s for s in container.streams if s.type == "video"), None)
        if stream is None:
            raise ConversionError("decode", "the file carries no video stream")
        stream.thread_type = "AUTO"
        if _is_hdr(stream):
            # The frames would reach the network as PQ/HLG code values read as
            # sRGB and come out tagged BT.709: washed out and desaturated, with
            # no word of warning - including the program's own HDR10
            # recordings. There is no tone mapping here yet, so it refuses.
            raise ConversionError(
                "hdr", "the source is HDR (PQ/HLG) - conversion takes SDR video")
        width = int(stream.codec_context.width)
        height = int(stream.codec_context.height)
        if width < 64 or height < 64:
            raise ConversionError(
                "decode", f"{width}x{height} is below the 64x64 the worker accepts")
        # An odd dimension cannot be encoded as yuv420p and the worker's own
        # sizing assumes even frames; rounding DOWN keeps us inside the source.
        even_w, even_h = width - (width % 2), height - (height % 2)
        if even_w > WORKER_MAX_W or even_h > WORKER_MAX_H:
            raise ConversionError(
                "too_large", f"{width}x{height} is larger than the "
                             f"{WORKER_MAX_W}x{WORKER_MAX_H} the worker processes")
        # The frames go through as they are stored, and the file says how to
        # turn them, the way the source did (_display_rotation).
        rotation = _display_rotation(source)
        rate = stream.average_rate or stream.guessed_rate or Fraction(30, 1)
        rate = Fraction(rate).limit_denominator(1001 * 1000)
        total = _estimate_frames(container, stream, rate)
        work_w, work_h = processing_size(even_w, even_h, work_scale,
                                         nr_small, nr_passes)
        size_text = f"{even_w}x{even_h}"

        guides = TemporalGuideGenerator(work_w, work_h, preset=flow_preset)
        stage = "encode"
        output.parent.mkdir(parents=True, exist_ok=True)
        container_format = _AV_FORMATS.get(output.suffix.lower(), "mp4")
        codec_used = _pick_video_encoder(av, codec_chain(codec), rate,
                                         even_w, even_h, quality)
        if codec_used == SOFTWARE_CODEC:
            notes.append(f"NVENC cannot encode {even_w}x{even_h} here - "
                         f"encoded on the CPU (x264)")
        elif codec != "auto" and not codec_used.startswith(codec):
            notes.append(f"{codec.upper()} is not available on this card - "
                         f"encoded with {codec_used.split('_')[0].upper()}")

        audio_in = next((s for s in container.streams if s.type == "audio"),
                        None)
        audio_mode = "off" if not copy_audio else (
            "none" if audio_in is None else
            audio_plan(audio_in.codec_context.name, container_format))
        # The clock the frames are written on. The source's own grid, not
        # 1/average_rate: on a variable-rate recording the average is coarser
        # than the frame spacing and two frames land on one tick, which the
        # muxer refuses outright (media_convert._encoder_grid has the numbers).
        grid = _encoder_grid(stream, rate)
        # The header is written HERE, before a single frame goes through the
        # network: a copied audio track the container refuses fails at this
        # point, and it is cheap to rebuild the output without it now - not
        # after an hour of frames. Copy falls back to AAC, AAC to no audio.
        while True:
            out_container = av.open(str(partial), mode="w",
                                    format=container_format)
            out_stream = _add_video_stream(out_container, codec_used, rate,
                                           even_w, even_h, quality, grid=grid)
            if rotation:
                out_stream.set_display_rotation(rotation)
            audio_out = None
            aac = None
            try:
                if audio_mode == "copy":
                    audio_out = out_container.add_stream_from_template(audio_in)
                elif audio_mode == "aac":
                    aac = _AacTrack(av, out_container)
                out_container.start_encoding()
                break
            except Exception as exc:
                try:
                    out_container.close()
                except Exception:
                    pass
                out_container = None
                _drop_partial(partial)
                if audio_mode == "copy":
                    notes.append(f"the audio could not be copied as it was "
                                 f"({exc}) - re-encoded to AAC")
                    audio_mode = "aac"
                elif audio_mode == "aac":
                    notes.append(f"the audio could not be carried ({exc})")
                    audio_mode = "dropped"
                else:
                    raise ConversionError("encode", exc) from exc

        # The stage follows the work, frame by frame: reading the source is
        # "decode", the network is "process", writing the file is "encode".
        # It used to be "process" for all three, and a truncated source was
        # reported as "the file could not be written".
        stage = "process"
        counted = {"done": 0, "skipped": 0}
        bad = {"n": 0}
        time_base = out_stream.time_base
        # One frame's length on the output clock: the step for a frame the
        # demuxer left without a timestamp. `last_pts + 1` assumed one tick
        # per frame, and the muxer's tick is far finer (1/12800 s for a
        # 25 fps AVI in mp4) - a 10-minute film came out a second long.
        step = max(1, int(round(float(1 / rate) / float(time_base))))
        streams = [stream] + ([audio_in] if audio_mode in ("copy", "aac") else [])
        say("starting", 0, total, size_text)
        # The worker is started HERE, not on the first decoded frame as it
        # used to be: the decode stage runs the guides, and whether their
        # field is built at work size or at flow size is the worker's answer
        # to MOTS. It still comes after the output header, so a file that
        # cannot be written costs no worker at all.
        engine = _Engine(params, even_w, even_h, work_w, work_h, nr_passes)
        engine.__enter__()
        guides.emit_small = engine.motion_small
        writer = _Writer(av, out_container, out_stream, audio_out, aac,
                         even_w, even_h, step)

        def middle(items, emit) -> None:
            """The network's own stage: one frame in, one frame out.

            Everything that is not a video frame passes straight through,
            which is what keeps the output interleaved like the source.
            """
            for item in items:
                _check(cancel)
                if item[0] != "video":
                    emit(item)
                    continue
                _kind, rgba, motion, reset, when = item
                pixels = engine.evaluate(counted["done"], rgba, motion, reset)
                if pixels is None:
                    counted["skipped"] += 1
                    pixels = rgba
                emit(("video", pixels, when))
                counted["done"] += 1
                say("processing", counted["done"],
                    max(total, counted["done"]), size_text)

        source_items = _decoded_items(container, streams, stream.index,
                                      audio_mode, even_w, even_h, guides,
                                      cancel, notes, bad)
        if PIPELINE:
            with _Overlap(source_items, writer.write, STAGE_DEPTH) as overlap:
                middle(overlap.items(), overlap.emit)
                overlap.finish()
        else:
            middle(_tagged(source_items, "decode"),
                   _staged("encode", writer.write))
        done, skipped = counted["done"], counted["skipped"]

        if done == 0:
            raise ConversionError("decode", "no frames could be decoded")

        stage = "encode"
        say("writing", done, max(total, done), output.name)
        for out_packet in out_stream.encode(None):
            out_container.mux(out_packet)
        if aac is not None:
            aac.finish(out_container)
        out_container.close()
        out_container = None
        os.replace(partial, output)
        finished = True
    except (ConversionCancelled, ConversionError):
        raise
    except Exception as exc:
        # The stage the failure happened in, not a catch-all: "process" is
        # the worker (a timeout, a pipe that closed), "encode" the file.
        raise ConversionError(stage, exc) from exc
    finally:
        if engine is not None:
            engine.__exit__(None, None, None)
        if out_container is not None:
            try:
                out_container.close()
            except Exception:
                pass
        if not finished:
            _drop_partial(partial)
        try:
            container.close()
        except Exception:
            pass

    return ConversionResult(
        source=source, output=output, kind="video", frames=done,
        seconds=time.perf_counter() - started, codec=codec_used,
        width=even_w, height=even_h, work_width=work_w, work_height=work_h,
        skipped=skipped, audio="copied" if audio_mode == "copy" else audio_mode,
        notes=notes)


def convert(source: Path, output: Path | None, params: dict, **kwargs) -> ConversionResult:
    """Convert by kind, so a caller does not have to know which this is.

    Accepts every option of both converters; the ones that do not apply to
    this kind of file are ignored, so a queue can hand the same settings to
    a still and a video. `out_dir` and `image_format` only decide the output
    name, and only when no `output` is given.
    """
    source = Path(source)
    kind = classify(source)
    if not kind:
        raise ConversionError(
            "decode", f"{source.suffix or 'that file'} is not a format this converts")
    out_dir = kwargs.pop("out_dir", None)
    image_format = kwargs.pop("image_format", "keep")
    if output is None:
        output = default_output_path(source, out_dir,
                                     output_suffix(source, image_format))
    if kind == "image":
        for video_only in ("flow_preset", "copy_audio", "codec", "quality"):
            kwargs.pop(video_only, None)
        return convert_image(source, Path(output), params, **kwargs)
    return convert_video(source, Path(output), params, **kwargs)
