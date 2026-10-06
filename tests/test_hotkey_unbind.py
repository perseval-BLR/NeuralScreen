"""A command can be taken off the keyboard entirely (#134).

The second half of the request (Koaxz, #134): "allow setting key assignments
to empty/None, so users can keep only the hotkeys they actually need". The
menu could always REPLACE a key, never remove one - `parse_binding("")`
returns None and build_bindings then leaves the default entry alone, so the
command kept the key it had. Rebounding is not the answer to "I do not want
this key": a new key is still a key.

What this locks, all of it through the real code paths a click takes:

  * build_bindings really DROPS the entry for an unbind word, and does not
    quietly fall back to the default;
  * an empty string in the overrides still means "leave this one alone" -
    the two cases must not collapse into one, or a hand-written config with
    "" would silently mute a hotkey;
  * the dispatcher accepts the unbind without calling it an unparsable
    combination (that was the trap: it alerted "not recognised" and changed
    nothing), writes it to config.json, and rebinds the controller;
  * the row is left with no key to conflict over - _hotkey_owner must not
    report a removed binding as "taken by another action" when a different
    command is given that key;
  * a config carrying the unbind word survives a restart.

Run:  runtime\\python.exe tests\\test_hotkey_unbind.py
"""
import json
import os
import queue
import sys
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ (autocheck)
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import hotkeys  # noqa: E402
from hotkeys import DEFAULT_BINDINGS, build_bindings  # noqa: E402

#: The words that take a command off the keyboard. Read defensively: on the
#: code before this fix the name does not exist at all, and the suite should
#: report one clear FAIL instead of an ImportError traceback.
try:
    from hotkeys import UNBIND_WORDS
except ImportError:                                     # pragma: no cover
    UNBIND_WORDS = frozenset()

PARAMS = {"intensity": 1.0, "local_tone": 0.5, "local_structure": 1.0,
          "skin_structure": -1.0, "style": 1}


def _commands(bindings: dict) -> list[str]:
    return sorted(entry[2] for entry in bindings.values())


def check_build_bindings(failures: list) -> None:
    """The parser/set side: an unbind word drops the entry, "" does not."""
    every = _commands(DEFAULT_BINDINGS)

    if not UNBIND_WORDS:
        failures.append("hotkeys.UNBIND_WORDS does not exist - there is no "
                        "value a field can hold that means 'no key' (#134)")
        return

    for word in sorted(UNBIND_WORDS):
        built = build_bindings({"record": word})
        if "record" in _commands(built):
            failures.append(f"build_bindings({word!r}) kept the record "
                            f"binding - the key is still held")
        if len(built) != len(DEFAULT_BINDINGS) - 1:
            failures.append(f"build_bindings({word!r}) left "
                            f"{len(built)} entries, expected "
                            f"{len(DEFAULT_BINDINGS) - 1}")

    # Case and whitespace are what a hand-edited config looks like.
    for word in ("  None  ", "OFF", "Unbound"):
        if "record" in _commands(build_bindings({"record": word})):
            failures.append(f"build_bindings({word!r}) kept the binding - the "
                            f"word is not matched case/whitespace-insensitively")

    # An empty string keeps its old meaning: leave this one alone.
    built = build_bindings({"record": ""})
    if "record" not in _commands(built):
        failures.append("an empty override removed the binding - a config "
                        "with \"\" used to mean 'no opinion' and must not "
                        "silently mute a hotkey")

    # And a word that is neither a key nor an unbind word still changes
    # nothing - the default stays, as before.
    built = build_bindings({"record": "NotAKey"})
    if "record" not in _commands(built):
        failures.append("an unparsable override removed the binding instead of "
                        "being ignored")

    # Every command can be removed, one at a time, and the rest stay.
    for cmd in every:
        built = build_bindings({cmd: "none"})
        if cmd in _commands(built):
            failures.append(f"{cmd} could not be taken off the keyboard")
        if len(built) != len(DEFAULT_BINDINGS) - 1:
            failures.append(f"removing {cmd} changed the other bindings: "
                            f"{_commands(built)}")

    # All of them at once: an empty binding set is a legitimate answer.
    built = build_bindings({cmd: "none" for cmd in every})
    if built:
        failures.append(f"removing every command left {_commands(built)}")

    # ...and it must stay empty at the next launch. startup hands exactly this
    # {} to the controller and to the Num Lock / log helpers; an empty dict
    # read as "no argument" brought all the defaults back on every restart.
    ctl = hotkeys.HotkeyController(queue.Queue(), built)
    if ctl._bindings:
        failures.append(f"a controller built with every command removed "
                        f"registers {_commands(ctl._bindings)} at launch")
    if hotkeys.numlock_needed(built):
        failures.append(f"with every command removed the Num Lock alert still "
                        f"names {hotkeys.numlock_needed(built)}")
    if hotkeys.describe(built):
        failures.append(f"with every command removed the startup log still "
                        f"describes {hotkeys.describe(built)!r}")


