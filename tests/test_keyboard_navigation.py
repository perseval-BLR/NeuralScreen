r"""Keyboard access to the overlay menu's interactive controls.

Run: runtime\python.exe tests\test_keyboard_navigation.py
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

import overlay_ui  # noqa: E402


def _font(size, **_kwargs):
    return pygame.font.Font(None, size)


def _menu():
    menu = overlay_ui.OverlayMenu(1.0, _font)
    menu.set_state({
        "nr": True,
        "nr_small": True,
        "screen_size": "1920x1080",
        "work_size": "1280x720",
        "work_scale": 0.65,
        "profile": "Natural",
        "profiles": ["Natural", "Strong / Cinematic"],
        "params": {"intensity": 1.0, "local_tone": 0.5,
                   "local_structure": 1.0, "skin_structure": -1.0},
        "style": 1,
        "split": 0.0,
        "open_on_start": True,
        "window_mode": False,
    })
    menu.visible = True
    menu.folds = {"net", "tune", "camera", "source"}   # every fold-out open (v2.6.0)
    menu.user_height = 360
    menu.draw(pygame.Surface((1280, 720)))
    return menu


def _press(menu, key, mod=0):
    return menu.handle_event(pygame.event.Event(
        pygame.KEYDOWN, {"key": key, "mod": mod, "unicode": ""}))


def _find(menu, kind, key):
    return next((item for item in menu.items
                 if item.kind == kind and item.key == key), None)


def _count_colour(surface, colour):
    target = pygame.Color(*colour)
    return sum(surface.get_at((x, y))[:3] == target[:3]
               for y in range(surface.get_height())
               for x in range(surface.get_width()))


def main() -> int:
    pygame.init()
    pygame.display.set_mode((64, 64))
    failures = []
    try:
        menu = _menu()
        order = [menu._focus_id(item) for item in menu._focusables()]
        if len(order) < 8 or len(order) != len(set(order)):
            failures.append(f"invalid focus order: {len(order)} controls, "
                            f"{len(set(order))} unique")

        # Tab visits every control in visual order, keeps it visible, and
        # wraps. Shift+Tab wraps in the opposite direction.
        visited = []
        for _ in order:
            _press(menu, pygame.K_TAB)
            item = menu.focused_item
            if item is None:
                failures.append("Tab produced no focused item")
                break
            visited.append(menu._focus_id(item))
            if item.kind != "icon" and not menu._viewport.contains(item.rect):
                failures.append(f"focus left {item.kind}:{item.key} off-screen")
        if visited != order:
            failures.append("Tab order does not match visual control order")
        _press(menu, pygame.K_TAB)
        if menu.focus_token != order[0]:
            failures.append("Tab did not wrap from the last control to the first")
        _press(menu, pygame.K_TAB, pygame.KMOD_SHIFT)
        if menu.focus_token != order[-1]:
            failures.append("Shift+Tab did not wrap to the last control")

        # A focused control has an explicit, high-contrast ring in both themes.
        slider = _find(menu, "slider", "intensity")
        if slider is None:
            failures.append("no intensity slider")
        else:
            menu._set_focus(slider)
            menu._ensure_focus_visible()
            for theme in ("light", "dark"):
                menu.set_state({"theme": theme})
                surface = pygame.Surface((1280, 720))
                surface.fill((1, 2, 3))
                menu.draw(surface)
                hits = _count_colour(surface,
                                     overlay_ui._rgb(menu.c["focus"]))
                if hits < 20:
                    failures.append(f"no visible {theme} focus ring ({hits} px)")

            old = menu.focused_item.value
            out = _press(menu, pygame.K_RIGHT)
            if not out or out[0][:2] != ("param", "intensity"):
                failures.append(f"Right did not move the slider: {out}")
            elif menu.focused_item.value <= old:
                failures.append("Right emitted but did not increase the slider")

        # Left/right operate segmented controls and deterministic toggles.
        menu.page = "main"
        menu.open_choice = None
        menu._relayout()
        style = _find(menu, "segmented", "style")
        if style is None:
            failures.append("no style segmented control")
        else:
            menu._set_focus(style)
            out = _press(menu, pygame.K_RIGHT)
            if out != [("style", "2")]:
                failures.append(f"Right on style emitted {out}")

        toggle = _find(menu, "tile", "nr")
        if toggle is None:
            failures.append("no NR tile")
        else:
            menu._set_focus(toggle)
            if _press(menu, pygame.K_LEFT) != [("nr",)]:
                failures.append("Left did not switch the focused toggle off")
            if _press(menu, pygame.K_RIGHT) != [("nr",)]:
                failures.append("Right did not switch the focused toggle on")

        # Space opens a choice, arrows move the highlighted option, Enter
        # selects it; Esc still closes list first and panel second.
        profile = _find(menu, "choice", "profile")
        if profile is None:
            failures.append("no profile choice")
        else:
            menu._set_focus(profile)
            _press(menu, pygame.K_SPACE)
            if menu.open_choice != "profile":
                failures.append("Space did not open the focused choice")
            _press(menu, pygame.K_DOWN)
            out = _press(menu, pygame.K_RETURN)
            if out != [("profile", "Strong / Cinematic")]:
                failures.append(f"keyboard choice emitted {out}")
            _press(menu, pygame.K_SPACE)
            first_esc = _press(menu, pygame.K_ESCAPE)
            second_esc = _press(menu, pygame.K_ESCAPE)
            if first_esc or menu.open_choice is not None:
                failures.append("first Esc did not close only the choice")
            if second_esc != [("button", "close")]:
                failures.append(f"second Esc emitted {second_esc}")

        # Enter/Space activate ordinary buttons and page navigation.
        screenshot = _find(menu, "button", "screenshot")
        if screenshot is None:
            failures.append("no screenshot button")
        else:
            menu._set_focus(screenshot)
            if _press(menu, pygame.K_RETURN) != [("button", "screenshot")]:
                failures.append("Enter did not activate the screenshot button")

        gear = _find(menu, "icon", "gear")
        if gear is None:
            failures.append("no settings icon")
        else:
            menu._set_focus(gear)
            out = _press(menu, pygame.K_RETURN)
            if out != [("capture", None)] or menu.page != "settings":
                failures.append(f"Enter did not open settings: {out}")
            if menu.focused_item is None:
                failures.append("focus was lost after keyboard page navigation")
    finally:
        pygame.quit()

    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: cyclic focus, activation, arrows, Esc and auto-scroll work")
    return 0


if __name__ == "__main__":
    sys.exit(main())
