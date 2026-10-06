r"""An alert that carries a path stays on the screen.

The alerts were drawn at the text's full width. The ones that carry a path -
diagnostics_saved, record_saved, convert_done, record_failed_stage - are
long enough to run past the right edge of a 1366-1600 px screen, and the
part that went missing was the file name, the one thing the alert is for.

Checked with the real Display (dummy SDL driver) and the real alert font:
* on a 1366x768 screen a diagnostics_saved alert with a real-length path is
  drawn as a panel inside the screen, with its edge gap;
* the text keeps its head (the sentence) and its tail (the file name) and
  loses the middle;
* a short alert is drawn as it is.

Run:  runtime\python.exe tests\test_alert_elided.py
"""
import os
import sys
from pathlib import Path


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

from i18n import STRINGS  # noqa: E402

PATH = (r"C:\Users\Alexander-Workstation\Documents\NeuralScreen\diagnostics"
        r"\neuralscreen-diag-20261006-235959.zip")


class _FontSpy:
    """The real alert font, noting what it was asked to render."""

    def __init__(self, font):
        self._font = font
        self.rendered = []

    def render(self, text, *a, **k):
        self.rendered.append(text)
        return self._font.render(text, *a, **k)

    def __getattr__(self, name):
        return getattr(self._font, name)


def _draw(disp, text):
    spy = _FontSpy(disp._alert_font)
    disp._alert_font = spy
    rects = []
    real_rect = disp._alert_rect

    def rect(w, h):
        r = real_rect(w, h)
        rects.append(r)
        return r

    disp._alert_rect = rect
    disp._alerts = []
    disp.alert(text, 5.0)
    disp._draw_alerts()
    disp._alert_font = spy._font
    del disp._alert_rect
    return (spy.rendered[-1] if spy.rendered else None,
            rects[-1] if rects else None)


def main() -> int:
    failures = []
    pygame.init()
    import display as display_mod

    disp = display_mod.Display(1366, 768, click_through=False)
    disp._screen_rect = lambda: None          # the dummy driver has no monitor
    try:
        edge = int(round(8 * disp.ui_scale))
        text = STRINGS["en"]["diagnostics_saved"].format(path=PATH)
        if disp._alert_font.size(text)[0] <= disp.width:
            print("SKIP: the alert font draws the long alert inside 1366 px")
            return 0
        drawn, rect = _draw(disp, text)
        if rect is None:
            failures.append("no alert was drawn")
        else:
            if rect.left < edge or rect.right > disp.width - edge:
                failures.append(f"the path alert spans x {rect.left}.."
                                f"{rect.right} on a {disp.width} px screen")
            head = text.split(":")[0]
            if not drawn.startswith(head):
                failures.append(f"the sentence was lost: {drawn!r}")
            if not drawn.endswith(".zip") or "235959" not in drawn:
                failures.append(f"the file name was lost: {drawn!r}")
        short = STRINGS["en"]["settings_applied"]
        drawn, rect = _draw(disp, short)
        if drawn != short:
            failures.append(f"a short alert was changed: {drawn!r}")
    finally:
        disp.close()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a path alert fits the screen and keeps both of its ends")
    return 0


if __name__ == "__main__":
    sys.exit(main())
