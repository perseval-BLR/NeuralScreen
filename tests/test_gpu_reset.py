"""After a driver reset under NR: wait for the driver, stop after one (#181).

An RTX 3090 user saw the device removed (0x887A0007) 12 s to 48 min into NR;
NeuralScreen restarted the worker within two seconds, while the driver was
still recovering (D3D12CreateDevice 0x887A0004), the auto-revive tried again
30 s later, and three times the PC froze on a black screen.

Checked without a GPU fault:

1. the worker's own reset lines are recognised, and ordinary failures are not;
2. the rule: an official card always gets its restart; a pre-Blackwell card
   one per session, the second reset leaves NR off;
3. the wait polls until the driver answers, keeps the window pumped, and gives
   up after its timeout; the real probe answers on this machine;
4. main's handler: the first reset on a pre-Blackwell card waits and lets the
   restart go on; the second stands down - NR off, one notice, no
   auto-revive, the lock set.

Fails before the change: there was no wait and no limit.

Run:  runtime\\python.exe tests\\test_gpu_reset.py
"""
import contextlib
import io
import os
import sys
import types

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))

RESET_LOG = [
    "16:47:20.358  [host] queue Signal failed 0x887A0005",
    "16:47:20.358  [failure] stage=queue-signal kind=device-removed code=0x887A0007",
    "16:47:20.358  [host] device removed at queue-signal: reason 0x887A0007",
]
ORDINARY_LOG = ["12:00:00.000  [main] worker silent/dead on frame 7 (timeout)",
                "12:00:00.001  [video] NGX shutdown"]


def main() -> int:
    failures = []
    import gpu_recovery as gr

    # 1. Recognition.
    if not gr.device_was_reset(RESET_LOG) or gr.device_was_reset(ORDINARY_LOG):
        failures.append("the reset lines are not told apart from an ordinary failure")

    # 2. The rule.
    got = [gr.stand_down_for_session(True, n) for n in (1, 2, 5)] + \
          [gr.stand_down_for_session(False, n) for n in (1, 2)]
    if got != [False, False, False, False, True]:
        failures.append(f"the stand-down rule gave {got}")

    # 3. The wait.
    now = [0.0]
    answers = iter([False, False, False, True])
    pumped = []
    took = gr.wait_for_driver(10.0, probe=lambda: next(answers), pump=lambda: pumped.append(1),
                              clock=lambda: now[0],
                              sleep=lambda s: now.__setitem__(0, now[0] + s), step=0.5)
    if took != 1.5 or len(pumped) != 3:
        failures.append(f"the wait took {took} with {len(pumped)} pumps (expected 1.5, 3)")
    now[0] = 0.0
    if gr.wait_for_driver(2.0, probe=lambda: False, clock=lambda: now[0],
                          sleep=lambda s: now.__setitem__(0, now[0] + s)) is not None:
        failures.append("the wait did not give up after its timeout")
    if gr.driver_ready() is not True:
        failures.append("the D3D12 support check does not answer on this machine")

    # 4. main's handler.
    import main as app
    waits = []
    real_wait = gr.wait_for_driver
    gr.wait_for_driver = lambda **kw: waits.append(1) or 0.4
    alerts = []
    display = types.SimpleNamespace(
        exit_switch_mode=lambda: None, set_visible=lambda v: None,
        alert=lambda text, duration=0: alerts.append(text),
        is_switch_active=lambda: False)
    st = types.SimpleNamespace(gpu_official=False, lang="en", worker=None, worker_stop=None,
                               display=display, tray=types.SimpleNamespace(_set_state=lambda **k: None),
                               next_auto_revive=99.0, paused=False, worker_failed=False,
                               consecutive_restarts=2, work_frame=object())
    real_shutdown = app.shutdown_worker
    app.shutdown_worker = lambda *a: None
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            first = app._after_device_reset(st)
            second = app._after_device_reset(st)
    finally:
        gr.wait_for_driver = real_wait
        app.shutdown_worker = real_shutdown
    if first is not False or waits != [1]:
        failures.append(f"the first reset: stand-down {first}, waits {waits}")
    if second is not True or not st.device_reset_lock or not st.paused \
            or st.next_auto_revive != 0.0 or len(alerts) != 1:
        failures.append(f"the second reset: stand-down {second}, lock "
                        f"{getattr(st, 'device_reset_lock', None)}, revive {st.next_auto_revive}, "
                        f"alerts {len(alerts)}")
    official = types.SimpleNamespace(**dict(vars(st), gpu_official=True, device_resets=5,
                                            device_reset_lock=False))
    gr.wait_for_driver = lambda **kw: 0.1
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            if app._after_device_reset(official) is not False:
                failures.append("an official card was stood down after a reset")
    finally:
        gr.wait_for_driver = real_wait

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
