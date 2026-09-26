"""The worker's NGX result is not a boolean: 0x00000000 means "no frame
this call" and must NOT crash the pipeline.

The regression: any ngx_result != 1 raised RuntimeError, the worker was
restarted three times and NR was turned off. On a laptop on the iGPU the
network skips evaluations (0x00000000) - the exact pattern from issue #11
(kortul, 4060 Laptop). Only the 0xBAD00000 family (NVSDK_NGX_FAILED) is
a real error.

Checked: ngx_result=0 passes pixels through and reports real work; a real
failure (0xBAD00001) raises; a missing OK bit raises; a normal frame passes
through; and the worker's scene score (FRAME_FLAG_WORKER_SCENE frames)
reaches the caller - the value in the high 16 bits and the cut bit - while
a reply without it reports none.
"""
import os
import queue
import struct
import sys
import threading
import time

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))  # the modules live in app/
from main import (CREATE_ACK_FMT, CREATE_ACK_MAGIC,
                  CREATE_CATEGORY_UNSUPPORTED, OUT_FMT, OUT_MAGIC,
                  OUT_STATUS_OK, WorkerReader)  # noqa: E402
from protocol import OUT_STATUS_SCENE, OUT_STATUS_SCENE_CUT  # noqa: E402


class FakeWorker:
    """A stand-in for the worker process: a pipe we write OUT headers into."""

    def __init__(self):
        self._r, self._w = os.pipe()
        self.stdout = os.fdopen(self._r, "rb")

    def send_out(self, index: int, ok: int, byte_count: int,
                 ngx_result: int, pts: int = 0, payload: bytes = b"") -> None:
        os.write(self._w, struct.pack(OUT_FMT, OUT_MAGIC, index, ok,
                                      byte_count, ngx_result, pts))
        if payload:
            os.write(self._w, payload)

    def send_create_ack(self, ok: int, ngx_result: int, category: int) -> None:
        os.write(self._w, struct.pack(
            CREATE_ACK_FMT, CREATE_ACK_MAGIC, ok, ngx_result, category, 0))

    def close(self) -> None:
        try:
            os.close(self._w)
        except OSError:
            pass


def main() -> int:
    failures = []
    fake = FakeWorker()
    # 1x1: the reader expects byte_count == width*height*4, so a tiny
    # frame keeps the test fast.
    reader = WorkerReader(fake, 1, 1, shm=None)
    try:
        # 0. The create verdict is an explicit packet, independent of the
        #    first output frame (SAFE PASSTHROUGH also returns RGBA).
        fake.send_create_ack(0, 0xBAD00001, CREATE_CATEGORY_UNSUPPORTED)
        if reader.wait_create_ack(5.0) != (
                0, 0xBAD00001, CREATE_CATEGORY_UNSUPPORTED):
            failures.append("CACK: explicit create verdict was not preserved")

        # 1. A normal frame: result 1, pixels inline.
        fake.send_out(1, 1, 4, 1, payload=b"\xAB" * 4)
        got = reader.recv(1, 5.0)
        if not isinstance(got, np.ndarray) or got.tobytes() != b"\xAB" * 4:
            failures.append(f"normal frame: expected 4 bytes, got {got!r}")

        # 2. ngx_result=0 is not an error marker. If pixels exist, pass them
        #    through and report real work.
        fake.send_out(3, OUT_STATUS_OK, 4, 0x00000000, payload=b"\xCD" * 4)
        got = reader.recv(3, 5.0)
        if not isinstance(got, np.ndarray) or got.tobytes() != b"\xCD" * 4 \
                or reader.last_ngx_result != 0:
            failures.append(
                f"zero-result with pixels: expected bytes, got {got!r}")

        # 3b. The worker's scene score: 0.31 in the high 16 bits, and the cut
        #     it caused. Then a plain reply, which must not keep the old score.
        score = round(0.31 * 65535)
        fake.send_out(30, OUT_STATUS_OK | OUT_STATUS_SCENE | OUT_STATUS_SCENE_CUT
                      | (score << 16), 0, 1)
        reader.recv(30, 5.0)
        if reader.last_scene is None or abs(reader.last_scene - 0.31) > 1e-4                 or not reader.last_scene_cut:
            failures.append(f"scene: expected 0.31 and a cut, got "
                            f"{reader.last_scene!r} cut={reader.last_scene_cut}")
        fake.send_out(31, OUT_STATUS_OK | OUT_STATUS_SCENE | (7 << 16), 0, 1)
        reader.recv(31, 5.0)
        if reader.last_scene is None or reader.last_scene > 0.001 or reader.last_scene_cut:
            failures.append(f"scene: a small score came back as {reader.last_scene!r} "
                            f"cut={reader.last_scene_cut}")
        fake.send_out(32, OUT_STATUS_OK, 0, 1)
        reader.recv(32, 5.0)
        if reader.last_scene is not None or reader.last_scene_cut:
            failures.append("scene: a reply without the score still reported one")

        # 4. ok=0 - the worker itself failed - must surface as an error.
        #    (The reader thread dies on the first error, so each error
        #    case gets its own reader.)
        fake.send_out(4, 0, 0, 1)
        got = reader._queue.get(timeout=5.0)
        if got[0] is not None or not isinstance(got[1], RuntimeError) or \
                "status=0" not in str(got[1]):
            failures.append(f"status=0: expected a RuntimeError, got {got!r}")

        # 5. A real NGX failure: 0xBAD00001 - must surface as an error.
        #    Its own worker as well: a reader that met an error goes on
        #    draining its stream (so a worker mid-payload can still exit), and
        #    a second reader on the same pipe would be read out from under.
        fake2 = FakeWorker()
        reader2 = WorkerReader(fake2, 1, 1, shm=None)
        fake2.send_out(5, 1, 0, 0xBAD00001)
        got = reader2._queue.get(timeout=5.0)
        if got[0] is not None or not isinstance(got[1], RuntimeError) or \
                "0xBAD00001" not in str(got[1]):
            failures.append(f"0xBAD00001: expected a RuntimeError, got {got!r}")
        if reader2.alive:
            failures.append("a reader that met an error still reports the worker alive")
        fake2.close()
        reader2._thread.join(timeout=2.0)
    finally:
        fake.close()
        reader._thread.join(timeout=2.0)

    print("=" * 60)
    if failures:
        print(f"FAIL: {len(failures)} - {failures}")
        return 1
    print("OK: OUT1 passes normal frames and zero-result frames, raises on a "
          "real NGX failure, and carries the worker's scene score")
    return 0


if __name__ == "__main__":
    sys.exit(main())
