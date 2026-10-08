"""Seed-reproducible grey sequences for the scene-cut tests.

The generators come from the scene-cut evaluation (_work/scene-cut, 08.10.2026):
each renders frames at 1280x720 and area-downsamples them to the 320x180 grey
NeuralScreen scores, so edges and sub-pixel motion look like a real capture.
Every generator takes a seed and returns (frames uint8 [N, 180, 320],
labels int8 [N]): labels[t] describes the pair (t-1, t) - 1 a scene cut that
must reset, 0 the same scene that must not, -1 don't care.

MUST_NOT_CUT / MUST_CUT are the regression vectors: the old 0.24 rule fails 9
of them, the rule in app/scene_cut.py passes all. BORDERLINE is informative.

Not a test itself: tests/test_scene_cut.py imports it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

W, H = 320, 180            # flow / scene grey size
RENDER_SCALE = 4           # frames are rendered at 1280x720, then INTER_AREA
NOISE_SIGMA = 1.0          # per-pixel noise at 320x180 (decode/capture noise)

# ---------------------------------------------------------------------------
# Textures (hi-res float32 / uint8 grey worlds)
# ---------------------------------------------------------------------------


def _octave_noise(rng: np.random.Generator, h: int, w: int, periods, slope=0.9):
    acc = np.zeros((h, w), np.float32)
    for p in periods:
        p_hi = p * RENDER_SCALE
        gh, gw = max(2, int(h / p_hi) + 2), max(2, int(w / p_hi) + 2)
        g = rng.standard_normal((gh, gw)).astype(np.float32)
        acc += cv2.resize(g, (w, h), interpolation=cv2.INTER_CUBIC) * (p ** slope)
    return acc


def _normalize(img: np.ndarray, lo: float, hi: float) -> np.ndarray:
    a, b = np.percentile(img, (1, 99))
    out = (img - a) / max(1e-6, b - a) * (hi - lo) + lo
    return np.clip(out, 0, 255).astype(np.uint8)


def tex_natural(rng, h, w, lo=15, hi=240):
    """Photo-like: 1/f noise plus soft-edged blobs (edges a natural image has)."""
    img = _octave_noise(rng, h, w, (160, 80, 40, 20, 10, 5, 2.5))
    out = _normalize(img, lo, hi).astype(np.float32)
    n = int(h * w / RENDER_SCALE ** 2 / 2500)
    over = np.zeros_like(out)
    alpha = np.zeros_like(out)
    for _ in range(n):
        cx, cy = int(rng.integers(0, w)), int(rng.integers(0, h))
        ax = int(rng.uniform(6, 40) * RENDER_SCALE)
        ay = int(ax * rng.uniform(0.4, 1.0))
        cv2.ellipse(over, (cx, cy), (ax, ay), float(rng.uniform(0, 180)), 0, 360,
                    float(rng.uniform(lo, hi)), -1)
        cv2.ellipse(alpha, (cx, cy), (ax, ay), 0, 0, 360, 0.5, -1)
    out = out * (1 - alpha) + over * alpha
    return np.clip(out, 0, 255).astype(np.uint8)


def tex_dark(rng, h, w):
    """Night game scene: luma 0..55 with a few bright lights."""
    img = tex_natural(rng, h, w, lo=2, hi=55)
    n = int(h * w / RENDER_SCALE ** 2 / 6000) + 1
    for _ in range(n):
        cx, cy = int(rng.integers(0, w)), int(rng.integers(0, h))
        r = int(rng.uniform(1, 5) * RENDER_SCALE)
        cv2.circle(img, (cx, cy), r, int(rng.integers(150, 255)), -1)
    return img


CEL_PALETTE = (245, 225, 200, 175, 150, 125, 100, 75, 50, 30)


def _cel_shape(rng, img, cx, cy, size, level, outline=12):
    thick = max(2, int(rng.integers(1, 3) * RENDER_SCALE * 0.75))
    kind = int(rng.integers(3))
    if kind == 0:
        k = int(rng.integers(3, 8))
        ang = np.sort(rng.uniform(0, 2 * np.pi, k))
        rad = size * rng.uniform(0.5, 1.0, k)
        pts = np.stack([cx + rad * np.cos(ang), cy + rad * np.sin(ang)], 1).astype(np.int32)
        cv2.fillPoly(img, [pts], level)
        cv2.polylines(img, [pts], True, outline, thick)
    elif kind == 1:
        axes = (int(size), int(size * rng.uniform(0.3, 1.0)))
        angle = float(rng.uniform(0, 180))
        cv2.ellipse(img, (int(cx), int(cy)), axes, angle, 0, 360, level, -1)
        cv2.ellipse(img, (int(cx), int(cy)), axes, angle, 0, 360, outline, thick)
    else:
        rw, rh = int(size), int(size * rng.uniform(0.5, 2.0))
        p0, p1 = (int(cx - rw / 2), int(cy - rh / 2)), (int(cx + rw / 2), int(cy + rh / 2))
        cv2.rectangle(img, p0, p1, level, -1)
        cv2.rectangle(img, p0, p1, outline, thick)


def tex_cel(rng, h, w):
    """Anime-like cel art: flat high-contrast fills with black outlines over a
    two-tone sky gradient. The reported failure content."""
    top, bot = int(rng.integers(150, 240)), int(rng.integers(60, 200))
    img = np.repeat(np.linspace(top, bot, h, dtype=np.float32)[:, None], w, 1).astype(np.uint8)
    n = int(h * w / RENDER_SCALE ** 2 / 700)
    for _ in range(n):
        _cel_shape(rng, img, rng.uniform(0, w), rng.uniform(0, h),
                   rng.uniform(8, 70) * RENDER_SCALE, int(rng.choice(CEL_PALETTE)))
    return img


def tex_text(rng, h, w):
    """Dark-theme desktop: panels and lines of small high-frequency 'glyphs'."""
    S = RENDER_SCALE
    img = np.full((h, w), 28, np.uint8)
    for _ in range(int(h * w / S ** 2 / 20000) + 2):
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
        x1, y1 = x0 + int(rng.uniform(60, 200) * S), y0 + int(rng.uniform(30, 150) * S)
        cv2.rectangle(img, (x0, y0), (x1, y1), int(rng.integers(34, 60)), -1)
        cv2.rectangle(img, (x0, y0), (x1, y1), 75, S // 2)
    y = 4 * S
    while y < h - 4 * S:
        x = int(rng.uniform(4, 40) * S)
        end = w - int(rng.uniform(4, 120) * S)
        gh = int(1.5 * S)
        while x < end:
            ww = int(rng.uniform(3, 14) * S)
            pat = (rng.random((gh, min(ww, w - x))) > 0.55) * int(rng.integers(150, 230))
            img[y:y + gh, x:x + pat.shape[1]] = np.maximum(img[y:y + gh, x:x + pat.shape[1]],
                                                           pat.astype(np.uint8))
            x += ww + int(1.2 * S)
        y += int(rng.uniform(5, 9) * S)
    return img


def tex_cel_flat(rng, h, w):
    """Cel art with large flat areas (backgrounds, close-ups): fewer, bigger shapes."""
    top, bot = int(rng.integers(150, 240)), int(rng.integers(60, 200))
    img = np.repeat(np.linspace(top, bot, h, dtype=np.float32)[:, None], w, 1).astype(np.uint8)
    n = int(h * w / RENDER_SCALE ** 2 / 3000) + 1
    for _ in range(n):
        _cel_shape(rng, img, rng.uniform(0, w), rng.uniform(0, h),
                   rng.uniform(20, 120) * RENDER_SCALE, int(rng.choice(CEL_PALETTE)))
    return img


TEXTURES = {"cel": tex_cel, "cel_flat": tex_cel_flat, "natural": tex_natural,
            "dark": tex_dark, "text": tex_text}


def make_world(family: str, seed: int, w320: float, h320: float) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return TEXTURES[family](rng, int(math.ceil(h320 * RENDER_SCALE)),
                            int(math.ceil(w320 * RENDER_SCALE)))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _down(view_hi: np.ndarray) -> np.ndarray:
    return cv2.resize(view_hi, (W, H), interpolation=cv2.INTER_AREA)


def _crop(world: np.ndarray, x320: float, y320: float) -> np.ndarray:
    x, y = int(round(x320 * RENDER_SCALE)), int(round(y320 * RENDER_SCALE))
    return world[y:y + H * RENDER_SCALE, x:x + W * RENDER_SCALE]


def _finish(frames, rng) -> np.ndarray:
    out = np.stack(frames).astype(np.float32)
    if NOISE_SIGMA > 0:
        out += rng.standard_normal(out.shape).astype(np.float32) * NOISE_SIGMA
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def _labels(n, cuts=(), dont_care=()):
    lab = np.zeros(n, np.int8)
    lab[0] = -1
    for c in cuts:
        lab[c] = 1
    for d in dont_care:
        lab[d] = -1
    return lab


def _pan_views(family, seed, speed, angle_deg, n, k_hold=1, extra=0.0):
    """Views of a translating camera; k_hold>1 = content updated every k frames
    (anime on twos/threes, 24 fps video captured at 60)."""
    a = math.radians(angle_deg)
    vx, vy = speed * math.cos(a), speed * math.sin(a)
    span_x, span_y = abs(vx) * n + 2, abs(vy) * n + 2
    world = make_world(family, seed, W + span_x + extra, H + span_y + extra)
    x0 = 1 if vx >= 0 else span_x - 1
    y0 = 1 if vy >= 0 else span_y - 1
    views = []
    for t in range(n):
        tt = (t // k_hold) * k_hold
        views.append(_down(_crop(world, x0 + vx * tt, y0 + vy * tt)))
    return views


def seq_pan(family, seed, speed, angle_deg=0.0, n=32, k_hold=1):
    rng = np.random.default_rng(seed + 7919)
    return _finish(_pan_views(family, seed, speed, angle_deg, n, k_hold), rng), _labels(n)


def seq_static(family, seed, n=24):
    return seq_pan(family, seed, 0.0, 0.0, n)


def _warp_views(family, seed, n, scale_step=1.0, rot_step=0.0):
    S = RENDER_SCALE
    base = make_world(family, seed, W * 1.6, H * 1.6)
    bh, bw = base.shape
    views = []
    for t in range(n):
        m = cv2.getRotationMatrix2D((bw / 2, bh / 2), rot_step * t, scale_step ** t)
        m[0, 2] -= (bw - W * S) / 2
        m[1, 2] -= (bh - H * S) / 2
        views.append(_down(cv2.warpAffine(base, m, (W * S, H * S), flags=cv2.INTER_LINEAR,
                                          borderMode=cv2.BORDER_REFLECT)))
    return views


def seq_zoom(family, seed, scale_step, n=24):
    rng = np.random.default_rng(seed + 7919)
    views = _warp_views(family, seed, n, scale_step=scale_step)
    if scale_step < 1.0:   # zoom-out: render the zoom-in and play it backwards
        views = _warp_views(family, seed, n, scale_step=1.0 / scale_step)[::-1]
    return _finish(views, rng), _labels(n)


def seq_rotate(family, seed, deg_per_frame, n=24):
    rng = np.random.default_rng(seed + 7919)
    return _finish(_warp_views(family, seed, n, rot_step=deg_per_frame), rng), _labels(n)


def seq_shake(family, seed, amplitude, n=24):
    """Impact shake: the camera jumps to a random offset in [-A, A]^2 each frame."""
    rng = np.random.default_rng(seed + 7919)
    world = make_world(family, seed, W + 2 * amplitude + 2, H + 2 * amplitude + 2)
    views = []
    for _ in range(n):
        dx, dy = rng.uniform(-amplitude, amplitude, 2)
        views.append(_down(_crop(world, amplitude + 1 + dx, amplitude + 1 + dy)))
    return _finish(views, rng), _labels(n)


def _character(rng, size320=(110, 150)):
    """A cel 'character': a cluster of outlined shapes with a mask."""
    S = RENDER_SCALE
    cw, ch = size320[0] * S, size320[1] * S
    canvas = np.zeros((ch, cw), np.uint8)
    mask = np.zeros((ch, cw), np.uint8)
    for _ in range(14):
        cx, cy = rng.uniform(0.25, 0.75) * cw, rng.uniform(0.2, 0.8) * ch
        size = rng.uniform(0.12, 0.3) * cw
        lvl = int(rng.choice(CEL_PALETTE))
        tmp = np.zeros_like(mask)
        _cel_shape(rng, tmp, cx, cy, size, 255, outline=255)
        _cel_shape(np.random.default_rng(int(rng.integers(1 << 30))), canvas, cx, cy, size, lvl)
        mask = np.maximum(mask, tmp)
    return canvas, mask > 0


def seq_object(family, seed, speed, n=24, k_hold=1):
    """A large cel character crossing a static background at `speed` px/frame
    (local motion: ~1/4 of the frame moves, the rest stands still)."""
    rng = np.random.default_rng(seed + 7919)
    S = RENDER_SCALE
    world = make_world(family, seed, W + 2, H + 2)
    char, mask = _character(np.random.default_rng(seed + 31))
    ch, cw = char.shape
    start = W / 2 - speed * n / 2 - cw / (2 * S)
    views = []
    for t in range(n):
        tt = (t // k_hold) * k_hold
        bg = _crop(world, 1, 1).copy()
        x = int(round((start + speed * tt) * S))
        y = int(0.15 * H * S)
        x0, x1 = max(0, x), min(W * S, x + cw)
        if x1 > x0:
            m = mask[:, x0 - x:x1 - x]
            region = bg[y:y + ch, x0:x1]
            region[m] = char[:, x0 - x:x1 - x][m]
        views.append(_down(bg))
    return _finish(views, rng), _labels(n)


def seq_fade(family, seed, frames_ramp, to_white=False, n_hold=3, pan_speed=0.0, floor=0.03):
    """Fade out to black (or white) over `frames_ramp` frames, hold, fade back in.
    floor = the gain at the bottom (0.03: the held frames are blank)."""
    rng = np.random.default_rng(seed + 7919)
    n = 2 * frames_ramp + n_hold + 4
    views = _pan_views(family, seed, pan_speed, 0.0, n)
    g = np.concatenate([np.ones(2), np.linspace(1, floor, frames_ramp + 1)[1:],
                        np.full(n_hold, floor), np.linspace(floor, 1, frames_ramp + 1)[1:],
                        np.ones(2)])[:n]
    out = []
    for v, k in zip(views, g):
        v = v.astype(np.float32)
        out.append(255 - (255 - v) * k if to_white else v * k)
    return _finish(out, rng), _labels(n)


def seq_dissolve(fam_a, fam_b, seed, frames_ramp, n_pad=4):
    """Cross-dissolve A -> B: gradual, no single frame is a cut."""
    rng = np.random.default_rng(seed + 7919)
    a = _down(_crop(make_world(fam_a, seed, W + 2, H + 2), 1, 1)).astype(np.float32)
    b = _down(_crop(make_world(fam_b, seed + 1000, W + 2, H + 2), 1, 1)).astype(np.float32)
    al = np.concatenate([np.zeros(n_pad), np.linspace(0, 1, frames_ramp + 1)[1:], np.ones(n_pad)])
    return _finish([a * (1 - x) + b * x for x in al], rng), _labels(len(al))


def seq_flash(family, seed, strength=0.7, pan_speed=8.0, n=16, at=8):
    """One white flash frame (anime impact frame). Pairs into and out of it are
    don't-care; everything else must not cut."""
    rng = np.random.default_rng(seed + 7919)
    views = [v.astype(np.float32) for v in _pan_views(family, seed, pan_speed, 0.0, n)]
    views[at] = views[at] * (1 - strength) + 255 * strength
    return _finish(views, rng), _labels(n, dont_care=(at, at + 1))


