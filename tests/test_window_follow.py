"""Window mode follows the focused window (#157, v2.6.0).

The reporter of #157 ran this daily on their machine: in window mode, a
window that keeps the focus becomes the capture source through the live
switch (#148 part 2), in under a second instead of a rebuild. Settings ->
Capture now has "Follow the focused window", off by default.

Checked without a GPU:

1. the timing: a window has to keep the focus 2.5 s; another window restarts
   the wait, the processed one or none clears it, and two switches are at
   least 8 s apart;
2. what is never followed: our own windows, hidden, minimised, LockApp,
   Explorer, dwm's ghosts, tool and cloaked windows, tiny ones, ones wider
   than their monitor, ones on another monitor - and an ordinary window is;
3. on this desktop, Windows' own shell window is not followable (the real
   window_info, not a fake);
4. the command layer: off - nothing; on - one switch after the dwell, none
   while recording, with the menu open or outside window mode; a window the
   capture refuses is tried once until the focus leaves it; a skipped window
   is logged once with its reason; a failure inside is shown once and stops
   the following instead of ending the program;
5. the switch is on the Capture tab, its command flips the config, and the
   key is validated as a boolean.

Fails before the change: there was no follower and no setting.

Run:  runtime\\python.exe tests\\test_window_follow.py
"""
import contextlib
import ctypes
import io
import os
import sys
import types

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))


