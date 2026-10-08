"""Temporal-stability harness for the NR edit under KNOWN motion (issue #151).

Not a test: a helper module and a CLI. It drives the real worker
(native/nvngx.dll --live, the raw protocol test_residual.py uses) with
synthetic moving content whose motion is known exactly, feeds a chosen motion
field variant, collects the composited full-res output and measures how
stable the network's edit is over time.

Why only supplied motion. The worker runs NVOFA only on frames it captured
itself (DDA/WGC + the GRAY channel: RunNvofa copies g_gray_uav, and the
branch is the NO_COLOR one). A colour frame shipped through the pipe always
takes UploadVideoFrame, which uses the motion that came with it. So NVOFA's
own output cannot be driven from here; the "coarse" variant mimics it
instead: ground truth on the 320x180 flow grid, block-averaged to grid 4,
quantised to 1/32 flow px and expanded exactly like kNvofaExpand (bilinear
to the work size, |v| < 0.5 work px -> 0).

Conventions (checked against the code, not assumed):
* motion is CURRENT -> PREVIOUS, in WORK-resolution pixels (guides.py:
  dis.calc(current, previous); EvalNrPass sets MVecScale = nr_w / work_w,
  which is 1 in Boost). current(x) ~ previous(x + d(x)).
* Boost = header work size < full size and NS_NR_SMALL=1: the worker
  area-reduces the full frame to the work size, runs NR there and composes
  native + (nr_out - nr_in) * strength at full res (residual composite).
* Motion goes inline as float16 at the work size (no MOTS): the variants are
  built here at the work size, so the worker's own bilinear stretch is not
  involved.

Metrics, on the edit E_t = out_t - in_t at full res (RGB, 0..255):
* instab  = mean |E_t(x) - E_{t-1}(x + d_t(x))| over valid pixels, split
  into moving / static / halo (static pixels within HALO px of motion -
  where a coarse field leaks object motion onto the background);
* warp_out = mean |out_t(x) - out_{t-1}(x + d_t(x))| (plain output warp
  error), with warp_in the same for the inputs as the floor the resampling of
  the synthetic scene itself leaves;
* edit = mean |E_t|, so "stable because the edit vanished" is visible;
  ratio = instab_moving / edit_moving.
Valid = previous position inside the frame (margin), same object label at
both ends (not disoccluded) and away from object boundaries.

    runtime\\python.exe tests\\nr_motion_harness.py --scene pan \\
        --variants gt,zero,coarse,noisy,flipped --frames 40

Variant syntax: gt, zero, flipped, noisy[:sigma], coarse[:grid],
scale:k (gt * k). A repeated name runs again (determinism check).
"""
from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paths import NATIVE_DIR, WORKER_EXE  # noqa: E402
from protocol import (FRAME_FLAG_WANT_PIXELS, FRAME_FMT, FRAME_MAGIC,  # noqa: E402
                      HEADER_FMT, OUT_FMT, OUT_MAGIC, VIDEO_MAGIC)
from worker_reply import read_exact, read_reply  # noqa: E402

# The shipped "Strong / Cinematic" profile (settings_io.PROFILES), copied so
# that importing this module does not pull in the capture/winreg stack.
PROFILE = dict(style=2, auto_mask=1, intensity=1.00, local_tone=0.90,
               local_structure=1.50, skin_structure=1.0)

FULL = (1920, 1080)
WORK = (1280, 720)
MARGIN = 8      # full px kept away from the frame border
BAND = 4        # full px kept away from object boundaries
HALO = 48       # full px: the static ring around moving content


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------