def seq_exposure_step(family, seed, gain=1.6, n=16, at=8):
    """Game auto-exposure / lighting snap: gain step that stays. Don't care."""
    rng = np.random.default_rng(seed + 7919)
    views = [v.astype(np.float32) for v in _pan_views(family, seed, 4.0, 0.0, n)]
    for t in range(at, n):
        views[t] = np.clip(views[t] * gain, 0, 255)
    return _finish(views, rng), _labels(n, dont_care=(at,))


def _shot(family, seed, motion, n):
    kind, val = motion
    if kind == "static":
        return _pan_views(family, seed, 0.0, 0.0, n)
    if kind == "pan":
        return _pan_views(family, seed, val, 0.0, n)
    if kind == "vpan":
        return _pan_views(family, seed, val, 90.0, n)
    if kind == "zoom":
        return _warp_views(family, seed, n, scale_step=val)
    raise ValueError(kind)


def seq_cut(fam_a, fam_b, seed, motion_a=("static", 0), motion_b=("static", 0), n_shot=10,
            match_histogram=False, same_world_far=False):
    """Hard cut between two shots. match_histogram: B gets A's exact luma
    histogram (rank-matched) - the histogram arm cannot see it. same_world_far:
    B is a far, non-overlapping region of A's world (same palette/style)."""
    rng = np.random.default_rng(seed + 7919)
    a = _shot(fam_a, seed, motion_a, n_shot)
    if same_world_far:
        world = make_world(fam_a, seed, W * 3 + 2, H + 2)
        b = [_down(_crop(world, 1 + 2 * W, 1))] * n_shot
        if motion_b[0] == "pan":
            world = make_world(fam_a, seed, W * 3 + 2 + motion_b[1] * n_shot, H + 2)
            b = [_down(_crop(world, 1 + 2 * W + motion_b[1] * t, 1)) for t in range(n_shot)]
    else:
        b = _shot(fam_b, seed + 1000, motion_b, n_shot)
    if match_histogram:
        ref = np.sort(a[-1].ravel())
        mb = []
        for f in b:
            order = np.argsort(f.ravel(), kind="stable")
            out = np.empty(f.size, np.uint8)
            out[order] = ref
            mb.append(out.reshape(f.shape))
        b = mb
    return _finish(list(a) + list(b), rng), _labels(2 * n_shot, cuts=(n_shot,))


