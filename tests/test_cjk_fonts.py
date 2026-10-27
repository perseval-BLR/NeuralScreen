"""Chinese, Japanese and Korean draw in a face that has their glyphs (#186).

A Chinese user saw every CJK character of the interface as a box. The CJK
faces were looked up by family name through pygame's SysFont, and on a
Windows whose font registry does not carry the English family name SysFont
does not fail - it hands back pygame's default face, which has no CJK glyphs.

Checked with the family names made to resolve the way they do there
(SysFont answers with the default face, match_font with None), every face
traced to the file it was opened from and that file asked, through
pygame.freetype, for the glyphs (pygame.font measures a missing character
as its box, so it cannot tell):

1. the interface font for zh / ja / ko, regular and bold, draws that
   language's own strings from i18n;
2. the panel's per-script faces (window titles, the language list, file
   names) draw their scripts;
3. a Latin language keeps the shipped Latin face.

Fails before the change: the CJK faces came from SysFont only.

Run:  runtime\\python.exe tests\\test_cjk_fonts.py
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))

import pygame  # noqa: E402

SCRIPT = {"zh": "中文", "ja": "日本語", "ko": "한국어"}


def draws(font, text: str, opened: dict) -> bool:
    """Whether the file `font` was opened from has every glyph of `text`."""
    path = opened.get(id(font))
    if path is None:
        return False            # pygame's default face: no CJK at all
    import pygame.freetype as ft
    ft.init()
    return all(m is not None for m in ft.Font(path, 16).get_metrics(text))


def main() -> int:
    failures = []
    pygame.init()
    pygame.display.set_mode((64, 64))
    import fonts
    import overlay_ui as ui
    from i18n import STRINGS

    real_font, real_sysfont, real_match = (pygame.font.Font, pygame.font.SysFont,
                                           pygame.font.match_font)
    opened = {}
    keep = []

    def traced(path, size):
        font = real_font(path, size)
        keep.append(font)
        opened[id(font)] = path
        return font
    pygame.font.Font = traced
    # A localized font registry: no family name resolves - SysFont falls back
    # to the default face the way pygame does, match_font finds nothing.
    pygame.font.SysFont = lambda name, size, bold=False, italic=False: traced(None, size)
    pygame.font.match_font = lambda name, bold=False, italic=False: None
    fonts._cache.clear()
    try:
        for lang in ("zh", "ja", "ko"):
            s = STRINGS[lang]
            text = "".join(str(s[k]) for k in ("started", "win_mode_on", "win_fail") if k in s)
            for bold in (False, True):
                face = fonts.load(18, bold=bold, lang=lang)
                if not draws(face, text, opened):
                    failures.append(f"{lang} {'bold' if bold else 'regular'}: "
                                    f"the interface face has no glyphs for {ascii(text[:12])}")
        menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False:
                              fonts.load(size, mono=mono, bold=bold, lang="en"))
        for lang, name in fonts.CJK_FONTS.items():
            face = menu._cjk_fonts.get(name)
            if face is None or not draws(face, SCRIPT[lang], opened):
                failures.append(f"the panel's {name} face does not draw {ascii(SCRIPT[lang])}")
        latin = fonts.load(18, lang="en")
        plex = fonts._find(fonts.UI_FACES)
        if plex is None or opened.get(id(latin)) != str(plex):
            failures.append("a Latin language no longer gets the shipped Latin face")
    finally:
        pygame.font.Font, pygame.font.SysFont, pygame.font.match_font = (
            real_font, real_sysfont, real_match)
        fonts._cache.clear()

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
