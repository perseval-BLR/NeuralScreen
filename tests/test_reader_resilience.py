"""WorkerReader keeps its answers straight when the worker misbehaves or dies.

Driven through a real pipe, the way the worker writes, with no GPU:

* a dead worker is reported by EVERY wait after it, at once - the sentinel
  used to be taken by the first wait, and each later one sat out its whole
  timeout (a minute of frozen UI across a startup's four negotiations);
* a frame reply that arrives while a probe waits for its ack is kept for
  recv - it used to be dropped, recv then timed out and the worker was
  restarted (a hotkey during a frame);
* an ack that arrives after its wait gave up is not taken as the answer to
  the next command of the same kind - and when it lands while main waits for
  something else (another ack, or a frame), it still settles that debt: the
  next prompt ack of its kind is not swallowed as "the late one" (#148);
* a protocol error does not leave the worker blocked on a full pipe: the rest
  of the stream is drained, and the reader stops reporting the worker alive.

Run:  runtime\\python.exe tests\\test_reader_resilience.py
"""
import io
import os
import struct
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import protocol  # noqa: E402
from protocol import (DDA_ACK_FMT, DDA_ACK_MAGIC, MOTION_ACK_FMT,  # noqa: E402
                      MOTION_ACK_MAGIC, OUT_FMT, OUT_MAGIC, OUT_STATUS_OK,
                      WGC_ACK_FMT, WGC_ACK_MAGIC, WINDOW_ACK_FMT,
                      WINDOW_ACK_MAGIC, WorkerReader)


class _Pipe:
    """A stand-in for the worker: a real OS pipe we write replies into."""

    def __init__(self):
        self._r, self._w = os.pipe()
        self.stdout = os.fdopen(self._r, "rb")

    def write(self, data: bytes) -> None:
        os.write(self._w, data)

    def close(self) -> None:
        try:
            os.close(self._w)
        except OSError:
            pass


def _out(index: int, byte_count: int = 0) -> bytes:
    return struct.pack(OUT_FMT, OUT_MAGIC, index, OUT_STATUS_OK, byte_count, 1, 0)