def seq_blank_insert(family, seed, n_blank=3, level=16, n_side=6):
    """Picture -> n_blank flat frames (level +-noise) -> the same picture.
    Into the blank: don't care; blank -> picture: must cut (history is empty)."""
    rng = np.random.default_rng(seed + 7919)
    pic = _down(_crop(make_world(family, seed, W + 2, H + 2), 1, 1)).astype(np.float32)
    flat = np.full_like(pic, level)
    frames = [pic] * n_side + [flat] * n_blank + [pic] * n_side
    return _finish(frames, rng), _labels(len(frames), cuts=(n_side + n_blank,), dont_care=(n_side,))


def seq_anime_clip(seed=1, n=120):
    """A 120-frame fast anime-like action clip assembled from the pieces above:
    fast pans, a character crossing on twos, impact shake with one flash,
    zoom, diagonal pan, 5 hard cuts. Analogue of SmoothMyVideo's report
    ("NeuralScreen's reset 42 of 120 frames of a fast anime clip")."""
    parts = [
        seq_pan("cel", seed, 24, 0.0, n=22),
        seq_object("cel", seed + 1, 32, n=18, k_hold=2),
        seq_shake("cel", seed + 2, 12, n=16),
        seq_pan("cel", seed + 3, 32, 90.0, n=16, k_hold=2),
        seq_zoom("cel", seed + 4, 1.05, n=16),
        seq_pan("cel", seed + 5, 40, 25.0, n=32),
    ]
    frames = np.concatenate([p[0] for p in parts])[:n]
    labels = np.concatenate([p[1] for p in parts])[:n].copy()
    starts = np.cumsum([0] + [len(p[0]) for p in parts])[:-1]
    for s in starts[1:]:
        labels[s] = 1
    labels[0] = -1
    # one impact flash inside the shake shot
    f = starts[2] + 7
    frames[f] = np.clip(frames[f].astype(np.float32) * 0.3 + 255 * 0.7, 0, 255).astype(np.uint8)
    labels[f] = labels[f + 1] = -1
    return frames, labels


