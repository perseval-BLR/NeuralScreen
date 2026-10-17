"""Frame caps are stable and the displayed NR rate counts real work only.

The cap holds its rate when the sleep wakes late, and makes up a frame that
ran a little over on the next one - never with a burst after a stall (#166
bench: a 72 cap ran at ~70.7 and lost 2.2% of a 71.93 fps source)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pygame

import commands
from pacing import FramePacer, FrameRateMeter
import settings_io
from test_ui_buttons import build, find, paint


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        assert delay >= 0
        self.sleeps.append(delay)
        self.now += delay


def check_pacer() -> None:
    fake = FakeTime()
    pacer = FramePacer(clock=fake.clock, sleeper=fake.sleep)
    starts = []
    for _ in range(121):
        starts.append(fake.now)
        fake.now += 0.002  # simulated capture/evaluate/present work
        pacer.wait(60, starts[-1])
    measured = (len(starts) - 1) / (starts[-1] - starts[0])
    assert abs(measured - 60.0) < 1e-6, measured

    # The sleep overshoots (Windows wakes late), and the cap still holds its
    # rate: 72 must not run at ~70.7 and drop frames of a 71.93 fps source
    # (#166 bench, 2.2% lost). Fails on the timeline that restarted from
    # each late start.
    late = FakeTime()
    late.sleep = lambda d: setattr(late, "now", late.now + d + 0.00025)
    pacer72 = FramePacer(clock=late.clock, sleeper=late.sleep)
    first = late.now
    for i in range(720):
        start = late.now
        late.now += 0.007 + (0.004 if i % 3 == 0 else 0.0)   # eval 7-11 ms
        pacer72.wait(72, start)
    measured = 720 / (late.now - first)
    assert abs(measured - 72.0) < 0.1, f"a 72 fps cap ran at {measured:.2f}"

    # A frame late by less than an interval is made up by the next one, and
    # only by it: the one after is a full interval again.
    late = FakeTime()
    pacer = FramePacer(clock=late.clock, sleeper=late.sleep)
    pacer.wait(60, late.now)                       # the timeline: 1/60 s ahead
    start = late.now
    late.now += 1 / 60 + 0.005                     # 5 ms over
    pacer.wait(60, start)
    made_up = late.now
    pacer.wait(60, made_up)
    assert abs((late.now - made_up) - (1 / 60 - 0.005)) < 1e-9, late.now - made_up
    full = late.now
    pacer.wait(60, full)
    assert abs((late.now - full) - 1 / 60) < 1e-9

    pacer = FramePacer(clock=fake.clock, sleeper=fake.sleep)
    pacer.wait(60, fake.now)
    # A slow frame never earns a catch-up burst on the next iteration.
    start = fake.now
    fake.now += 0.050
    pacer.wait(60, start)
    next_start = fake.now
    fake.now += 0.001
    pacer.wait(60, next_start)
    assert fake.now - next_start >= 1 / 60 - 1e-9

    # A live mode change takes effect from the current frame, while unlimited
    # sleeps nothing and resets the old deadline.
    start = fake.now
    fake.now += 0.001
    pacer.wait(30, start)
    changed_start = fake.now
    fake.now += 0.001
    pacer.wait(60, changed_start)
    assert fake.now - changed_start >= 1 / 60 - 1e-9
    before = fake.now
    pacer.wait(0, before)
    assert fake.now == before


def check_rate_meter() -> None:
    meter = FrameRateMeter(window=2.0)
    for stamp in (0.0, 0.1, 0.2, 0.3):
        meter.record(stamp)
    assert abs(meter.rate(0.3) - 10.0) < 1e-9
    # No real completion for a full window: the honest answer is zero, not
    # the last active rate. Idle replies are deliberately never recorded.
    assert meter.rate(2.31) == 0.0


def check_settings_and_ui() -> None:
    assert settings_io.frame_limit_fps({}) == 0
    assert settings_io.frame_limit_fps({"frame_limit_mode": "30"}) == 30
    assert settings_io.frame_limit_fps({"frame_limit_mode": "60"}) == 60
    assert settings_io.frame_limit_fps(
        {"frame_limit_mode": "custom", "frame_limit_custom": 999}) == 240
    assert settings_io.frame_limit_fps(
        {"frame_limit_mode": "custom", "frame_limit_custom": "bad"}) == 90

    pygame.init()
    try:
        menu = build()
        paint(menu)
        assert find(menu, "choice", "frame_limit_mode") is not None
        assert find(menu, "slider", "frame_limit_custom") is None
        assert menu._pick("frame_limit_mode", "custom") == [
            ("frame_limit_mode", "custom")]
        paint(menu)
        assert find(menu, "slider", "frame_limit_custom") is not None

        st = SimpleNamespace(cfg={})
        with patch.object(settings_io, "save_menu_layout") as save:
            commands.apply_menu_action(st, ("frame_limit_mode", "60"))
            commands.apply_menu_action(st, ("frame_limit_custom", 144))
        assert st.cfg["frame_limit_mode"] == "60"
        assert st.cfg["frame_limit_custom"] == 144
        assert save.call_count == 2
    finally:
        pygame.quit()


def main() -> int:
    check_pacer()
    check_rate_meter()
    check_settings_and_ui()
    print("OK: frame pacing, real-work rate and live controls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
