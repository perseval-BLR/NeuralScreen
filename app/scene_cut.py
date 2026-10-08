"""The scene-cut decision: when the network's temporal history is thrown away.

A reset drops what the network has built up, so the picture visibly pops; a
missed cut warps the history of one picture into another. The rule this
replaces - mean(|grey - previous grey|) / 255 > 0.24 - was wrong both ways:
on cel-shaded animation panning faster than its flat areas the difference of
two frames of ONE scene reaches 0.25-0.36 (4.8% false cuts over the test set,
47-54 resets in 120 frames of a fast anime clip with five real cuts), while
a cut between two dark or two text-heavy pictures scores 0.05-0.10 and was
missed (39% of the cuts). One absolute number cannot be both.

This rule (evaluation and test vectors: _work/scene-cut, tests/scene_vectors.py):

1. Work on L1 = the 320x180 grey box-averaged to 160x90 ((a+b+c+d+2)>>2).
2. Mean and mean absolute deviation (MAD) of both frames. If the
   lower-contrast frame is blank (MAD < 3 levels), it is a cut exactly when
   the other is a picture (MAD >= 6): history from a blank frame holds nothing.
3. Map the higher-contrast frame onto the other's mean and contrast (gain <=
   1): a fade or an exposure step is close to affine and leaves no residual.
4. Find the best global translation, +-48 px at 320x180, on a pyramid
   (exhaustive +-6 at 40x22, the three best refined at 80x45 and 160x90, no
   shift competing at the end) - a pan of the same scene then lines up.
5. A cut needs BOTH: the aligned mean absolute difference above 0.40 x 2 x
   MAD_lo, and the part of the current frame outside the 3x3 min/max envelope
   of the aligned previous frame above 0.08 x 2 x MAD_lo (sub-pixel motion,
   zoom and resampling stay inside the envelope; a different picture does not).

Measured on the evaluation set: 0.61% false cuts and 1.3% missed against
4.78% / 39.4%; the anime clip resets on its five cuts and nowhere else.

Everything is integer arithmetic up to cross-multiplied comparisons, so the
worker's C++ (native/scene_cut.h) gives the same decisions on the same grey;
tests/test_scene_cut.py checks that they agree.
"""
from __future__ import annotations

import numpy as np

NORM_CUT_PERMILLE = 400       # aligned SAD / n > 0.40 * 2 * MAD_lo / N
ENV_CUT_PERMILLE = 80         # envelope residual / n > 0.08 * 2 * MAD_lo / N
BLANK_X16 = 48                # MAD_sum * 16 < 48 * N: blank (3 levels)
PICTURE_X16 = 96              # MAD_sum * 16 >= 96 * N: a picture (6 levels)
COARSE_RADIUS = 6             # at 40x22: +-48 px at 320x180
TOP_K = 3
GAIN_BITS = 12


