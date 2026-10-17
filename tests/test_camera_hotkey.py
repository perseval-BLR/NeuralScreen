"""The camera has a hotkey of its own (#164).

The request: the camera's look is switched while a game runs, and the switch
lived only on the main page of the menu. The camera now has a global hotkey -
Num8 by default, rebindable on the KEYS page like the others.

Checked without a window, on the real modules:

* the default bindings: "lens" is on Num8, and no two commands share a key;
* the KEYS page lists it, so it can be rebound or taken off the keyboard;
* the command, through the real commands.drain_commands as the hotkey thread
  posts it: the camera goes on, exactly one LENS command with the on bit
  reaches the worker's pipe, the menu state follows, the pop-up says so - and
  a second press turns it off the same way.

Fails before the change: there was no "lens" command, no binding and no row.

Run:  runtime\\python.exe tests\\test_camera_hotkey.py
"""
import io
import os
import queue
import struct
import sys
import types
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))

import pygame  # noqa: E402


class Worker:
    pid = 4242

    def __init__(self):
        self.stdin = io.BytesIO()

    def poll(self):
        return None


def main() -> int:
    failures = []
    pygame.init()
    import commands
    import hotkeys
    import overlay_ui
    import protocol

    lens = [entry for entry in hotkeys.DEFAULT_BINDINGS.values() if entry[2] == "lens"]
    if [e[3] for e in lens] != ["Num8"]:
        failures.append(f"the camera's default binding is {[e[3] for e in lens]}, not Num8")
    keys = [(mods & ~hotkeys.MOD_NOREPEAT, vk)
            for mods, vk, _c, _n in hotkeys.DEFAULT_BINDINGS.values()]
    if len(keys) != len(set(keys)):
        failures.append("two default hotkeys share a key")
    if "lens" not in [cmd for cmd, _label in overlay_ui.HOTKEY_ROWS]:
        failures.append("the KEYS page has no row for the camera")

    worker = Worker()
    alerts, states = [], []
    st = types.SimpleNamespace(
        cfg={"lens": False, "lens_fov": 110.0, "lens_noise": 0.3},
        worker=worker, worker_failed=False, lang="en",
        tray_commands=queue.Queue(), running=True,
        display=types.SimpleNamespace(
            menu=types.SimpleNamespace(set_state=states.append, visible=False),
            alert=alerts.append))
    size = struct.calcsize(protocol.LENS_FMT)
    for want in (True, False):
        before = len(worker.stdin.getvalue())
        st.tray_commands.put("lens")
        try:
            commands.drain_commands(st)
        except Exception as exc:  # noqa: BLE001 - reported
            failures.append(f"the camera hotkey raised {type(exc).__name__}: {exc}")
            break
        sent = worker.stdin.getvalue()[before:]
        if st.cfg.get("lens") is not want:
            failures.append(f"after the hotkey the camera is {st.cfg.get('lens')}, not {want}")
        if len(sent) != size:
            failures.append(f"the hotkey wrote {len(sent)} bytes, not one LENS command")
            continue
        flags = struct.unpack(protocol.LENS_FMT, sent)[1]
        if bool(flags & protocol.LENS_FLAG_ON) is not want:
            failures.append(f"the LENS command says on={bool(flags & 1)}, not {want}")
        if not states or states[-1].get("lens") is not want:
            failures.append(f"the menu was not told the camera is {want}: {states[-1:]}")
        if not alerts or ("ON" if want else "OFF") not in alerts[-1]:
            failures.append(f"the pop-up after the hotkey was {alerts[-1:]}")
    pygame.quit()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the camera has its own hotkey (Num8) and it switches the camera "
          "the way the menu does")
    return 0


if __name__ == "__main__":
    sys.exit(main())
