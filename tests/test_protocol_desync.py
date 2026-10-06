r"""A message the worker does not know is a desync, not a clean shutdown.

ReadVideoMessage returned 0 for an unknown magic, the same value as the end of
input, so a stream out of step with the client was logged as "input stream
closed", cleaned up and exited 0. In a report that reads as a normal stop -
the one thing a desync must not look like (pre-release audit).

Checked on the worker: a header with a magic nobody owns makes it say so,
naming the magic, and exit non-zero; closing stdin is still a clean exit 0.

Run:  runtime\\python.exe tests\\test_protocol_desync.py
"""
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))

from paths import WORKER_EXE  # noqa: E402

PARAMS = {"style": 1, "auto_mask": 0, "intensity": 1.0, "local_tone": 0.5,
          "local_structure": 1.0, "skin_structure": -1.0}


def _start():
    from pipeline import start_worker
    worker, logs, reader, stop = start_worker(PARAMS, 640, 360, 0, 0, 0, None)
    reader.wait_create_ack(60)
    return worker, logs, stop


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    from pipeline import shutdown_worker
    failures = []

    worker, logs, stop = _start()
    try:
        worker.stdin.write(struct.pack("<4Iq", 0xDEADBEEF, 0, 0, 0, 0))
        worker.stdin.flush()
        code = worker.wait(timeout=20)
    except Exception as exc:
        failures.append(f"the desync run raised {type(exc).__name__}: {exc}")
        code = None
    finally:
        shutdown_worker(worker, stop)
    text = "\n".join(logs)
    if code == 0:
        failures.append("an unknown message ended the worker with exit 0 - a desync "
                        "reads as a clean shutdown")
    if "0xDEADBEEF" not in text:
        failures.append("the log does not name the unknown message")
    if "input stream closed" in text:
        failures.append("the desync was logged as the input stream closing")

    worker, logs, stop = _start()
    try:
        worker.stdin.close()
        code = worker.wait(timeout=30)
    finally:
        shutdown_worker(worker, stop)
    if code != 0:
        failures.append(f"closing the input is no longer a clean exit ({code})")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: an unknown message is a named desync with a non-zero exit; EOF "
          "is still clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
