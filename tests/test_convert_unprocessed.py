"""A conversion says so when the network did not process its frames.

A frame the worker skipped (no reply) or handed back without evaluating it
(its NGX result 0 - main.py's "NR ON but not evaluating" signal) was written
as it came in, and the file was reported "Converted" all the same. A
conversion where the network processed nothing is now a failure ("the
network stopped"), and one where it missed some frames says how many on its
row.

The worker is a stub with a fake reader that reports a chosen NGX result per
frame; the encoder is x264. No GPU.

Run:  runtime\\python.exe tests\\test_convert_unprocessed.py
"""
import os
import sys
import tempfile
import time
import types
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import av  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import convert_jobs  # noqa: E402
import media_convert  # noqa: E402
from i18n import STRINGS  # noqa: E402

FRAMES = 10


def stub_engine(schedule):
    """An engine whose frame i comes back as schedule(i) says:
    "ok" (evaluated), "raw" (pixels, NGX result 0) or "none" (no reply)."""

    class StubEngine:
        def __init__(self, params, width, height, work_w, work_h, nr_passes=1):
            self.work_w, self.work_h = work_w, work_h
            self.motion_small = False
            self.reader = types.SimpleNamespace(last_ngx_result=0)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        @property
        def motion_size(self):
            return self.work_w, self.work_h

        def evaluate(self, index, rgba, motion, reset):
            what = schedule(index)
            # The reader's value is the worker's LAST evaluation: it stays
            # 0 until the network has run once.
            if what == "ok":
                self.reader.last_ngx_result = 1      # NVSDK_NGX_Result_Success
            if what == "none":
                return None
            return np.ascontiguousarray(rgba).copy()

    return StubEngine


def make_video(path: Path) -> None:
    with av.open(str(path), "w", format="mp4") as out:
        stream = out.add_stream("libx264", rate=30)
        stream.width, stream.height, stream.pix_fmt = 128, 96, "yuv420p"
        for k in range(FRAMES):
            frame = av.VideoFrame.from_ndarray(
                np.full((96, 128, 3), 30 + 15 * k, np.uint8), format="rgb24")
            frame.pts = k
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)


def run(source: Path, schedule):
    real_engine, real_chain = media_convert._Engine, media_convert.codec_chain
    media_convert._Engine = stub_engine(schedule)
    media_convert.codec_chain = lambda choice: ()
    try:
        return media_convert.convert(source, None, {}, nr_small=False,
                                     copy_audio=False)
    finally:
        media_convert._Engine = real_engine
        media_convert.codec_chain = real_chain


def expect_failure(label: str, source: Path, schedule, failures: list) -> None:
    try:
        result = run(source, schedule)
    except media_convert.ConversionError as exc:
        reason = convert_jobs.friendly_error(exc)
        print(f"{label}: failed ({exc}) -> row says "
              f"'{STRINGS['en'].get('convert_err_' + reason)}'")
        if reason != "worker":
            failures.append(f"{label}: the row reason is '{reason}', not 'worker'")
        left = [p.name for p in source.parent.iterdir()
                if p.name.startswith(source.stem + "-nr")]
        if left:
            failures.append(f"{label}: a file was left behind: {left}")
        return
    failures.append(f"{label}: reported converted ({result.output.name}, "
                    f"skipped {result.skipped})")
    result.output.unlink(missing_ok=True)


def check_partial(folder: Path, failures: list) -> None:
    """Some frames missed: the job is done, and its row says how many."""
    source = folder / "partly.mp4"
    make_video(source)
    # The first two came back unevaluated, the fifth not at all.
    schedule = (lambda i: "raw" if i < 2 else "none" if i == 4 else "ok")
    real_engine, real_chain = media_convert._Engine, media_convert.codec_chain
    media_convert._Engine = stub_engine(schedule)
    media_convert.codec_chain = lambda choice: ()
    queue = convert_jobs.ConvertQueue()
    try:
        queue.add([source], convert_jobs.ConvertSettings(params={},
                                                         nr_small=False))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            rows = queue.rows()
            if rows and rows[0]["status"] in (convert_jobs.DONE,
                                              convert_jobs.FAILED):
                break
            time.sleep(0.05)
    finally:
        media_convert._Engine = real_engine
        media_convert.codec_chain = real_chain
    row = queue.rows()[0]
    for lang in ("en", "ru"):
        text, tone = convert_jobs.status_line(row, STRINGS[lang])
        print(f"partly [{lang}]: {row['status']}, row: {text} ({tone})")
    text, _tone = convert_jobs.status_line(row, STRINGS["en"])
    if row["status"] != convert_jobs.DONE:
        failures.append(f"partly: the job ended {row['status']} ({row['error']})")
    elif "3 frames not processed" not in text:
        failures.append(f"partly: the row does not say 3 frames were not "
                        f"processed: {text!r}")
    if row.get("output"):
        Path(row["output"]).unlink(missing_ok=True)


def main() -> int:
    failures: list = []
    folder = Path(tempfile.mkdtemp(prefix="ns-convert-unprocessed-"))
    video = folder / "clip.mp4"
    make_video(video)
    expect_failure("video, NGX result 0 on every frame", video,
                   lambda i: "raw", failures)
    expect_failure("video, no reply on any frame", video,
                   lambda i: "none", failures)
    still = folder / "still.png"
    Image.fromarray(np.full((96, 128, 3), 90, np.uint8), "RGB").save(still)
    expect_failure("still, NGX result 0", still, lambda i: "raw", failures)
    check_partial(folder, failures)
    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: unprocessed frames fail the job or are named on its row")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
