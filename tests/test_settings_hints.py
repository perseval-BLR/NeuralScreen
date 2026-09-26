"""A hint under a control is one line, and it fits.

The settings page had grown paragraphs: three lines under the Spout2 switch,
two under four more controls. Fourteen controls with explanations under them
stop reading as a list of options - the page becomes a document, and the
thing the user came for is somewhere inside it (user, 12.09).

So the rule, for both pages: the label says what the control is, the hint
adds the ONE thing that is not obvious from the label, and everything that
needs a paragraph has one in the README.

The other half is the same rule the Boost hint was written against and the
menu enforces nowhere: `_draw_toggle`, `_draw_choice` and `_draw_slider` all
put a hint through `_clip`, which TRUNCATES it - silently, with an ellipsis.
Nothing wraps. A translation that grew past the row simply lost its end, in
a language the person who wrote it does not read.

Measured with the real faces, at the real row widths, in all twelve
languages, on both pages - including the controls that only appear in some
states (the resolution slider, which Boost reveals; the monitor and card
pickers, which need more than one of each).

Run:  runtime\\python.exe tests\\test_settings_hints.py
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

#: How much of the row a hint may take. Not 100%: _clip only truncates at
#: the very edge, and a line that reaches it reads as running out of the
#: panel. The widest today is 80%.
ROOM = 0.92

STATE = {
    "nr": True, "profile": "Natural", "profiles": ["Natural", "Extreme"],
    "params": {"intensity": 1.0, "local_tone": 1.0,
               "local_structure": 1.0, "skin_structure": -1.0},
    "split": 0.35, "work_scale": 0.5, "work_scale_cap": 0.65,
    "work_scale_min": 0.1,
    # Boost on: the resolution slider (and its hint) only exists then.
    "nr_small": True,
    "screen_size": "3840x2160", "theme": "light",
    "gpu_text": "RTX 5070 Ti", "gpu_ok": True,
    # Two of each: the monitor and card pickers are laid out only when
    # there is something to choose, and both carry a hint.
    "monitors": ["0: 3840x2160", "1: 1920x1080"],
    "monitor": "0: 3840x2160",
    "gpus": ["0: RTX 5070 Ti", "1: RTX 2060"], "gpu": "0: RTX 5070 Ti",
    "hdr": False, "spout": False, "rec_indicator": True,
    "windows": ["1A2B3C: Notepad"], "window_current": "1A2B3C: Notepad",
    # Frame Generation ON with the runtime refusing the pick: this is the one
    # state that adds the "this card caps at x2" hint to the FG row (issue
    # #100), so the state must be exercised or that hint escapes the length
    # check below.
    "frame_generation": True, "frame_multiplier": 4,
    "frame_multiplier_active": 2,
}


def main() -> int:
    import pygame
    pygame.init()
    pygame.display.set_mode((64, 64))
    import fonts
    from i18n import STRINGS
    from overlay_ui import SETTINGS_TABS, OverlayMenu

    failures = []
    widest = (0.0, "")
    seen_keys = set()
    for lang in STRINGS:
        menu = OverlayMenu(1.0, lambda size=14, mono=False, bold=False, L=lang:
                           fonts.load(size, mono=mono, bold=bold, lang=L))
        menu.lang = lang
        menu.set_state(dict(STATE, lang=lang))
        menu.visible = True
        for page, tab in [("main", None)] + [("settings", t)
                                            for t in SETTINGS_TABS]:
            menu.page = page
            if tab is not None:
                menu.settings_tab = tab
            menu.layout(3840, 2160)
            for item in menu.items:
                hint = item.extra.get("hint")
                if not hint:
                    continue
                seen_keys.add(f"{page}/{item.key}")
                lines = str(hint).split("\n")
                if len(lines) > 1:
                    failures.append(
                        f"{lang} {page}/{item.key}: the hint is "
                        f"{len(lines)} lines - one line per control")
                for line in lines:
                    width = menu._small_font.size(line)[0]
                    share = width / item.rect.w
                    if share > widest[0]:
                        widest = (share, f"{lang} {page}/{item.key}")
                    if share > ROOM:
                        failures.append(
                            f"{lang} {page}/{item.key}: the hint takes "
                            f"{share * 100:.0f}% of the row ({width} of "
                            f"{item.rect.w} px) and is clipped, not wrapped: "
                            f"{line[:40]}...")

    # A segmented control has no hint - its captions ARE the control - so
    # the same silent truncation applies to them, inside a cell a third the
    # width of a row. _draw_segmented centres the caption and lets it spill,
    # which reads as a misspelt word rather than as a clipped one.
    widest_seg = (0.0, "")
    for lang in STRINGS:
        menu = OverlayMenu(1.0, lambda size=14, mono=False, bold=False, L=lang:
                           fonts.load(size, mono=mono, bold=bold, lang=L))
        menu.lang = lang
        menu.set_state(dict(STATE, lang=lang))
        menu.visible = True
        for page, tab in [("main", None)] + [("settings", t)
                                            for t in SETTINGS_TABS]:
            menu.page = page
            if tab is not None:
                menu.settings_tab = tab
            menu.layout(3840, 2160)
            for item in menu.items:
                if item.kind != "segmented":
                    continue
                labels = item.extra.get("labels") or item.payload or []
                if not labels:
                    continue
                cell = item.rect.w // len(labels)
                for label in labels:
                    width = menu._small_font.size(str(label))[0]
                    share = width / max(1, cell)
                    if share > widest_seg[0]:
                        widest_seg = (share, f"{lang} {page}/{item.key} "
                                             f"{label!r}")
                    if share > ROOM:
                        failures.append(
                            f"{lang} {page}/{item.key}: the caption "
                            f"{label!r} takes {share * 100:.0f}% of its cell "
                            f"({width} of {cell} px) and spills over its "
                            f"neighbour")
    print(f"    widest segment caption: {widest_seg[1]} at "
          f"{widest_seg[0] * 100:.0f}% of its cell")

    # The controls that carry a hint at all. If a page stops laying one of
    # these out, this test would go quiet about it - and the quiet would
    # look like a pass.
    # The resolution slider is deliberately not here: it names the trade at
    # both ENDS of its track instead of under it, which is the same rule in
    # a better place.
    expected = {"main/split",
                "settings/monitor", "settings/gpu", "settings/hdr",
                "settings/spout"}
    lost = sorted(k for k in expected if k not in seen_keys)
    if lost:
        failures.append(f"these hinted controls were not laid out: {lost} - "
                        f"either they moved or this test stopped seeing them")

    print(f"    hinted controls: {len(seen_keys)}, languages: {len(STRINGS)}")
    print(f"    widest hint: {widest[1]} at {widest[0] * 100:.0f}% of its row")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: hints are one line, segment captions fit, in twelve languages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