def make_texture(h: int, w: int, rng: np.random.Generator) -> np.ndarray:
    """A seeded RGB texture with structure at every scale: smooth colour,
    mid-frequency shading, fine grain, anti-aliased shapes and text. Lightly
    band-limited so sub-pixel resampling does not flicker on its own."""
    gh, gw = max(2, h // 180), max(2, w // 180)
    img = cv2.resize(rng.uniform(40, 215, (gh, gw, 3)).astype(np.float32),
                     (w, h), interpolation=cv2.INTER_CUBIC)
    mid = cv2.GaussianBlur(rng.normal(0, 1, (h, w)).astype(np.float32), (0, 0), 6)
    img += (mid * (35.0 / max(1e-6, float(mid.std()))))[..., None]
    fine = cv2.GaussianBlur(rng.normal(0, 1, (h, w, 3)).astype(np.float32), (0, 0), 1.0)
    img += fine * (12.0 / max(1e-6, float(fine.std())))
    out = np.clip(img, 0, 255).astype(np.uint8)
    for _ in range(max(8, w * h // 30000)):
        colour = tuple(int(c) for c in rng.integers(0, 256, 3))
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        r = int(rng.integers(6, 60))
        kind = int(rng.integers(0, 3))
        thick = -1 if rng.random() < 0.5 else int(rng.integers(1, 4))
        if kind == 0:
            cv2.circle(out, (x, y), r, colour, thick, cv2.LINE_AA)
        elif kind == 1:
            cv2.rectangle(out, (x, y), (x + r, y + int(r * rng.uniform(0.3, 1.5))),
                          colour, thick, cv2.LINE_AA)
        else:
            cv2.line(out, (x, y), (x + int(rng.integers(-120, 120)),
                                   y + int(rng.integers(-120, 120))),
                     colour, int(rng.integers(1, 4)), cv2.LINE_AA)
    for y in range(40, h, 90):
        x = int(rng.integers(0, max(1, w // 3)))
        colour = (245, 245, 245) if rng.random() < 0.5 else (15, 15, 15)
        cv2.putText(out, "NeuralScreen 0123 motion text", (x, y),
                    cv2.FONT_HERSHEY_SIMPLEX, float(rng.uniform(0.6, 1.3)),
                    colour, 2, cv2.LINE_AA)
    return cv2.GaussianBlur(out, (0, 0), 0.6)


def _rgba(rgb: np.ndarray) -> np.ndarray:
    out = np.empty(rgb.shape[:2] + (4,), np.uint8)
    out[..., :3] = rgb
    out[..., 3] = 255
    return out


class Scene:
    """A synthetic sequence with exact current->previous motion (full px)."""

    name = "scene"

    def __init__(self, full=FULL, seed: int = 1):
        self.w, self.h = full
        self.seed = seed
        ys, xs = np.mgrid[0:self.h, 0:self.w].astype(np.float32)
        self._xs, self._ys = xs, ys

    def frame(self, t: int) -> np.ndarray:
        """RGBA uint8, full size."""
        raise NotImplementedError

    def flow(self, t: int, xs: np.ndarray, ys: np.ndarray):
        """current->previous displacement at (xs, ys) in FULL px, frame t."""
        raise NotImplementedError

    def labels(self, t: int, xs: np.ndarray, ys: np.ndarray) -> np.ndarray | None:
        """Object id at (xs, ys) in frame t; None = one rigid layer."""
        return None

    def gt_full(self, t: int) -> np.ndarray:
        dx, dy = self.flow(t, self._xs, self._ys)
        return np.stack([dx, dy], -1).astype(np.float32)

    def gt_work(self, t: int, work=WORK) -> np.ndarray:
        """Ground truth at the work size, in work px (the worker's units)."""
        ww, wh = work
        sx, sy = self.w / ww, self.h / wh
        ys, xs = np.mgrid[0:wh, 0:ww].astype(np.float32)
        fx, fy = (xs + 0.5) * sx - 0.5, (ys + 0.5) * sy - 0.5
        dx, dy = self.flow(t, fx, fy)
        return np.stack([dx / sx, dy / sy], -1).astype(np.float32)


class PanScene(Scene):
    """A textured image panned by (vx, vy) px/frame (sub-pixel allowed)."""

    name = "pan"

    def __init__(self, vx=3.5, vy=1.25, frames=64, full=FULL, seed=1):
        super().__init__(full, seed)
        self.vx, self.vy = float(vx), float(vy)
        pad = 16
        tw = int(self.w + abs(self.vx) * frames + 2 * pad + 4)
        th = int(self.h + abs(self.vy) * frames + 2 * pad + 4)
        self.ox = pad + max(0.0, self.vx * frames)
        self.oy = pad + max(0.0, self.vy * frames)
        self.tex = make_texture(th, tw, np.random.default_rng(seed))

    def frame(self, t):
        # content moves by +v per frame: frame_t(x) = tex(x - v t + o)
        mx = self._xs - self.vx * t + self.ox
        my = self._ys - self.vy * t + self.oy
        return _rgba(cv2.remap(self.tex, mx, my, cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REFLECT))

    def flow(self, t, xs, ys):
        if t == 0:
            return np.zeros_like(xs), np.zeros_like(ys)
        return np.full_like(xs, -self.vx), np.full_like(ys, -self.vy)


class StaticScene(PanScene):
    name = "static"

    def __init__(self, frames=64, full=FULL, seed=1):
        super().__init__(0.0, 0.0, frames, full, seed)


class ObjectScene(Scene):
    """A textured rectangle moving over a static textured background."""

    name = "object"

    def __init__(self, vx=7.25, vy=3.5, size=(420, 300), start=(300, 250),
                 full=FULL, seed=1):
        super().__init__(full, seed)
        self.vx, self.vy = float(vx), float(vy)
        self.ow, self.oh = size
        self.x0, self.y0 = start
        rng = np.random.default_rng(seed)
        self.bg = make_texture(self.h, self.w, rng)
        obj = make_texture(self.oh, self.ow, np.random.default_rng(seed + 1000))
        # A visibly different object: brighter, stronger contrast.
        obj = np.clip(obj.astype(np.float32) * 1.25 - 20, 0, 255).astype(np.uint8)
        self.obj = obj

    def _pos(self, t):
        return self.x0 + self.vx * t, self.y0 + self.vy * t

    def frame(self, t):
        px, py = self._pos(t)
        mx, my = self._xs - px, self._ys - py
        layer = cv2.remap(self.obj, mx, my, cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        # Anti-aliased coverage of the rectangle [0, ow) x [0, oh) in object
        # space, sampled at pixel centres: a box of width 1 px.
        ax = np.clip(np.minimum(mx + 0.5, self.ow - mx - 0.5) + 0.5, 0, 1)
        ay = np.clip(np.minimum(my + 0.5, self.oh - my - 0.5) + 0.5, 0, 1)
        a = (ax * ay)[..., None]
        rgb = self.bg.astype(np.float32) * (1 - a) + layer.astype(np.float32) * a
        return _rgba(np.clip(rgb + 0.5, 0, 255).astype(np.uint8))

    def _inside(self, t, xs, ys):
        px, py = self._pos(t)
        return ((xs >= px) & (xs < px + self.ow - 1)
                & (ys >= py) & (ys < py + self.oh - 1))

    def flow(self, t, xs, ys):
        if t == 0:
            return np.zeros_like(xs), np.zeros_like(ys)
        inside = self._inside(t, xs, ys)
        return (np.where(inside, -self.vx, 0.0).astype(np.float32),
                np.where(inside, -self.vy, 0.0).astype(np.float32))

    def labels(self, t, xs, ys):
        return self._inside(t, xs, ys).astype(np.uint8)


class RotZoomScene(Scene):
    """A texture rotating by `deg` and zooming by `zoom` per frame about the
    centre."""

    name = "rotzoom"

    def __init__(self, deg=0.4, zoom=1.004, full=FULL, seed=1):
        super().__init__(full, seed)
        self.deg, self.zoom = float(deg), float(zoom)
        side = int(np.hypot(self.w, self.h) * 1.15) // 2 * 2
        self.side = side
        self.tex = make_texture(side, side, np.random.default_rng(seed))
        self.cx, self.cy = (self.w - 1) / 2.0, (self.h - 1) / 2.0

    def _m(self, t):
        a = np.deg2rad(self.deg * t)
        s = self.zoom ** t
        return s * np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])

    def frame(self, t):
        inv = np.linalg.inv(self._m(t))
        c = (self.side - 1) / 2.0
        # tex coordinate = inv @ (x - centre) + texture centre
        aff = np.zeros((2, 3), np.float64)
        aff[:, :2] = inv
        aff[:, 2] = np.array([c, c]) - inv @ np.array([self.cx, self.cy])
        rgb = cv2.warpAffine(self.tex, aff, (self.w, self.h),
                             flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP,
                             borderMode=cv2.BORDER_REFLECT)
        return _rgba(rgb)

    def flow(self, t, xs, ys):
        if t == 0:
            return np.zeros_like(xs), np.zeros_like(ys)
        step = self._m(t - 1) @ np.linalg.inv(self._m(t))
        rx, ry = xs - self.cx, ys - self.cy
        px = step[0, 0] * rx + step[0, 1] * ry + self.cx
        py = step[1, 0] * rx + step[1, 1] * ry + self.cy
        return (px - xs).astype(np.float32), (py - ys).astype(np.float32)


def make_scene(name: str, frames: int, full=FULL, seed=1, speed=None) -> Scene:
    if name == "pan":
        vx, vy = speed if speed else (3.5, 1.25)
        return PanScene(vx, vy, frames + 2, full, seed)
    if name == "object":
        vx, vy = speed if speed else (7.25, 3.5)
        return ObjectScene(vx, vy, full=full, seed=seed)
    if name == "static":
        return StaticScene(frames + 2, full, seed)
    if name == "rotzoom":
        return RotZoomScene(full=full, seed=seed)
    raise ValueError(f"unknown scene {name!r}")


# ---------------------------------------------------------------------------
# Motion variants (work size, work px, current->previous)
# ---------------------------------------------------------------------------

def flow_size(work=WORK, flow_width=320):
    """The optical-flow grid guides.TemporalGuideGenerator uses."""
    w, h = work
    scale = min(1.0, flow_width / w)
    return (max(64, int(round(w * scale / 2) * 2)),
            max(64, int(round(h * scale / 2) * 2)))


def nvofa_like(gt: np.ndarray, grid: int = 4, work=WORK) -> np.ndarray:
    """Ground truth reduced the way NVOFA grid N sees it, expanded the way
    kNvofaExpand stretches it.

    gt (work px) -> area-averaged to the 320x180 flow grid -> block-averaged
    to the N x N output grid -> quantised to S10.5 flow px (1/32) -> bilinear
    expand to the work size with the shader's sample positions and clamping
    -> |v|^2 < 0.25 zeroed."""
    w, h = work
    iw, ih = flow_size(work)
    small = cv2.resize(gt, (iw, ih), interpolation=cv2.INTER_AREA)
    small = small * np.array([iw / w, ih / h], np.float32)       # -> flow px
    fw, fh = (iw + grid - 1) // grid, (ih + grid - 1) // grid
    pad = np.zeros((fh * grid, fw * grid, 2), np.float32)
    pad[:ih, :iw] = small
    pad[ih:, :iw] = small[ih - 1:ih]
    pad[:, iw:] = pad[:, iw - 1:iw]
    blocks = pad.reshape(fh, grid, fw, grid, 2).mean(axis=(1, 3))
    blocks = np.round(blocks * 32.0) / 32.0
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    qx = (xs + 0.5) * iw / w / grid - 0.5
    qy = (ys + 0.5) * ih / h / grid - 0.5
    ax, ay = np.floor(qx).astype(np.int32), np.floor(qy).astype(np.int32)
    tx, ty = (qx - ax)[..., None], (qy - ay)[..., None]
    x0, x1 = np.clip(ax, 0, fw - 1), np.clip(ax + 1, 0, fw - 1)
    y0, y1 = np.clip(ay, 0, fh - 1), np.clip(ay + 1, 0, fh - 1)
    v = ((blocks[y0, x0] * (1 - tx) + blocks[y0, x1] * tx) * (1 - ty)
         + (blocks[y1, x0] * (1 - tx) + blocks[y1, x1] * tx) * ty)
    v = v * np.array([w / iw, h / ih], np.float32)                 # -> work px
    v[(v ** 2).sum(-1) < 0.25] = 0.0
    return v.astype(np.float32)


def parse_variant(spec: str):
    name, _, arg = spec.partition(":")
    name = name.strip().lower()
    if name not in ("gt", "zero", "flipped", "noisy", "coarse", "scale"):
        raise ValueError(f"unknown motion variant {spec!r}")
    if name == "noisy":
        return name, float(arg) if arg else 1.0
    if name == "coarse":
        return name, int(arg) if arg else 4
    if name == "scale":
        return name, float(arg) if arg else 1.0
    return name, None


def motion_for(variant: str, scene: Scene, t: int, work=WORK, seed=0) -> np.ndarray:
    """The motion field sent with frame t (float32, work px)."""
    name, arg = parse_variant(variant)
    w, h = work
    if t == 0 or name == "zero":
        return np.zeros((h, w, 2), np.float32)
    gt = scene.gt_work(t, work)
    if name == "gt":
        return gt
    if name == "flipped":
        return -gt
    if name == "scale":
        return gt * np.float32(arg)
    if name == "coarse":
        return nvofa_like(gt, arg, work)
    # noisy: i.i.d. N(0, sigma) per vector on the 320x180 flow grid, fresh
    # every frame, stretched bilinearly to the work size - spatially
    # correlated like a real flow field's error (sigma is per flow vector,
    # in work px; between grid points the bilinear blend lowers it).
    iw, ih = flow_size(work)
    rng = np.random.default_rng((seed, t))
    noise = rng.normal(0.0, arg, (ih, iw, 2)).astype(np.float32)
    noise = cv2.resize(noise, (w, h), interpolation=cv2.INTER_LINEAR)
    return gt + noise


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    variant: str
    outputs: list = field(default_factory=list)   # RGB uint8 full size, per frame
    log: str = ""
    ok: bool = True
    error: str = ""


def run_worker(scene: Scene, variant: str, frames: int = 40, work=WORK,
               env: dict | None = None, profile: dict | None = None,
               warmup: int = 8, seed: int = 0, timeout: float = 180.0) -> RunResult:
    """One fresh worker in Boost (work < full, NS_NR_SMALL=1), `frames`
    frames of `scene` with the `variant` motion field. Frame 0 is a reset
    with zero motion (the warmup re-evaluates frame 0 with its own motion,
    so anything else would warp the history by a vector that never
    happened)."""
    p = dict(PROFILE if profile is None else profile)
    full_w, full_h = scene.w, scene.h
    ww, wh = work
    header = struct.pack(
        HEADER_FMT, VIDEO_MAGIC, ww, wh, warmup, 0, 0, 0,
        int(p["style"]), int(p["auto_mask"]), 0,
        float(p["intensity"]), float(p["local_tone"]),
        float(p["local_structure"]), float(p["skin_structure"]),
        full_w, full_h)
    e = dict(os.environ)
    e.pop("NS_MOTION_BACKEND", None)
    e["NS_NR_SMALL"] = "1"
    e.update(env or {})
    res = RunResult(variant)
    proc = subprocess.Popen([str(WORKER_EXE), "--live"], cwd=str(NATIVE_DIR), env=e,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    logs: list = []
    drain = threading.Thread(target=lambda: logs.extend(iter(proc.stderr.readline, b"")),
                             daemon=True)
    drain.start()
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.start()
    out_size = struct.calcsize(OUT_FMT)
    try:
        proc.stdin.write(header)
        proc.stdin.flush()
        for t in range(frames):
            rgba = scene.frame(t)
            mv = motion_for(variant, scene, t, work, seed).astype(np.float16)
            proc.stdin.write(struct.pack(FRAME_FMT, FRAME_MAGIC, t, 1 if t == 0 else 0,
                                         FRAME_FLAG_WANT_PIXELS, t)
                             + rgba.tobytes() + np.ascontiguousarray(mv).tobytes())
            proc.stdin.flush()
            magic, idx, ok, nbytes, ngx, _pts = struct.unpack(
                OUT_FMT, read_reply(proc.stdout, out_size))
            if magic != OUT_MAGIC or not ok or idx != t:
                raise RuntimeError(f"bad reply frame {t}: magic=0x{magic:08X} "
                                   f"idx={idx} ok={ok} ngx=0x{ngx:08X}")
            if not nbytes:
                raise RuntimeError(f"frame {t}: no pixels came back")
            data = read_exact(proc.stdout, nbytes)
            px = np.frombuffer(data, np.uint8).reshape(full_h, full_w, 4)
            res.outputs.append(np.ascontiguousarray(px[..., :3]))
    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        res.ok, res.error = False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        watchdog.cancel()
        drain.join(timeout=5)
    res.log = b"".join(logs).decode("utf-8", "replace")
    return res


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _boundary(lbl: np.ndarray, band: int) -> np.ndarray:
    k = np.ones((2 * band + 1, 2 * band + 1), np.uint8)
    return cv2.dilate(lbl, k) != cv2.erode(lbl, k)


def valid_masks(scene: Scene, t: int, d: np.ndarray):
    """(valid, moving, static, halo) boolean masks for frame t."""
    h, w = scene.h, scene.w
    xs, ys = scene._xs, scene._ys
    px, py = xs + d[..., 0], ys + d[..., 1]
    m = MARGIN
    valid = ((xs >= m) & (xs <= w - 1 - m) & (ys >= m) & (ys <= h - 1 - m)
             & (px >= m) & (px <= w - 1 - m) & (py >= m) & (py <= h - 1 - m))
    lab_t = scene.labels(t, xs, ys)
    if lab_t is not None:
        lab_p = scene.labels(t - 1, xs, ys)
        ix = np.clip(np.rint(px), 0, w - 1).astype(np.int32)
        iy = np.clip(np.rint(py), 0, h - 1).astype(np.int32)
        valid &= lab_t == lab_p[iy, ix]
        valid &= ~_boundary(lab_t, BAND)
        valid &= ~_boundary(lab_p, BAND)[iy, ix]
    mag = np.hypot(d[..., 0], d[..., 1])
    moving_any = mag > 1e-3
    moving = valid & moving_any
    static = valid & ~moving_any
    if moving_any.any() and (~moving_any).any():
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * HALO + 1, 2 * HALO + 1))
        near = cv2.dilate(moving_any.astype(np.uint8), k).astype(bool)
        halo = static & near
    else:
        halo = np.zeros_like(static)
    return valid, moving, static, halo


def _warp_prev(img: np.ndarray, scene: Scene, d: np.ndarray) -> np.ndarray:
    return cv2.remap(img, scene._xs + d[..., 0], scene._ys + d[..., 1],
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def _mean(a: np.ndarray, m: np.ndarray) -> float:
    n = int(m.sum())
    return float(a[m].mean()) if n else float("nan")


def stability_metrics(scene: Scene, outputs: list, skip: int = 4) -> dict:
    """Per-frame metrics of the edit, averaged over frames t >= max(1, skip)."""
    keys = ("instab_mov", "instab_sta", "instab_halo", "warp_out_mov",
            "warp_out_sta", "warp_in_mov", "warp_in_sta", "edit_mov", "edit_sta")
    rows = {k: [] for k in keys}
    start = max(1, skip)
    prev_in = scene.frame(start - 1)[..., :3].astype(np.float32)
    prev_out = outputs[start - 1].astype(np.float32)
    for t in range(start, len(outputs)):
        cur_in = scene.frame(t)[..., :3].astype(np.float32)
        cur_out = outputs[t].astype(np.float32)
        d = scene.gt_full(t)
        _valid, mov, sta, halo = valid_masks(scene, t, d)
        e_cur = cur_out - cur_in
        e_prev = _warp_prev(prev_out - prev_in, scene, d)
        inst = np.abs(e_cur - e_prev).mean(-1)
        wout = np.abs(cur_out - _warp_prev(prev_out, scene, d)).mean(-1)
        win = np.abs(cur_in - _warp_prev(prev_in, scene, d)).mean(-1)
        emag = np.abs(e_cur).mean(-1)
        rows["instab_mov"].append(_mean(inst, mov))
        rows["instab_sta"].append(_mean(inst, sta))
        rows["instab_halo"].append(_mean(inst, halo))
        rows["warp_out_mov"].append(_mean(wout, mov))
        rows["warp_out_sta"].append(_mean(wout, sta))
        rows["warp_in_mov"].append(_mean(win, mov))
        rows["warp_in_sta"].append(_mean(win, sta))
        rows["edit_mov"].append(_mean(emag, mov))
        rows["edit_sta"].append(_mean(emag, sta))
        prev_in, prev_out = cur_in, cur_out
    agg = {}
    for k, v in rows.items():
        arr = np.array(v, np.float64)
        agg[k] = float(np.nanmean(arr)) if np.isfinite(arr).any() else float("nan")
    agg["ratio_mov"] = agg["instab_mov"] / agg["edit_mov"] if agg["edit_mov"] else float("nan")
    agg["frames_scored"] = len(rows["instab_mov"])
    agg["per_frame_instab_mov"] = rows["instab_mov"]
    return agg


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

COLUMNS = [("instab_mov", "inst_mov"), ("instab_sta", "inst_sta"),
           ("instab_halo", "inst_halo"), ("ratio_mov", "inst/edit"),
           ("warp_out_mov", "wout_mov"), ("warp_out_sta", "wout_sta"),
           ("edit_mov", "edit_mov"), ("edit_sta", "edit_sta")]


def format_table(scene_name: str, results: list, md: bool = False) -> str:
    def f(x):
        return "-" if x != x else f"{x:.3f}"
    head = ["variant"] + [c[1] for c in COLUMNS]
    lines = []
    if md:
        lines.append("| " + " | ".join(head) + " |")
        lines.append("|" + "---|" * len(head))
        for name, m in results:
            lines.append("| " + " | ".join([name] + [f(m[k]) for k, _ in COLUMNS]) + " |")
    else:
        lines.append(f"{'variant':<12}" + "".join(f"{h:>11}" for h in head[1:]))
        for name, m in results:
            lines.append(f"{name:<12}" + "".join(f"{f(m[k]):>11}" for k, _ in COLUMNS))
    return "\n".join(lines)


def _size(s: str):
    a, b = s.lower().split("x")
    return int(a), int(b)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scene", default="pan",
                    help="pan, object, static, rotzoom (comma list)")
    ap.add_argument("--variants", default="gt,zero,coarse,noisy,flipped")
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--skip", type=int, default=4, help="frames left out of the averages")
    ap.add_argument("--full", type=_size, default=FULL)
    ap.add_argument("--work", type=_size, default=WORK)
    ap.add_argument("--speed", default=None, help="vx,vy full px/frame (pan, object)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE for the worker")
    ap.add_argument("--md", default=None, help="append markdown tables to this file")
    ap.add_argument("--dump", default=None, help="save the last output/edit PNGs here")
    a = ap.parse_args(argv)
    if not WORKER_EXE.is_file():
        print(f"worker not found: {WORKER_EXE}")
        return 1
    env = dict(kv.split("=", 1) for kv in a.env)
    speed = tuple(float(v) for v in a.speed.split(",")) if a.speed else None
    variants = [v.strip() for v in a.variants.split(",") if v.strip()]
    for v in variants:
        parse_variant(v)
    md_out = []
    for scene_name in [s.strip() for s in a.scene.split(",") if s.strip()]:
        scene = make_scene(scene_name, a.frames, a.full, a.seed, speed)
        results = []
        seen: dict = {}
        for v in variants:
            seen[v] = seen.get(v, 0) + 1
            label = v if seen[v] == 1 else f"{v}#{seen[v]}"
            r = run_worker(scene, v, a.frames, a.work, env=env, seed=a.seed)
            if not r.ok:
                tail = [ln for ln in r.log.splitlines() if ln.strip()][-5:]
                print(f"[{scene_name}/{label}] FAILED: {r.error}\n  " + "\n  ".join(tail))
                continue
            m = stability_metrics(scene, r.outputs, a.skip)
            results.append((label, m))
            print(f"[{scene_name}/{label}] inst_mov {m['instab_mov']:.3f} "
                  f"inst_sta {m['instab_sta']:.3f} edit_mov {m['edit_mov']:.3f}",
                  flush=True)
            if a.dump:
                d = Path(a.dump)
                d.mkdir(parents=True, exist_ok=True)
                t = len(r.outputs) - 1
                cv2.imwrite(str(d / f"{scene_name}-{label}-out.png"),
                            cv2.cvtColor(r.outputs[t], cv2.COLOR_RGB2BGR))
                e = (r.outputs[t].astype(np.int16)
                     - scene.frame(t)[..., :3].astype(np.int16))
                cv2.imwrite(str(d / f"{scene_name}-{label}-edit.png"),
                            cv2.cvtColor(np.clip(e * 4 + 128, 0, 255).astype(np.uint8),
                                         cv2.COLOR_RGB2BGR))
            r.outputs.clear()
        if not results:
            continue
        floor = results[0][1]
        title = (f"scene={scene_name} frames={a.frames} skip={a.skip} "
                 f"full={a.full[0]}x{a.full[1]} work={a.work[0]}x{a.work[1]} "
                 f"warp_in mov/sta={floor['warp_in_mov']:.3f}/{floor['warp_in_sta']:.3f}")
        print("\n" + title)
        print(format_table(scene_name, results))
        md_out.append(f"### {title}\n\n{format_table(scene_name, results, md=True)}\n")
    if a.md and md_out:
        with open(a.md, "a", encoding="utf-8") as fh:
            fh.write("\n".join(md_out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
