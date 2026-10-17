"""An empty HUD layer is not flipped again and again (#166).

The report: a PS5 through a capture card at 4K59.94, shown on a 119.88 Hz
monitor, stuttered "like 30 fps" as soon as NR was on, with the GPU at 35%.
Measured on the bench with a source that presents every second vblank and
writes its frame number into the picture: with NeuralScreen running, the
capture lost 5-11% of the source's frames and a fifth of the rest arrived a
vblank early or late. An independent Desktop Duplication reader running
next to it saw the same losses, so it was the compositor, not our capture.

The cause was the HUD layer - a full-screen layered window that
draw_overlay() cleared and flipped ten times a second with nothing on it:
every flip of it costs DWM a composition. With the flips gone the same run
kept every frame, 99% of them exactly two vblanks apart.

Checked on a real Display (SDL's dummy driver, a fake clock), by counting
the flips draw_overlay() makes:

1. menu closed, no alert, no badge, not recording: the layer goes out once
   and then not again, however long the loop runs;
2. an alert still shows - flipped while it is up - and the layer is flipped
   once more when it expires, so the alert does not stay on screen;
3. a visible element (the FPS badge) is flipped at the HUD's rate as before;
4. a layer that resize() rebuilt (set_mode) is flipped once even when
   empty - its content is not known.

(1) fails on the code before the fix: one flip per 100 ms.

Run:  runtime\\python.exe tests\\test_hud_idle_flip.py
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))  # the modules live in app/

import pygame  # noqa: E402
import display as display_mod  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def run(disp, clock, flips, seconds: float) -> int:
    """Drive draw_overlay() as the main loop does (every ~7 ms) for `seconds`
    of fake time; return how many flips it made."""
    before = flips[0]
    end = clock.now + seconds
    while clock.now < end:
        disp.draw_overlay()
        clock.now += 0.007
    return flips[0] - before


def main() -> int:
    failures = []
    pygame.init()
    clock = Clock()
    flips = [0]
    real_flip = pygame.display.flip
    real_monotonic = display_mod.time.monotonic

    def counting_flip():
        flips[0] += 1
        real_flip()

    display_mod.pygame.display.flip = counting_flip
    display_mod.time.monotonic = clock
    try:
        disp = display_mod.Display(640, 360)
        disp.menu.visible = False
        disp.set_hud({"recording": False, "rec_indicator": True,
                      "fps_overlay": "off", "fps": 59.0, "status": "NR ON",
                      "nr": True, "gpu_ok": True})

        # 1. Nothing to show: one flip, then none.
        n = run(disp, clock, flips, 3.0)
        print(f"    empty layer, 3 s: {n} flip(s)")
        if n > 1:
            failures.append(f"an empty HUD layer was flipped {n} times in 3 s "
                            f"(expected once) - every flip costs DWM a composition")

        # 2. An alert shows, and is cleared once it expires.
        disp.alert("test notice", duration=1.0)
        n = run(disp, clock, flips, 0.5)
        if n < 1:
            failures.append("an alert was never flipped onto the screen")
        n = run(disp, clock, flips, 1.0)   # it expires in here
        if n < 1:
            failures.append("the layer was not flipped when the alert expired - "
                            "the alert would stay on screen")
        n = run(disp, clock, flips, 2.0)
        print(f"    after the alert, 2 s: {n} flip(s)")
        if n > 0:
            failures.append(f"the empty layer after the alert was flipped {n} more times")

        # 3. A visible badge keeps being redrawn at the HUD's rate.
        disp.set_hud({"recording": False, "rec_indicator": True,
                      "fps_overlay": "tr", "fps": 59.0, "status": "NR ON",
                      "nr": True, "gpu_ok": True})
        n = run(disp, clock, flips, 1.0)
        print(f"    FPS badge, 1 s: {n} flip(s)")
        if n < 5:
            failures.append(f"the FPS badge was flipped only {n} times in 1 s")

        # 4. A layer that resize() rebuilt is flipped even with nothing on it.
        disp.set_hud({"recording": False, "rec_indicator": True,
                      "fps_overlay": "off", "fps": 59.0, "status": "NR ON",
                      "nr": True, "gpu_ok": True})
        run(disp, clock, flips, 1.0)
        disp.resize(640, 360)
        n = run(disp, clock, flips, 1.0)
        if n != 1:
            failures.append(f"the layer rebuilt by resize() was flipped {n} times "
                            f"(expected once)")
    finally:
        display_mod.pygame.display.flip = real_flip
        display_mod.time.monotonic = real_monotonic
        pygame.quit()

    for f in failures:
        print(f"  FAIL: {f}")
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