MUST_NOT_CUT = {
    # the reported failure: fast cel-art motion (today's rule fires on these)
    "cel_pan_h_40": lambda: seq_pan("cel", 101, 40.0, 0.0),
    "cel_pan_diag_32": lambda: seq_pan("cel", 102, 32.0, 30.0),
    "cel_pan_v_16_twos": lambda: seq_pan("cel", 103, 16.0, 90.0, k_hold=2),
    "cel_shake_12": lambda: seq_shake("cel", 106, 12.0),
    "anime_clip_no_cut_pairs": lambda: seq_anime_clip(seed=1),   # only label-0 pairs checked
    # other motion
    "natural_pan_h_40": lambda: seq_pan("natural", 104, 40.0, 0.0),
    "text_scroll_v_40": lambda: seq_pan("text", 105, 40.0, 90.0),
    "cel_zoom_1.05": lambda: seq_zoom("cel", 107, 1.05),
    "cel_rotate_3": lambda: seq_rotate("cel", 113, 3.0),
    "cel_object_40": lambda: seq_object("cel", 114, 40.0),
    # brightness
    "natural_fade_to_25pc_4": lambda: seq_fade("natural", 108, 4, floor=0.25),
    "cel_fade_white_to_25pc_8": lambda: seq_fade("cel", 109, 8, to_white=True, floor=0.25),
    "cel_flash_0.8": lambda: seq_flash("cel", 115, 0.8),         # flash pairs are don't-care
    "natural_exposure_x1.8": lambda: seq_exposure_step("natural", 116, 1.8),
    "dissolve_cel_natural_8": lambda: seq_dissolve("cel", "natural", 110, 8),
    "static_text": lambda: seq_static("text", 111),
}