def box2(g: np.ndarray) -> np.ndarray:
    """2x2 box average, round half up; an odd last row or column is dropped."""
    h, w = (g.shape[0] // 2) * 2, (g.shape[1] // 2) * 2
    s = (g[0:h:2, 0:w:2].astype(np.uint16) + g[1:h:2, 0:w:2]
         + g[0:h:2, 1:w:2] + g[1:h:2, 1:w:2])
    return ((s + 2) >> 2).astype(np.uint8)


def _overlap(shape, dx: int, dy: int):
    h, w = shape
    return max(0, -dx), min(w, w - dx), max(0, -dy), min(h, h - dy)


def _sad(cur: np.ndarray, prev: np.ndarray, dx: int, dy: int):
    """sum |cur(x, y) - prev(x + dx, y + dy)| over the overlap, and its size."""
    x0, x1, y0, y1 = _overlap(cur.shape, dx, dy)
    if x1 <= x0 or y1 <= y0:
        return 0, 0
    c = cur[y0:y1, x0:x1].astype(np.int16)
    p = prev[y0 + dy:y1 + dy, x0 + dx:x1 + dx].astype(np.int16)
    return int(np.abs(c - p).sum(dtype=np.int64)), (y1 - y0) * (x1 - x0)


def _better(s: int, n: int, bs: int, bn: int) -> bool:
    """s/n < bs/bn, exactly; bn == 0 means there is no best yet."""
    return bn == 0 or s * bn < bs * n


def _align(cur: np.ndarray, prev: np.ndarray):
    """Best global integer shift cur -> prev; returns (dx, dy, sad, n) on cur's level."""
    pyr_c, pyr_p = [cur], [prev]
    for _ in range(2):
        pyr_c.append(box2(pyr_c[-1]))
        pyr_p.append(box2(pyr_p[-1]))
    c, p = pyr_c[-1], pyr_p[-1]
    top: list = []          # (s, n, dx, dy), best first
    for dy in range(-COARSE_RADIUS, COARSE_RADIUS + 1):
        for dx in range(-COARSE_RADIUS, COARSE_RADIUS + 1):
            s, n = _sad(c, p, dx, dy)
            if not n:
                continue
            if len(top) == TOP_K and not _better(s, n, top[-1][0], top[-1][1]):
                continue
            i = len(top)
            while i > 0 and _better(s, n, top[i - 1][0], top[i - 1][1]):
                i -= 1
            top.insert(i, (s, n, dx, dy))
            del top[TOP_K:]
    refined = []
    for _, _, bx, by in top:
        for lc, lp in ((pyr_c[1], pyr_p[1]), (pyr_c[0], pyr_p[0])):
            cx, cy = 2 * bx, 2 * by
            bs = bn = 0
            for dy in range(cy - 1, cy + 2):
                for dx in range(cx - 1, cx + 2):
                    s, n = _sad(lc, lp, dx, dy)
                    if n and _better(s, n, bs, bn):
                        bs, bn, bx, by = s, n, dx, dy
        refined.append((bx, by))
    bs, bn = _sad(cur, prev, 0, 0)
    bx = by = 0
    for dx, dy in refined:
        s, n = _sad(cur, prev, dx, dy)
        if n and _better(s, n, bs, bn):
            bs, bn, bx, by = s, n, dx, dy
    return bx, by, bs, bn


def _stats(g: np.ndarray):
    n = g.size
    mu = (int(g.sum(dtype=np.int64)) * 2 + n) // (2 * n)
    return mu, int(np.abs(g.astype(np.int16) - mu).sum(dtype=np.int64))


def _map(hi: np.ndarray, st_hi, st_lo) -> np.ndarray:
    mu_hi, mad_hi = st_hi
    mu_lo, mad_lo = st_lo
    g = (mad_lo * (2 << GAIN_BITS) + mad_hi) // (2 * mad_hi) if mad_hi else 0
    out = mu_lo + (((hi.astype(np.int32) - mu_hi) * g + (1 << (GAIN_BITS - 1))) >> GAIN_BITS)
    return np.clip(out, 0, 255).astype(np.uint8)


def _envelope(cur: np.ndarray, prev: np.ndarray, dx: int, dy: int) -> int:
    """How far cur falls outside the 3x3 min/max of prev (edge replicated)."""
    padded = np.pad(prev, 1, mode="edge")
    h, w = prev.shape
    views = [padded[y:y + h, x:x + w] for y in range(3) for x in range(3)]
    pmax = np.maximum.reduce(views)
    pmin = np.minimum.reduce(views)
    x0, x1, y0, y1 = _overlap(cur.shape, dx, dy)
    c = cur[y0:y1, x0:x1].astype(np.int16)
    hi = pmax[y0 + dy:y1 + dy, x0 + dx:x1 + dx].astype(np.int16)
    lo = pmin[y0 + dy:y1 + dy, x0 + dx:x1 + dx].astype(np.int16)
    return int((np.maximum(c - hi, 0) + np.maximum(lo - c, 0)).sum(dtype=np.int64))


def measure(cur_grey: np.ndarray, prev_l1: np.ndarray):
    """Compare a new grey frame with the previous frame's L1.

    Returns (cut, score, l1, detail): l1 is this frame's L1, to pass as
    prev_l1 next time; score is the aligned difference in units of 2 x MAD_lo,
    clamped to 1 (a cut needs more than 0.40 of it, and the envelope test);
    detail carries the integers the C++ port has to reproduce.
    """
    l1 = box2(cur_grey)
    st_c, st_p = _stats(l1), _stats(prev_l1)
    npix = l1.size
    mad_lo, mad_hi = min(st_c[1], st_p[1]), max(st_c[1], st_p[1])
    detail = {"mad_lo": mad_lo, "mad_hi": mad_hi}
    if mad_lo * 16 < BLANK_X16 * npix:
        cut = mad_hi * 16 >= PICTURE_X16 * npix
        return cut, 1.0 if cut else 0.0, l1, detail
    if st_c[1] <= st_p[1]:
        c_img, p_img = l1, _map(prev_l1, st_p, st_c)
    else:
        c_img, p_img = _map(l1, st_c, st_p), prev_l1
    dx, dy, s, n = _align(c_img, p_img)
    e = _envelope(c_img, p_img, dx, dy)
    den = n * 2 * mad_lo
    cut = (s * npix * 1000 > NORM_CUT_PERMILLE * den
           and e * npix * 1000 > ENV_CUT_PERMILLE * den)
    score = min(1.0, (s * npix) / den) if den else 1.0
    detail.update({"dx": dx, "dy": dy, "sad": s, "n": n, "env": e})
    return cut, score, l1, detail


class SceneCutDetector:
    """Frame-by-frame scene cuts on the 320x180 grey; the first frame is a cut."""

    def __init__(self) -> None:
        self.prev_l1: np.ndarray | None = None
        self.last_score = 1.0
        self.last_detail: dict = {}

    def reset(self) -> None:
        self.prev_l1 = None

    def step(self, grey: np.ndarray) -> bool:
        grey = np.ascontiguousarray(grey, dtype=np.uint8)
        if (self.prev_l1 is None
                or self.prev_l1.shape != (grey.shape[0] // 2, grey.shape[1] // 2)):
            self.prev_l1 = box2(grey)
            self.last_score = 1.0
            self.last_detail = {}
            return True
        cut, score, l1, detail = measure(grey, self.prev_l1)
        self.prev_l1 = l1
        self.last_score = score
        self.last_detail = detail
        return cut
