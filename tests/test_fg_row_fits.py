r"""The Frame Generation row: every locale fits, and its cells react.

Two properties of the same row, both regressions of the v1.17.0 redesign.

1. FIT. The row is a label plus a segment group of cells - since v2.6.0 the
   steps x2-x6, FG's on/off being its tile above - right-aligned against the
   panel. The cells used to be sized to the
   WIDEST label, which is "off": three characters in English and eleven in
   Spanish ("desactivado"). Three two-character steps were each given that
   width, the group took 380 px of the row's 488, and the control's own name
   was clipped into what was left. Measured, not asserted by eye: the label
   must fit beside the group in all twelve languages.

2. HOVER. The row carries no click zone of its own (`hit` is None) - its
   cells do. The hover loop fell back to the row's rect, and since the toggle
   is built before its cells and spans the panel, the cursor over x3 matched
   the toggle and broke out: no cell ever highlighted, and the row draws no
   ring, so there was no feedback at all before the click.

Run:  runtime\python.exe tests\test_fg_row_fits.py
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

import protocol  # noqa: E402

FG_LABEL = "DLSS 4.5 FG"
#: The state square the PROCESSING section puts before every caption, plus its
#: gap - the caption starts that far right of the row.
SQUARE_SHIFT = 7 + 10


def main() -> int:
    failures = []
    pygame.init()
    pygame.display.set_mode((64, 64))
    try:
        import fonts
        import i18n
        import overlay_ui as ui

        def build(lang: str):
            """A menu laid out in `lang`, with that language's fonts."""
            menu = ui.OverlayMenu(
                1.0,
                lambda size, mono=False, bold=False, _l=lang:
                    fonts.load(size, mono=mono, lang=_l))
            menu.visible = True
            menu.page = "main"
            menu.set_state({"lang": lang, "nr": True, "gpu_ok": True,
                            "frame_generation": False, "frame_multiplier": 2})
            menu.draw(pygame.Surface((1920, 1080)))
            return menu

        for lang in i18n.STRINGS:
            menu = build(lang)
            row = next((it for it in menu.items
                        if it.kind == "toggle" and it.key == "frame_multiplier"),
                       None)
            cells = [it for it in menu.items
                     if it.kind == "button"
                     and str(it.key).startswith(("frame_multiplier:",
                                                 "frame_generation:"))]
            if row is None or not cells:
                failures.append(f"{lang}: no FG row in the layout")
                continue
            # Read off the PRODUCT's own rects: the room the layout actually
            # leaves the caption, against what the caption actually measures.
            room = min(c.rect.x for c in cells) - row.rect.x - SQUARE_SHIFT
            need = menu._font.size(str(row.extra.get("label") or FG_LABEL))[0]
            group = sum(c.rect.w for c in cells)
            if room < need:
                failures.append(
                    f"{lang}: the FG row leaves {room} px for its name and the "
                    f"name needs {need} - the group of {len(cells)} cells took "
                    f"{group} px, and the caption is clipped")

        # The hover half, on the real menu.
        menu = ui.OverlayMenu(
            1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
        menu.visible = True
        menu.page = "main"
        menu.set_state({"nr": True, "gpu_ok": True, "frame_generation": False,
                        "frame_multiplier": 2})
        surface = pygame.Surface((1920, 1080))
        menu.draw(surface)
        cells = [it for it in menu.items
                 if it.kind == "button"
                 and str(it.key).startswith(("frame_multiplier:",
                                             "frame_generation:"))]
        want_cells = protocol.FG_MULTIPLIER_MAX - 1   # x2 .. x6 (#163, v2.6.0)
        if len(cells) != want_cells:
            failures.append(
                f"the FG row has {len(cells)} cells, expected {want_cells} "
                f"(x2..x{protocol.FG_MULTIPLIER_MAX}) - the rest of this check cannot run")
        else:
            for cell in cells:
                menu.handle_event(pygame.event.Event(
                    pygame.MOUSEMOTION, {"pos": cell.rect.center,
                                         "rel": (0, 0), "buttons": (0, 0, 0)}))
                want = f"button:{cell.key}"
                if menu.hover != want:
                    failures.append(
                        f"hovering the {cell.key} cell set hover={menu.hover!r}, "
                        f"expected {want!r} - the zone-less FG row swallowed it, "
                        f"so no cell highlights under the cursor")
    finally:
        pygame.quit()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the FG row's name fits in all twelve languages, and each of its "
          f"{protocol.FG_MULTIPLIER_MAX - 1} cells takes the hover")
    return 0


if __name__ == "__main__":
    sys.exit(main())