MUST_CUT = {
    "cut_cel_cel_static": lambda: seq_cut("cel", "cel", 201),
    "cut_cel_cel_pan24": lambda: seq_cut("cel", "cel", 202, ("pan", 24), ("pan", 24)),
    "cut_natural_natural": lambda: seq_cut("natural", "natural", 203),
    "cut_dark_dark": lambda: seq_cut("dark", "dark", 204),               # today's rule misses
    "cut_text_text": lambda: seq_cut("text", "text", 205),               # today's rule misses
    "cut_natural_hist_matched": lambda: seq_cut("natural", "cel", 206, match_histogram=True),
    "cut_natural_same_world_far": lambda: seq_cut("natural", "natural", 207, same_world_far=True),
    "cut_from_black": lambda: seq_blank_insert("natural", 209),
}


# Inside the grey zone of the recommended rule (envelope residual 0.07-0.10 on
# noise-like fine text): informative, NOT for a pass/fail gate.
BORDERLINE = {
    "text_pan_subpixel_4_a30 (no cut)": lambda: seq_pan("text", 112, 4.0, 30.0),
    "text_pan_13.25_a30 (no cut)": lambda: seq_pan("text", 1036, 13.25, 30.0, n=16),
    "cut_text_text_pan24 (cut)": lambda: seq_cut("text", "text", 208, ("pan", 24), ("pan", 24)),
}


