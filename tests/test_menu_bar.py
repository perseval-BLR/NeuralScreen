"""Redesign C: the collapsed menu is a bar that stays out of the game's way.

Mini mode kept a hand-picked subset of the panel's rows. The bar that
replaced it (v2.6.0) is one row at the top of the screen: NR, FG and its step,
Boost, the camera, a screenshot, a recording, the way back to the panel and
the collapse. It is meant to stay up through a game, which the open panel
cannot: the panel takes the mouse across the whole screen and the keyboard
with it.

Checked on a real Display (SDL's dummy driver) and the real menu:

1. the bar holds those buttons and nothing else, and its clicks are the
   panel's actions - a tile is the same ("nr",) / ("toggle", key), the
   expand button brings the panel back;
2. dragging its handle moves the bar's own position, not the panel's;
3. input: with the pointer away from the bar the layer stays click-through;
   over it, the layer takes the mouse WITHOUT activation (the keyboard stays
   with the game); away again, click-through;
4. the HUD layer under an unchanged bar is flipped once and then not again
   (#166: every flip of that layer costs the compositor a frame), and a
   hover that changes its pixels is flipped at once.

Fails before the change: there was no bar (mini mode was a short panel that
took the whole screen's input), and an open menu flipped ten times a second
whatever it showed.

Run:  runtime\\python.exe tests\\test_menu_bar.py
"""
import ctypes
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))

import pygame  # noqa: E402
import display as display_mod  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


def run(disp, clock, flips, seconds):
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
    cursor = [(-5000, -5000)]
    real_flip = pygame.display.flip
    real_monotonic = display_mod.time.monotonic
    real_cursor = display_mod.user32.GetCursorPos

    def counting_flip():
        flips[0] += 1
        real_flip()

    def fake_cursor(ref):
        ref._obj.x, ref._obj.y = cursor[0]
        return 1

    display_mod.pygame.display.flip = counting_flip
    display_mod.time.monotonic = clock
    display_mod.user32.GetCursorPos = fake_cursor
    try:
        disp = display_mod.Display(1280, 720)
        menu = disp.menu
        menu.set_state({"nr": True, "gpu_ok": True, "frame_generation": True,
                        "frame_multiplier": 3, "nr_small": True, "lens": False,
                        "profile": "Natural", "profiles": ["Natural"]})
        disp.set_hud({"fps_overlay": "off", "nr": True, "gpu_ok": True})
        menu.visible = True
        menu.mini = True
        menu.layout(1280, 720)

        # 1. What the bar holds, and what its clicks do.
        order = [(i.kind, i.key) for i in sorted(menu.items, key=lambda i: i.rect.x)]
        want = [("tile", "nr"), ("tile", "frame_generation"), ("tile", "boost"),
                ("tile", "lens"), ("button", "screenshot"), ("button", "record"),
                ("icon", "mini"), ("icon", "min")]
        print(f"    bar: {[k for _, k in order]}, {menu.panel_rect.size}")
        if order != want:
            failures.append(f"the bar holds {order}")
        fg = next(i for i in menu.items if i.key == "frame_generation")
        if fg.extra.get("label") != "FG ×3":
            failures.append(f"the FG button reads {fg.extra.get('label')!r}")
        if menu.panel_rect.h > 80 or menu.panel_rect.y > 40:
            failures.append(f"the bar is not a row at the top: {menu.panel_rect}")
        nr = next(i for i in menu.items if i.key == "nr")
        if menu._activate_item(nr) != [("nr",)]:
            failures.append("the NR button is not the NR switch")
        boost = next(i for i in menu.items if i.key == "boost")
        if menu._activate_item(boost) != [("toggle", "boost")]:
            failures.append("the Boost button is not the Boost switch")

        # 2. The handle moves the bar, not the panel.
        handle = menu._title_bar.center
        panel_offset = list(menu.offset)
        for ev, pos in ((pygame.MOUSEBUTTONDOWN, handle),
                        (pygame.MOUSEMOTION, (handle[0] + 60, handle[1] + 10)),
                        (pygame.MOUSEBUTTONUP, (handle[0] + 60, handle[1] + 10))):
            data = {"pos": pos, "button": 1} if ev != pygame.MOUSEMOTION \
                else {"pos": pos, "rel": (0, 0), "buttons": (1, 0, 0)}
            menu.handle_event(pygame.event.Event(ev, data))
        if menu.bar_offset != [60, 10] or list(menu.offset) != panel_offset:
            failures.append(f"the drag moved bar={menu.bar_offset} panel={menu.offset}")
        menu.layout(1280, 720)

        # 3. Input only over the bar, never the keyboard.
        calls = []
        disp.set_menu_input = lambda enabled, activate=True: calls.append(
            (bool(enabled), bool(activate)))
        disp.update_bar_input()
        ox, oy = getattr(disp, "_origin", (0, 0))
        cursor[0] = (menu.panel_rect.centerx + ox, menu.panel_rect.centery + oy)
        disp.update_bar_input()
        disp.update_bar_input()
        cursor[0] = (menu.panel_rect.centerx + ox, menu.panel_rect.bottom + 200 + oy)
        disp.update_bar_input()
        print(f"    input switches: {calls}")
        if calls != [(True, False), (False, False)]:
            failures.append(f"the bar's input went {calls} - expected the mouse "
                            f"without activation over it, click-through away")

        # 4. An unchanged bar is flipped once, a changed one at once.
        n = run(disp, clock, flips, 2.0)
        if n != 1:
            failures.append(f"an unchanged bar was flipped {n} times in 2 s "
                            f"(expected once)")
        menu.hover = "tile:nr"
        n = run(disp, clock, flips, 0.5)
        if n != 1:
            failures.append(f"a hover over the bar was flipped {n} times (expected once)")

        # And the way back.
        expand = next(i for i in menu.items if i.kind == "icon" and i.key == "mini")
        if menu._activate_item(expand) != [("mini", False)] or menu.mini:
            failures.append("the expand button did not bring the panel back")
    finally:
        display_mod.pygame.display.flip = real_flip
        display_mod.time.monotonic = real_monotonic
        display_mod.user32.GetCursorPos = real_cursor
        pygame.quit()

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
