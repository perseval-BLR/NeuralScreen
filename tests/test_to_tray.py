r"""Minimise to tray and close to tray, and what they must not do (#93).

"When you minimize NeuralScreen, it remains in the Start menu as a
minimized window, goes to the tray, but doesn't disappear. Or there's no
option in the settings for 'minimize to tray' or 'close to tray'."

Three things have to hold, and each was a way to get this wrong.

1. THE POLICY IS ASKED, NOT ASSUMED. The window procedure runs on its own
   thread and must not read the config, so the two answers are pushed into
   it. With both off, minimise and close behave exactly as before - the 1x1
   window is never really minimised (its taskbar button would go with it)
   and close is ignored, so the button cannot be lost for the session.
2. THE PASS KEEPS RUNNING. The picture on screen is this program's output.
   A "minimise" that also stopped the neural pass would change what the
   user is looking at without saying so; going to the tray hides the panel
   and the button, and nothing else.
3. THE WAY BACK EXISTS. Asking for the menu from the tray restores the
   taskbar button first - a menu with no button in the taskbar is the state
   the ticket is complaining about.

The window procedure is driven directly with the messages Windows sends,
rather than through a real taskbar: the point is which command each
message produces under each setting, and that is the part that has to be
right.

Run:  runtime\python.exe tests\test_to_tray.py
"""
import os
import queue
import sys
import types
from pathlib import Path


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

