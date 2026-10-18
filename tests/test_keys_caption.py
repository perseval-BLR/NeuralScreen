"""Settings captions read as captions, whole (v2.6.0 design pass).

Under the key fields one caption says where the keys live and that Backspace
takes a key off the keyboard (#134) - the second half is discoverable only if
it is written down. On one line the Russian lost it to the ellipsis at 100%
on 1080p: "... Backspace очищает клав...". The caption now wraps.

Checked in every language at 100% on 1920x1080: the caption's lines, joined,
are the whole text, each line fits the panel, and the reserved space holds
all of them.

Also: the Media tab's idle conversion row ("what the button is for") is a
caption in the hints' face - it was a value cell, in the numbers' mono face.

Fails before the change: one clipped line, and a value cell.

Run:  runtime\\python.exe tests\\test_keys_caption.py
"""
import os
import sys

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
    from i18n import STRINGS
    for lang in sorted(STRINGS):
        menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False, lang=lang:
                              fonts.load(size, mono=mono, bold=bold, lang=lang))
        menu.set_state({"lang": lang})
        menu.visible = True
        menu.page, menu.settings_tab = "settings", "keys"
        menu.layout(1920, 1080)
        s = STRINGS[lang]
        full = f"{s['hotkey_hint']} - {s['hotkey_clear']}"
        rect = menu._hint_rel
        lines = menu._keys_hint_lines(s, rect.w)
        joined = " ".join(lines)
        if joined.replace(" ", "") != full.replace(" ", ""):
            failures.append(f"{lang}: the caption's lines are not the whole text: {lines}")
        wide = [ln for ln in lines if menu._small_font.size(ln)[0] > rect.w]
        if wide:
            failures.append(f"{lang}: a line is wider than the panel: {wide}")
        line_h = menu._small_font.get_height() + menu._u(4)
        if rect.h < len(lines) * line_h:
            failures.append(f"{lang}: {len(lines)} lines in {rect.h} px")
        if lang == "ru":
            print(f"    ru: {len(lines)} lines: {lines}")
        # The Media tab's idle conversion row is a caption, not a value: it
        # was drawn in the numbers' mono face.
        menu.settings_tab = "rec"
        menu.layout(1920, 1080)
        row = next((i for i in menu.items if i.key == "convert_status"), None)
        if row is None or row.kind != "note" or row.extra.get("text") != s["convert_hint"]:
            failures.append(f"{lang}: the idle conversion row is "
                            f"{row and (row.kind, row.extra)}")
    pygame.quit()

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
