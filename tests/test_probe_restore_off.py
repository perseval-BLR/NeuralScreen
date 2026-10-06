"""A window that cannot be captured, picked while NR is OFF, does not wake the idle worker's capture.

With NR OFF the worker stays alive but idle: its capture and its picture
window are closed (channels.suspend_for_off) and no frames flow. The menu
still works, and picking a window there probes it on that idle worker
(WGCW). When the probe is refused - or the window is too small to process -
switch_window puts "the previous source" back, and for a desktop pipeline
that meant DDA1 at the monitor's size: the idle worker went back to
capturing the desktop behind NR OFF, the state the low-cost OFF exists to
avoid.

What this pins, on the real switch_window and channels.probe_window_capture
with a fake worker that records every command written to it:

* NR OFF, a refused probe: the last capture command is a close, and no DDA1
  with a size is sent;
* NR OFF, a window too small to process: the same;
* NR ON, a refused probe: the desktop is still restored with DDA1 at the
  monitor's size, as before.

Run:  runtime\\python.exe tests\\test_probe_restore_off.py
"""
import io
import struct
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import pipeline  # noqa: E402
from protocol import DDA_FMT, DDA_MAGIC, WGC_FMT, WGC_MAGIC  # noqa: E402

HWND = 0x7FFF0001   # not a window: IsIconic says no, the fake answers for it


def _commands(raw: bytes) -> list:
    """The capture commands the worker received, in order."""
    out, pos = [], 0
    while pos < len(raw):
        magic = struct.unpack_from("<I", raw, pos)[0]
        if magic == WGC_MAGIC:
            _m, w, h, _f, _pts, hwnd = struct.unpack_from(WGC_FMT, raw, pos)
            out.append(("WGCW", hwnd))
            pos += struct.calcsize(WGC_FMT)
        elif magic == DDA_MAGIC:
            _m, w, h, _f, _pts = struct.unpack_from(DDA_FMT, raw, pos)
            out.append(("DDA1", w, h))
            pos += struct.calcsize(DDA_FMT)
        else:
            raise AssertionError(f"unexpected command 0x{magic:08X}")
    return out


class _Reader:
    def __init__(self, probe):
        self._probe = probe
        self.calls = 0

    def wait_wgak(self, timeout):
        self.calls += 1
        if self.calls == 1:
            if isinstance(self._probe, Exception):
                raise self._probe
            return self._probe
        return (0, 0)          # the close's answer

    def wait_dack(self, timeout):
        return None


def _run(off: bool, probe):
    st = types.SimpleNamespace(
        want_dda=True, window_hwnd=None, off_suspended=off,
        width=1920, height=1080, output_rgba=None, lang="en",
        worker=types.SimpleNamespace(stdin=io.BytesIO()),
        reader=_Reader(probe), follow_size=None,
        capture=types.SimpleNamespace(resolution=(1920, 1080)),
        display=types.SimpleNamespace(
            enter_switch_mode=lambda *a, **k: None,
            exit_switch_mode=lambda: None, alert=lambda *a, **k: None))
    saved = pipeline.window_frame_rect
    pipeline.window_frame_rect = lambda hwnd: None
    try:
        pipeline.switch_window(st, HWND)
    finally:
        pipeline.window_frame_rect = saved
    return _commands(st.worker.stdin.getvalue())


def main() -> int:
    failures = []
    for label, probe in (("a refused probe", RuntimeError("no capture")),
                         ("a window too small to process", (32, 32))):
        cmds = _run(off=True, probe=probe)
        print(f"    NR OFF, {label}: {cmds}")
        if any(c[0] == "DDA1" and c[1] for c in cmds):
            failures.append(f"NR OFF, {label}: the idle worker was told to "
                            f"capture the desktop again ({cmds})")
        if not cmds or cmds[-1] not in (("WGCW", 0), ("DDA1", 0, 0)):
            failures.append(f"NR OFF, {label}: the probe's capture was not "
                            f"closed afterwards ({cmds})")

    cmds = _run(off=False, probe=RuntimeError("no capture"))
    print(f"    NR ON, a refused probe: {cmds}")
    if ("DDA1", 1920, 1080) not in cmds:
        failures.append(f"NR ON: the desktop capture was not restored after a "
                        f"refused probe ({cmds})")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a refused window probe leaves an NR OFF worker's capture "
          "closed, and still restores the desktop with NR on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
