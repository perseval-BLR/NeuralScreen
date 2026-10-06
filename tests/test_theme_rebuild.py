"""The theme the user picked survives a pipeline rebuild.

A rebuild - a monitor switch, a GPU switch, one-window mode - recreates the
menu and restores its theme from st.cfg. The theme was only written to
config.json when the menu closed, so a switch while the menu was open
restored the OLD value and threw the user back to light (issue #33, seen in
a user's log: "menu theme -> dark", then a monitor change, then light
again).

So the action lands in st.cfg immediately, and the file write stays where it
was. This test drives the menu action and then the restore that
rebuild_pipeline performs.

The restore is exercised through the PRODUCT's rebuild_pipeline, not a copy
of its three lines. An earlier version of this file re-implemented the
restore locally, so deleting pipeline.py's actual restore left the test
green (audit: DISHONEST-DOC). The stubs follow test_rebuild_warmup.py, which
already drives the real rebuild for the warm-up contract.

Run:  runtime\\python.exe tests\\test_theme_rebuild.py
"""
import re
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import commands  # noqa: E402
import pipeline  # noqa: E402
import settings_io  # noqa: E402


class _Menu:
    """A stand-in for the overlay menu that records what it was told."""

    def __init__(self):
        self.state = {"theme": "light", "lang": "en"}
        self.visible = False
        self.offset = [0, 0]
        self.user_scale = 1.0
        self.user_height = None
        self.applied = []

    def set_state(self, payload):
        self.applied.append(dict(payload))
        for k, v in payload.items():
            if k in self.state:
                self.state[k] = v

    # The rebuild calls these on the recreated menu; they are not what this
    # test is about, but the real code path touches them.
    def set_hotkeys(self, *a, **k):
        pass

    def set_stats(self, *a, **k):
        pass


class _Display:
    def __init__(self, menu):
        self.menu = menu

    def set_origin(self, *a, **k):
        pass

    def set_lang(self, *a, **k):
        pass

    def set_excluded_from_capture(self, *a, **k):
        pass

    def enter_switch_mode(self, *a, **k):
        pass

    def resize(self, *a, **k):
        pass

    def set_visible(self, *a, **k):
        pass

    def is_visible(self):
        return True


def _state(menu):
    return types.SimpleNamespace(
        width=2560, height=1600, work_w=1664, work_h=1040,
        params={"intensity": 1.0}, effective_warmup=120,
        cfg={"theme": "light", "profile": "Natural", "lang": "en"}, lang="en",
        display=_Display(menu), output_rgba=None, mon_origin=(0, 0),
        mon_w=2560, mon_h=1600, window_hwnd=None, hotkey_bindings={},
        capture=types.SimpleNamespace(resolution=(2560, 1600)),
        worker=None, worker_logs=[], reader=None, worker_stop=None, shm=None,
        # rebuild_pipeline sizes the guides before the worker starts now.
        motion_small=False)


def _rebuild(st):
    """Run the PRODUCT's rebuild with the window/worker work stubbed out.

    Everything after start_worker touches the real window and the real
    channels; the theme restore happens before that, and the menu state
    survives the exception, which is what we read back.
    """
    saved = (pipeline.start_worker, pipeline.SharedFrameBuffer,
             pipeline.require_compatibility, pipeline.Display)
    pipeline.SharedFrameBuffer = lambda w, h: types.SimpleNamespace(
        name="x", close=lambda: None, width=w, height=h)
    pipeline.start_worker = lambda params, w, h, warmup, full_w, full_h, shm: (
        types.SimpleNamespace(poll=lambda: None), [], None, None)
    pipeline.require_compatibility = lambda st: None
    pipeline.Display = lambda *a, **k: st.display
    try:
        pipeline.rebuild_pipeline(st, "note")
    except Exception:
        # The rebuild finishes its window work after the restore; a stub can
        # only get so far. The assertions below read what was applied.
        pass
    finally:
        (pipeline.start_worker, pipeline.SharedFrameBuffer,
         pipeline.require_compatibility, pipeline.Display) = saved


