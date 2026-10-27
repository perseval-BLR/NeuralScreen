"""Which typefaces the overlay draws with.

Up to 1.6.0 every glyph in the program was Consolas - titles, labels,
hints, values alike. One monospaced face for a whole interface is why the
menu read as a debug console rather than as a program.

The split here is by ROLE, not by taste:

* proportional (Segoe UI) for language - titles, labels, hints, buttons;
* monospaced for READINGS - fps, resolutions, timers, key captions. A
  fixed advance keeps digits from dancing sideways as they change, which
  is the one thing Consolas was actually good for.

Faces are looked up by FILE, not by family name. pygame's SysFont
normalises "segoeui" to segoeuil.ttf - the Light weight, too thin over a
game - and does not resolve CascadiaMono.ttf at all. A `fonts/` directory
next to the program wins over the system ones, so a shipped face (IBM
Plex, say) replaces Segoe without touching this module: drop the files in
under the names listed below.

CJK needs fonts of its own: Chinese, Japanese and Korean have no glyphs in
the Latin faces (tofu boxes), and the system fonts that do carry them differ
per script. They are looked up by file too (#186). By family name, SysFont
found them on an English Windows and not on a Chinese one, where the font
registry carries other names - and SysFont does not fail then: it hands
back pygame's default face, which has no CJK at all, so the whole Chinese
interface was boxes. On an English Windows it also resolved "microsoftyahei"
to msyhl.ttc, the Light weight. A face is taken only when it really draws
the script; the family name stays as the last resort.
"""
from __future__ import annotations

import os
from pathlib import Path

import pygame

from paths import BASE_DIR


# Shipped faces win over system ones - first name that exists is used.
UI_FACES = ("IBMPlexSans-Regular.ttf", "segoeui.ttf")
UI_BOLD_FACES = ("IBMPlexSans-SemiBold.ttf", "seguisb.ttf", "segoeuib.ttf")
MONO_FACES = ("IBMPlexMono-Regular.ttf", "CascadiaMono.ttf", "consola.ttf")
MONO_BOLD_FACES = ("IBMPlexMono-Medium.ttf", "CascadiaMono.ttf",
                   "consolab.ttf")

# Where to look, in order: our own folder first, then the system one.
FONT_DIRS = (BASE_DIR / "fonts",
             Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts")

# Per-script system families. Yu Gothic covers Japanese kanji, Malgun
# Gothic hangul, YaHei simplified Chinese.
CJK_FONTS = {"zh": "microsoftyahei", "ja": "yugothic", "ko": "malgungothic"}

# The same scripts by file, regular and bold, each with the fonts Windows
# ships as fallbacks after the first choice.
CJK_FILES = {
    "zh": (("msyh.ttc", "simhei.ttf", "simsun.ttc"),
           ("msyhbd.ttc", "msyh.ttc", "simhei.ttf")),
    "ja": (("YuGothM.ttc", "YuGothR.ttc", "meiryo.ttc", "msgothic.ttc"),
           ("YuGothB.ttc", "meiryob.ttc", "YuGothM.ttc", "msgothic.ttc")),
    "ko": (("malgun.ttf", "gulim.ttc"), ("malgunbd.ttf", "malgun.ttf", "gulim.ttc")),
}
# What a face must draw to count as covering the script.
CJK_SAMPLES = {"zh": "中文设置", "ja": "日本語の設定", "ko": "한국어 설정"}

_cache: dict = {}


def _find(names) -> Path | None:
    for directory in FONT_DIRS:
        for name in names:
            path = directory / name
            if path.exists():
                return path
    return None


def load(size: int, mono: bool = False, bold: bool = False,
         lang: str = "en"):
    """A font for `size` px. Cached - the loaders are called per redraw."""
    size = max(8, int(size))
    key = (size, mono, bold, lang if lang in CJK_FONTS else "")
    hit = _cache.get(key)
    if hit is not None:
        return hit
    font = _build(size, mono, bold, lang)
    _cache[key] = font
    return font


def covers(path, text: str) -> bool:
    """Whether the font file at `path` has a glyph for every character.

    Through pygame.freetype: pygame.font measures a missing character as
    the box it draws, so only freetype can tell "no glyph" from a glyph.
    """
    try:
        import pygame.freetype as ft
        if not ft.get_init():
            ft.init()
        face = ft.Font(str(path), 16)
        return all(m is not None for m in face.get_metrics(text))
    except Exception:
        return False


def cjk(lang: str, size: int, bold: bool = False):
    """A face that draws `lang`'s script (zh/ja/ko), or None (#186).

    By file first, in the shipped folder and then the system one; the
    family name only after that, through match_font, which answers an
    unknown name with None - SysFont answers it with pygame's default
    face, and that is how the boxes got in.
    """
    if lang not in CJK_FILES:
        return None
    sample = CJK_SAMPLES[lang]
    candidates = [directory / name for directory in FONT_DIRS
                  for name in CJK_FILES[lang][1 if bold else 0]]
    try:
        by_name = pygame.font.match_font(CJK_FONTS[lang], bold=bold)
    except Exception:
        by_name = None
    if by_name:
        candidates.append(Path(by_name))
    for path in candidates:
        if not path.exists() or not covers(path, sample):
            continue
        try:
            return pygame.font.Font(str(path), size)
        except Exception:
            continue
    return None


def _build(size: int, mono: bool, bold: bool, lang: str):
    if lang in CJK_FONTS:
        font = cjk(lang, size, bold=bold)
        if font is not None:
            return font
    faces = ((MONO_BOLD_FACES if bold else MONO_FACES) if mono
             else (UI_BOLD_FACES if bold else UI_FACES))
    path = _find(faces)
    if path is not None:
        try:
            font = pygame.font.Font(str(path), size)
            # Cascadia and Consolas ship no semibold file: when the bold
            # face resolved to the regular one, synthesise the weight.
            if bold and path.name in ("CascadiaMono.ttf",):
                font.set_bold(True)
            return font
        except Exception:
            pass
    # Nothing found on disk: fall back the way the program did before.
    try:
        return pygame.font.SysFont("consolas", size, bold=bold)
    except Exception:
        return pygame.font.Font(None, size)
