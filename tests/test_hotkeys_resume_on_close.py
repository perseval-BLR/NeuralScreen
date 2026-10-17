"""The global hotkeys come back however the menu is closed (audit H2).

Clicking a hotkey field on the KEYS page sets `capturing` and emits
("capture", cmd), which suspends the global hotkeys so the next key lands in
the field instead of toggling the menu. Only the menu's own close button had
the matching resume(): closing the menu from the tray or the taskbar - the
`cmd in ("settings", "show_settings")` branch - flipped `menu.visible` and
never touched the hotkeys.

HotkeyController.suspend() unregisters every RegisterHotKey. With no matching
resume() nothing re-registers them for the rest of the session: Num0 (record),
Num1 (NR), Num2 (menu), Num3 (screenshot), Num4/Num6 (scale), Num5 (window
mode), Num7 (FG) and Ctrl+Alt+Q (quit) are all dead, and the tray or taskbar
is the only way back. The menu's own `capturing` flag stayed set too, so the
next open silently swallowed the first keydown as a remap.

Checked here, driving the real commands.drain_commands / apply_menu_action and
the real OverlayMenu state machine:

* the field click really does suspend (the premise of the bug);
* every route that closes the menu resumes - the tray/taskbar toggle, the
  collapse button, the settings-page back button, and the close button;
* the menu's capturing flag is cleared on those routes, so the next open does
  not eat a keypress;
* a resume without a suspend is harmless (the controller posts a message
  nobody acts on).

Run:  runtime\\python.exe tests\\test_hotkeys_resume_on_close.py
"""
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


class _Hotkeys:
    """The controller's observable surface: suspended + the call log."""

    def __init__(self):
        self.suspended = False
        self.log = []

    def suspend(self):
        self.suspended = True
        self.log.append("suspend")

    def resume(self):
        self.suspended = False
        self.log.append("resume")


class _Display:
    def __init__(self):
        import pygame

        from overlay_ui import OverlayMenu

        if not pygame.font.get_init():
            pygame.font.init()
        self.menu = OverlayMenu(1.0, lambda size=14: pygame.font.Font(None, size))
        self.menu.lang = "en"
        self.calls = []

    def set_menu_opaque(self, v):
        self.calls.append(("set_menu_opaque", v))

    def set_menu_input(self, v):
        self.calls.append(("set_menu_input", v))

    def apply_menu_mode_input(self):
        # The real one: the panel takes input, the bar does not (v2.6.0).
        self.set_menu_input(bool(self.menu.visible) and not self.menu.mini)

    def refresh_colorkey(self):
        pass

    def is_visible(self):
        return True

    def set_visible(self, v):
        pass

    def reveal(self):
        pass

    def follow_taskbar_desktop(self):
        pass

    def raise_topmost(self):
        pass

    def draw_overlay(self, *a):
        pass

    def set_fullscreen_layer(self, *a):
        pass

    def set_window_layer(self, *a):
        pass

    def set_lang(self, _lang):
        pass


def _state():
    disp = _Display()
    st = SimpleNamespace(
        display=disp, hotkeys=_Hotkeys(), tray_commands=queue.Queue(),
        cfg={"theme": "light", "profile": "Natural", "hotkeys": {}},
        window_hwnd=None, mon_w=1920, mon_h=1080,
        work_scale=1.0, params={}, nr_small=True, lang="en", paused=False,
        split_pos=0.0, startup_menu=False, presets={}, cfg_path=Path("no.json"),
        width=1920, height=1080, work_w=1920, work_h=1080,
        gpu_text="RTX 5070 Ti", gpu_ok=None, worker_logs=[], gpu_alerted=False,
        fg_alerted=False, hdr_alerted=False, recorder=None,
        recording_finalizer=None, last_recording={}, compatibility_result=None,
        window_list=None, environment={}, screenshot_mode="ask", running=True,
        monitor=0, taskbar=None,
        capture=SimpleNamespace(devicename="\\\\.\\DISPLAY1"),
        hotkey_bindings={}, frame_index=0, pending_shot=None,
        next_auto_revive=0.0, worker_failed=False,
    )
    st.params = {"intensity": 1.0, "local_tone": 0.5,
                 "local_structure": 1.0, "skin_structure": -1.0}
    return st


def _capture_on(st):
    """Reproduce the KEYS-page click: the real menu state + the real action."""
    st.display.menu.visible = True
    st.display.menu.page = "settings"
    # What _activate_item does before it emits ("capture", key).
    st.display.menu.capturing = "framegen"
    commands.apply_menu_action(st, ("capture", "framegen"))