def check_no_owner_conflict(failures: list) -> None:
    """A removed binding must not read as a key somebody else holds."""
    import commands

    # Bindings with `record` taken off, and a fresh key offered to `toggle`.
    bindings = build_bindings({"record": "none"})
    record_vk = next(vk for _m, vk, cmd, _n in DEFAULT_BINDINGS.values()
                     if cmd == "record")
    got = commands._hotkey_owner(
        SimpleNamespace(hotkey_bindings=bindings),
        (hotkeys.MOD_NOREPEAT, record_vk), "toggle")
    if got is not None:
        failures.append(f"a removed binding is still reported as holding its "
                        f"key: the new command was refused by {got!r}")


def check_dispatch(failures: list) -> None:
    """The click: the real dispatcher removes the key and writes it down."""
    import commands

    tmp = Path(BASE / "_work" / "test-hotkey-unbind")
    tmp.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps({"hotkeys": {}, "profile": "Natural",
                                    "theme": "light"}), encoding="utf-8")

    alerts: list = []
    st = SimpleNamespace(
        cfg={"hotkeys": {}, "profile": "Natural", "theme": "light"},
        hotkeys=_RecordingHotkeys(),
        display=SimpleNamespace(menu=_Menu(), alert=alerts.append),
        lang="en", params=dict(PARAMS), cfg_path=cfg_path, presets={},
        monitor=0, work_scale=1.0, split_pos=0.0, startup_menu=False,
        nr_small=True, window_hwnd=None, width=1920, height=1080,
        work_w=1920, work_h=1080, hotkey_bindings=dict(DEFAULT_BINDINGS),
    )
    commands.apply_menu_action(st, ("hotkey", "record", "none"))

    if "record" in _commands(st.hotkey_bindings):
        failures.append("the dispatcher did not remove the binding - the key "
                        "is still registered")
    if st.hotkeys.last is None:
        failures.append("the dispatcher did not rebind the controller")
    elif "record" in _commands(st.hotkeys.last):
        failures.append("the controller was handed the binding that was "
                        "just removed")
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    if saved.get("hotkeys", {}).get("record") != "none":
        failures.append(f"the removal did not reach config.json "
                        f"({saved.get('hotkeys')!r}) - it would come back on "
                        f"the next launch")
    # The user must be told it worked, and NOT told the combination is
    # unrecognised - that is the trap this path had.
    import i18n
    strings = i18n.STRINGS["en"]
    if strings["hotkey_bad"] in alerts:
        failures.append("the removal was reported as an unrecognised "
                        "combination")
    if strings["settings_applied"] not in alerts:
        failures.append(f"the removal was not confirmed to the user "
                        f"(alerts={alerts})")

    # And it survives a restart: the stored word rebuilds to no binding.
    rebuilt = build_bindings(saved.get("hotkeys"))
    if "record" in _commands(rebuilt):
        failures.append("the stored config does not rebuild without the "
                        "binding - the key comes back after a restart")


