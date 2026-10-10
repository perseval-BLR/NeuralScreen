"""A wipe position that rounds to zero does not turn the wipe on.

The before/after wipe rides in the frame header: a SPLIT flag and the position
in the top 16 bits. A slider value above 0 but below half a step (1/65535) set
the flag with position 0. In SDR the worker skipped it, but the HDR composite
drew its 2 px divider at the left edge, and the changing split state kept
Frame Generation resetting on every frame.

Checked on the header send_frame writes, with a stand-in for the worker's
stdin: a tiny position sends no flag and no position; real positions still do.

Run:  runtime\python.exe tests\test_split_flag.py
"""
import io
import struct
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import numpy as np  # noqa: E402

import protocol  # noqa: E402


def _flags(split: float) -> int:
    pipe = io.BytesIO()
    worker = types.SimpleNamespace(stdin=pipe)
    motion = np.zeros((4, 4, 2), np.float16)
    protocol.send_frame(worker, 0, None, motion, False, 0, no_color=True, split=split)
    _magic, _index, _reset, flags, _pts = struct.unpack(
        protocol.FRAME_FMT, pipe.getvalue()[:struct.calcsize(protocol.FRAME_FMT)])
    return flags


def main() -> int:
    failures = []
    for split in (1e-6, 0.5 / 0xFFFF - 1e-9):
        flags = _flags(split)
        print(f"    split {split:.3g}: flags 0x{flags:08X}")
        if flags & protocol.FRAME_FLAG_SPLIT or flags >> 16:
            failures.append(f"split {split!r} turned the wipe on (0x{flags:08X})")
    for split, frac in ((0.5, 0x8000), (1.0, 0xFFFF), (1.0 / 0xFFFF, 1)):
        flags = _flags(split)
        if not flags & protocol.FRAME_FLAG_SPLIT or flags >> 16 != frac:
            failures.append(f"split {split!r} sent 0x{flags:08X}, expected position {frac}")
    if _flags(0.0) & protocol.FRAME_FLAG_SPLIT:
        failures.append("split 0 turned the wipe on")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: only a position the worker can draw turns the wipe on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
