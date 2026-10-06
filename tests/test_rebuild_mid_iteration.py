"""A pipeline rebuilt in the middle of a loop iteration gets its channels before its first frame.

The frame loop negotiates a fresh worker's channels near the top of an
iteration - the picture window (WNDO), the capture (DDA1/WGCW), the motion
size, the pixel section - and then grabs, computes the guides and sends.
Several steps in between can REBUILD the pipeline: the window follower (a
resize), the monitor follower (a mode change), the window mode giving up
(WGCW refused), and a menu action (a monitor, a window, Spout, HDR, a GPU).
The rest of that iteration went on with the new worker as if it had been
negotiated: its first frame was a dxcam grab of the whole monitor sent as
the colour of a pipeline built for one window, and that frame's reply took
the veil down before the new pipeline had shown anything.

Run through main.main()'s real loop (tests/loop_harness.py). For each of
three rebuild points: the first frame the NEW worker receives must come after
its picture window and its capture were negotiated, and must carry no colour.

Run:  runtime\\python.exe tests\\test_rebuild_mid_iteration.py
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loop_harness as H  # noqa: E402  (puts app/ on the path)
import channels  # noqa: E402
import commands  # noqa: E402
import main as main_mod  # noqa: E402
import pipeline  # noqa: E402

TRIGGER = 5


def _rebuild(st, window_hwnd="keep"):
    """What rebuild_pipeline leaves behind: a new worker that knows nothing."""
    st.reader = H.FakeReader("rebuilt")
    if window_hwnd != "keep":
        st.window_hwnd = window_hwnd
    st.present_mode = st.present_attempted = False
    st.dda_mode = st.dda_attempted = False
    st.gray_active = False
    st.motion_small = st.motion_attempted = False
    st.out_shm = st.out_attempted = False
    st.frame_index = 0
    st.pts = 0
    st.work_frame = None
    st.output_rgba = None
    st.display.enter_switch_mode()


def _check(label, events):
    problems = []
    first = next((i for i, e in enumerate(events)
                  if e[0] == "send" and e[1].name == "rebuilt"), None)
    if first is None:
        return [f"{label}: the rebuilt worker never received a frame"]
    before = [e[0] for e in events[:first] if len(e) > 1
              and getattr(e[1], "name", None) == "rebuilt"]
    if "present" not in before:
        problems.append(f"{label}: the rebuilt worker's first frame went out "
                        f"before its picture window was asked for")
    if "dda" not in before and "wgc" not in before:
        problems.append(f"{label}: the rebuilt worker's first frame went out "
                        f"before its capture was negotiated")
    if not events[first][2].get("no_color"):
        problems.append(f"{label}: the rebuilt worker's first frame carried a "
                        f"colour grab from the old pipeline's path")
    return problems


def _menu_case():
    def patch(p, events):
        p.set(main_mod, "pygame", types.SimpleNamespace(
            event=types.SimpleNamespace(get=lambda: [object()], pump=lambda: None),
            image=types.SimpleNamespace(frombuffer=lambda *a: None)))

        def apply(st, action):
            st.display.menu.visible = False
            _rebuild(st)
        p.set(commands, "apply_menu_action", apply)

    def on_pass(st, n):
        if n == TRIGGER:
            st.display.menu.visible = True
            st.display.menu.handle_event = lambda ev: [("monitor", 1)]
        return n < TRIGGER + 4

    rc, st, log, info = H.run(on_pass, patch=patch)
    if rc != 0:
        return [f"a menu action: main() returned {rc}"]
    return _check("a menu action", info["events"])


def _follow_case():
    def patch(p, events):
        def follow(st):
            if st.mon_resize == "go":
                st.mon_resize = None
                _rebuild(st)
        p.set(pipeline, "follow_window", follow)

    def on_pass(st, n):
        if n == TRIGGER:
            st.mon_resize = "go"
        return n < TRIGGER + 4

    rc, st, log, info = H.run(on_pass, patch=patch,
                              state={"window_hwnd": 0x1234})
    if rc != 0:
        return [f"a window resize: main() returned {rc}"]
    return _check("a window resize", info["events"])


def _wgc_refused_case():
    def patch(p, events):
        def wgc(st):
            st.dda_attempted = True
            if st.mon_resize == "refuse":
                st.mon_resize = None
                return False
            st.dda_mode = st.gray_active = True
            events.append(("wgc", st.reader))
            return True
        p.set(channels, "enable_wgc", wgc)
        p.set(pipeline, "switch_window",
              lambda st, hwnd: _rebuild(st, window_hwnd=None))

    def on_pass(st, n):
        if n == TRIGGER:
            # The window pipeline was rebuilt (a resize) and its WGCW is
            # about to be refused.
            st.mon_resize = "refuse"
            st.dda_mode = st.dda_attempted = st.gray_active = False
        return n < TRIGGER + 4

    rc, st, log, info = H.run(on_pass, patch=patch,
                              state={"window_hwnd": 0x1234})
    if rc != 0:
        return [f"a refused window capture: main() returned {rc}"]
    return _check("a refused window capture", info["events"])


def main() -> int:
    failures = _menu_case() + _follow_case() + _wgc_refused_case()
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a pipeline rebuilt mid-iteration gets its window and capture "
          "before its first frame")
    return 0


if __name__ == "__main__":
    sys.exit(main())
