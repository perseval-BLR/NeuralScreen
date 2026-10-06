r"""A typing key is never accepted as a global hotkey.

RegisterHotKey takes a key away from every program on the system. The
module promised that bare arrows are never registered, but parse_binding
accepted any key it knew with no modifier at all: pressing E, 1, Up, Insert,
Home or PgDn in a hotkey field bound it, and from then on that key stopped
typing everywhere. Shift alone is still typing (Shift+Insert pastes,
Shift+arrows select).

Shift with a numpad digit or the numpad dot is the other dead end: with Num
Lock on, Windows sends those as the navigation keys while Shift is held, so
the combination was saved, reported as applied, and never fired.

Checked:
* parse_binding refuses the typing keys bare and with Shift only, refuses
  Shift+numpad digit/dot, and still takes F-keys, the numpad, and any key
  with Ctrl or Alt;
* every default binding still parses;
* the hotkey field (the real apply_menu_action) answers a bare letter with
  the `hotkey_bad` alert and saves nothing;
* a config that stored such a key from an older version falls back to the
  command's default key, with a log line.

Run:  runtime\python.exe tests\test_hotkey_typing_keys.py
"""
import contextlib
import io
import queue
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import commands  # noqa: E402
from hotkeys import DEFAULT_BINDINGS, build_bindings, parse_binding  # noqa: E402
from i18n import STRINGS  # noqa: E402


def main() -> int:
    failures = []

    for text in ("E", "q", "1", "0", "Up", "Down", "Left", "Right", "Insert",
                 "Delete", "Home", "End", "PgUp", "PgDn",
                 "Shift+E", "Shift+Insert", "Shift+Up", "Shift+Home",
                 "Shift+Num1", "Shift+Num0", "Shift+Numdot",
                 "Ctrl+Shift+Num1", "Alt+Shift+Num5"):
        if parse_binding(text) is not None:
            failures.append(f"parse_binding({text!r}) accepted a key that "
                            f"cannot be a global hotkey")

    for text in ("F1", "F12", "Num1", "Num0", "Numdot", "Numplus", "Numdiv",
                 "Ctrl+E", "Alt+Up", "Ctrl+Insert", "Ctrl+Alt+Q", "Shift+F1",
                 "Shift+Numplus", "Ctrl+Num1", "Ctrl+Shift+K"):
        if parse_binding(text) is None:
            failures.append(f"parse_binding({text!r}) refused a valid hotkey")

    for _id, (_m, _vk, cmd, name) in DEFAULT_BINDINGS.items():
        if parse_binding(name) is None:
            failures.append(f"the default {name!r} for {cmd} no longer parses")

    # The hotkey field: a bare letter is refused with the existing alert.
    alerts = []
    rebinds = []
    st = types.SimpleNamespace(
        display=types.SimpleNamespace(
            alert=lambda text, *a, **kw: alerts.append(text),
            menu=types.SimpleNamespace(set_hotkeys=lambda *_: None)),
        lang="en", cfg={"hotkeys": {}}, hotkey_bindings=build_bindings({}),
        hotkeys=types.SimpleNamespace(rebind=rebinds.append),
        tray_commands=queue.Queue())
    for text in ("E", "Up", "Shift+Num1"):
        alerts.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            commands.apply_menu_action(st, ("hotkey", "toggle", text))
        if st.cfg["hotkeys"] or rebinds:
            failures.append(f"the hotkey field saved {text!r}: "
                            f"{st.cfg['hotkeys']}")
            st.cfg["hotkeys"] = {}
            rebinds.clear()
        if alerts != [STRINGS["en"]["hotkey_bad"]]:
            failures.append(f"the hotkey field answered {text!r} with "
                            f"{alerts}, not the hotkey_bad alert")

    # A config from an older version that stored a bare Insert for record.
    defaults = {cmd: (mods, vk) for mods, vk, cmd, _n in DEFAULT_BINDINGS.values()}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        built = build_bindings({"record": "Insert", "toggle": "Ctrl+F10"})
    by_cmd = {cmd: (mods, vk) for mods, vk, cmd, _n in built.values()}
    if by_cmd.get("record") != defaults["record"]:
        failures.append(f"a stored bare Insert was registered for record: "
                        f"{by_cmd.get('record')}")
    if "record" not in out.getvalue() or "Insert" not in out.getvalue():
        failures.append(f"the fallback for a stored invalid binding was not "
                        f"logged: {out.getvalue()!r}")
    if by_cmd.get("toggle", (0, 0))[1] != 0x79:
        failures.append("a valid override next to the invalid one was dropped")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: typing keys stay with the user, Shift+numpad digits are refused, "
          "and old configs fall back to the defaults")
    return 0


if __name__ == "__main__":
    sys.exit(main())
