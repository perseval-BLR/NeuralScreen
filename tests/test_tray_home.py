"""With "Minimise to tray" on, the tray is the program's home (#171).

The setting used to apply to the taskbar button's minimise only. The "—" in
the menu's header looks like a window's minimise but only hid the menu, and
a menu opened from the tray (Num2, the tray icon) brought the taskbar button
back and left it there after Num2 or Esc closed the menu - the program ended
up on the taskbar although the user chose the tray.

Checked on the real command layer with a stand-in display, taskbar and tray:

1. setting on, in the tray: Num2 opens the menu and the button stays hidden;
   Num2 closes it and the program is still in the tray;
2. setting on, never minimised: closing the menu with the header's "—" / Esc
   (the close button) hides the button - to the tray;
3. setting off: opening from the tray brings the button back and closing
   leaves it, as before;
4. setting on but no tray icon alive: nothing goes to the tray (it would be
   a program with no way back);
5. the header's "x": to the tray with "Close to tray" on, otherwise quit.

Fails before the change: the button came back and stayed, and "—" ignored
the setting.

Run:  runtime\\python.exe tests\\test_tray_home.py
"""
import contextlib
import io
import os
import queue
import sys
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))


class Anything:
    """Every method the drain calls on the display exists and does nothing."""

    def __getattr__(self, name):
        return lambda *a, **k: True


class Menu(Anything):
    def __init__(self):
        self.visible = False
        self.capturing = None

    def toggle(self):
        self.visible = not self.visible
        return self.visible


def main() -> int:
    failures = []
    import commands
    import settings_io

    real_save = settings_io.save_menu_layout
    settings_io.save_menu_layout = lambda st: True
    real_payload = settings_io.menu_payload
    settings_io.menu_payload = lambda st: {}
    real_focus = commands._give_back_focus
    commands._give_back_focus = lambda st: None

    def state(on: bool, in_tray: bool, tray_alive: bool = True):
        shown = []
        display = Anything()
        display.menu = Menu()
        st = types.SimpleNamespace(
            cfg={"tray_on_minimise": on}, in_tray=in_tray, lang="en", running=True,
            frame_index=0, window_hwnd=None, tray_commands=queue.Queue(),
            display=display, hotkeys=Anything(),
            tray=types.SimpleNamespace(alive=lambda: tray_alive),
            taskbar=types.SimpleNamespace(set_visible=shown.append))
        return st, shown

    def run(st, *cmds):
        for c in cmds:
            st.tray_commands.put(c)
        with contextlib.redirect_stdout(io.StringIO()):
            commands.drain_commands(st)

    try:
        st, shown = state(on=True, in_tray=True)
        run(st, "settings")
        if True in shown or not st.in_tray or not st.display.menu.visible:
            failures.append(f"opening from the tray with the setting on: button {shown}, "
                            f"in_tray {st.in_tray}, menu {st.display.menu.visible}")
        run(st, "settings")
        if True in shown or not st.in_tray or st.display.menu.visible:
            failures.append(f"Num2 closing it left in_tray {st.in_tray}, button {shown}")

        st, shown = state(on=True, in_tray=False)
        run(st, "settings")
        with contextlib.redirect_stdout(io.StringIO()):
            commands.apply_menu_action(st, ("button", "close"))
        if shown != [False] or not st.in_tray:
            failures.append(f"the header's minimise with the setting on: button {shown}, "
                            f"in_tray {st.in_tray}")

        st, shown = state(on=False, in_tray=True)
        run(st, "settings", "settings")
        if shown != [True] or st.in_tray:
            failures.append(f"with the setting off the button did not come back to stay: "
                            f"{shown}, in_tray {st.in_tray}")

        st, shown = state(on=True, in_tray=False, tray_alive=False)
        run(st, "settings", "settings")
        if shown or st.in_tray:
            failures.append(f"with no tray icon the program went to the tray: {shown}")
        # 5. The header's "x" (#171): to the tray with "Close to tray" and a
        #    live tray icon; otherwise the program quits.
        for on, alive, to_tray, running in ((True, True, True, True),
                                            (True, False, False, False),
                                            (False, True, False, False)):
            st, shown = state(on=False, in_tray=False, tray_alive=alive)
            st.cfg["tray_on_close"] = on
            with contextlib.redirect_stdout(io.StringIO()):
                commands.apply_menu_action(st, ("button", "window_close"))
            queued = [] if st.tray_commands.empty() else [st.tray_commands.get_nowait()]
            if (queued == ["to_tray"]) is not to_tray or st.running is not running:
                failures.append(f"x with Close to tray {on}, tray alive {alive}: "
                                f"queued {queued}, running {st.running}")
    finally:
        settings_io.save_menu_layout = real_save
        settings_io.menu_payload = real_payload
        commands._give_back_focus = real_focus

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
