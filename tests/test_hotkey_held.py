"""A key held down at startup must not fire a hotkey.

The polling fallback fired a command for any key that was down on its
first tick: `_poll_down` started empty, so the first sample looked like
a fresh press. A physically held Num1 (stuck key, a game holding it)
produced a spurious "toggle" right after every start - NR came up and
immediately went OFF (user: "NR OFF (bypass NGX)" in every test run).

The fix: the first sample is the baseline (`was_down = down`), so the
poller only triggers on a real press edge.

Checked: with a key held at start, no command fires; after the key is
released and pressed again, the command fires exactly once.
"""
import os
import sys
import threading
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))  # the modules live in app/
import hotkeys  # noqa: E402

VK_NUMPAD1 = 0x61


class _Queue:
    """A minimal queue stand-in: the test reads the commands as a list."""

    def __init__(self, out: list):
        self._out = out

    def put(self, cmd) -> None:
        self._out.append(cmd)


class FakeKeys:
    """A stand-in for the Win32 key state: the test controls it directly."""

    def __init__(self):
        self.down = set()

    def pressed(self, vk):
        return vk in self.down


def main() -> int:
    failures = []
    fake = FakeKeys()
    real_pressed = hotkeys._pressed
    hotkeys._pressed = fake.pressed
    try:
        commands = []
        hk = hotkeys.HotkeyController(
            _Queue(commands),
            {"toggle": ("Num1", VK_NUMPAD1)})
        # The poller loop is what we test; drive it directly.
        hk._active = True
        hk._bindings = {1: (0, VK_NUMPAD1, "toggle", "Num1")}
        hk._live_ids = {1}          # what _register records for an accepted id

        # 1. The key is held BEFORE the first poll: no command.
        fake.down.add(VK_NUMPAD1)
        hk._poll_tick()
        print(f"held at start -> commands {commands}")
        if commands:
            failures.append("a key held at startup must not fire")

        # 2. Still held: still nothing.
        hk._poll_tick()
        if commands:
            failures.append("a held key must not fire on later ticks")

        # 3. Released, then pressed again: exactly one command.
        fake.down.discard(VK_NUMPAD1)
        hk._poll_tick()
        fake.down.add(VK_NUMPAD1)
        hk._poll_tick()
        print(f"after a real press -> commands {commands}")
        if commands != ["toggle"]:
            failures.append(f"a real press should fire once, got {commands}")

        # 4. Held again: no repeat.
        hk._poll_tick()
        if commands != ["toggle"]:
            failures.append("holding must not repeat the command")
    finally:
        hotkeys._pressed = real_pressed

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: a key held at startup is the baseline, not an event")
    return 0


if __name__ == "__main__":
    sys.exit(main())
