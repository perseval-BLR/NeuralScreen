"""A command handled while the loop waits for a frame does not use up the frame's watchdog.

While the loop waits for a frame reply (5 s at most - a worker silent for
longer is restarted) it handles hotkeys and tray commands in between, so they
keep working during a heavy frame. Some commands take seconds themselves: Num0
waits up to 8 s for the GPU recorder to answer. The deadline passed inside
that wait, and when the command returned the loop checked the clock, found
the 5 s gone and declared the worker silent - without one more look, although
the reply had arrived meanwhile. A healthy worker was restarted (a black
screen, and one of the three restarts that turn NR off) because the user
started a recording.

Run through main.main()'s real loop (tests/loop_harness.py) on a clock the
test controls: the reply arrives while a command takes 6 s, and the loop must
take it - no restart, no failure counted.

Run:  runtime\\python.exe tests\\test_recv_after_command.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_harness as H  # noqa: E402  (puts app/ on the path)
import main as main_mod  # noqa: E402
import pipeline  # noqa: E402

TRIGGER = 5


class _Clock:
    """main's time module, with a monotonic clock the test can move on."""

    def __init__(self):
        self.skew = 0.0

    def __getattr__(self, name):
        return getattr(time, name)

    def monotonic(self):
        return time.monotonic() + self.skew


def main() -> int:
    failures = []
    clock = _Clock()
    restarts = []
    state = {"phase": None, "ready": False, "frame": None, "got": False}

    def patch(p, events):
        p.set(main_mod, "time", clock)
        p.set(pipeline, "require_compatibility",
              lambda st: restarts.append("require"))
        p.set(main_mod, "restart_worker",
              lambda *a, **k: restarts.append("restart") or (
                  H.FakeWorker(), [], H.FakeReader("restarted"), None))

    def on_pass(st, n):
        if n == TRIGGER:
            first = st.reader

            def answer(index, timeout):
                if state["ready"]:
                    state["got"] = True
                    first.answer = lambda i, t: None
                    return None
                state["phase"] = "waiting"
                raise TimeoutError("not yet")
            first.answer = answer
        elif state["phase"] == "waiting":
            # A command (Num0 starting the GPU recorder) takes 6 s, and the
            # frame reply arrives while it runs.
            state["phase"] = "done"
            clock.skew += 6.0
            state["ready"] = True
        return n < TRIGGER + 8

    rc, st, log, info = H.run(on_pass, patch=patch)
    if rc != 0:
        failures.append(f"main() returned {rc}")
    elif state["phase"] != "done":
        failures.append("the scenario never reached the command inside the "
                        "frame wait - this test no longer covers the path")
    else:
        if restarts:
            failures.append(f"the worker was restarted ({restarts}) although "
                            f"its reply was waiting - the command's 6 s were "
                            f"counted as the worker's silence")
        if not state["got"]:
            failures.append("the frame reply that arrived during the command "
                            "was never taken")
        if st.worker_failed:
            failures.append("the worker was declared failed")
    if failures:
        print(log[-1500:])
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a command handled during the frame wait does not count as the "
          "worker's silence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
