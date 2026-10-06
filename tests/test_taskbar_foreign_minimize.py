"""A foreign window being minimised must not open our menu (issue #96).

Report (Saymoin): "I minimize and restore any other program's window, and
after that NeuralScreen unfolds by itself." Measured on the bench, the message
stream makes the two cases identical:

  * a real click on our taskbar button arrives as WM_NCACTIVATE(1);
  * the fallback activation Windows hands our 1x1 window right after ANOTHER
    window is minimised arrives as the SAME WM_NCACTIVATE(1),
    with the cursor over the taskbar and our window in the foreground.

The old guard saw `wparam == 1` + cursor over the taskbar + we are foreground
and opened the menu - for a window the user never touched. The state around
the message is what separates them: a real click leaves the previous
application alive, the fallback leaves it minimised (measured with a live
probe, _work/probe_taskbar_activation.py).

Checked here, with a REAL foreign window on its own thread and a real
SW_MINIMIZE:

* minimising a foreign window that owns the foreground emits NOTHING;
* an activation while the previous app is alive still opens the menu (the
  click path must not be lost while fixing the false one);
* the second click on our already-active button still works (issue #93:
  "залипает" - the second click was dead without this path);
* SC_MINIMIZE/SC_RESTORE from the taskbar button still open the menu.

Run:  runtime\\python.exe tests\\test_taskbar_foreign_minimize.py
"""
import ctypes
import ctypes.wintypes as wt
import os
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))  # the modules live in app/
import taskbar  # noqa: E402

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_ACTIVATE = taskbar.WM_ACTIVATE
WM_NCACTIVATE = taskbar.WM_NCACTIVATE
SW_MINIMIZE = 6
SW_RESTORE = 9


def _make_foreign(title: str) -> int:
    """A real top-level window on its own thread, with its own message pump.

    Without the pump ShowWindow(SW_MINIMIZE) on it blocks forever - measured;
    a separate thread also mirrors reality (the user's other programs run in
    other processes).
    """
    cls = "ProbeTestForeign" + str(abs(hash(title)) % 100000)
    ready = threading.Event()
    box: dict = {}

    def run() -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = taskbar.WNDCLASSW()
        proc = taskbar.WNDPROC(lambda h, m, w, l: taskbar.user32.DefWindowProcW(h, m, w, l))
        wc.lpfnWndProc = proc          # keep the reference alive
        wc.hInstance = hinst
        wc.lpszClassName = cls
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(
            0, cls, title,
            taskbar.WS_POPUP | taskbar.WS_VISIBLE | taskbar.WS_CAPTION
            | taskbar.WS_SYSMENU | taskbar.WS_MINIMIZEBOX,
            90, 90, 360, 200, None, None, hinst, None)
        user32.ShowWindow(hwnd, 5)
        box["hwnd"] = hwnd
        box["proc"] = proc
        ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.DestroyWindow(hwnd)

    threading.Thread(target=run, daemon=True, name="test-foreign").start()
    ready.wait(5.0)
    return box.get("hwnd", 0)


