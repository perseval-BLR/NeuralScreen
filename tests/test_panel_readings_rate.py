"""The open panel's live readings change at 2 Hz, not with every frame (#166).

A reporter on an RTX 5090 saw micro-stutter in a 59.94 fps capture while the
NeuralScreen panel was on screen, and none with it closed. The open panel
shows live readings (the processing rate, the frame counter); each change
redraws it, and every redraw of the overlay layer costs the compositor a
frame of timing - the mechanism #166 was first about. The readings now
change at most every 0.5 s while the panel is open.

Checked on the real menu: open, a stream of readings within half a second
keeps the first; after half a second the newest is taken; closed, every one
is taken at once; and the panel's drawn pixels do not change between two
readings within the interval.

Fails before the change: every reading was taken.

Run:  runtime\python.exe tests\test_panel_readings_rate.py
"""
import os
import sys
import zlib

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))

import pygame  # noqa: E402


def main() -> int:
    failures = []
    pygame.init()
    pygame.display.set_mode((64, 64))
    import fonts
    import overlay_ui as ui
    menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
    menu.set_state({"nr": True, "gpu_ok": True, "profile": "Natural", "profiles": ["Natural"]})
    menu.visible = True
    surf = pygame.Surface((1920, 1080))

    def frame_crc():
        surf.fill((0, 0, 0))
        menu.layout(1920, 1080)
        menu.draw(surf)
        return zlib.crc32(pygame.image.tobytes(surf, "RGB"))

    menu.set_stats({"fps": 58.1, "status": "NR ON", "frames": 100}, now=10.0)
    first = frame_crc()
    for i, t in enumerate((10.1, 10.2, 10.3, 10.45)):
        menu.set_stats({"fps": 59.0 + i, "status": "NR ON", "frames": 101 + i}, now=t)
    if menu.stats.get("fps") != 58.1 or frame_crc() != first:
        failures.append(f"within 0.5 s the open panel took {menu.stats.get('fps')}")
    menu.set_stats({"fps": 61.5, "status": "NR ON", "frames": 120}, now=10.55)
    if menu.stats.get("fps") != 61.5:
        failures.append(f"after 0.5 s the reading is still {menu.stats.get('fps')}")
    menu.visible = False
    for i, t in enumerate((11.0, 11.01, 11.02)):
        menu.set_stats({"fps": 70.0 + i}, now=t)
        if menu.stats.get("fps") != 70.0 + i:
            failures.append("a closed panel did not take every reading")
            break
    pygame.quit()

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
