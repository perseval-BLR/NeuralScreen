r"""The on-screen frame counter: what it says, and where it sits (#109).

"It lacks the ability to display generated frames alongside real ones, such
as 120 (60), and the ability to change their position - top-left,
top-right, bottom-left, bottom-right."

The reading itself is shared with the panel's status line, on purpose: the
pair `FG 167 (55.1)` is not two numbers side by side, it is the output rate
with the rate it is built on, and a second copy of that rule would drift
from the first. So the rule is tested once, here, for both.

The rule reads "is the pass on" and "can this card run it" from the HUD
first (#131): the panel snapshot is rebuilt only while the panel is open,
so a switch made outside it left the counter invisible. The last check is
therefore a wiring check - the HUD is only useful if main actually puts
those two keys into it, and a test that only exercises status_readings
would stay green with the feed deleted.

Then the corner: a badge must land INSIDE the corner it names, with the
same margin on every side, and two badges that want the same corner must
stack rather than overlap - a recording that hid the counter would be the
one moment the counter matters most.

And the setting is a corner or it is off. An unknown string would be
carried into the HUD and compared against the four names on every frame:
a setting that silently does nothing.

Run:  runtime\python.exe tests\test_fps_overlay.py
"""
import json
import os
import re
import sys
import tempfile
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

S = {"fg_short": "FG", "nr_short": "NR"}

#: state -> the readings it must produce. Every row is a mode the program is
#: really in, and the last two are the ones that were reported wrong.
READINGS = (
    ({"nr": True, "gpu_ok": True, "fps": 55.1, "display_fps": 167.0},
     ["FG 167 (55.1)"],
     "the pass and FG both running: the pair, output rate first"),
    ({"nr": True, "gpu_ok": True, "fps": 55.1},
     ["NR 55.1"],
     "the pass alone: its own rate"),
    ({"nr": False, "gpu_ok": True, "fps": 0.0, "display_fps": 120.0},
     ["FG 120"],
     "the pass off, FG running: the only rate there is (#107)"),
    ({"nr": False, "gpu_ok": True, "fps": 0.0},
     [],
     "nothing running: silence, not 'NR 0.0'"),
    ({"nr": True, "gpu_ok": False, "fps": 55.1},
     [],
     "the card cannot run the pass: no rate to report"),
)

#: The same rule, with the live HUD in `stats` and a STALE snapshot in
#: `state` - the state #131 was reported in. The panel snapshot is rebuilt
#: only while the panel is open, so a switch made outside it (Num1) or a
#: worker that died left `state["nr"]` saying "off" while the pipeline was
#: running. The readings must follow the HUD.
LIVE_WINS = (
    ({"nr": False},
     {"nr": True, "gpu_ok": True, "fps": 96.2},
     ["NR 96.2"],
     "Num1 turned NR on with the panel closed: the counter must appear"),
    ({"nr": False},
     {"nr": True, "gpu_ok": True, "fps": 61.4, "display_fps": 187.0},
     ["FG 187 (61.4)"],
     "the same, with Frame Generation running: the pair"),
    ({"nr": True},
     {"nr": False, "gpu_ok": True, "fps": 0.0, "display_fps": 141.0},
     ["FG 141"],
     "Num1 turned NR off: the snapshot still says on, FG is the only rate"),
    ({"nr": False, "gpu_ok": True},
     {"nr": True, "gpu_ok": False, "fps": 55.1},
     [],
     "a verdict of 'this card cannot run it' outranks a stale snapshot"),
)

SCREEN = (1920, 1080)


