"""No new frame from the Python capture is not a reason to spin a core.

With the capture in Python - capture_in_worker off, or a split-GPU pipeline
whose worker cannot open the display (#88) - a static desktop makes dxcam
answer "no new frame" (None) on almost every call. The loop then went
straight round again without resting, and because the frame counter does not
move while no frame is sent, its "every 30 frames" housekeeping ran on EVERY
pass: the HUD z-order query, the monitor watch, the verdict checks, the power
opt-out's two SetProcessInformation calls and the hook scan, thousands of
times a second, one core busy for a desktop doing nothing.

Run through main.main()'s real loop (tests/loop_harness.py) for one second
with a capture that never has a frame: the loop must rest between passes
(a bounded number of passes) and the housekeeping must keep its
twice-a-second cadence (power.apply_both a handful of times, not per pass).

Run:  runtime\\python.exe tests\\test_idle_grab_backoff.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_harness as H  # noqa: E402

SECONDS = 1.0


def main() -> int:
    failures = []
    started = {}

    def on_pass(st, n):
        if n == 1:
            st.capture.frame = lambda: None      # a static desktop
            started["t"] = time.monotonic()
        return time.monotonic() - started["t"] < SECONDS

    rc, st, log, info = H.run(on_pass, state={"want_dda": False},
                              max_passes=10 ** 7)
    took = time.monotonic() - started.get("t", time.monotonic())
    power = sum(1 for e in info["events"] if e[0] == "power")
    passes = info["passes"]
    print(f"    {took:.2f}s: {passes} passes, {st.capture.grabs if st else '?'} "
          f"grabs, power.apply_both x{power}")
    if rc != 0:
        failures.append(f"main() returned {rc}")
    if passes > 1000 * SECONDS:
        failures.append(f"the loop went round {passes} times in {took:.1f}s "
                        f"with no frame to process - a busy spin")
    if power > 2 * SECONDS + 2:
        failures.append(f"the housekeeping ran {power} times in {took:.1f}s "
                        f"(twice a second expected) - it runs on every pass "
                        f"while no frame comes")
    if power < 1:
        failures.append("the housekeeping never ran")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: with no frame to grab the loop rests and keeps its "
          "housekeeping to twice a second")
    return 0


if __name__ == "__main__":
    sys.exit(main())