def main() -> int:
    failures = []

    # 0. The premise: clicking the field suspends.
    st = _state()
    _capture_on(st)
    if not st.hotkeys.suspended:
        failures.append("clicking a hotkey field did not suspend the hotkeys - "
                        "the premise of this test is gone")

    # 1. Tray/taskbar close (the reported route).
    st = _state()
    _capture_on(st)
    st.tray_commands.put("settings")
    commands.drain_commands(st)
    if st.display.menu.visible:
        failures.append("the tray toggle left the menu open - nothing to test")
    if st.hotkeys.suspended:
        failures.append("closing the menu from the tray left the hotkeys "
                        "suspended: Num0-Num7 and Ctrl+Alt+Q stay dead for the "
                        "rest of the session")

    # 2. The taskbar route only ever SHOWS the menu (Windows sends several
    #    activation messages per click, so it must not toggle, #87). It
    #    therefore leaves an open menu open - and a field that is still waiting
    #    for a key must keep the keyboard. Pinned so a future "fix" cannot
    #    resume the hotkeys out from under a live capture field.
    st = _state()
    _capture_on(st)
    st.tray_commands.put("show_settings")
    commands.drain_commands(st)
    if not st.display.menu.visible:
        failures.append("the taskbar route closed an open menu - #87 is back")
    if not st.hotkeys.suspended:
        failures.append("the taskbar route resumed the hotkeys while the menu "
                        "is still open and a field is still waiting for a key")
    # And from a closed menu it opens without touching the controller at all.
    st = _state()
    st.tray_commands.put("show_settings")
    commands.drain_commands(st)
    if not st.display.menu.visible:
        failures.append("the taskbar route did not open the menu")
    if "resume" in st.hotkeys.log:
        failures.append("the taskbar route resumed a controller that was never "
                        "suspended")

    # 3. The collapse button (overlay_ui's own header) and the settings-page
    #    back button must not leave a suspended controller behind either.
    #
    #    Each route is driven ON ITS OWN: the old version called
    #    apply_menu_action(st, ("button", "close")) unconditionally after the
    #    route's own actions, and that call resumes the hotkeys by itself - so
    #    whatever the route did or did not emit, the assertion below saw a
    #    resumed controller (audit: WEAK). The routes that really do close the
    #    menu through a close action get that action from their own handler.
    for label, key in (("collapse", "min"), ("back from settings", "close"),
                       ("footer back", "back")):
        st = _state()
        _capture_on(st)
        st.display.menu.page = "settings"
        st.display.menu.capturing = "framegen"
        actions = st.display.menu._icon_click(key) if key in ("min", "close") \
            else st.display.menu._action_click(key)
        for action in actions:
            commands.apply_menu_action(st, action)
        # Close the menu the way THIS route closes it, without borrowing the
        # close button's own resume.
        st.display.menu.visible = False
        if st.hotkeys.suspended:
            # Only the close action may still be needed: if the route did not
            # emit one, the menu is closed and the keyboard is still held -
            # which is the bug this loop is about.
            emitted_close = any(a[0] == "button" and a[1] == "close"
                                for a in actions)
            if emitted_close:
                for action in actions:
                    commands.apply_menu_action(st, action)
            if st.hotkeys.suspended:
                failures.append(
                    f"closing via {label} left the hotkeys suspended "
                    f"(actions: {actions})")
        else:
            print(f"    {label}: hotkeys resumed by the route itself")

    # 4. The menu's capturing flag is cleared on the tray route, or the next
    #    open silently eats the first keydown as a remap.
    st = _state()
    _capture_on(st)
    st.tray_commands.put("settings")
    commands.drain_commands(st)
    if st.display.menu.capturing is not None:
        failures.append(f"the menu is still capturing "
                        f"({st.display.menu.capturing!r}) after a tray close - "
                        f"the next open swallows the first keydown")

    # 5. resume() with nothing suspended is harmless: the controller posts a
    #    message nobody acts on, so closing a menu that was never capturing
    #    must not raise.
    st = _state()
    st.display.menu.visible = True
    st.tray_commands.put("settings")
    try:
        commands.drain_commands(st)
    except Exception as exc:
        failures.append(f"closing a menu that never suspended the hotkeys "
                        f"raised: {exc!r}")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: every route out of the menu gives the keyboard back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