def main() -> int:
    failures = []

    # 1. dead worker: every wait hears it, immediately
    class _Eof:
        def __init__(self):
            self.stdout = io.BytesIO(b"")
    reader = WorkerReader(_Eof(), 1, 1, None)
    time.sleep(0.2)
    for name, call in (("wait_mack", lambda: reader.wait_mack(3.0)),
                       ("wait_dack", lambda: reader.wait_dack(3.0)),
                       ("recv", lambda: reader.recv(0, 3.0))):
        t0 = time.monotonic()
        try:
            call()
            failures.append(f"{name} returned on a dead worker")
        except TimeoutError:
            failures.append(f"{name} sat out its timeout on a dead worker "
                            f"({time.monotonic() - t0:.1f}s)")
        except EOFError:
            if time.monotonic() - t0 > 1.0:
                failures.append(f"{name} heard the dead worker too late")
    if reader.alive:
        failures.append("a reader at EOF still says the worker is alive")

    # 2. a frame reply met by a probe is kept for recv
    pipe = _Pipe()
    reader = WorkerReader(pipe, 1, 1, None)
    try:
        pipe.write(_out(7))                                  # frame 7 in flight
        pipe.write(struct.pack(WGC_ACK_FMT, WGC_ACK_MAGIC, 1, 640, 480, 0))
        size = reader.wait_wgak(3.0)
        if tuple(size) != (640, 480):
            failures.append(f"the probe got {size}, not the window's size")
        try:
            reader.recv(7, 1.0)
        except TimeoutError:
            failures.append("the frame reply that arrived during the probe was "
                            "dropped - recv timed out on it")

        # 3. a late ack belongs to the wait that gave up on it
        try:
            reader.wait_mack(0.2)
            failures.append("wait_mack returned with no MACK sent")
        except TimeoutError:
            pass
        pipe.write(struct.pack(MOTION_ACK_FMT, MOTION_ACK_MAGIC, 1, 0, 0, 0))   # the late one
        time.sleep(0.1)
        try:
            reader.wait_mack(0.3)
            failures.append("the next MOTS took the late MACK of the one before "
                            "it as its own answer")
        except TimeoutError:
            pass                                             # correct: its own never came
        pipe.write(struct.pack(MOTION_ACK_FMT, MOTION_ACK_MAGIC, 1, 0, 0, 0))
        pipe.write(struct.pack(MOTION_ACK_FMT, MOTION_ACK_MAGIC, 1, 0, 0, 0))
        try:
            reader.wait_mack(1.0)                            # its own, after the orphan
        except Exception as exc:
            failures.append(f"a MACK after the orphan did not arrive: {exc!r}")

        # 3b. the late ack lands while main waits for a DIFFERENT ack (#148:
        # WNDO gave up during a slow NGX init, its WACK came during WGCW).
        wack = struct.pack(WINDOW_ACK_FMT, WINDOW_ACK_MAGIC, 1, 0, 0, 0)
        try:
            reader.wait_wack(0.2)
            failures.append("wait_wack returned with no WACK sent")
        except TimeoutError:
            pass
        pipe.write(wack)                                     # the late one
        pipe.write(struct.pack(WGC_ACK_FMT, WGC_ACK_MAGIC, 1, 640, 480, 0))
        reader.wait_wgak(3.0)
        pipe.write(wack)                                     # the next WNDO's own
        try:
            reader.wait_wack(1.0)
        except TimeoutError:
            failures.append("a late WACK dropped during another wait left its "
                            "debt behind - the next WNDO's prompt WACK was "
                            "swallowed and its wait timed out")

        # 3c. ...or while main waits for a frame.
        try:
            reader.wait_wack(0.2)
            failures.append("wait_wack returned with no WACK sent")
        except TimeoutError:
            pass
        pipe.write(wack)                                     # the late one
        pipe.write(_out(9))
        try:
            reader.recv(9, 3.0)
        except TimeoutError:
            failures.append("the frame behind a late WACK was not delivered")
        pipe.write(wack)
        try:
            reader.wait_wack(1.0)
        except TimeoutError:
            failures.append("a late WACK dropped by recv left its debt behind - "
                            "the next WNDO's prompt WACK was swallowed")
    finally:
        pipe.close()
        reader._thread.join(timeout=2.0)

    # 4. a protocol error drains the rest of the stream
    pipe = _Pipe()
    reader = WorkerReader(pipe, 64, 64, None)
    wrote = threading.Event()
    try:
        pipe.write(struct.pack("<I", 0x0BADF00D) + b"\0" * 20)   # an invalid magic
        time.sleep(0.2)
        if reader.alive:
            failures.append("a reader that met a protocol error still reports "
                            "the worker alive")
        # A frame-sized payload behind the error: the writer must not block on
        # it (it would never reach the EOF of its stdin and never exit).

        def _write_big():
            try:
                pipe.write(b"\xAB" * (4 * 1024 * 1024))
            finally:
                wrote.set()
        threading.Thread(target=_write_big, daemon=True).start()
        if not wrote.wait(5.0):
            failures.append("the worker's write blocked after a protocol error - "
                            "the rest of the stream is not drained")
        try:
            reader.wait_dack(1.0)
            failures.append("a wait after the protocol error returned")
        except (RuntimeError, EOFError):
            pass
        except TimeoutError:
            failures.append("a wait after the protocol error timed out instead "
                            "of reporting it")
    finally:
        # Only once the write has finished: closing a descriptor another
        # thread is blocked writing to waits for that write (the CRT locks
        # the descriptor), and the test would hang on exactly the failure it
        # exists to report.
        if wrote.is_set():
            pipe.close()
        reader._thread.join(timeout=2.0)

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a dead worker reaches every wait at once, frames met during a "
          "probe are kept, late acks stay with their command, and a protocol "
          "error drains the pipe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
