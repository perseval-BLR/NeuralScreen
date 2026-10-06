"""One-window mode with the gray channel refused sends zero motion, not the monitor's.

In window mode the worker captures the window (WGCW) and writes its luminance
back for the flow guides (GRAY). When GRAY is refused the loop fell back to
what it does on the desktop: grab a frame with dxcam and compute the flow
from it. But dxcam captures the whole MONITOR - so the frame was the desktop,
resized to the window's size, and the motion the worker applied to the window
described whatever moved around it.

Run through main.main()'s real loop (tests/loop_harness.py):

* window mode, worker capture on, GRAY refused: nothing is grabbed, the
  guides never see a dxcam frame, and every frame goes out with zero motion
  and no colour;
* desktop mode with GRAY refused keeps its dxcam guides - there the monitor
  IS the picture.

Run:  runtime\\python.exe tests\\test_window_gray_refused.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_harness as H  # noqa: E402  (puts app/ on the path)
import channels  # noqa: E402

PASSES = 12


def _refused_gray(p, events):
    def wgc(st):
        st.dda_attempted = True
        st.dda_mode = True
        st.gray_active = False       # GRAY refused
        return True

    def dda(st):
        st.dda_attempted = True
        st.dda_mode = True
        st.gray_active = False       # GRAY refused
    p.set(channels, "enable_wgc", wgc)
    p.set(channels, "enable_dda", dda)


def main() -> int:
    failures = []

    rc, st, log, info = H.run(lambda st, n: n < PASSES, patch=_refused_gray,
                              state={"window_hwnd": 0x1234})
    sends = [e for e in info["events"] if e[0] == "send"]
    if rc != 0 or not sends:
        failures.append(f"window mode: the loop did not run (rc={rc}, "
                        f"{len(sends)} frames)")
    else:
        if "frame" in st.guides.processed:
            failures.append(f"window mode: the flow was computed from a dxcam "
                            f"grab of the whole monitor "
                            f"({st.guides.processed.count('frame')} frames)")
        if st.capture.grabs:
            failures.append(f"window mode: the monitor was grabbed "
                            f"{st.capture.grabs} times for a window pipeline")
        if st.guides.zeroed < len(sends):
            failures.append(f"window mode: {len(sends) - st.guides.zeroed} of "
                            f"{len(sends)} frames went out with motion that "
                            f"was not zero")
        if any(not e[2].get("no_color") for e in sends):
            failures.append("window mode: a frame carried colour")

    rc, st, log, info = H.run(lambda st, n: n < PASSES, patch=_refused_gray)
    if rc != 0:
        failures.append(f"desktop mode: main() returned {rc}")
    elif "frame" not in st.guides.processed:
        failures.append("desktop mode with GRAY refused lost its dxcam guides")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: window mode without the gray channel sends zero motion; the "
          "desktop keeps its dxcam guides")
    return 0


if __name__ == "__main__":
    sys.exit(main())
