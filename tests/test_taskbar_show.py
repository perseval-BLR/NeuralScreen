"""Taskbar activation may show the menu, but must never close it.

Windows can emit more than one activation notification around one taskbar
click. If each notification is treated as a toggle, a currently visible menu
closes without a user action and reads as the whole application disappearing
(issue #87). Tray/hotkey ``settings`` remains a toggle; taskbar
``show_settings`` is deliberately idempotent and re-applies layered attrs.

Run: runtime\\python.exe tests\\test_taskbar_show.py
"""
from __future__ import annotations

import os
import queue
import sys
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import commands  # noqa: E402


class _Menu:
    def __init__(self, visible: bool):
        self.visible = visible
        self.toggle_calls = 0
        self.state_calls = 0

    def set_state(self, _payload) -> None:
        self.state_calls += 1

    def toggle(self) -> bool:
        self.toggle_calls += 1
        self.visible = not self.visible
        return self.visible


class _Display:
    def __init__(self, menu_visible: bool, window_visible: bool = True):
        self.menu = _Menu(menu_visible)
        self.opaque = []
        self.input = []
        self.refreshes = 0
        self.window_visible = window_visible
        self.reveals = 0
        self.visibility = []
        self.raises = 0
        self.draws = []

    def refresh_colorkey(self) -> None:
        self.refreshes += 1

    def set_menu_opaque(self, enabled: bool) -> None:
        self.opaque.append(enabled)

    def set_menu_input(self, enabled: bool) -> None:
        self.input.append(enabled)

    def apply_menu_mode_input(self) -> None:
        # The real one: the panel takes input, the bar does not (v2.6.0).
        self.set_menu_input(bool(self.menu.visible)
                            and not getattr(self.menu, "mini", False))

    def is_visible(self) -> bool:
        return self.window_visible

    def reveal(self) -> None:
        self.reveals += 1
        self.window_visible = True

    def set_visible(self, enabled: bool) -> None:
        self.visibility.append(enabled)
        self.window_visible = enabled

    def raise_topmost(self) -> None:
        self.raises += 1

    def follow_taskbar_desktop(self) -> None:
        # Virtual-desktop placement is exercised by the vdesk probe test; the
        # stub only has to exist for the menu-show path.
        pass

    def draw_overlay(self, interval: float) -> None:
        self.draws.append(interval)


def _state(visible: bool, *, window_visible: bool = True):
    # st.hotkeys is part of the state the real startup always builds
    # (startup.py: "st.hotkeys = HotkeyController(...)") and commands.py
    # touches it on several routes, among them closing the menu - which has to
    # resume the controller (audit H2). The stub carries it like the rest.
    class _Hotkeys:
        def __init__(self):
            self.log = []

        def resume(self):
            self.log.append("resume")

        def suspend(self):
            self.log.append("suspend")

    return SimpleNamespace(
        tray_commands=queue.Queue(),
        display=_Display(visible, window_visible),
        hotkeys=_Hotkeys(),
        window_hwnd=None, running=True, frame_index=0,
    )


def main() -> int:
    failures = []
    original_payload = commands.settings_io.menu_payload
    original_save = commands.settings_io.save_menu_layout
    commands.settings_io.menu_payload = lambda _st: {"probe": True}
    commands.settings_io.save_menu_layout = lambda _st: True
    try:
        # A duplicate taskbar activation must leave an open menu open, while
        # forcing the visual attrs back onto the native window.
        st = _state(True)
        st.tray_commands.put("show_settings")
        commands.drain_commands(st)
        if not st.display.menu.visible:
            failures.append("show_settings closed an already visible menu")
        if st.display.menu.toggle_calls:
            failures.append("show_settings used toggle() for an open menu")
        if st.display.refreshes != 1:
            failures.append("show_settings did not reapply the layered attributes")
        if st.display.opaque != [True] or st.display.input != [True]:
            failures.append("show_settings did not retain menu opacity/input")
        if st.display.visibility != [True] or st.display.raises != 1 \
                or st.display.draws != [0.0]:
            failures.append("duplicate show_settings did not physically re-show the menu")

        # It must also open a genuinely closed menu and recover a hidden HWND.
        st = _state(False, window_visible=False)
        st.tray_commands.put("show_settings")
        commands.drain_commands(st)
        if not st.display.menu.visible:
            failures.append("show_settings did not open a closed menu")
        if st.display.menu.toggle_calls:
            failures.append("show_settings used toggle() while opening")
        if st.display.reveals != 1 or st.display.visibility != [True] \
                or st.display.raises != 1 or st.display.draws != [0.0]:
            failures.append("show_settings logged open without recovering the hidden HWND")

        # The existing hotkey/tray command retains its explicit toggle contract.
        st = _state(True)
        st.tray_commands.put("settings")
        commands.drain_commands(st)
        if st.display.menu.visible or st.display.menu.toggle_calls != 1:
            failures.append("settings no longer toggles the menu")
        if st.display.refreshes:
            failures.append("ordinary settings toggle unexpectedly used taskbar recovery")
        if st.display.raises or st.display.draws:
            failures.append("closing the menu unexpectedly raised or redrew it")
    finally:
        commands.settings_io.menu_payload = original_payload
        commands.settings_io.save_menu_layout = original_save

    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: taskbar show is idempotent; tray/hotkey settings still toggles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
