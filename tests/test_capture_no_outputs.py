r"""No display output at all does not take the start down.

At logon the display driver and the monitors come up after the program is
already starting (it runs from the Run key), and for a moment DXGI can list
no output at all. The capture opens dxcam with a fallback chain - refresh the
factory, then output 0, then GDI (mss) - written so that "the capture must
never take the app down". But the last attempt at output 0 ran inside the
IndexError handler, where the GDI fallback beside it could not catch its
raise: ScreenCapture raised IndexError, and startup ended in "NeuralScreen
failed to start".

Checked on capture.ScreenCapture with the dxcam topology helpers stubbed to
"no outputs" (dxcam itself is imported as usual): the capture opens on the
GDI path and hands back a frame.

Run:  runtime\python.exe tests\test_capture_no_outputs.py
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import capture  # noqa: E402


def main() -> int:
    failures = []
    saved = {name: getattr(capture, name) for name in
             ("_output_count", "_refresh_dxcam_factory", "_dxcam_capture_target",
              "devicename_for_output_idx")}
    capture._output_count = lambda: 0
    capture._refresh_dxcam_factory = lambda: None
    capture._dxcam_capture_target = lambda idx: None
    capture.devicename_for_output_idx = lambda idx: None
    cap = None
    try:
        try:
            cap = capture.ScreenCapture(0)
        except Exception as exc:
            failures.append(f"the capture raised {type(exc).__name__}: {exc}")
        if cap is not None:
            print(f"    opened: dxcam={cap._camera is not None}, mss={cap._mss is not None}")
            if cap._mss is None:
                failures.append("no output and no GDI fallback either")
            else:
                frame = cap.grab()
                if frame is None or getattr(frame, "ndim", 0) != 3:
                    failures.append(f"the GDI fallback gave no frame: {type(frame)}")
    finally:
        for name, value in saved.items():
            setattr(capture, name, value)
        if cap is not None:
            try:
                cap.close()
            except Exception:
                pass
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: with no display output the capture falls back to GDI instead of raising")
    return 0


if __name__ == "__main__":
    sys.exit(main())
