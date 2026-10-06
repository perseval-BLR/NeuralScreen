"""A worker start that fails inside the frame loop turns NR off - it does not close the program.

The frame loop starts workers in more places than its restart branches look
after: a queued settings apply that falls back to a full restart, a monitor or
window that changed under the pipeline, and the two restarts themselves.
Every one of them goes through require_pass and start_worker, and a raise from
either - a DLL dropped into native/libraries while the program runs changes
the compatibility key, and require_pass refuses every start after it - went
straight to main()'s last handler: the program closed.

What this runs is main.main()'s real loop (tests/loop_harness.py: fakes for
the worker, the window and the capture) with a start that raises on each of
those paths, and it checks that the loop goes on with the pipeline stood down:
worker_failed and NR OFF, the automatic revive armed, the veil down.

Run:  runtime\\python.exe tests\\test_lifecycle_containment.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_harness as H  # noqa: E402
import main as main_mod  # noqa: E402
import pipeline  # noqa: E402

TRIGGER = 6          # the pass the failure is provoked on
AFTER = 6            # how many passes the loop must survive it by


def _refuse(*a, **k):
    raise RuntimeError("production worker blocked: compatibility key changed")


def _case(label, arm, patch):
    """Run the loop, provoke the failure on pass TRIGGER, keep going."""
    def on_pass(st, n):
        if n == TRIGGER:
            arm(st)
        return n < TRIGGER + AFTER

    rc, st, log, info = H.run(on_pass, patch=patch)
    problems = []
    if rc != 0:
        problems.append(f"{label}: main() returned {rc} - the failure escaped "
                        f"the loop and closed the program")
    if info["passes"] < TRIGGER + AFTER:
        problems.append(f"{label}: the loop stopped on pass {info['passes']}")
    if st is not None and rc == 0:
        if not st.worker_failed or not st.paused:
            problems.append(f"{label}: the loop went on as if the worker were "
                            f"there (worker_failed={st.worker_failed}, "
                            f"paused={st.paused})")
        if not st.next_auto_revive:
            problems.append(f"{label}: no automatic revive was armed")
        if st.display.is_switch_active():
            problems.append(f"{label}: the veil was left over the desktop")
    if problems:
        print(log[-2000:])
    return problems


def main() -> int:
    failures = []

    # 1. A queued settings apply: RNSZ refused, the full restart refused too.
    def apply_patch(p, events):
        p.set(pipeline, "send_resize", lambda *a, **k: (_ for _ in ()).throw(
            TimeoutError("no RACK")))
        p.set(pipeline, "require_compatibility", _refuse)

    def apply_arm(st):
        st.pending_apply = (0.5, "Natural", dict(st.params), None)
        st.pending_apply_due = 0.0
        st.last_restart = 0.0
    failures += _case("a settings apply", apply_arm, apply_patch)

    # 2. The worker dies before a send, and its restart is refused.
    def send_patch(p, events):
        p.set(pipeline, "require_compatibility", _refuse)

    def send_arm(st):
        st.worker.code = 1
    failures += _case("a restart after a lost send", send_arm, send_patch)

    # 3. The worker goes away during recv, and the new process cannot start.
    def recv_patch(p, events):
        p.set(pipeline, "require_compatibility", lambda st: None)
        p.set(main_mod, "restart_worker", lambda *a, **k: (_ for _ in ()).throw(
            OSError("the worker could not be started")))

    def recv_arm(st):
        def gone(index, timeout):
            raise EOFError("the worker stopped")
        st.reader.answer = gone
    failures += _case("a restart after a silent worker", recv_arm, recv_patch)

    # 4. The monitor changed size and the rebuild for it is refused.
    def monitor_patch(p, events):
        def follow(st):
            if st.frame_index % 30 == 0 and getattr(st, "mon_resize", None) == "go":
                _refuse()
        p.set(pipeline, "follow_monitor", follow)

    def monitor_arm(st):
        st.mon_resize = "go"
        st.frame_index = 30
    failures += _case("a monitor rebuild", monitor_arm, monitor_patch)

    # 5. The captured window changed size and its rebuild is refused.
    def window_patch(p, events):
        def follow(st):
            if st.mon_resize == "go":
                _refuse()
        p.set(pipeline, "follow_window", follow)

    def window_arm(st):
        st.window_hwnd = 0x1234
        st.mon_resize = "go"
    failures += _case("a window rebuild", window_arm, window_patch)

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a worker start that fails inside the frame loop turns NR off, "
          "arms the revive and keeps the program running")
    return 0


if __name__ == "__main__":
    sys.exit(main())