WM_SYSCOMMAND = 0x0112
SC_MINIMIZE = 0xF020
SC_CLOSE = 0xF060


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def main() -> int:
    failures = []
    import taskbar as tb_mod

    # 1. The policy.
    cases = (
        (False, False, SC_MINIMIZE, ["show_settings"],
         "minimise with the setting off still just shows the menu"),
        (False, False, SC_CLOSE, [],
         "close with the setting off is still ignored - the button must not "
         "be lost for the session"),
        (True, False, SC_MINIMIZE, ["to_tray"],
         "minimise to tray"),
        (True, False, SC_CLOSE, [],
         "close is not minimise: its own setting governs it"),
        (False, True, SC_CLOSE, ["to_tray"],
         "close to tray"),
        (False, True, SC_MINIMIZE, ["show_settings"],
         "minimise is not close: its own setting governs it"),
    )
    for on_min, on_close, sc, want, why in cases:
        q = queue.Queue()
        tb = tb_mod.TaskbarWindow(q)
        tb.to_tray_on_minimise = on_min
        tb.to_tray_on_close = on_close
        tb._wnd_proc(0, WM_SYSCOMMAND, sc, 0)
        got = drain(q)
        if got != want:
            failures.append(f"{why}: got {got}, want {want}")

    # 2 and 3: what the command does to the program. commands.drain_tray is
    # driven with a stub that records what it was asked to do.
    import commands

    class FakeTaskbar:
        def __init__(self):
            self.visible = True
            self.to_tray_on_minimise = False
            self.to_tray_on_close = False

        def set_visible(self, visible):
            self.visible = bool(visible)

    class FakeTray:
        """The tray icon, and whether it is really there."""

        def __init__(self, alive=True):
            self._alive = bool(alive)

        def alive(self):
            return self._alive

    class FakeMenu:
        def __init__(self):
            self.visible = True
            self.state = {}
            # The remap field: set while the menu waits for a key.
            self.capturing = None

        def set_state(self, payload):
            self.state.update(payload or {})

        def toggle(self):
            self.visible = not self.visible
            return self.visible

    class FakeDisplay:
        def __init__(self):
            self.menu = FakeMenu()
            self.window_layer = None

        def set_window_layer(self, *rect):
            self.window_layer = tuple(rect)

        def set_menu_opaque(self, *a):
            pass

        def set_menu_input(self, *a):
            pass

        def apply_menu_mode_input(self):
            pass

        def refresh_colorkey(self):
            pass

        def raise_topmost(self):
            pass

        def reveal(self):
            pass

        def is_visible(self):
            return True

        def set_visible(self, *a):
            pass

        def follow_taskbar_desktop(self):
            pass

        def alert(self, *a):
            pass

        def draw_overlay(self, *a):
            pass

        def exit_switch_mode(self, *a):
            pass

        def set_hud_only(self, *a):
            pass

    st = types.SimpleNamespace(
        tray_commands=queue.Queue(), display=FakeDisplay(),
        taskbar=FakeTaskbar(), tray=FakeTray(), in_tray=False, paused=False,
        hotkeys=types.SimpleNamespace(resume=lambda: None,
                                      suspend=lambda: None),
        cfg={}, running=True, frame_index=0, menu_opened_at=0.0,
        # The show_settings path walks a little of the pipeline on its way to
        # the menu; these are the fields it reads, and nothing here is what
        # the test is about.
        window_hwnd=None, lang="en", paused_by_user=False,
    )
    # save_menu_layout writes a file; this test is about the tray, not the
    # config, so it is neutralised for the duration.
    real_save = commands.settings_io.save_menu_layout
    real_payload = commands.settings_io.menu_payload
    commands.settings_io.save_menu_layout = lambda _st: True
    commands.settings_io.menu_payload = lambda _st: {}
    try:
        # A remap field waiting for a key, and a captured window under the
        # menu: the two pieces of state the ordinary close puts back and this
        # path did not (audit 20.09).
        st.display.menu.capturing = "nr"
        st.tray_commands.put("to_tray")
        commands.drain_commands(st)
        if st.display.menu.capturing is not None:
            failures.append(
                "to_tray left the menu waiting for a key: the next open - "
                "hours later, from the tray - would swallow the first "
                "keydown as a remap")
        if st.display.menu.visible:
            failures.append("to_tray left the panel open")
        if st.taskbar.visible:
            failures.append("to_tray left the taskbar button visible - that "
                            "is the whole request")
        if not st.in_tray:
            failures.append("to_tray did not record that it is in the tray")
        if st.paused:
            failures.append(
                "to_tray paused the neural pass - going to the tray must not "
                "change what is on screen, only what is on the taskbar")
        if not st.running:
            failures.append("to_tray stopped the program")

        st.tray_commands.put("show_settings")
        commands.drain_commands(st)
        if not st.taskbar.visible:
            failures.append(
                "coming back from the tray left the button hidden - a menu "
                "with no taskbar button is the state #93 reports")
        if st.in_tray:
            failures.append("coming back from the tray did not clear the flag")
    finally:
        commands.settings_io.save_menu_layout = real_save
        commands.settings_io.menu_payload = real_payload

    # 4. In one-window mode the HUD layer is stretched to the whole monitor
    # while the menu is up. Going to the tray is a close, so it has to be put
    # back on the captured window - or the layer stays monitor-sized for as
    # long as the program sits in the tray.
    st_w = types.SimpleNamespace(
        tray_commands=queue.Queue(), display=FakeDisplay(),
        taskbar=FakeTaskbar(), tray=FakeTray(), in_tray=False, paused=False,
        hotkeys=types.SimpleNamespace(resume=lambda: None,
                                      suspend=lambda: None),
        cfg={}, running=True, frame_index=0, menu_opened_at=0.0,
        window_hwnd=0x1234, lang="en", paused_by_user=False,
    )
    real_save3 = commands.settings_io.save_menu_layout
    real_rect = commands.window_frame_rect
    commands.settings_io.save_menu_layout = lambda _st: True
    commands.window_frame_rect = lambda _hwnd: (10, 20, 300, 400)
    try:
        st_w.tray_commands.put("to_tray")
        commands.drain_commands(st_w)
        if st_w.display.window_layer != (10, 20, 300, 400):
            failures.append(
                "to_tray left the HUD layer where the open menu put it "
                f"({st_w.display.window_layer!r}) - in one-window mode that "
                "is the whole monitor, for as long as the program is away")
    finally:
        commands.settings_io.save_menu_layout = real_save3
        commands.window_frame_rect = real_rect

    # 5. No tray icon, no hiding. The icon runs in a daemon thread whose death
    # is silent; hiding the taskbar button on the strength of it would leave
    # Task Manager as the only way out.
    st_n = types.SimpleNamespace(
        tray_commands=queue.Queue(), display=FakeDisplay(),
        taskbar=FakeTaskbar(), tray=FakeTray(alive=False), in_tray=False,
        paused=False,
        hotkeys=types.SimpleNamespace(resume=lambda: None,
                                      suspend=lambda: None),
        cfg={}, running=True, frame_index=0, menu_opened_at=0.0,
        window_hwnd=None, lang="en", paused_by_user=False,
    )
    real_save4 = commands.settings_io.save_menu_layout
    commands.settings_io.save_menu_layout = lambda _st: True
    try:
        st_n.tray_commands.put("to_tray")
        commands.drain_commands(st_n)
        if not st_n.taskbar.visible:
            failures.append(
                "the button was hidden with no tray icon to come back from - "
                "the program would be reachable only through Task Manager")
        if st_n.in_tray:
            failures.append("a refused to_tray still recorded being in the tray")
        if not st_n.running:
            failures.append("a refused to_tray stopped the program")
    finally:
        commands.settings_io.save_menu_layout = real_save4

    # 6. The click itself. A switch arrives as ("toggle", name) - written as
    # its own `kind`, both of these fell into the generic toggle branch and
    # were dropped in silence, so the cells did not react at all (user,
    # 20.09). Driven through the same entry point the menu uses.
    import types as _types
    st2 = _types.SimpleNamespace(
        cfg={"tray_on_minimise": False, "tray_on_close": False},
        taskbar=FakeTaskbar(), display=FakeDisplay(), lang="en",
        tray_commands=queue.Queue(), running=True, frame_index=0,
    )
    real_save2 = commands.settings_io.save_menu_layout
    commands.settings_io.save_menu_layout = lambda _st: True
    try:
        for name in ("tray_on_minimise", "tray_on_close"):
            commands.apply_menu_action(st2, ("toggle", name))
            if st2.cfg.get(name) is not True:
                failures.append(
                    f"clicking {name} left the config at "
                    f"{st2.cfg.get(name)!r} - the switch does not react")
            commands.apply_menu_action(st2, ("toggle", name))
            if st2.cfg.get(name) is not False:
                failures.append(f"{name} does not switch back off")
        # And the policy reaches the window procedure, which cannot read the
        # config itself.
        commands.apply_menu_action(st2, ("toggle", "tray_on_close"))
        if not st2.taskbar.to_tray_on_close:
            failures.append(
                "the taskbar window was not told about tray_on_close - it "
                "runs on its own thread and only knows what it is pushed")
    finally:
        commands.settings_io.save_menu_layout = real_save2

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: each setting governs its own button, the tray keeps the pass "
          "running, and the way back restores the button")
    return 0


if __name__ == "__main__":
    sys.exit(main())
