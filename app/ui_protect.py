"""Frame Generation's interface protection: where a still interface sits (v2.6.0).

DLSS-G here has no engine to tell it what is HUD: the depth is flat and the
motion estimated, so a label over a moving scene is warped with the scene
around it - text and HUD smear in the generated frames, the known cost of the
desktop approach. The worker can show the real frame's pixels instead in
places it is told about (UIRC, frame_generation.inl); this module finds them.

The rule, on the 320x180 luminance the worker already hands over (the GRAY
mapping), in 10x10 tiles:

* a tile is MOVING when it changed by more than MOVE_DIFF grey levels on
  average since the last frame, STILL when it changed by less than STILL_DIFF
  while carrying texture (a flat sky is still and needs no protection);
* a still tile counts only while the scene around it moves - at least
  MOVING_SHARE of the tiles: over a still picture the generated frames are the
  real one anyway, and protecting nothing then is the honest answer;
* it has to stay still for EVIDENCE frames in a row, and a single moving
  frame starts it over - a slow pan must not leave a protected block behind;
* the kept tiles are merged into at most MAX_RECTS rectangles covering at most
  MAX_AREA of the picture, largest first: past that it is not an interface.

The rectangles are in 1/65535ths of the frame, which is what the worker takes.
"""
from __future__ import annotations

import numpy as np

TILE = 10
MOVE_DIFF = 3.0
STILL_DIFF = 0.8
TEXTURE = 6.0
MOVING_SHARE = 0.12
EVIDENCE = 8
MAX_RECTS = 32
MAX_AREA = 0.20


class UiDetector:
    """One per session: feed it every new gray frame, it answers rectangles."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._prev = None
        self._streak = None
        self._last: list[tuple[int, int, int, int]] = []

    def update(self, gray: np.ndarray) -> list[tuple[int, int, int, int]]:
        """The still-interface rectangles for this frame, normalised."""
        g = np.asarray(gray, dtype=np.float32)
        h, w = g.shape[:2]
        th, tw = h // TILE, w // TILE
        if th == 0 or tw == 0:
            return []
        g = g[:th * TILE, :tw * TILE]
        tiles = g.reshape(th, TILE, tw, TILE)
        texture = tiles.std(axis=(1, 3))
        prev, self._prev = self._prev, g
        if prev is None or prev.shape != g.shape:
            self._streak = np.zeros((th, tw), np.int32)
            return []
        diff = np.abs(g - prev).reshape(th, TILE, tw, TILE).mean(axis=(1, 3))
        if not diff.any():
            # The same gray again (the loop ran faster than the game drew, or
            # the gray was not refreshed for this frame): no news. Read as a
            # frame it is "nothing moves" - the protection went off for one
            # frame and on again with the next, and every textured tile of
            # the moving scene gained a frame of stillness.
            return list(self._last)
        moving = diff > MOVE_DIFF
        still = (diff < STILL_DIFF) & (texture > TEXTURE)
        self._streak = np.where(still, self._streak + 1, 0)
        if moving.mean() < MOVING_SHARE:
            self._last = []
            return []
        keep = self._streak >= EVIDENCE
        self._last = _rects(keep, w=tw * TILE, h=th * TILE)
        return list(self._last)


def _rects(keep: np.ndarray, *, w: int, h: int) -> list[tuple[int, int, int, int]]:
    """Kept tiles as rectangles: runs along each row, stacked while the next
    row has a run over exactly the same columns."""
    th, tw = keep.shape
    open_runs: dict[tuple[int, int], int] = {}   # (x0, x1) -> first row
    boxes: list[tuple[int, int, int, int]] = []  # tile units: x, y, w, h
    for y in range(th + 1):
        runs = set()
        if y < th:
            x = 0
            while x < tw:
                if keep[y, x]:
                    x0 = x
                    while x < tw and keep[y, x]:
                        x += 1
                    runs.add((x0, x))
                else:
                    x += 1
        for span, top in list(open_runs.items()):
            if span not in runs:
                boxes.append((span[0], top, span[1] - span[0], y - top))
                del open_runs[span]
        for span in runs:
            open_runs.setdefault(span, y)
    boxes.sort(key=lambda b: b[2] * b[3], reverse=True)
    total = th * tw
    out, used = [], 0
    for bx, by, bw, bh in boxes:
        if len(out) >= MAX_RECTS or (used + bw * bh) > MAX_AREA * total:
            continue
        used += bw * bh
        out.append(tuple(int(round(v * 65535 / d)) for v, d in
                         ((bx, tw), (by, th), (bw, tw), (bh, th))))
    return out
