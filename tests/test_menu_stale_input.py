r"""Keys typed while the menu is closed are not replayed into the next open.

Events are read only while the menu is open; the closed paths pump() and
never read. A close turns the layer click-through but leaves it the
foreground window, so what the user types next - meant for the window under
the overlay - lands in our queue and stays there. The next open read it all
at once: a Space on the switch that still had the keyboard focus toggled NR,
an Esc closed the menu that had just been opened.

Checked here with the real Display (dummy SDL driver), the real OverlayMenu
and the real commands.drain_commands / apply_menu_action:

* focus the NR switch, close the menu from the command path, post KEYDOWN
  Space and Esc, pump the way the closed loop does, reopen from the command
  path and read the events the way the open loop does: no NR toggle is
  queued and the menu is still open;
* a close while our layer holds the foreground hands it back to the last
  window that was not ours, and leaves it alone when the user is already
  somewhere else.

Run:  runtime\python.exe tests\test_menu_stale_input.py
"""
import os
import queue
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


def _repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "main.py").is_file():
            return p
    return start


BASE = _repo_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

import commands  # noqa: E402
import winapi  # noqa: E402


class _Hotkeys:
    def suspend(self):
        pass

    def resume(self):
        pass


def _state(disp, cfg_path):
    st = SimpleNamespace(
        display=disp, hotkeys=_Hotkeys(), tray_commands=queue.Queue(),
        cfg={"theme": "light", "profile": "Natural", "hotkeys": {}},
        window_hwnd=None, mon_w=1920, mon_h=1080,
        work_scale=1.0, params={}, nr_small=True, lang="en", paused=False,
        split_pos=0.0, startup_menu=False, presets={}, cfg_path=cfg_path,
        width=1920, height=1080, work_w=1920, work_h=1080,
        gpu_text="RTX 5070 Ti", gpu_ok=True, worker_logs=[], gpu_alerted=False,
        fg_alerted=False, hdr_alerted=False, recorder=None,
        recording_finalizer=None, last_recording={}, compatibility_result=None,
        window_list=None, environment={}, screenshot_mode="ask", running=True,
        monitor=0, taskbar=None, last_foreground=0,
        capture=SimpleNamespace(devicename="\\\\.\\DISPLAY1"),
        hotkey_bindings={}, frame_index=0, pending_shot=None,
        next_auto_revive=0.0, worker_failed=False,
    )
    st.params = {"intensity": 1.0, "local_tone": 0.5,
                 "local_structure": 1.0, "skin_structure": -1.0}
    return st


def _toggle_menu(st):
    """The tray / Num2 route: the real command queue and its drain."""
    st.tray_commands.put("settings")
    commands.drain_commands(st)


def _open_loop_read(st):
    """What main's open-menu branch does with the queue, the actions applied."""
    for ev in pygame.event.get():
        for action in st.display.menu.handle_event(ev):
            commands.apply_menu_action(st, action)


def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


class _FakeUser32:
    """The foreground calls faked; everything else is the real user32."""

    def __init__(self, foreground):
        self.foreground = foreground
        self.set_to = []
        self._real = winapi._user32

    def __getattr__(self, name):
        return getattr(self.__dict__["_real"], name)

    def GetForegroundWindow(self):
        return self.foreground

    def IsWindow(self, hwnd):
        return True

    def SetForegroundWindow(self, hwnd):
        self.set_to.append(int(hwnd))
        self.foreground = hwnd
        return True


def main() -> int:
    failures = []
    pygame.init()
    import display as display_mod

    disp = display_mod.Display(1920, 1080, click_through=False)
    # The dummy driver has no window handle; the calls that only move the
    # real window around are stubbed. set_menu_input - where the open is
    # seen - and the menu itself stay real.
    for name in ("reveal", "raise_topmost", "follow_taskbar_desktop",
                 "draw_overlay", "refresh_colorkey"):
        setattr(disp, name, lambda *a, **k: None)
    disp.is_visible = lambda: True
    disp.set_visible = lambda *a: None
    tmp = tempfile.mkdtemp()
    st = _state(disp, Path(tmp) / "config.json")
    try:
        # 1. Open, put the keyboard focus on the NR switch (a mouse click
        #    does exactly this), close.
        _toggle_menu(st)
        if not disp.menu.visible:
            failures.append("the command path did not open the menu")
        disp.menu.layout(disp.width, disp.height)
        nr = next((i for i in disp.menu.items
                   if i.kind == "toggle" and i.key == "nr"), None)
        if nr is None:
            failures.append("no NR switch on the main page - nothing to focus")
            raise SystemExit
        disp.menu._set_focus(nr, from_mouse=True)
        _open_loop_read(st)
        _toggle_menu(st)
        if disp.menu.visible:
            failures.append("the command path did not close the menu")
        _drain(st.tray_commands)

        # 2. The user types into what they think is their own window. Our
        #    layer is still foreground, so the keys are ours; the closed loop
        #    only pumps.
        for key in (pygame.K_SPACE, pygame.K_ESCAPE):
            for kind in (pygame.KEYDOWN, pygame.KEYUP):
                pygame.event.post(pygame.event.Event(
                    kind, key=key, mod=0, unicode="", scancode=0))
        for _ in range(20):
            pygame.event.pump()

        # 3. Reopen through the real command path and read like the open
        #    loop does.
        _toggle_menu(st)
        _open_loop_read(st)
        queued = _drain(st.tray_commands)
        if "toggle" in queued:
            failures.append("a Space typed while the menu was closed toggled "
                            "NR on the next open")
        if not disp.menu.visible:
            failures.append("an Esc typed while the menu was closed closed "
                            "the menu on the next open")

        # 4. Keys typed while the menu IS open still reach it.
        pygame.event.post(pygame.event.Event(
            pygame.KEYDOWN, key=pygame.K_ESCAPE, mod=0, unicode="", scancode=0))
        _open_loop_read(st)
        if disp.menu.visible:
            failures.append("Esc pressed with the menu open no longer closes "
                            "it - the clear ate live input")

        # 5. The foreground goes back on close when it is ours, and only then.
        real = winapi._user32
        try:
            ours, theirs, other = 0x7701, 0x1_8000_1234, 0x5505
            disp.get_hwnd = lambda: ours
            st.last_foreground = theirs
            _toggle_menu(st)                     # open
            fake = _FakeUser32(foreground=ours)
            winapi._user32 = fake
            _toggle_menu(st)                     # close
            if fake.set_to != [theirs]:
                failures.append(f"closing the menu while our layer was "
                                f"foreground handed it to {fake.set_to}, "
                                f"expected [{theirs:#x}]")
            winapi._user32 = real
            _toggle_menu(st)                     # open
            fake = _FakeUser32(foreground=other)
            winapi._user32 = fake
            _toggle_menu(st)                     # close
            if fake.set_to:
                failures.append("closing the menu took the foreground from a "
                                "window the user had already gone to")
        finally:
            winapi._user32 = real
    except SystemExit:
        pass
    finally:
        try:
            disp.close()
        except Exception:
            pass
        pygame.quit()

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: input typed while the menu is closed is not replayed, and the "
          "foreground goes back on close")
    return 0


if __name__ == "__main__":
    sys.exit(main())