def _park_cursor_on_taskbar() -> bool:
    """Put the cursor where the user's repro has it (he just clicked there)."""
    tb = user32.FindWindowW("Shell_TrayWnd", None)
    if not tb:
        return False
    r = wt.RECT()
    if not user32.GetWindowRect(tb, ctypes.byref(r)):
        return False
    user32.SetCursorPos((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    return True


def _force_foreground(hwnd: int, timeout: float = 3.0) -> bool:
    """Make `hwnd` the foreground window, or report that Windows refused.

    Windows restricts SetForegroundWindow for a process that does not own the
    foreground. The ALT trick (a synthetic Alt press releases the restriction)
    is what the project's other tests use, and this retries until the state is
    really what the test needs - a test that silently proceeds with the wrong
    foreground window reports a false failure.
    """
    VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if user32.GetForegroundWindow() == hwnd:
            return True
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.15)
    return user32.GetForegroundWindow() == hwnd


def _drain(commands: queue.Queue) -> list:
    got = []
    while not commands.empty():
        got.append(commands.get_nowait())
    return got


#: The window procedure dedupes commands: one click can deliver both
#: WA_CLICKACTIVE and WA_ACTIVE, so a repeat within this window is dropped.
#: Wait past it (and drain) so each assertion sees only its own message.
#: Measured: the dedup is 0.5 s, so the wait below has a thin margin - but it
#: is NOT what the suite flake was: in a caught failure the guard had emitted
#: nothing at all (`last=0.000`), so nothing was there to dedupe.
EMIT_DEDUP_S = 0.55


def _arm(commands: queue.Queue) -> None:
    """Wait past the command dedup and drop what earlier steps emitted."""
    time.sleep(EMIT_DEDUP_S)
    _drain(commands)


def _park_before_send(failures: list, step: str) -> bool:
    """Re-assert the cursor over the taskbar right before a synthetic send.

    The guard reads the cursor AT THE MOMENT the message arrives, and the
    cursor is a shared resource: the user, another test or a foreground change
    can take it off the taskbar between the park at startup and the assertion
    seconds later. Measured (probe_cursor_away): moving it away mid-run fails
    steps 2 and 3 with `cursor_tb=False` - which reads as "the click path
    broke" while nothing in the product changed. That was the suite flake.

    Parking at the point of use shrinks the window from seconds to
    microseconds, and a park that cannot be made is REPORTED: a step whose
    precondition silently disappeared must not pass vacuously.
    """
    if _park_cursor_on_taskbar():
        return True
    failures.append(f"{step}: could not park the cursor over the taskbar - "
                    f"the activation check cannot be trusted")
    return False


def main() -> int:
    failures: list[str] = []
    commands: queue.Queue = queue.Queue()
    win = taskbar.TaskbarWindow(commands, "NeuralScreenTest96")
    win.start()
    deadline = time.monotonic() + 5.0
    while win.hwnd is None and time.monotonic() < deadline:
        time.sleep(0.05)
    hwnd = win.hwnd
    if not hwnd:
        print("FAIL: the taskbar window was not created")
        return 1
    print(f"taskbar hwnd: {hwnd}")

    foreign = _make_foreign("Foreign window for #96")
    if not foreign:
        print("FAIL: could not create the foreign test window")
        win.stop()
        return 1
    print(f"foreign hwnd: 0x{foreign:X}")
    time.sleep(0.3)
    if not _park_cursor_on_taskbar():
        print("note: no taskbar rect found - the cursor test may not engage")
    time.sleep(0.3)
    _drain(commands)                          # drop startup noise

    # --- 1. the reported bug: a foreign window is minimised ----------------
    # It owns the foreground; Windows then activates us as a fallback.
    print("\n[1] minimising a foreign window that owns the foreground")
    user32.ShowWindow(foreign, SW_RESTORE)
    time.sleep(0.3)
    # _force_foreground, not a bare SetForegroundWindow: Windows refuses the
    # bare call for a process that does not own the foreground. Measured on
    # this bench: the bare call left `foreign foreground=False`, so the window
    # was never the foreground one and minimising it produced no activation at
    # all - the step passed without ever reaching the bug it exists for.
    if not _force_foreground(foreign):
        failures.append("could not give the foreign window the foreground for "
                        "the minimise check (Windows refused)")
    time.sleep(0.5)
    print(f"    foreign foreground={user32.GetForegroundWindow() == foreign}, "
          f"minimised={bool(user32.IsIconic(foreign))}")
    user32.ShowWindow(foreign, SW_MINIMIZE)
    time.sleep(0.9)
    got = _drain(commands)
    print(f"    commands: {got}")
    if got:
        failures.append("minimising a foreign window opened the menu (issue "
                        f"#96): {got}")

    # --- 1b. the same fallback, arriving as WM_ACTIVATE ---------------------
    # Windows does not always pick the WM_NCACTIVATE form: a fallback
    # activation can arrive as WM_ACTIVATE(WA_ACTIVE) carrying the MINIMISED
    # window in lParam. That is the same false open by another message, and it
    # is what the lParam half of the guard exists for.
    print("\n[1b] the fallback arriving as WM_ACTIVATE with a minimised window")
    _arm(commands)
    user32.ShowWindow(foreign, SW_RESTORE)
    time.sleep(0.3)
    # Same as step 1: the bare call is refused, so the foreign window never
    # becomes the previous foreground one and the premise of this step is gone.
    if not _force_foreground(foreign):
        failures.append("could not give the foreign window the foreground for "
                        "the minimised-window check (Windows refused)")
    time.sleep(0.4)
    user32.ShowWindow(foreign, SW_MINIMIZE)
    time.sleep(0.5)
    # _force_foreground, not a bare SetForegroundWindow: Windows refuses the
    # latter for a process that does not own the foreground, and with our
    # window NOT in front this step is rejected by the foreground test before
    # it ever reaches the minimised-window condition it exists to check.
    # Measured, with that condition removed (mutation M1): caught once in 8
    # runs - the other 7 passed vacuously.
    ours_in_front = _force_foreground(hwnd)
    time.sleep(0.3)
    # Taking the foreground is a real activation and a real click path - it
    # can emit on its own. Drain it so the assertion below can only be
    # satisfied by the synthetic message this step sends. Without this the
    # step failed on the emit caused by its own setup (measured: the emit came
    # from taskbar.py:164, the WM_NCACTIVATE branch, at the moment our window
    # took the foreground - the synthetic WM_ACTIVATE that follows was
    # correctly rejected and there was nothing left to attribute it to).
    _arm(commands)
    # Park here too: the guard reads the cursor at the moment of the message.
    parked = _park_before_send(failures, "step 1b")
    over_tray = taskbar.TaskbarWindow._cursor_over_taskbar(win)
    print(f"    previous minimised={bool(user32.IsIconic(foreign))}, "
          f"ours foreground={user32.GetForegroundWindow() == hwnd}, "
          f"cursor over taskbar={over_tray}")
    if not ours_in_front:
        failures.append("could not take the foreground for the minimised-window "
                        "check (Windows refused) - the check cannot be trusted")
    elif parked:
        user32.SendMessageW(hwnd, WM_ACTIVATE, taskbar.WA_ACTIVE, foreign)
        time.sleep(0.5)
        got = _drain(commands)
        print(f"    commands: {got}")
        if got:
            failures.append("a WM_ACTIVATE carrying a minimised window opened the "
                            f"menu (issue #96, other message form): {got}")

    # --- 1c. the same fallback in the WM_NCACTIVATE form --------------------
    # Step 1b covers the WM_ACTIVATE branch; this is the other one. Measured:
    # with the minimised-window condition removed from the WM_NCACTIVATE branch
    # (the exact bug #96 reports), step 1 did not fail - on this bench Windows
    # delivered the real fallback as WM_ACTIVATE, so that branch is never
    # reached by a live minimise and needs to be driven directly. Both branches
    # carry the condition and either one regressing reopens the report.
    print("\n[1c] the fallback arriving as WM_NCACTIVATE with a minimised window")
    _arm(commands)
    # Self-contained, not "whatever 1b left behind": measured, the foreign
    # window was no longer the previous foreground one by this point (it can be
    # re-sampled or dropped between steps), and the step then failed on its own
    # precondition rather than on the guard. Give it the foreground, minimise
    # it, then let our window take the foreground - the same shape as 1b.
    if not _force_foreground(foreign):
        failures.append("could not give the foreign window the foreground for "
                        "the WM_NCACTIVATE check (Windows refused)")
    else:
        time.sleep(0.4)
        user32.ShowWindow(foreign, SW_MINIMIZE)
        time.sleep(0.5)
        _force_foreground(hwnd)
        time.sleep(0.3)
        prev_now = win._prev_minimised()
        print(f"    previous minimised={prev_now}, "
              f"ours foreground={user32.GetForegroundWindow() == hwnd}, "
              f"cursor over taskbar="
              f"{taskbar.TaskbarWindow._cursor_over_taskbar(win)}")
        if not prev_now:
            failures.append("the previous foreground window is not minimised "
                            "after the setup - the WM_NCACTIVATE check cannot "
                            "be trusted")
        elif _park_before_send(failures, "step 1c"):
            user32.SendMessageW(hwnd, WM_NCACTIVATE, 1, 0)
            time.sleep(0.5)
            got = _drain(commands)
            print(f"    commands: {got}")
            if got:
                failures.append("a WM_NCACTIVATE with the previous window "
                                f"minimised opened the menu (issue #96): {got}")

    # --- 2. a real click must still work -----------------------------------
    # Measured: a click activates OUR window (Windows makes us the foreground
    # as part of the click) and reports the previous app in lParam. Nothing
    # may be lost while fixing #96.
    print("\n[2] an activation while the previous app is alive (click path)")
    user32.ShowWindow(foreign, SW_RESTORE)
    time.sleep(0.4)
    if not _force_foreground(foreign):
        failures.append("could not give the foreign window the foreground - "
                        "the click-path check cannot be trusted")
    else:
        # Windows hands US the foreground as part of a click; reproduce that
        # state before sending the activation the click delivers.
        if not _force_foreground(hwnd):
            failures.append("could not take the foreground for the click-path "
                            "check (Windows refused)")
        time.sleep(0.3)
        # Taking the foreground may itself have emitted (the real activation
        # sequence); wait out the dedup and drop it, so the assertion below
        # can only be satisfied by the message this step sends.
        _arm(commands)
        # The cursor must be over the taskbar WHEN the message arrives: park it
        # again here, not only at startup (the suite flake - see _park_before_send).
        parked = _park_before_send(failures, "step 2")
        print(f"    ours foreground={user32.GetForegroundWindow() == hwnd}, "
              f"previous alive={bool(user32.IsWindow(foreign))} "
              f"minimised={bool(user32.IsIconic(foreign))}")
        if parked:
            user32.SendMessageW(hwnd, WM_ACTIVATE, taskbar.WA_ACTIVE, foreign)
            time.sleep(0.5)
            got = _drain(commands)
            print(f"    commands: {got}")
            if "show_settings" not in got:
                failures.append("an activation with the previous app alive no "
                                f"longer opens the menu: {got}")

    # --- 3. the second click on our already-active button ------------------
    # Issue #93: this arrives as WM_NCACTIVATE(1) with no WM_ACTIVATE before
    # it, while our window is ALREADY the active one (step 2 left it so). The
    # guard's new "were we already active?" test must not kill this path.
    print("\n[3] second click on our already-active button")
    if not _force_foreground(hwnd):
        failures.append("could not take the foreground for the second-click "
                        "check (Windows refused)")
    else:
        time.sleep(0.3)
        _arm(commands)          # drop anything taking the foreground emitted
        # Same as step 2: the cursor must be on the taskbar when the message
        # lands, and it may have been moved since the park at startup.
        parked = _park_before_send(failures, "step 3")
        print(f"    we are foreground={user32.GetForegroundWindow() == hwnd}")
        if parked:
            user32.SendMessageW(hwnd, WM_NCACTIVATE, 1, 0)
            time.sleep(0.5)
            got = _drain(commands)
            print(f"    commands: {got}")
            if "show_settings" not in got:
                failures.append("the second click on our active button was dropped "
                                f"(#93 regression): {got}")

    # --- 4. the taskbar button's own minimize/restore ----------------------
    print("\n[4] SC_MINIMIZE from the taskbar button")
    _arm(commands)
    user32.SendMessageW(hwnd, taskbar.WM_SYSCOMMAND, taskbar.SC_MINIMIZE, 0)
    time.sleep(0.4)
    got = _drain(commands)
    print(f"    commands: {got}")
    if "show_settings" not in got:
        failures.append(f"the taskbar button's own minimize no longer shows "
                        f"the menu: {got}")

    # --- 5. an activation with the cursor OFF the taskbar ------------------
    # Issue #93: switching programs also happens with the cursor over the
    # taskbar, and clicking ANOTHER app's icon activates that app. The cursor
    # condition is what separates those from a click on our own button, so
    # removing it must break this step. Measured: with the condition dropped
    # (mutation M2), the rest of the test still passed - nothing exercised it.
    print("\n[5] an activation while the cursor is OFF the taskbar")
    if not _force_foreground(hwnd):
        failures.append("could not take the foreground for the off-taskbar "
                        "check (Windows refused)")
    else:
        time.sleep(0.3)
        _arm(commands)
        # Park the cursor AWAY from the taskbar: same message, same foreground
        # window, only the cursor differs - which is the whole point.
        user32.SetCursorPos(600, 300)
        time.sleep(0.2)
        over_tray = taskbar.TaskbarWindow._cursor_over_taskbar(win)
        print(f"    cursor over taskbar={over_tray}, "
              f"foreground ours={user32.GetForegroundWindow() == hwnd}")
        if over_tray:
            failures.append("could not move the cursor off the taskbar - the "
                            "off-taskbar check cannot be trusted")
        else:
            user32.SendMessageW(hwnd, WM_ACTIVATE, taskbar.WA_ACTIVE, foreign)
            time.sleep(0.5)
            got = _drain(commands)
            print(f"    commands: {got}")
            if got:
                failures.append("an activation with the cursor off the taskbar "
                                f"opened the menu (#93 regression): {got}")
        _park_cursor_on_taskbar()

    # --- 5b. the same, in the WM_NCACTIVATE form ---------------------------
    # Step 5 drives the WM_ACTIVATE branch only; both branches carry the cursor
    # and foreground conditions, and measured, removing either one from the
    # WM_NCACTIVATE branch left the whole test green (mutations M6 and M7) -
    # nothing exercised that branch's negative cases, so a wrong fix there
    # would have shipped.
    print("\n[5b] WM_NCACTIVATE with the cursor OFF the taskbar")
    if not _force_foreground(hwnd):
        failures.append("could not take the foreground for the off-taskbar "
                        "WM_NCACTIVATE check (Windows refused)")
    else:
        time.sleep(0.3)
        _arm(commands)
        user32.SetCursorPos(600, 300)
        time.sleep(0.2)
        over_tray = taskbar.TaskbarWindow._cursor_over_taskbar(win)
        print(f"    cursor over taskbar={over_tray}, "
              f"foreground ours={user32.GetForegroundWindow() == hwnd}, "
              f"previous minimised={win._prev_minimised()}")
        if over_tray:
            failures.append("could not move the cursor off the taskbar - the "
                            "WM_NCACTIVATE cursor check cannot be trusted")
        else:
            user32.SendMessageW(hwnd, WM_NCACTIVATE, 1, 0)
            time.sleep(0.5)
            got = _drain(commands)
            print(f"    commands: {got}")
            if got:
                failures.append("a WM_NCACTIVATE with the cursor off the taskbar "
                                f"opened the menu (#93 regression): {got}")

    # --- 5c. WM_NCACTIVATE while ANOTHER app holds the foreground ----------
    # The reported #93 shape: the user clicks another app's taskbar icon, the
    # cursor IS over the taskbar, and the activation arrives as
    # WM_NCACTIVATE(1). The foreground test is the only thing rejecting it.
    print("\n[5c] WM_NCACTIVATE while another app holds the foreground")
    user32.ShowWindow(foreign, SW_RESTORE)
    time.sleep(0.3)
    if not _force_foreground(foreign):
        failures.append("could not give the foreign window the foreground - "
                        "the WM_NCACTIVATE foreground check cannot be trusted")
    else:
        time.sleep(0.3)
        _arm(commands)
        _park_before_send(failures, "step 5c")
        print(f"    foreground ours={user32.GetForegroundWindow() == hwnd}, "
              f"previous minimised={win._prev_minimised()}, "
              f"cursor over taskbar="
              f"{taskbar.TaskbarWindow._cursor_over_taskbar(win)}")
        user32.SendMessageW(hwnd, WM_NCACTIVATE, 1, 0)
        time.sleep(0.5)
        got = _drain(commands)
        print(f"    commands: {got}")
        if got:
            failures.append("a WM_NCACTIVATE while another app held the "
                            f"foreground opened the menu (#93): {got}")

    user32.DestroyWindow(foreign)
    win.stop()
    time.sleep(0.2)

    print("=" * 60)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: a minimised foreign window no longer opens the menu; a real "
          "click still does")
    return 0


if __name__ == "__main__":
    sys.exit(main())
