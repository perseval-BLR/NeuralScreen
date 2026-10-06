r"""Two bindings on one key with different modifiers both fire through the poller.

The poller's state was keyed by the virtual key alone. With Num1 and
Ctrl+Num1 bound to two commands, the first binding looked at in a tick
stored "Num1 is down", and the second then saw no press edge - it could
never fire through the poller, which is the only path in a game that
swallows WM_HOTKEY. The cooldown was keyed by the key too, so Ctrl+Num1
right after Num1 was taken for the duplicate of the first.

Checked with the real controller, a fake user32 that accepts every
registration and a fake key state, in both binding orders:
* Ctrl+Num1 fires its own command (and not the bare Num1's);
* a bare Num1 fires its own command (and not Ctrl+Num1's);
* Ctrl+Num1 pressed right after Num1, inside the cooldown, still fires.

Run:  runtime\python.exe tests\test_hotkey_same_key_mods.py
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import hotkeys  # noqa: E402

VK_NUM1 = hotkeys.VK_NUMPAD[1]
BARE = (hotkeys.MOD_NOREPEAT, VK_NUM1, "toggle", "Num1")
CTRL = (hotkeys.MOD_CONTROL | hotkeys.MOD_NOREPEAT, VK_NUM1, "framegen",
        "Ctrl+Num1")


class _User32:
    def __init__(self, real):
        self._real = real

    def RegisterHotKey(self, *_a):
        return 1

    def UnregisterHotKey(self, *_a):
        return 1

    def __getattr__(self, name):
        return getattr(self._real, name)


class _Keys:
    def __init__(self):
        self.down = set()

    def pressed(self, vk):
        return vk in self.down


def _run(order, keys, failures):
    out = []

    class _Out:
        put = out.append

    hk = hotkeys.HotkeyController(_Out(), dict(enumerate(order, start=1)))
    hk._register()
    label = " then ".join(b[3] for b in order)

    keys.down = set()
    hk._poll_tick()
    keys.down = {hotkeys.VK_CONTROL, VK_NUM1}
    hk._poll_tick()
    if out != ["framegen"]:
        failures.append(f"[{label}] Ctrl+Num1 produced {out}, expected "
                        f"['framegen']")
    keys.down = set()
    hk._poll_tick()
    hk._poll_last.clear()                 # past the cooldown
    out.clear()
    keys.down = {VK_NUM1}
    hk._poll_tick()
    if out != ["toggle"]:
        failures.append(f"[{label}] a bare Num1 produced {out}, expected "
                        f"['toggle']")
    # Ctrl+Num1 right after it - a different combination, no cooldown shared.
    keys.down = set()
    hk._poll_tick()
    keys.down = {hotkeys.VK_CONTROL, VK_NUM1}
    hk._poll_tick()
    if out != ["toggle", "framegen"]:
        failures.append(f"[{label}] Ctrl+Num1 right after Num1 produced "
                        f"{out}, expected ['toggle', 'framegen']")


def main() -> int:
    failures = []
    keys = _Keys()
    real_user32, real_pressed = hotkeys.user32, hotkeys._pressed
    hotkeys.user32 = _User32(real_user32)
    hotkeys._pressed = keys.pressed
    try:
        _run((BARE, CTRL), keys, failures)
        _run((CTRL, BARE), keys, failures)
    finally:
        hotkeys.user32, hotkeys._pressed = real_user32, real_pressed

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: Num1 and Ctrl+Num1 are two bindings to the poller")
    return 0


if __name__ == "__main__":
    sys.exit(main())
