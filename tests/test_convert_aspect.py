"""A converted video keeps the shape of its pixels (the sample aspect ratio).

An anamorphic source - a DVD's 720x480 with 32:27 pixels - is 16:9 only
because the stream says its pixels are wide. The converter writes the frames
at their stored size into a new stream, and that stream said nothing: the
output played squeezed to 3:2. The source's SAR now goes onto the output.

The worker is a stub that hands each frame back untouched, and the encoder is
x264, so nothing here needs a GPU.

Run:  runtime\\python.exe tests\\test_convert_aspect.py
"""
import os
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import av  # noqa: E402
import numpy as np  # noqa: E402

import media_convert  # noqa: E402


class StubEngine:
    """Gives every frame back as it came."""

    def __init__(self, params, width, height, work_w, work_h, nr_passes=1):
        self.work_w, self.work_h = work_w, work_h
        self.motion_small = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    @property
    def motion_size(self):
        return self.work_w, self.work_h

    def evaluate(self, index, rgba, motion, reset):
        return np.ascontiguousarray(rgba).copy()


def make_source(path: Path, width: int, height: int, sar) -> None:
    with av.open(str(path), "w", format=media_convert._AV_FORMATS.get(
            path.suffix, "mp4")) as out:
        stream = out.add_stream("libx264", rate=30)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        if sar is not None:
            stream.codec_context.sample_aspect_ratio = sar
        for k in range(8):
            frame = av.VideoFrame.from_ndarray(
                np.full((height, width, 3), 40 + 10 * k, np.uint8), format="rgb24")
            frame.pts = k
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)


def display(path: Path):
    with av.open(str(path)) as probe:
        stream = probe.streams.video[0]
        ctx = stream.codec_context
        sar = ctx.sample_aspect_ratio or Fraction(1, 1)
        return sar, Fraction(ctx.width, ctx.height) * sar


def convert(source: Path, output: Path):
    real_engine, real_chain = media_convert._Engine, media_convert.codec_chain
    media_convert._Engine = StubEngine
    media_convert.codec_chain = lambda choice: ()
    try:
        return media_convert.convert_video(source, output, {}, nr_small=False,
                                           copy_audio=False)
    finally:
        media_convert._Engine = real_engine
        media_convert.codec_chain = real_chain


def main() -> int:
    failures = []
    folder = Path(tempfile.mkdtemp(prefix="ns-convert-aspect-"))
    cases = (("dvd.mp4", 720, 480, Fraction(32, 27), Fraction(16, 9)),
             ("dvd.mkv", 720, 480, Fraction(32, 27), Fraction(16, 9)),
             ("square.mp4", 640, 360, None, Fraction(16, 9)))
    for name, width, height, sar, want in cases:
        source = folder / name
        output = folder / f"{source.stem}-nr{source.suffix}"
        make_source(source, width, height, sar)
        src_sar, src_dar = display(source)
        convert(source, output)
        out_sar, out_dar = display(output)
        print(f"{name}: source SAR {src_sar} DAR {src_dar} -> output SAR "
              f"{out_sar} DAR {out_dar}")
        if src_dar != want:
            failures.append(f"{name}: the test source is {src_dar}, not {want}")
        if out_dar != want:
            failures.append(f"{name}: the converted file shows {out_dar}, "
                            f"the source {want}")
    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: a converted video keeps its source's pixel shape")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