def main() -> int:
    failures = []

    # 1. Picking dark reaches the state, not just the menu.
    menu = _Menu()
    st = _state(menu)
    menu.set_state({"theme": "dark"})      # the menu applies it itself
    commands.apply_menu_action(st, ("theme", "dark"))
    if st.cfg.get("theme") != "dark":
        failures.append(f"the state says theme={st.cfg.get('theme')!r}")
    print(f"    after the action: cfg theme={st.cfg.get('theme')!r}")

    # 2. The PRODUCT's rebuild restores what the user picked, not what was
    #    on disk. This is the assertion the old local copy could not make:
    #    remove pipeline.py's restore and this stops passing.
    menu.applied.clear()
    _rebuild(st)
    applied_theme = [p.get("theme") for p in menu.applied if "theme" in p]
    print(f"    rebuild applied theme={applied_theme}")
    if "dark" not in applied_theme:
        failures.append(
            "a rebuild did not restore the chosen theme - the menu came "
            "back as the disk value (issue #33)")

    # 3. Back to light works the same way round.
    commands.apply_menu_action(st, ("theme", "light"))
    menu.applied.clear()
    _rebuild(st)
    applied_theme = [p.get("theme") for p in menu.applied if "theme" in p]
    if st.cfg.get("theme") != "light" or "light" not in applied_theme:
        failures.append("switching back to light did not survive the rebuild")

    # 4. A value that is not a theme is refused rather than stored.
    commands.apply_menu_action(st, ("theme", "chartreuse"))
    if st.cfg.get("theme") != "light":
        failures.append(f"a bogus theme was stored: {st.cfg.get('theme')!r}")

    # 5. The restore must stay in the product, not only in this test: if the
    #    three lines leave pipeline.py, parts 2-3 are the only guard left and
    #    they run through the same file - spell the requirement out.
    src = (BASE / "app" / "pipeline.py").read_text(encoding="utf-8")
    if "saved_theme = st.cfg.get(\"theme\")" not in src:
        failures.append("pipeline.py no longer reads st.cfg's theme for the "
                        "restore - the menu comes back light after a rebuild")

    # 6. EVERY theme the menu offers must survive the restore, not just the
    #    two that came first. A third theme added to the control and left out
    #    of the restore whitelists looks applied until the first restart,
    #    then quietly reverts - and the user has no reason to connect the
    #    two events. This is the assertion that fails on a stale whitelist.
    theme_names = getattr(settings_io, "THEME_NAMES", ())
    if not theme_names:
        failures.append("settings_io has no THEME_NAMES - the theme list "
                        "must have one home")
    for name in theme_names:
        st = _state(menu)
        commands.apply_menu_action(st, ("theme", name))
        menu.applied.clear()
        _rebuild(st)
        applied = [p.get("theme") for p in menu.applied if "theme" in p]
        if st.cfg.get("theme") != name or name not in applied:
            failures.append(
                f"the {name!r} theme does not survive a rebuild: cfg="
                f"{st.cfg.get('theme')!r}, applied={applied} - a restore "
                f"whitelist is stale")
    print(f"    every offered theme survives: {list(theme_names)}")

    # 7. And no module may keep its own copy of the theme list. The copies
    #    are what made part 6 fail in the first place, and another one will
    #    do it again: scan the sources for a two-name theme whitelist
    #    (`in ("light", "dark")` and its list spelling) instead of trusting
    #    the reader to notice. The canonical three-name tuple in settings_io
    #    is the one this test wants, so it is not matched here. main.py is
    #    scanned too: its window-recreate path (a fullscreen game changing
    #    the display mode) keeps a third copy of the restore, and v2.1.3
    #    shipped it with the two-name list after the app/ copies were fixed.
    stale = []
    pair = re.compile(r"""[([]\s*["']light["']\s*,\s*["']dark["']\s*[)\]]""")
    for path in sorted((BASE / "app").glob("*.py")) + [BASE / "main.py"]:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if pair.search(code):
                stale.append(f"{path.name}:{lineno}: {line.strip()[:70]}")
    if stale:
        failures.append("a theme whitelist is duplicated again (use "
                        "settings_io.THEME_NAMES): " + "; ".join(stale))

    # 8. The same recreate path in main.py snapshots the panel height into
    #    cfg before the window dies; it has to give it back to the new menu
    #    as rebuild_pipeline does, or the next save_menu_layout writes
    #    menu_height null over the height the user dragged.
    main_text = (BASE / "main.py").read_text(encoding="utf-8")
    recreate = main_text.split("recreating the window", 1)[-1][:6000]
    if 'st.cfg["menu_height"]' not in recreate:
        failures.append("main.py's window recreate no longer snapshots the "
                        "panel height - update this check")
    elif "st.display.menu.user_height = int(saved_height)" not in recreate:
        failures.append("main.py's window recreate snapshots the panel height "
                        "but never gives it back to the new menu")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the chosen theme survives a rebuild (through the real "
          "rebuild_pipeline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
