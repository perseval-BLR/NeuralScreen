"""Frame Generation backend choice: FSR on RTX 20/30, DLSS-G on RTX 40/50."""
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/
import gpuinfo
import protocol


def main():
    # Architecture groups as nvapi reports them (see gpuinfo.ARCH_NAMES).
    turing, ampere, ada, blackwell = ({"arch_group": g} for g in (0x160, 0x170, 0x190, 0x1B0))
    assert gpuinfo.fg_backend(turing) == "fsr"
    assert gpuinfo.fg_backend(ampere) == "fsr"
    assert gpuinfo.fg_backend(ada) == "dlss"
    assert gpuinfo.fg_backend(blackwell) == "dlss"
    # Unknown card, empty probe, junk: the old behaviour (DLSS-G).
    assert gpuinfo.fg_backend({}) == "dlss"
    assert gpuinfo.fg_backend(None) == "dlss"
    assert gpuinfo.fg_backend({"arch_group": "x" and 0}) == "dlss"
    # The config override wins; junk values mean auto.
    assert gpuinfo.fg_backend(ada, "fsr") == "fsr"
    assert gpuinfo.fg_backend(turing, "dlss") == "dlss"
    assert gpuinfo.fg_backend(turing, "nonsense") == "fsr"
    assert gpuinfo.fg_backend(turing, None) == "fsr"

    # The wire format: bit 0x40 rides next to the FG enable bits and nothing
    # else of the header changes.
    assert protocol.FRAME_FLAG_FG_FSR == 0x40
    taken = (protocol.FRAME_FLAG_SHM | protocol.FRAME_FLAG_WANT_PIXELS | protocol.FRAME_FLAG_MOTION_SMALL
             | protocol.FRAME_FLAG_NO_COLOR | protocol.FRAME_FLAG_BYPASS | protocol.FRAME_FLAG_SPLIT
             | protocol.FRAME_FLAG_PREPARED | protocol.FRAME_FLAG_EARLY_REPLY
             | protocol.FRAME_FLAG_WORKER_SCENE | protocol.FRAME_FLAG_FG_MULT_HIGH
             | 0x100 | 0x200 | 0x400 | 0x800)
    assert not (protocol.FRAME_FLAG_FG_FSR & taken), "flag 0x40 collides with another flag"

    import io
    import numpy as np

    def sent_flags(**kw):
        buf = io.BytesIO()
        worker = type("W", (), {"stdin": buf})()
        protocol.send_frame(worker, 5, np.zeros((2, 2, 4), np.uint8), np.zeros((2, 2, 2), np.float16),
                            False, 0, None, no_color=True, **kw)
        magic, index, reset, flags, pts = struct.unpack(protocol.FRAME_FMT, buf.getvalue()[:struct.calcsize(protocol.FRAME_FMT)])
        return flags

    base = sent_flags(frame_generation=True, frame_multiplier=2)
    fsr = sent_flags(frame_generation=True, frame_multiplier=2, fg_fsr=True)
    assert fsr == base | 0x40
    assert sent_flags(frame_generation=True, frame_multiplier=4, fg_fsr=False) & 0x40 == 0
    # No FG header at all (older call sites): the FSR bit is not sent either.
    assert sent_flags() & 0x40 == 0
    print("ok")


if __name__ == "__main__":
    main()
