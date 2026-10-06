"""A converted still keeps what its format can carry: transparency, losslessness,
and only a colour profile that describes its pixels.

Three things the converter lost (audit 2, media):

  * transparency - the worker answers an opaque frame, and the output was
    written as RGB: a logo's transparent background came back black. A PNG,
    WebP or TIFF now gets the source's alpha back; a JPEG (or BMP), which
    cannot hold it, gets the picture as it is seen on a white page;
  * WebP quality - Pillow writes lossy WebP at quality 80 unless told
    otherwise, so a lossless WebP came back lossy and a lossy one softer;
  * a CMYK photo's colour profile was written onto the RGB result, which
    makes a viewer read RGB values as ink.

The worker is a stub that hands each frame back with an opaque alpha (as the
real one does), so nothing here needs a GPU.

Run:  runtime\\python.exe tests\\test_convert_still_formats.py
"""
import io
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import numpy as np  # noqa: E402
from PIL import Image, ImageCms  # noqa: E402

import media_convert  # noqa: E402


class OpaqueEngine:
    """Hands every frame back as it came, but opaque - as the worker does."""

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
        out = np.ascontiguousarray(rgba).copy()
        out[..., 3] = 255
        return out


def convert(source: Path, output: Path) -> None:
    real = media_convert._Engine
    media_convert._Engine = OpaqueEngine
    try:
        media_convert.convert_image(source, output, {}, nr_small=False)
    finally:
        media_convert._Engine = real


def logo() -> np.ndarray:
    """A yellow square on a fully transparent background (RGB 0 under it)."""
    pixels = np.zeros((128, 128, 4), np.uint8)
    pixels[32:96, 32:96] = (255, 200, 0, 255)
    return pixels


def check_alpha(folder: Path, failures: list) -> None:
    source = folder / "logo.png"
    Image.fromarray(logo(), "RGBA").save(source)
    for suffix in (".png", ".webp", ".tiff"):
        output = folder / f"logo-nr{suffix}"
        convert(source, output)
        with Image.open(output) as done:
            done.load()
            rgba = np.asarray(done.convert("RGBA"))
        corner = tuple(int(v) for v in rgba[0, 0])
        centre = tuple(int(v) for v in rgba[64, 64])
        print(f"{suffix}: mode {done.mode}, corner {corner}, centre {centre}")
        if corner[3] != 0:
            failures.append(f"{suffix}: the transparent corner came back with "
                            f"alpha {corner[3]}")
        # A PNG source becomes a lossy WebP: a code value or two of slack.
        slack = 3 if suffix == ".webp" else 0
        if max(abs(a - b) for a, b in zip(centre, (255, 200, 0, 255))) > slack:
            failures.append(f"{suffix}: the opaque square changed to {centre}")
    output = folder / "logo-nr.jpg"
    convert(source, output)
    with Image.open(output) as done:
        rgb = np.asarray(done.convert("RGB")).astype(int)
    corner = tuple(int(v) for v in rgb[0, 0])
    centre = tuple(int(v) for v in rgb[64, 64])
    print(f".jpg: corner {corner}, centre {centre}")
    if min(corner) < 245:
        failures.append(f".jpg: the transparent background came out {corner}, "
                        f"not white")
    if max(abs(a - b) for a, b in zip(centre, (255, 200, 0))) > 8:
        failures.append(f".jpg: the square came out {centre}")


def check_webp(folder: Path, failures: list) -> None:
    rng = np.random.default_rng(7)
    noise = rng.integers(0, 255, (96, 128, 3), dtype=np.uint8)
    source = folder / "lossless.webp"
    Image.fromarray(noise, "RGB").save(source, format="WEBP", lossless=True)
    output = folder / "lossless-nr.webp"
    convert(source, output)
    with Image.open(source) as a, Image.open(output) as b:
        diff = np.abs(np.asarray(a.convert("RGB")).astype(int)
                      - np.asarray(b.convert("RGB")).astype(int)).max()
    diff = int(diff)
    print(f"lossless webp: largest change {diff}")
    if diff != 0:
        failures.append(f"a lossless WebP came back lossy (changed by up to {diff})")

    # A lossy source: the converted file must be closer to what went in than
    # Pillow's default quality makes it.
    ramp = np.dstack([np.tile(np.linspace(0, 255, 128, dtype=np.uint8), (96, 1))] * 3)
    smooth = np.clip(ramp.astype(int) + rng.integers(-20, 20, ramp.shape), 0, 255)
    source = folder / "lossy.webp"
    Image.fromarray(smooth.astype(np.uint8), "RGB").save(source, format="WEBP",
                                                         quality=98)
    output = folder / "lossy-nr.webp"
    convert(source, output)
    with Image.open(source) as handle:
        fed = np.asarray(handle.convert("RGB")).astype(int)
    default = io.BytesIO()
    Image.fromarray(fed.astype(np.uint8), "RGB").save(default, format="WEBP")
    with Image.open(default) as handle:
        at_default = np.abs(np.asarray(handle.convert("RGB")).astype(int) - fed).mean()
    with Image.open(output) as handle:
        written = np.abs(np.asarray(handle.convert("RGB")).astype(int) - fed).mean()
    print(f"lossy webp: mean error {written:.2f} (Pillow's default: {at_default:.2f})")
    if written >= at_default * 0.8:
        failures.append(f"a lossy WebP was written at about Pillow's default "
                        f"quality (error {written:.2f} vs {at_default:.2f})")


def check_cmyk_profile(folder: Path, failures: list) -> None:
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    # The header's colour-space field is what says what the profile is for.
    cmyk_icc = srgb[:16] + b"CMYK" + srgb[20:]
    source = folder / "print.jpg"
    Image.new("CMYK", (96, 96), (0, 100, 200, 0)).save(
        source, format="JPEG", quality=95, icc_profile=cmyk_icc)
    output = folder / "print-nr.jpg"
    convert(source, output)
    with Image.open(output) as done:
        icc = done.info.get("icc_profile")
        mode = done.mode
    space = icc[16:20] if icc else None
    print(f"cmyk source: output {mode}, profile colour space {space}")
    if icc and space != b"RGB ":
        failures.append(f"the RGB output carries a {space!r} colour profile")


def main() -> int:
    failures: list = []
    folder = Path(tempfile.mkdtemp(prefix="ns-convert-stills-"))
    check_alpha(folder, failures)
    check_webp(folder, failures)
    check_cmyk_profile(folder, failures)
    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: stills keep their transparency, their WebP quality, and only an "
          "RGB colour profile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
