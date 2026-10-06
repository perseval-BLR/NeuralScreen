r"""Shared memory is agreed even when the worker starts slowly (#148).

The report (Truandale, #148): on EVERY pipeline rebuild the log said

    shared memory unavailable (the worker did not acknowledge SHMI within 10s)

and the mapping then came up on its own seconds later. The worker reads
SHMI only from its command loop, after the D3D12 device, NVSDK_NGX_D3D12_Init
(5-19 s on that machine) and the first CreateFeature; the client started the
10 s SACK clock the moment it WROTE SHMI. A slow start was reported as a
refusal and every rebuild began in the degraded mode.

What this locks, through the real _negotiate_shm and WorkerReader over a real
pipe: a worker that answers CACK (its startup verdict) only after longer than
the SACK budget, then SACK at once, ends up negotiated - and a worker that
refuses SHMI after a normal start still is not.

Run:  runtime\python.exe tests\test_shm_startup.py
"""
import io
import os
import struct
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

from protocol import (CREATE_ACK_FMT, CREATE_ACK_MAGIC,  # noqa: E402
                      SHM_ACK_FMT, SHM_ACK_MAGIC, WorkerReader, _negotiate_shm)


class _Worker:
    """stdin collects what the client writes; stdout is a real OS pipe."""

    def __init__(self):
        self._r, self._w = os.pipe()
        self.stdout = os.fdopen(self._r, "rb")
        self.stdin = io.BytesIO()

    def write(self, data: bytes) -> None:
        os.write(self._w, data)

    def close(self) -> None:
        try:
            os.close(self._w)
        except OSError:
            pass


def _shm():
    return SimpleNamespace(color_capacity=1024, motion_capacity=256,
                           name="NeuralScreen_test_shm", size=1280,
                           negotiated=False)


def _run(start_delay: float, sack_ok: int) -> bool:
    worker = _Worker()
    reader = WorkerReader(worker, 64, 64, None)
    shm = _shm()

    def worker_side():
        time.sleep(start_delay)          # device + NGX init + CreateFeature
        worker.write(struct.pack(CREATE_ACK_FMT, CREATE_ACK_MAGIC, 1, 1, 0, 0))
        worker.write(struct.pack(SHM_ACK_FMT, SHM_ACK_MAGIC, sack_ok, 0, 0, 0))

    t = threading.Thread(target=worker_side, daemon=True)
    t.start()
    try:
        _negotiate_shm(worker, reader, shm, timeout=1.0)
    finally:
        t.join(timeout=5.0)
        worker.close()
        reader._thread.join(timeout=2.0)
    return bool(shm.negotiated)


def main() -> int:
    failures = []
    if not _run(start_delay=1.6, sack_ok=1):
        failures.append("a worker whose startup took longer than the SACK "
                        "timeout was reported as refusing shared memory - "
                        "every slow rebuild starts degraded")
    if _run(start_delay=0.1, sack_ok=0):
        failures.append("a worker that refused SHMI was taken as agreeing")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the SACK clock starts when the worker can answer, and a refusal "
          "is still a refusal (#148)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
