"""A desktop capture refused "for now" is asked for again (#158).

At logon (and under a UAC prompt or the lock screen) Desktop Duplication is
refused with E_ACCESSDENIED: the input desktop is Winlogon's, not ours. The
client asked the worker for the capture once per worker, so one refusal at the
wrong moment kept the whole session on the slow path - GDI grabs and colour
through the pipe - until NR was switched off and on.

Checked through channels.enable_dda and channels.rearm_dda_if_due with the
worker's reply stubbed (nothing is launched):
* a refusal the worker logs as 0x80070005 schedules another attempt a few
  seconds later, and the loop's gate opens again once it is due;
* the next attempt succeeding clears the count;
* a refusal for any other reason (a card that drives no display, #88) is
  final, as before;
* the attempts stop at the limit.

Run:  runtime\python.exe tests\test_dda_retry_access_denied.py
"""
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import channels  # noqa: E402


def _state(answers, logs):
    """answers: per attempt, None for a DACK OK or the line the worker logs."""
    st = types.SimpleNamespace(
        worker=object(), width=1920, height=1080, window_hwnd=None,
        dda_mode=False, dda_attempted=False, dda_retry_at=0.0, dda_retries=0,
        gpu_switch_pending=False, gray_active=False, lang="en",
        worker_logs=logs, display=types.SimpleNamespace(alert=lambda *a, **k: None))
    queue = list(answers)

    def wait_dack(timeout):
        line = queue.pop(0)
        if line is not None:
            logs.append(line)
            raise RuntimeError("DACK failed")
    st.reader = types.SimpleNamespace(wait_dack=wait_dack)
    return st


def main() -> int:
    failures = []
    real_send, real_sync = channels.send_dda, channels.sync_gray
    channels.send_dda = lambda *a, **k: None
    channels.sync_gray = lambda st: None
    clock = [1000.0]
    real_time = channels.time.monotonic
    channels.time.monotonic = lambda: clock[0]
    try:
        denied = "21:51:18.000  [dda] DuplicateOutput failed 0x80070005"
        st = _state([denied, None], [])
        channels.enable_dda(st)
        print(f"    after E_ACCESSDENIED: mode={st.dda_mode}, retry in "
              f"{st.dda_retry_at - clock[0]:.0f} s, attempt {st.dda_retries}")
        if st.dda_mode or not st.dda_retry_at:
            failures.append("a refusal for now scheduled no retry")
        if channels.rearm_dda_if_due(st):
            failures.append("the retry opened the gate before it was due")
        clock[0] += channels.DDA_RETRY_DELAY + 0.1
        if not channels.rearm_dda_if_due(st) or st.dda_attempted:
            failures.append("the gate did not open once the retry was due")
        channels.enable_dda(st)
        if not st.dda_mode or st.dda_retries != 0 or st.dda_retry_at:
            failures.append(f"a retry that succeeded left mode={st.dda_mode}, "
                            f"retries={st.dda_retries}, due={st.dda_retry_at}")

        st = _state(["[dda] DuplicateOutput failed 0x887A0004"], [])
        channels.enable_dda(st)
        if st.dda_retry_at:
            failures.append("a final refusal (unsupported) was scheduled again")

        st = _state([denied] * (channels.DDA_RETRY_LIMIT + 2), [])
        attempts = 0
        for _ in range(channels.DDA_RETRY_LIMIT + 2):
            channels.enable_dda(st)
            attempts += 1
            clock[0] += channels.DDA_RETRY_DELAY + 0.1
            if not channels.rearm_dda_if_due(st):
                break
        print(f"    a desktop that never lets go: {attempts} attempts")
        if attempts != channels.DDA_RETRY_LIMIT + 1:
            failures.append(f"{attempts} attempts, expected the first plus "
                            f"{channels.DDA_RETRY_LIMIT} retries")
    finally:
        channels.send_dda, channels.sync_gray = real_send, real_sync
        channels.time.monotonic = real_time
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a capture refused for now is asked for again; a final refusal is not")
    return 0


if __name__ == "__main__":
    sys.exit(main())