def check_menu_field(failures: list) -> None:
    """The field: Backspace/Delete clears, and the row says "none"."""
    import pygame
    from overlay_ui import OverlayMenu

    if not pygame.font.get_init():
        pygame.font.init()
    # key_text() reads the modifier state through pygame.key.get_mods(), which
    # needs the video system; the same call the other menu tests make.
    pygame.display.set_mode((64, 64))
    menu = OverlayMenu(1.0, lambda size=14: pygame.font.Font(None, size))
    menu.lang = "en"
    menu.visible = True
    menu.page = "settings"
    menu.settings_tab = "keys"

    # A field waiting for a key, cleared with Backspace and with Delete.
    for key in (pygame.K_BACKSPACE, pygame.K_DELETE):
        menu.capturing = "record"
        event = pygame.event.Event(pygame.KEYDOWN, key=key, mod=0)
        actions = menu.handle_event(event)
        unbinds = [a for a in actions
                   if a[0] == "hotkey" and a[1] == "record"
                   and str(a[2]).lower() in UNBIND_WORDS]
        if not unbinds:
            failures.append(f"{pygame.key.name(key)} did not emit an unbind "
                            f"action (actions={actions})")
        if menu.capturing is not None:
            failures.append(f"{pygame.key.name(key)} left the field capturing")

    # With a modifier held, Delete is a key to bind, not the clear gesture:
    # "Ctrl+Delete" was assignable before #134 and must stay so.
    pygame.key.set_mods(pygame.KMOD_LCTRL)
    try:
        menu.capturing = "record"
        actions = menu.handle_event(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_DELETE, mod=pygame.KMOD_LCTRL))
    finally:
        pygame.key.set_mods(0)
    bound = [a for a in actions if a[0] == "hotkey" and a[1] == "record"]
    text = str(bound[0][2]).lower() if bound else ""
    if not bound or text in UNBIND_WORDS or "delete" not in text:
        failures.append(f"Ctrl+Delete unbound the command instead of binding "
                        f"it (actions={actions})")

    # Backspace with a modifier still clears: it was never bindable, and
    # handing "Ctrl+Backspace" to the parser only earned a "not recognised".
    pygame.key.set_mods(pygame.KMOD_LCTRL)
    try:
        menu.capturing = "record"
        actions = menu.handle_event(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_BACKSPACE, mod=pygame.KMOD_LCTRL))
    finally:
        pygame.key.set_mods(0)
    if ("hotkey", "record", "none") not in actions:
        failures.append(f"Ctrl+Backspace did not clear the field "
                        f"(actions={actions})")

    # Esc still cancels rather than clearing.
    menu.capturing = "record"
    actions = menu.handle_event(pygame.event.Event(pygame.KEYDOWN,
                                                   key=pygame.K_ESCAPE, mod=0))
    if any(a[0] == "hotkey" for a in actions):
        failures.append("Escape unbound the command instead of cancelling")

    # Every language can say "no key" in the row it draws.
    import i18n
    for lang, table in i18n.STRINGS.items():
        if not table.get("hotkey_none"):
            failures.append(f"{lang} cannot render a cleared key field")
        if not table.get("hotkey_clear"):
            failures.append(f"{lang} does not say how to clear a key")


class _RecordingHotkeys:
    """The controller's observable surface: the last bindings it was given."""

    def __init__(self):
        self.last = None

    def rebind(self, bindings):
        self.last = dict(bindings)

    def wait_rebound(self, timeout=0.5):
        return True

    def suspend(self):
        pass

    def resume(self):
        pass

    @property
    def failed(self):
        return []


class _Menu:
    """The menu's surface: set_hotkeys is what the dispatcher calls."""

    def __init__(self):
        self.labels = None

    def set_hotkeys(self, mapping):
        self.labels = dict(mapping)


def main() -> int:
    failures: list = []
    check_build_bindings(failures)
    print("build_bindings removes what was asked and nothing else: checked")
    check_no_owner_conflict(failures)
    print("a removed key is free for another command: checked")
    check_dispatch(failures)
    print("the dispatcher removes it, saves it and says so: checked")
    check_menu_field(failures)
    print("Backspace clears a field, Escape still cancels: checked")

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: a command can be taken off the keyboard (#134)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
