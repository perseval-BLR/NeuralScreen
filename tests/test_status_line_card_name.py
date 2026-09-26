"""The status line: the readings must be visible, and in the right order.

The bug: the readings were laid out from the RIGHT edge in reverse order and
any value that ran out of room was silently skipped, which made the FIRST
casualty NR - the rate people watch - while the resolution (printed again in
the source section above) stayed. Measured at 4K with a real card name: NR and
FG gone, "SKIP 0  3840x2160" on screen. A reading nobody can see reads as a
broken counter.

The line is now: dot, state, card name on the left; NR and FG anchored to the
right edge, FG at the very edge. The card name takes what room is left, so it
can never push a reading off the bar. Resolution, the skipped-frame count and
the frame counter are gone from the line by decision - all three are numbers
nobody acts on, and the resolution is already printed above.

What this test locks:
  * one line, and it fits inside its block (no leftover reserved strip);
  * NR and FG are on screen, in that order, with FG at the right edge - not
    merely rendered, but blitted where they can be seen;
  * the removed values really are gone, so nobody re-adds them by accident;
  * the card name is still drawn (it is the one value that cannot be guessed);
  * at a width where something must be dropped, NR survives.

Run:  runtime\\python.exe tests\\test_status_line_card_name.py
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import overlay_ui  # noqa: E402
import pygame  # noqa: E402

CARDS = (
    "NVIDIA GeForce RTX 5070 Ti",
    "NVIDIA GeForce RTX 5070 Ti Laptop GPU",
    "NVIDIA GeForce RTX 4090 Laptop GPU",
    "NVIDIA GeForce RTX 3080 Ti Laptop GPU",
)
SIZES = ((1920, 1080), (2560, 1440), (3840, 2160))
LANGS = ("en", "de", "ru")
#: What the line must show, left to right. FG is last so it lands on the right
#: edge.
# The pair: the output rate with the rate it is built on in brackets. Both
# numbers are asserted - as ONE string, so going back to two separate readings
# fails here (#109).
WANT = ("FG 167 (98.8)",)


def _state(width: int, height: int, lang: str, card: str) -> dict:
    return {
        "lang": lang, "nr": True, "profile": "Natural",
        "profiles": ["Natural"], "params": {},
        "split": 0.0, "work_scale": 0.65, "work_scale_cap": 0.65,
        "work_scale_min": 0.1, "nr_small": True,
        "screen_size": f"{width}x{height}", "theme": "light",
        "gpu_text": card, "gpu_ok": True,
        "hdr": False, "spout": False, "rec_indicator": True,
        "windows": [], "window_current": "",
        "monitors": [], "monitor": "", "gpus": [], "gpu": "",
        "version": "1.15.0", "channel": "@perseval_BLR",
        "autostart": True, "open_on_start": True,
        "recording": False, "screenshot_dir": "", "recording_dir": "",
    }


def _watch(menu, stats: dict, w: int, h: int) -> dict:
    """Draw for real and report what the status line PUT ON SCREEN.

    `render()` is not proof of visibility - the drawer builds an image for
    every value and only then decides whether it fits - so the capture is the
    blit, scoped to `_draw_stats` (other sections print similar strings).
    Images are kept alive because CPython reuses `id()` once an object is
    collected, which silently aliases unrelated strings.

    Returns {"mono": [(label, x, y)], "ui": [(label, x, y)]}.
    """
    keep: list = []
    registry: dict[int, tuple[str, str]] = {}
    placed: dict[str, list] = {"mono": [], "ui": []}
    active = {"on": False}

    class _TaggedFont:
        def __init__(self, font, tag):
            self._font = font
            self._tag = tag

        def render(self, text, *a, **kw):
            img = self._font.render(text, *a, **kw)
            keep.append(img)
            registry[id(img)] = (str(text), self._tag)
            return img

        def __getattr__(self, name):
            return getattr(self._font, name)

    class _WatchSurface(pygame.Surface):
        __slots__ = ()

        def blit(self, source, dest, *a, **kw):
            if active["on"]:
                info = registry.get(id(source))
                if info is not None:
                    placed[info[1]].append((info[0], int(dest[0]), int(dest[1])))
            return super().blit(source, dest, *a, **kw)

    real_mono, real_ui = menu._mono_small, menu._small_font
    object.__setattr__(menu, "_mono_small", _TaggedFont(real_mono, "mono"))
    object.__setattr__(menu, "_small_font", _TaggedFont(real_ui, "ui"))
    real_draw = menu._draw_stats

    def scoped(surface, s):
        active["on"] = True
        try:
            return real_draw(surface, s)
        finally:
            active["on"] = False

    menu._draw_stats = scoped
    menu.stats = stats
    try:
        menu.draw(_WatchSurface((w, h), pygame.SRCALPHA))
    finally:
        menu._draw_stats = real_draw
        object.__setattr__(menu, "_mono_small", real_mono)
        object.__setattr__(menu, "_small_font", real_ui)
    return placed


def main() -> int:
    pygame.init()
    pygame.display.set_mode((64, 64))
    import fonts
    from overlay_ui import OverlayMenu

    failures = []
    checked = 0
    edge_cases = 0

    # ---- the line as a whole, at every size/language/card ---------------
    for (w, h) in SIZES:
        for lang in LANGS:
            for card in CARDS:
                menu = OverlayMenu(1.0, lambda size=14, mono=False, bold=False,
                                   L=lang: fonts.load(size, mono=mono,
                                                      bold=bold, lang=L))
                menu.lang = lang
                menu.set_state(_state(w, h, lang, card))
                menu.visible = True
                menu.page = "main"
                menu.layout(w, h)
                stats = {"fps": 98.8, "display_fps": 167.3,
                         "resolution": f"{w}x{h}",
                         "frames": 19704}

                drawn = []
                real_clip = menu._clip

                def spy(font, text, color, max_w, _d=drawn, _r=real_clip):
                    img = _r(font, text, color, max_w)
                    _d.append((str(text), max_w, img.get_width()))
                    return img

                menu._clip = spy
                try:
                    placed = _watch(menu, stats, w, h)
                except Exception as exc:
                    failures.append(f"{w}x{h}/{lang}/{card[:28]}: draw raised "
                                    f"{exc!r}")
                    menu._clip = real_clip
                    continue
                finally:
                    menu._clip = real_clip
                checked += 1

                # One line, and it must sit inside the block it was laid out
                # for - a line taller than its block draws over the section
                # below while still looking fine in the strings.
                line = getattr(menu, "_stats_line1", None)
                block = menu._stats_rect
                if line is None or line.h <= 0:
                    failures.append(f"{w}x{h}/{lang}/{card[:28]}: the status "
                                    f"line was not laid out")
                elif not block.contains(line):
                    failures.append(
                        f"{w}x{h}/{lang}/{card[:28]}: the status line "
                        f"{tuple(line)} is outside its block {tuple(block)} - "
                        f"it draws over the section below")
                elif block.h > line.h + menu._u(6):
                    # The block is sized for exactly one line. A taller block
                    # is a leftover of the two-line layout: it pushes every
                    # section below it down and leaves a dead strip.
                    failures.append(
                        f"{w}x{h}/{lang}/{card[:28]}: the status block is "
                        f"{block.h}px tall for a {line.h}px line - the layout "
                        f"still reserves room for a second line")

                # Blit order is right-to-left (the counter is drawn first so it
                # lands at the edge), so the visual order is by x, not by the
                # order the calls happened to be made in.
                mono_sorted = sorted(placed["mono"], key=lambda p: p[1])
                labels = [label for label, _x, _y in mono_sorted]
                for want in WANT:
                    if want not in labels:
                        failures.append(
                            f"{w}x{h}/{lang}/{card[:28]}: {want!r} is not on "
                            f"screen. Drawn: {labels}")
                if labels == list(WANT):
                    # The counter has to end at the right edge: it is the
                    # value the reporter could not find, and an anchored run
                    # that stops short reads as a line that failed to draw.
                    right = line.right - menu._u(overlay_ui.STAT_PAD)
                    last_label, last_x, _ly = mono_sorted[-1]
                    end = last_x + menu._mono_small.size(last_label)[0]
                    if abs(end - right) > 1:
                        failures.append(
                            f"{w}x{h}/{lang}/{card[:28]}: the readings end at "
                            f"{end}px, not at the right edge {right}px")
                    # The name must not collide with the first reading. It is
                    # elided when long, so it is matched by prefix, not by
                    # equality.
                    head = card[:12]
                    names = [p for p in placed["ui"]
                             if p[0].startswith(head)
                             or card.startswith(p[0].rstrip("\u2026"))]
                    if names:
                        name_label = max(names, key=lambda p: p[1])
                        name_end = name_label[1] + \
                            menu._small_font.size(name_label[0])[0]
                        if name_end + menu._u(14) > mono_sorted[0][1]:
                            failures.append(
                                f"{w}x{h}/{lang}/{card[:28]}: the card name "
                                f"runs into the readings")
                        else:
                            floor = menu._small_font.size("NVIDIA")[0] + \
                                menu._u(6)
                            if menu._small_font.size(name_label[0])[0] <= floor:
                                failures.append(
                                    f"{w}x{h}/{lang}/{card[:28]}: the name is "
                                    f"elided to {name_label[0]!r} - it does "
                                    f"not say which card")
                    else:
                        failures.append(
                            f"{w}x{h}/{lang}/{card[:28]}: the card name is not "
                            f"drawn - it is the one value that cannot be "
                            f"guessed from elsewhere")
                else:
                    # The order is the fix: NR first, the counter last so it
                    # sits at the edge. A different order means the priority
                    # logic changed.
                    if set(labels) == set(WANT):
                        failures.append(
                            f"{w}x{h}/{lang}/{card[:28]}: the readings are on "
                            f"screen but in the wrong order: {labels}")

                # The two values the owner removed must stay removed: a
                # permanent "SKIP 0" spends width on a number nobody acts on,
                # and the resolution is already printed in the source section.
                for gone in ("SKIP 12", f"{w}x{h}", "FR 19704"):
                    if gone in labels:
                        failures.append(
                            f"{w}x{h}/{lang}/{card[:28]}: {gone!r} is back on "
                            f"the status line - it was removed by decision")

    # ---- Frame Generation running alone (NR off) ------------------------
    # Reported from a live log: with only Frame Generation on, the worker was
    # interpolating (`[fg] displayed 287.7 FPS (real + generated, 3x)`) while
    # the line showed no counter at all. The readings were gated on the neural
    # pass being on, so the one mode where FG is the only thing running hid the
    # one number that proves it runs.
    alone = 0
    for (w, h) in ((1920, 1080), (2560, 1440)):
        menu = OverlayMenu(1.0, lambda size=14, mono=False, bold=False,
                           L="en": fonts.load(size, mono=mono, bold=bold,
                                              lang="en"))
        menu.lang = "en"
        state = _state(w, h, "en", "NVIDIA GeForce RTX 5070 Ti")
        state["nr"] = False                    # the neural pass is off
        state["frame_generation"] = True
        menu.set_state(state)
        menu.visible = True
        menu.page = "main"
        menu.layout(w, h)
        stats = {"fps": 0.0, "display_fps": 287.7, "resolution": f"{w}x{h}", "frames": 92372}
        placed = _watch(menu, stats, w, h)
        alone += 1
        labels = [label for label, _x, _y in
                  sorted(placed["mono"], key=lambda p: p[1])]
        if not any(l.startswith("FG ") for l in labels):
            failures.append(
                f"{w}x{h}/FG alone: the Frame Generation counter is not on "
                f"screen while NR is off. Drawn: {labels}")
        # NR must NOT be drawn: "NR 0.0" reads as a broken network rather than
        # a switched-off one, and the state label already says which mode.
        if any(l.startswith("NR ") for l in labels):
            failures.append(
                f"{w}x{h}/FG alone: NR is drawn while its pass is off "
                f"({labels}) - it reads as a broken counter")

    # ---- priority: when the line runs out of room ----------------------
    # Small panel scales and long numbers force a choice. NR must survive it:
    # dropping NR while keeping a lesser value was the original bug.
    for scale in (0.3, 0.35, 0.4):
        menu = OverlayMenu(scale, lambda size=14, mono=False, bold=False,
                           L="en": fonts.load(size, mono=mono, bold=bold,
                                              lang="en"))
        menu.lang = "en"
        menu.set_state(_state(1920, 1080, "en",
                              "NVIDIA GeForce RTX 5070 Ti Laptop GPU"))
        menu.visible = True
        menu.page = "main"
        menu.layout(1920, 1080)
        stats = {"fps": 98.8, "display_fps": 167.3,
                 "resolution": "3840x2160", "frames": 197045678}

        placed = _watch(menu, stats, 1920, 1080)
        edge_cases += 1
        mono_sorted = sorted(placed["mono"], key=lambda p: p[1])
        labels = [label for label, _x, _y in mono_sorted]
        if "FG 167 (98.8)" not in labels:
            failures.append(
                f"scale {scale}: NR was dropped to make room for something "
                f"else - the rate is the last thing that may go. Drawn: "
                f"{labels}")
        if labels and labels[-1] != "FG 167 (98.8)":
            failures.append(
                f"scale {scale}: FG is not the rightmost value ({labels}) - "
                f"it is anchored to the edge by design")

    if not checked:
        print("FAIL: no status line was drawn - this test no longer covers "
              "what it exists for")
        return 1

    for f in failures[:15]:
        print("FAIL:", f)
    if len(failures) > 15:
        print(f"... and {len(failures) - 15} more")
    if failures:
        return 1
    print(f"OK: one status line, readings NR/FG with FG at the right edge, "
          f"on {checked} combinations and {edge_cases} tight widths")
    return 0


if __name__ == "__main__":
    sys.exit(main())
