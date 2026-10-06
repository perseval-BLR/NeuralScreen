r"""A screenshot is of the picture, not of our menu (#140).

The report (Mnilionic, #140): "the program's menu gets into the exported
screenshot". It was deliberate - save_screenshot baked an open menu onto the
frame, because the overlay layer itself is excluded from capture. But the
Screenshot button lives in that menu, so every screenshot taken from it
carried the panel over the picture. Recordings keep baking the menu (main.py),
which this test does not touch.

What this locks, through the real save_screenshot: with the menu open, the
pixels written are exactly the frame that came in, and the menu painter is
not called.

Run:  runtime\python.exe tests\test_screenshot_clean.py
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import numpy as np  # noqa: E402

import commands  # noqa: E402


class _Display:
    """The real Display's capture painter draws the menu over the frame; this
    one paints the same area so a baked menu shows up in the saved pixels."""

    def __init__(self):
        self.menu = SimpleNamespace(visible=True)
        self.painted = 0
        self.alerts = []

    def draw_capture_overlay(self, surface) -> None:
        self.painted += 1
        surface.fill((255, 0, 255), (0, 0, 32, 32))

    def alert(self, text) -> None:
        self.alerts.append(text)


def main() -> int:
    failures = []
    frame = np.zeros((48, 64, 4), dtype=np.uint8)
    frame[..., 0] = 40
    frame[..., 1] = 90
    frame[..., 2] = 140
    expected = frame.copy()

    saved = []
    real_save = commands.dialogs.save_image
    commands.dialogs.save_image = lambda path, rgba: saved.append(rgba.copy()) or True
    try:
        st = SimpleNamespace(display=_Display(), lang="en")
        commands.save_screenshot(st, Path("shot.png"), frame)
    finally:
        commands.dialogs.save_image = real_save

    if len(saved) != 1:
        failures.append(f"save_screenshot wrote {len(saved)} image(s), not one")
    elif not np.array_equal(saved[0], expected):
        changed = int(np.any(saved[0] != expected, axis=2).sum())
        failures.append(f"the open menu was baked into the screenshot "
                        f"({changed} pixels differ from the frame)")
    if st.display.painted:
        failures.append("save_screenshot still calls the menu painter")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a screenshot carries the picture, not the open menu (#140)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