def main() -> int:
    failures = []
    import window_follow as wf

    # 1. Timing.
    f = wf.FocusFollower()
    got = [f.update(t, 0xA, 0x1) for t in (0.0, 1.0, 2.4)]
    if any(got):
        failures.append(f"switched before the dwell: {got}")
    if f.update(2.6, 0xA, 0x1) != 0xA:
        failures.append("no switch after the focus stayed 2.5 s")
    f = wf.FocusFollower()
    f.update(0.0, 0xA, 0x1)
    f.update(2.0, 0xB, 0x1)                  # another window: the wait restarts
    if f.update(3.0, 0xB, 0x1) is not None or f.update(4.6, 0xB, 0x1) != 0xB:
        failures.append("another window did not restart the wait")
    f.update(5.0, 0xC, 0x1)
    if f.update(8.0, 0xC, 0x1) is not None:
        failures.append("two switches 3.4 s apart")
    if f.update(12.7, 0xC, 0x1) != 0xC:
        failures.append("no switch 8 s after the last one")
    f = wf.FocusFollower()
    f.update(0.0, 0xA, 0x1)
    f.update(1.5, 0x1, 0x1)                  # back to the processed window
    if f.update(3.0, 0xA, 0x1) is not None:
        failures.append("going back to the processed window did not clear the wait")

    # 2. Exclusions.
    mon = (0, 0, 2560, 1440)
    good = dict(hwnd=0x10, process="game.exe", cls="UnityWndClass",
                rect=(0, 0, 2560, 1440), monitor=mon)
    if wf.why_not(wf.WindowInfo(**good)) is not None:
        failures.append(f"an ordinary full-screen game is not followed: "
                        f"{wf.why_not(wf.WindowInfo(**good))}")
    for change, what in ((dict(ours=True), "ours"), (dict(visible=False), "hidden"),
                         (dict(iconic=True), "minimised"),
                         (dict(process="lockapp.exe"), "LockApp"),
                         (dict(process="explorer.exe"), "Explorer"),
                         (dict(process="dwm.exe", cls="Ghost"), "a ghost"),
                         (dict(cls="Ghost"), "a ghost by class"),
                         (dict(taskbar=False), "a tool or cloaked window"),
                         (dict(rect=(0, 0, 40, 300)), "a tiny window"),
                         (dict(rect=(0, 0, 7680, 1440)), "a window over three monitors"),
                         (dict(same_monitor=False), "a window on another monitor")):
        if wf.why_not(wf.WindowInfo(**dict(good, **change))) is None:
            failures.append(f"{what} would be followed")

    # 3. The real thing on this desktop: the shell's window is never taken.
    shell = int(ctypes.windll.user32.GetShellWindow() or 0)
    if shell:
        info = wf.window_info(shell, shell)
        print(f"    shell window: {info.process} {info.cls} -> {wf.why_not(info)}")
        if wf.why_not(info) is None:
            failures.append(f"the shell window ({info.process}, {info.cls}) is followable")

    # 4. The command layer, with the window queries and the switch stubbed.
    import pipeline
    import commands
    switched = []
    real = (wf.focused_window, wf.window_info, pipeline.switch_window,
            pipeline.live_switch_possible)
    live = {"ok": True}
    veils = []
    focus = {"hwnd": 0x20}
    wf.focused_window = lambda: focus["hwnd"]
    wf.window_info = lambda h, cur: wf.WindowInfo(
        **dict(good, hwnd=h, process="explorer.exe" if h == 0x30 else "game.exe"))
    pipeline.live_switch_possible = lambda st, h: live["ok"]

    def fake_switch(st, h, veil=True):
        switched.append(h)
        veils.append(veil)
        if h != 0x40:                 # 0x40: a window the capture refuses
            st.window_hwnd = h
    pipeline.switch_window = fake_switch

    class Worker:
        def poll(self):
            return None

    def state(**cfg):
        menu = types.SimpleNamespace(visible=False)
        return types.SimpleNamespace(
            cfg=dict(cfg), window_hwnd=0x1, worker=Worker(), dda_mode=True,
            recorder=None, display=types.SimpleNamespace(
                menu=menu, is_switch_active=lambda: False))

    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            st = state()
            for t in range(20):
                commands.update_window_follow(st, now=t * 0.25)
            if switched:
                failures.append("the follower switched with the setting off")
            st = state(window_follow=True)
            for t in range(20):
                commands.update_window_follow(st, now=t * 0.25)
            if switched != [0x20]:
                failures.append(f"with the setting on the switches were {switched}")
            if veils != [False]:
                failures.append(f"the follow switch asked for the veil: {veils}")
            # A window only a rebuild could take (the old one closed, another
            # monitor's worker state) is not followed at all.
            switched.clear()
            live["ok"] = False
            st = state(window_follow=True)
            for t in range(20):
                commands.update_window_follow(st, now=t * 0.25)
            live["ok"] = True
            if switched:
                failures.append("a window only a rebuild could take was followed")
            for name, change in (("recording", dict(recorder=object())),
                                 ("full screen", dict(window_hwnd=None))):
                switched.clear()
                st = state(window_follow=True)
                for k, v in change.items():
                    setattr(st, k, v)
                for t in range(20):
                    commands.update_window_follow(st, now=t * 0.25)
                if switched:
                    failures.append(f"switched while {name}")
            switched.clear()
            st = state(window_follow=True)
            st.display.menu.visible = True
            for t in range(20):
                commands.update_window_follow(st, now=t * 0.25)
            if switched:
                failures.append("switched with the menu open")
            # A window the capture refuses is tried once, not every 8 s -
            # until the focus leaves it and comes back.
            switched.clear()
            focus["hwnd"] = 0x40
            st = state(window_follow=True)
            for t in range(120):                     # 30 s on it
                commands.update_window_follow(st, now=t * 0.25)
            once = list(switched)
            focus["hwnd"] = 0x20
            commands.update_window_follow(st, now=30.25)
            focus["hwnd"] = 0x40
            for t in range(122, 160):
                commands.update_window_follow(st, now=t * 0.25)
            if once != [0x40] or switched != [0x40, 0x40]:
                failures.append(f"a refused window was tried {once}, then {switched}")
            switched.clear()
            focus["hwnd"] = 0x30
            st = state(window_follow=True)
            for t in range(20):
                commands.update_window_follow(st, now=t * 0.25)
            # A failure inside does not reach the main loop: shown once, and
            # the following stops for the session.
            calls = []

            def broken():
                calls.append(1)
                raise OSError("a Win32 query failed")
            wf.focused_window = broken
            alerts = []
            st = state(window_follow=True)
            st.lang = "en"
            st.display.alert = alerts.append
            with contextlib.redirect_stderr(io.StringIO()):
                for t in range(40, 60):
                    commands.update_window_follow(st, now=t * 0.25)
            if len(calls) != 1 or len(alerts) != 1:
                failures.append(f"a failing follower ran {len(calls)} times and "
                                f"alerted {len(alerts)} times")
    except Exception as exc:  # noqa: BLE001 - reported
        failures.append(f"the follower raised into the loop: {exc!r}")
    finally:
        (wf.focused_window, wf.window_info, pipeline.switch_window,
         pipeline.live_switch_possible) = real
    log = out.getvalue()
    if switched:
        failures.append(f"Explorer was switched to: {switched}")
    if log.count("not taking 0x30") != 1 or "explorer.exe is never followed" not in log:
        failures.append("the skipped Explorer window was not logged once with its reason")
    if "follow: 0x20 (game.exe" not in log:
        failures.append("the switch was not logged with the process")
    if "the live switch cannot take it" not in log:
        failures.append("a window the live switch cannot take was not logged")

    # The live-switch test itself, on real windows: the shell window stands
    # in for the processed one, the desktop window for the new one.
    shell = int(ctypes.windll.user32.GetShellWindow() or 0)
    other = int(ctypes.windll.user32.GetDesktopWindow() or 0)
    if shell and other:
        base = dict(window_hwnd=shell, dda_mode=True, recorder=None, off_suspended=False)
        for change, want in ((dict(), True), (dict(recorder=object()), False),
                             (dict(window_hwnd=None), False), (dict(dda_mode=False), False),
                             (dict(off_suspended=True), False),
                             (dict(window_hwnd=other), False),        # the same window
                             (dict(window_hwnd=0x7FFFFFF0), False)):  # the old one is gone
            got = pipeline.live_switch_possible(
                types.SimpleNamespace(**dict(base, **change)), other)
            if got is not want:
                failures.append(f"live_switch_possible with {change or 'nothing changed'} "
                                f"is {got}, not {want}")

    # 5. The menu, the command, the config.
    import pygame
    pygame.init()
    import fonts
    import overlay_ui as ui
    import settings_io
    menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
    menu.visible = True
    menu.page, menu.settings_tab = "settings", "capture"
    menu.set_state({"window_follow": False})
    menu.layout(1920, 1080)
    sw = next((i for i in menu.items if i.kind == "toggle" and i.key == "window_follow"), None)
    if sw is None or menu._activate_item(sw) != [("toggle", "window_follow")]:
        failures.append("the Capture tab has no follow switch")
    st = types.SimpleNamespace(cfg={}, display=types.SimpleNamespace(menu=menu))
    real_save = settings_io.save_menu_layout
    settings_io.save_menu_layout = lambda st: True
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            commands.apply_menu_action(st, ("toggle", "window_follow"))
    finally:
        settings_io.save_menu_layout = real_save
    if st.cfg.get("window_follow") is not True or menu.state.get("window_follow") is not True:
        failures.append(f"the command left cfg={st.cfg}")
    if "window_follow" not in settings_io._BOOL_KEYS:
        failures.append("window_follow is not validated as a boolean")
    pygame.quit()

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
