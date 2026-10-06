r"""The polling fallback fires only what RegisterHotKey accepted, race-free.

Two defects in the poller:

* it walked every binding, including the ones RegisterHotKey refused. A
  refused combination is held by another program, which gets the press - so
  the poller made one key do two things, while the panel had just told the
  user that key would not work (`hotkey_in_use`);
* the cooldown table is check-and-stamped by two threads (the WM_HOTKEY
  loop and the poller) without the lock, so both could read "not yet" for
  the same press and deliver it twice.

Checked:
* with RegisterHotKey refusing one id (a fake user32, a fake key state), a
  fresh press of the refused key fires nothing and a press of the accepted
  one fires once;
* the cooldown table is only touched while the controller's lock is held -
  by the poller's tick and by the real message loop handling a WM_HOTKEY.

Run:  runtime\python.exe tests\test_hotkey_poll_registered.py
"""
import ctypes
import queue
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import hotkeys  # noqa: E402

VK_NUM1 = hotkeys.VK_NUMPAD[1]
VK_NUM2 = hotkeys.VK_NUMPAD[2]
REFUSED_ID = 2


class _User32:
    """The real user32, with RegisterHotKey refusing one id."""

    def __init__(self, real):
        self._real = real

    def RegisterHotKey(self, _hwnd, hk_id, _mods, _vk):
        return 0 if hk_id == REFUSED_ID else 1

    def UnregisterHotKey(self, _hwnd, _hk_id):
        return 1

    def __getattr__(self, name):
        return getattr(self._real, name)


class _Keys:
    def __init__(self):
        self.down = set()

    def pressed(self, vk):
        return vk in self.down


class _Guarded(dict):
    """The cooldown table, noting every touch made without the lock."""

    def __init__(self, lock, violations, where):
        super().__init__()
        self._lock = lock
        self._violations = violations
        self._where = where

    def _check(self, op):
        if not self._lock.locked():
            self._violations.append(f"{self._where}: {op} without the lock")

    def get(self, *a):
        self._check("read")
        return super().get(*a)

    def __getitem__(self, k):
        self._check("read")
        return super().__getitem__(k)

    def __setitem__(self, k, v):
        self._check("write")
        super().__setitem__(k, v)


def _bindings():
    return {1: (hotkeys.MOD_NOREPEAT, VK_NUM1, "toggle", "Num1"),
            REFUSED_ID: (hotkeys.MOD_NOREPEAT, VK_NUM2, "settings", "Num2")}


def main() -> int:
    failures = []
    keys = _Keys()
    real_user32, real_pressed = hotkeys.user32, hotkeys._pressed
    hotkeys.user32 = _User32(real_user32)
    hotkeys._pressed = keys.pressed
    try:
        # 1. A refused binding is not polled.
        out = []

        class _Out:
            put = out.append

        hk = hotkeys.HotkeyController(_Out(), _bindings())
        hk._register()
        if hk.failed != ["Num2"] or hk.registered != ["Num1"]:
            failures.append(f"the fake registration did not split as meant: "
                            f"registered {hk.registered}, failed {hk.failed}")
        hk._poll_tick()                          # baseline: all keys up
        keys.down = {VK_NUM2}
        hk._poll_tick()
        if "settings" in out:
            failures.append("the poller fired Num2, which RegisterHotKey "
                            "refused - another program holds that key")
        keys.down = set()
        hk._poll_tick()
        keys.down = {VK_NUM1}
        hk._poll_tick()
        if out.count("toggle") != 1:
            failures.append(f"a press of the registered Num1 fired "
                            f"{out.count('toggle')} times through the poller")

        # 2. The poller's check-and-stamp happens under the lock.
        violations = []
        keys.down = set()
        hk._poll_tick()
        hk._poll_last = _Guarded(hk._lock, violations, "poller")
        keys.down = {VK_NUM1}
        hk._poll_tick()
        if out.count("toggle") != 2:
            failures.append("the second press did not fire - nothing checked")

        # 3. And so does the message loop's, on a real WM_HOTKEY.
        cmds = queue.Queue()
        hk2 = hotkeys.HotkeyController(cmds, _bindings())
        thread = threading.Thread(target=hk2._run, daemon=True)
        thread.start()
        if not hk2._ready.wait(3.0):
            failures.append("the hotkey thread did not come up")
        else:
            hk2._poll_last = _Guarded(hk2._lock, violations, "message loop")
            real_user32.PostThreadMessageW(hk2._tid, hotkeys.WM_HOTKEY, 1, 0)
            try:
                got = cmds.get(timeout=2.0)
            except queue.Empty:
                got = None
            if got != "toggle":
                failures.append(f"the posted WM_HOTKEY delivered {got!r}")
            real_user32.PostThreadMessageW(hk2._tid, hotkeys.WM_QUIT, 0, 0)
            thread.join(2.0)
        for v in dict.fromkeys(violations):
            failures.append(f"the cooldown table is touched unlocked ({v}): "
                            f"a WM_HOTKEY and a poller sample can both fire "
                            f"one press")
    finally:
        hotkeys.user32, hotkeys._pressed = real_user32, real_pressed

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: only registered keys are polled, and the cooldown is claimed "
          "under the lock on both paths")
    return 0


if __name__ == "__main__":
    sys.exit(main())
