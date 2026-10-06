r"""A worker that never starts is a failed start, not a running worker without shared memory.

start_worker hands the shared section to a new worker and waits for two
answers: CACK, the verdict of the first CreateFeature (the worker is up), then
SACK (it opened the section). Both waits sat in one catch-all that called
every failure "shared memory unavailable - frames through the pipe". So a
worker hung in its init, or dead in it, came back from start_worker as a
running worker, and the first frame was then written down the pipe inline -
33 MB into a process that reads nothing, a write that never returns. The
audit probe (probe_cack.py) showed exactly that: no exception, and the 4K
send still blocked after 5 s.

What this pins, with real processes on real pipes (a small Python script
stands in for nvngx.dll, and the startup budget is cut to one second):

* a worker that reads its header and never answers makes start_worker
  RAISE within the budget, and the process is reaped;
* so does a worker that dies after the header;
* a refused SHMI after a good start is still not fatal - the worker comes
  back, with the section not negotiated;
* the compatibility preflight's start (no shared section) does not wait for
  anything and is unaffected.

Run:  runtime\python.exe tests\test_worker_startup_hang.py
"""
import struct
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import pipeline  # noqa: E402
import protocol  # noqa: E402

BUDGET = 1.0
HEADER = struct.calcsize(protocol.HEADER_FMT)
SHMI = struct.calcsize(protocol.SHM_FMT)

# The worker's side. It reads the header and SHMI, then behaves as told.
WORKER = r'''
import struct, sys, time
mode = sys.argv[1]
src, dst = sys.stdin.buffer, sys.stdout.buffer
src.read({header})
if mode == "dies":
    sys.exit(3)
if mode == "preflight":
    while src.read(65536):
        pass
    sys.exit(0)
src.read({shmi})
if mode == "refuses":
    dst.write(struct.pack("<4Iq", {cack}, 1, 1, 0, 0))
    dst.write(struct.pack("<4Iq", {sack}, 0, 0, 0, 0))
    dst.flush()
# "hangs" (and "refuses" afterwards): never another word; exit on stdin EOF.
while src.read(65536):
    pass
'''.format(header=HEADER, shmi=SHMI, cack=protocol.CREATE_ACK_MAGIC,
           sack=protocol.SHM_ACK_MAGIC)

PARAMS = {"style": 0, "auto_mask": 1, "intensity": 1.0, "local_tone": 0.0,
          "local_structure": 1.0, "skin_structure": -1.0}


class _Subprocess:
    """pipeline's subprocess module, with Popen starting the stand-in."""

    def __init__(self, mode: str, started: list):
        self._mode = mode
        self._started = started

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def Popen(self, args, **kwargs):
        kwargs.pop("cwd", None)
        proc = subprocess.Popen([sys.executable, "-c", WORKER, self._mode],
                                **kwargs)
        self._started.append(proc)
        return proc


def _shm():
    return SimpleNamespace(color_capacity=1024, motion_capacity=256,
                           name="NeuralScreen_test_hang", size=1280,
                           negotiated=False)


def _start(mode: str, shm):
    """start_worker against the stand-in; (result or exception, seconds, proc)."""
    started: list = []
    saved = (pipeline.subprocess, pipeline.WORKER_EXE,
             protocol.WORKER_STARTUP_TIMEOUT_S)
    pipeline.subprocess = _Subprocess(mode, started)
    pipeline.WORKER_EXE = Path(sys.executable)
    protocol.WORKER_STARTUP_TIMEOUT_S = BUDGET
    t0 = time.monotonic()
    try:
        try:
            result = pipeline.start_worker(PARAMS, 64, 36, 0, 0, 0, shm)
        except Exception as exc:
            result = exc
        return result, time.monotonic() - t0, started[0] if started else None
    finally:
        (pipeline.subprocess, pipeline.WORKER_EXE,
         protocol.WORKER_STARTUP_TIMEOUT_S) = saved


def main() -> int:
    failures = []

    for mode, what in (("hangs", "never answers after its header"),
                       ("dies", "dies after its header")):
        result, took, proc = _start(mode, _shm())
        if not isinstance(result, BaseException):
            failures.append(f"a worker that {what} came back from start_worker "
                            f"as a running worker - its first frame goes down "
                            f"a pipe nobody reads")
            pipeline.shutdown_worker(result[0], result[3])
        elif took > BUDGET + 5.0:
            failures.append(f"a worker that {what}: start_worker took "
                            f"{took:.1f}s with a {BUDGET:.0f}s budget")
        if proc is not None:
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            if proc.poll() is None:
                failures.append(f"a worker that {what} was left running")

    shm = _shm()
    result, took, proc = _start("refuses", shm)
    if isinstance(result, BaseException):
        failures.append(f"a refused SHMI after a good start became fatal: "
                        f"{result!r}")
    else:
        if shm.negotiated:
            failures.append("a refused SHMI was taken as agreed")
        pipeline.shutdown_worker(result[0], result[3])

    result, took, proc = _start("preflight", None)
    if isinstance(result, BaseException):
        failures.append(f"the preflight's start (no shared section) failed: "
                        f"{result!r}")
    else:
        if took > 2.0:
            failures.append(f"the preflight's start waited {took:.1f}s for "
                            f"answers it never asks for")
        pipeline.shutdown_worker(result[0], result[3])

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a worker that never starts makes start_worker raise and is "
          "reaped; a refused SHMI and the preflight start are unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
