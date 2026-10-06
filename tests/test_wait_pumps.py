r"""The window keeps answering while the program waits on a worker (#135 class).

Starting, restarting and stopping a worker waits on the main thread: up to
45 s for the startup verdict (CACK), 20 s for RACK, 15 s per channel ack, 5 s
for a capture, a 2 s pause between two workers, 10 s and more for a worker to
exit. None of those waits pumped the window. Windows marks a window that has
not answered for 5 s as Not Responding, and #135 measured what follows: the
Save As dialog the window owns stops with it.

What this pins, with real processes on real pipes (a small Python script
stands in for nvngx.dll and takes a second over each step):

* an idle hook installed on this thread runs at least every 100 ms during
  start_worker's startup wait, shutdown_worker's wait for the exit and
  restart_worker's pause - five times and more for a one-second step;
* a hook installed by ANOTHER thread is never called here: a conversion
  starts its own workers on its own thread, and a window may only be pumped
  from the thread that owns it;
* main installs one that pumps the window (or draws the veil while it is
  up), and removes it on the way out - run through main.main()'s real loop
  (tests/loop_harness.py).

Run:  runtime\python.exe tests\test_wait_pumps.py
"""
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline  # noqa: E402
import protocol  # noqa: E402

HEADER = struct.calcsize(protocol.HEADER_FMT)
SHMI = struct.calcsize(protocol.SHM_FMT)

# A worker that takes a second to start and a second to exit.
WORKER = r'''
import struct, sys, time
src, dst = sys.stdin.buffer, sys.stdout.buffer
src.read({header})
src.read({shmi})
time.sleep(1.0)
dst.write(struct.pack("<4Iq", {cack}, 1, 1, 0, 0))
dst.write(struct.pack("<4Iq", {sack}, 1, 0, 0, 0))
dst.flush()
while src.read(65536):
    pass
time.sleep(1.0)
'''.format(header=HEADER, shmi=SHMI, cack=protocol.CREATE_ACK_MAGIC,
           sack=protocol.SHM_ACK_MAGIC)

PARAMS = {"style": 0, "auto_mask": 1, "intensity": 1.0, "local_tone": 0.0,
          "local_structure": 1.0, "skin_structure": -1.0}


class _Subprocess:
    """pipeline's subprocess module, with Popen starting the stand-in."""

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def Popen(self, args, **kwargs):
        kwargs.pop("cwd", None)
        return subprocess.Popen([sys.executable, "-c", WORKER], **kwargs)


def _shm():
    return SimpleNamespace(color_capacity=1024, motion_capacity=256,
                           name="NeuralScreen_test_pump", size=1280,
                           negotiated=False)


def _start():
    return pipeline.start_worker(PARAMS, 64, 36, 0, 0, 0, _shm())


def _lifecycle(failures: list) -> None:
    calls = []
    protocol.set_idle_hook(lambda: calls.append(time.monotonic()))
    try:
        t0 = time.monotonic()
        worker, _logs, _reader, stop = _start()
        took = time.monotonic() - t0
        n = len(calls)
        print(f"    start_worker {took:.1f}s, hook {n}x")
        if n < 5:
            failures.append(f"the window was let answer {n} times during a "
                            f"{took:.1f}s worker start")

        del calls[:]
        t0 = time.monotonic()
        worker, _logs, _reader, stop = pipeline.restart_worker(
            worker, PARAMS, 64, 36, 0, 0, 0, stop, _shm())
        took = time.monotonic() - t0
        n = len(calls)
        print(f"    restart_worker {took:.1f}s, hook {n}x")
        if n < 25:
            failures.append(f"the window was let answer {n} times during a "
                            f"{took:.1f}s worker restart (exit, the 2 s pause, "
                            f"start)")
        gaps = [b - a for a, b in zip(calls, calls[1:])]
        if gaps and max(gaps) > 0.5:
            failures.append(f"the window went {max(gaps):.2f}s without an "
                            f"answer during the restart")

        del calls[:]
        t0 = time.monotonic()
        pipeline.shutdown_worker(worker, stop)
        took = time.monotonic() - t0
        n = len(calls)
        print(f"    shutdown_worker {took:.1f}s, hook {n}x")
        if n < 5:
            failures.append(f"the window was let answer {n} times while a "
                            f"worker took {took:.1f}s to exit")
    finally:
        protocol.set_idle_hook(None)


def _other_thread(failures: list) -> None:
    calls = []
    t = threading.Thread(target=protocol.set_idle_hook,
                         args=(lambda: calls.append(1),))
    t.start()
    t.join()
    try:
        worker, _logs, _reader, stop = _start()
        pipeline.shutdown_worker(worker, stop)
    finally:
        protocol.set_idle_hook(None)
    if calls:
        failures.append(f"a hook installed by another thread was called "
                        f"{len(calls)} times from this one - a conversion's "
                        f"worker would pump the window off its thread")


def _main_installs(failures: list) -> None:
    import loop_harness as H
    seen = {}

    def on_pass(st, n):
        if n == 3:
            seen["hooked"] = protocol.has_idle_hook()
            for _ in range(5):
                protocol.idle_tick()
            st.display.switch_active = True
            protocol.idle_tick()
            st.display.switch_active = False
        return n < 5

    rc, st, log, info = H.run(on_pass)
    if rc != 0:
        failures.append(f"main() returned {rc}")
        return
    if not seen.get("hooked"):
        failures.append("main() runs its loop with no idle hook installed - "
                        "every wait on a worker leaves the window unpumped")
        return
    if info["pumps"] < 5:
        failures.append(f"main's idle hook pumped the window {info['pumps']} "
                        f"times for 5 calls")
    if "display.draw_overlay" not in st.display._log:
        failures.append("main's idle hook did not keep the veil animating "
                        "while it was up")
    if protocol.has_idle_hook():
        failures.append("main() left its idle hook installed after it returned")


def main() -> int:
    if not hasattr(protocol, "set_idle_hook"):
        print("FAIL: a wait on a worker has no way to let the window answer "
              "(no protocol.set_idle_hook)")
        return 1
    failures = []
    saved = (pipeline.subprocess, pipeline.WORKER_EXE)
    pipeline.subprocess = _Subprocess()
    pipeline.WORKER_EXE = Path(sys.executable)
    try:
        _lifecycle(failures)
        _other_thread(failures)
    finally:
        pipeline.subprocess, pipeline.WORKER_EXE = saved
    _main_installs(failures)

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the window answers while a worker starts, restarts and exits, "
          "only from its own thread, and main installs the hook")
    return 0


if __name__ == "__main__":
    sys.exit(main())
