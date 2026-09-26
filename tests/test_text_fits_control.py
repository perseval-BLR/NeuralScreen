"""Text must stay inside its control, in every shipped language (audit M1).

The hint columns are already measured by tests/test_settings_hints.py. These
two were not, and each one bled over its neighbours:

* the recording-path row rendered its value at full width and moved the label
  aside by that width, with nothing truncating the value. The payload's path is
  <recording_dir>/neuralscreen-YYYYMMDD-HHMMSS-mmm.mp4, so its length is the
  user's folder plus a fixed tail. Measured at 1080p: 492 px of value in a
  488 px row, leaving the label `room = -136 px` - _clip returns an empty
  surface for a negative width, so the "Path" caption disappeared as well.
* the choice's value inside the select field was rendered unclipped into a
  field 12 px from the left edge and 16 px from the arrow. The GPU picker's
  value is "<i>: <name>" plus " - <no_nr>" for an adapter that was already
  refused (the machine from issue #33). Measured 1080p/ja: 566 px against
  460 px of field room - the value ran under the arrow and past the border.

Measured the way the rest of the suite measures text: the real faces, the real
row geometry, in all twelve languages at 1080p and 4K. The rule is not "the
string is short" but "what the control gives it is at least what it needs",
and the label must keep a readable width instead of collapsing to nothing.

Run:  runtime\\python.exe tests\\test_text_fits_control.py
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

#: A realistic long value: the user's folder plus the fixed tail.
LONG_PATH = ("C:\\Users\\User\\Desktop\\NeuralScreen\\recordings\\"
             "neuralscreen-20260917-004012-123.mp4")
#: The GPU label from issue #33: a refused adapter names it in the value.
LONG_GPU = "0: NVIDIA GeForce RTX 5070 Ti Laptop GPU - no neural pass"

SIZES = ((1920, 1080), (3840, 2160))


def labels_lookup(item, current: str) -> str:
    """The label shown for a choice value (same rule as the code)."""
    labels = item.extra.get("labels") or item.payload or []
    payload = item.payload or []
    if current in payload:
        index = payload.index(current)
        if index < len(labels):
            return str(labels[index])
    return str(current)


def _state(width: int, height: int, lang: str) -> dict:
    return {
        "lang": lang, "nr": True, "profile": "Natural",
        "profiles": ["Natural"], "params": {},
        "split": 0.0, "work_scale": 0.65, "work_scale_cap": 0.65,
        "work_scale_min": 0.1, "nr_small": True,
        "screen_size": f"{width}x{height}", "theme": "light",
        "gpu_text": LONG_GPU, "gpu_ok": True,
        "hdr": False, "spout": False, "rec_indicator": True,
        "windows": [], "window_current": "",
        "recording": False, "recording_status": "published",
        "recording_details": "MP4 \u00b7 av1_nvenc \u00b7 30 fps \u00b7 AAC",
        "recording_path": LONG_PATH,
        "autostart": True, "open_on_start": True, "version": "1.13.1",
        "channel": "@perseval_BLR",
        "monitors": ["0: 3840x2160 (\\\\.\\DISPLAY1)"],
        "monitor": "0: 3840x2160 (\\\\.\\DISPLAY1)",
        "gpus": [LONG_GPU, "1: NVIDIA GeForce RTX 2060"],
        "gpu": LONG_GPU,
        "screenshot_dir": "", "recording_dir": "",
        "screenshot_mode": "ask", "screenshot_format": "png",
        "motion_backend": "nvofa", "frame_limit_mode": "unlimited",
        "frame_limit_custom": 90, "frame_generation": False,
        "frame_multiplier": 2, "hotkeys": {},
    }


def main() -> int:
    import pygame
    pygame.init()
    pygame.display.set_mode((64, 64))
    import fonts
    from i18n import STRINGS
    from overlay_ui import SETTINGS_TABS, OverlayMenu

    failures = []
    seen_info = seen_choice = 0

    for (w, h) in SIZES:
        for lang in STRINGS:
            menu = OverlayMenu(1.0, lambda size=14, mono=False, bold=False,
                               L=lang: fonts.load(size, mono=mono, bold=bold,
                                                  lang=L))
            menu.lang = lang
            menu.set_state(_state(w, h, lang))
            menu.visible = True
            for page, tab in [("main", None)] + [("settings", t)
                                                 for t in SETTINGS_TABS]:
                menu.page = page
                if tab is not None:
                    menu.settings_tab = tab
                menu.layout(w, h)

                # Watch what the DRAWING code asks for, rather than repeating
                # its arithmetic here: _clip is the one funnel every drawn
                # string goes through, so recording its (text, max_w) pairs
                # shows exactly what the code under test gives each piece.
                calls = []
                real_clip = menu._clip

                def spy_clip(font, text, color, max_w, _calls=calls,
                             _real=real_clip):
                    _calls.append((str(text), max_w))
                    return _real(font, text, color, max_w)

                menu._clip = spy_clip
                # Draw for real: the row geometry is a draw-time product.
                surface = pygame.Surface((w, h), pygame.SRCALPHA)
                try:
                    menu.draw(surface)
                except Exception as exc:
                    failures.append(f"{w}x{h}/{lang} {page}: draw raised "
                                    f"{exc!r}")
                    menu._clip = real_clip
                    continue
                finally:
                    menu._clip = real_clip

                for item in menu.items:
                    if item.kind == "info":
                        value = str(item.extra.get("value") or "")
                        label = str(item.extra.get("label") or "")
                        if not value or not label:
                            continue
                        seen_info += 1
                        # Both pieces must have been given a usable width, and
                        # the value must not have been handed more than the row.
                        pairs = [c for c in calls if c[0] in (value, label)]
                        if not pairs:
                            failures.append(
                                f"{w}x{h}/{lang} {page}/{item.key}: neither "
                                f"the value nor the label reached _clip - the "
                                f"row is drawn unclipped")
                            continue
                        for text, max_w in pairs:
                            if max_w < 0:
                                failures.append(
                                    f"{w}x{h}/{lang} {page}/{item.key}: "
                                    f"{text[:28]!r} gets max_w={max_w} - a "
                                    f"negative width renders nothing")
                            elif max_w > item.rect.w:
                                failures.append(
                                    f"{w}x{h}/{lang} {page}/{item.key}: "
                                    f"{text[:28]!r} gets max_w={max_w} in a "
                                    f"{item.rect.w}px row - it can bleed past "
                                    f"the control")
                        got_value = any(c[0] == value for c in calls)
                        if not got_value:
                            failures.append(
                                f"{w}x{h}/{lang} {page}/{item.key}: the value "
                                f"is drawn without going through _clip - it "
                                f"can run over the row")
                    elif item.kind == "choice":
                        strip = item.extra.get("strip")
                        current = str(item.extra.get("current") or "")
                        if strip is None or not current:
                            continue
                        seen_choice += 1
                        # The field leaves 12 px on the left and 16 px for the
                        # arrow. What matters is the rendered width: whatever
                        # the code does, the value must fit that space.
                        room = strip.w - menu._u(12) - menu._u(16)
                        shown = str(labels_lookup(item, current))
                        width = menu._clip(menu._font, shown, (0, 0, 0),
                                           room).get_width()
                        if width > room:
                            failures.append(
                                f"{w}x{h}/{lang} {page}/{item.key}: the choice "
                                f"value {shown[:24]!r} renders {width}px in "
                                f"{room}px of field")
                        # A value that did not go through _clip is drawn at its
                        # full width, which is what the probe measured.
                        full = menu._font.size(shown)[0]
                        if full > room:
                            got_clipped = any(c[0] == shown and 0 <= c[1] <= room
                                              for c in calls)
                            if not got_clipped:
                                failures.append(
                                    f"{w}x{h}/{lang} {page}/{item.key}: the "
                                    f"choice value {shown[:24]!r} is "
                                    f"{full}px in {room}px of field and was "
                                    f"not clipped")

    if not seen_info or not seen_choice:
        print(f"FAIL: the probe never reached the info rows "
              f"({seen_info}) or the choice rows ({seen_choice}) - this test "
              f"no longer covers what it exists for")
        return 1

    for f in failures[:20]:
        print("FAIL:", f)
    if len(failures) > 20:
        print(f"... and {len(failures) - 20} more")
    if failures:
        return 1
    print(f"OK: {seen_info} info rows and {seen_choice} choice fields fit "
          f"their controls in all {len(STRINGS)} languages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
