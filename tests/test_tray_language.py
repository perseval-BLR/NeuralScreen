r"""The tray menu speaks the interface language, and follows a change of it.

The tray menu was half English in every language: "NR: ON", "NR: OFF",
"Scale: 0.50", "Scale +0.05", "Scale -0.05" were literals in tray.py, and
Settings / Exit were taken once at launch - a language picked in the panel
never reached the tray for the rest of the session.

Checked with the real TrayController menu (pystray items, read the way
update_menu reads them):
* in each of the 12 languages every menu line is that language's i18n text,
  and none is an English literal (unless the language's own string is);
* the real apply_menu_action language switch relabels the menu that is
  already built - the one the running icon holds.

Run:  runtime\python.exe tests\test_tray_language.py
"""
import queue
import sys
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import pystray  # noqa: E402

import commands  # noqa: E402
import tray as tray_mod  # noqa: E402
from i18n import STRINGS  # noqa: E402
from tray import TrayController  # noqa: E402


def tray_labels(strings):
    """tray.tray_labels; before it existed, what startup passed at launch."""
    pick = getattr(tray_mod, "tray_labels", None)
    if pick is not None:
        return pick(strings)
    return {"settings": strings.get("settings_title", "Settings"),
            "quit": strings.get("exit", "Exit")}


ENGLISH_LITERALS = ("NR: ON", "NR: OFF", "Scale +0.05", "Scale -0.05")


def _texts(menu) -> list[str]:
    return [str(item.text) for item in menu.items
            if item is not pystray.Menu.SEPARATOR]


def _expected(lang: str, scale: float) -> list[str]:
    s = STRINGS[lang]
    return [s["nr_on"], s["nr_off"], f"{s['nr_res']}: {scale:.2f}",
            s["hk_scale_up"], s["hk_scale_down"], s["settings_title"],
            s["exit"]]


def main() -> int:
    # Twelve scripts in the messages; a cp1251 console must not end the run.
    sys.stdout.reconfigure(errors="backslashreplace")
    failures = []
    scale = 0.5

    for lang in STRINGS:
        tray = TrayController(queue.Queue(), labels=tray_labels(STRINGS[lang]))
        tray._set_state(nr=True, scale=scale)
        got = _texts(tray._build_menu())
        if got != _expected(lang, scale):
            failures.append(f"[{lang}] the tray reads {got}, the language "
                            f"says {_expected(lang, scale)}")
        if lang != "en":
            english = [t for t in got if t in ENGLISH_LITERALS
                       or t.startswith("Scale:")]
            if english:
                failures.append(f"[{lang}] English in the tray menu: {english}")

    # The language switch from the panel reaches the menu already built.
    tray = TrayController(queue.Queue(), labels=tray_labels(STRINGS["en"]))
    tray._set_state(nr=True, scale=scale)
    menu = tray._build_menu()
    st = SimpleNamespace(
        lang="en", tray=tray,
        display=SimpleNamespace(set_lang=lambda _l: None,
                                menu=SimpleNamespace(set_state=lambda _s: None)))
    commands.apply_menu_action(st, ("lang", "ru"))
    if st.lang != "ru":
        failures.append("the language action did not switch - nothing to check")
    got = _texts(menu)
    if got != _expected("ru", scale):
        failures.append(f"after switching to Russian the tray still reads "
                        f"{got}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print(f"OK: the tray menu is localized in {len(STRINGS)} languages and "
          f"follows a language change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