def main() -> int:
    failures = []
    pygame.init()
    pygame.display.set_mode((64, 64))
    disp = None
    try:
        import overlay_ui as ui
        from display import Display

        for state, want, why in READINGS:
            got = ui.status_readings(dict(state), dict(state), S)
            if got != want:
                failures.append(f"{why}: got {got}, want {want}")

        for state, stats, want, why in LIVE_WINS:
            got = ui.status_readings(dict(state), dict(stats), S)
            if got != want:
                failures.append(f"{why}: got {got}, want {want}")

        disp = Display(*SCREEN, fullscreen=False)
        m = int(round(14 * disp.ui_scale))
        bw, bh = 200, 40
        corners = {
            "tl": (m, m),
            "tr": (SCREEN[0] - bw - m, m),
            "bl": (m, SCREEN[1] - bh - m),
            "br": (SCREEN[0] - bw - m, SCREEN[1] - bh - m),
        }
        for corner, want_xy in corners.items():
            got_xy = disp._badge_origin(corner, bw, bh)
            if got_xy != want_xy:
                failures.append(
                    f"a badge asked for {corner} landed at {got_xy}, want "
                    f"{want_xy} - the same margin on every side")
        # The second row stacks INTO the screen, never off it.
        for corner in ("tl", "tr"):
            first = disp._badge_origin(corner, bw, bh, 0)
            second = disp._badge_origin(corner, bw, bh, 1)
            if second[1] <= first[1]:
                failures.append(
                    f"{corner}: the second badge is at y={second[1]}, not "
                    f"below the first at y={first[1]}")
        for corner in ("bl", "br"):
            first = disp._badge_origin(corner, bw, bh, 0)
            second = disp._badge_origin(corner, bw, bh, 1)
            if second[1] >= first[1]:
                failures.append(
                    f"{corner}: the second badge is at y={second[1]}, not "
                    f"above the first at y={first[1]} - a bottom corner "
                    f"stacks upward")

        # With the panel open the badge stays away: the panel says the same
        # numbers one line up, and two copies on one screen read as a bug.
        disp.menu.set_state({"nr": True, "gpu_ok": True, "fps": 55.1,
                             "display_fps": 167.0})
        disp.set_hud({"fps_overlay": "tl", "fps": 55.1, "display_fps": 167.0})
        for visible, want_drawn in ((True, False), (False, True)):
            disp.menu.visible = visible
            disp.screen.fill((0, 0, 0))
            disp._draw_fps_badge()
            # Sampled where a top-left badge has to be, not averaged over the
            # screen: a 200x40 badge is 0.4% of 1920x1080 and its average
            # rounds back to black, so an averaged check passes whatever
            # happens.
            probe = disp.screen.subsurface(
                pygame.Rect(m, m, 40, 20)).copy()
            drawn = pygame.transform.average_color(probe)[:3] != (0, 0, 0)
            if drawn != want_drawn:
                failures.append(
                    f"menu.visible={visible}: the badge was "
                    f"{'drawn' if drawn else 'not drawn'} - it must be "
                    f"{'drawn' if want_drawn else 'left out'}")
    finally:
        try:
            if disp is not None:
                disp.close()
        except Exception:
            pass
        pygame.quit()

    # The setting is a corner or it is off.
    from settings_io import CONFIG_SCHEMA_VERSION, load_config
    base = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "monitor": 0, "width": 3840, "height": 2160, "fullscreen": True,
        "warmup": 120, "work_scale": 0.65, "lang": "en", "profile": "Natural",
        "intensity": None, "local_tone": None, "local_structure": None,
        "skin_structure": None,
    }
    for value, want in (("tl", "tl"), ("br", "br"), ("off", "off"),
                        ("middle", "off"), (5, "off"), (None, "off")):
        data = dict(base)
        data["fps_overlay"] = value
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                        encoding="utf-8")
        json.dump(data, f)
        f.close()
        try:
            got = load_config(Path(f.name)).get("fps_overlay")
        finally:
            os.unlink(f.name)
        if got != want:
            failures.append(
                f"fps_overlay {value!r} loaded as {got!r}, want {want!r}")

    # The wiring: status_readings can only prefer the live value if the live
    # value is there. Every `set_hud({...})` in main.py must carry both keys -
    # the frame loop has one, and a second call site added later without them
    # would silently bring the stale-snapshot bug back for the branch it
    # serves. The regex is anchored on the call so a mention in a comment
    # cannot satisfy it (the lesson from test_gpu_alert_path).
    main_src = (BASE / "main.py").read_text(encoding="utf-8")
    feeds = re.findall(r"st\.display\.set_hud\(\{(.*?)\n\s*\}\)", main_src, re.S)
    if not feeds:
        failures.append("no set_hud({...}) call found in main.py - re-check "
                        "the wiring check")
    for body in feeds:
        for key in ("nr", "gpu_ok"):
            if f'"{key}":' not in body:
                failures.append(
                    f"a set_hud({{...}}) call does not carry {key!r}: the "
                    f"counter would fall back to the panel snapshot and go "
                    f"blank after a switch made outside the panel (#131)")
    print(f"    set_hud calls checked for the live nr/gpu_ok feed: {len(feeds)}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the counter reads the same rule as the status line, lands in "
          "the corner it names, and only takes a corner it knows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
