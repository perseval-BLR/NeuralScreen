"""The scene cut, decided in the worker: no capture round trip per frame.

With NVOFA the loop's only use for the capture before a frame was the scene
score, and fetching it (CAP1 -> the gray -> FRM1) cost a round trip with the
GPU idle: 2.07 ms a frame in a Boost run. FRAME_FLAG_WORKER_SCENE leaves it
to the worker, which scores the gray it has just captured and sets the reset
itself - with native/scene_cut.h, the twin of the client's app/scene_cut.py.
Checked:

* the score and the decision are the client's: after every frame the test
  runs app/scene_cut.py on the same gray the worker wrote, and the reply must
  agree to the 1/65535 it is sent in, and on the cut;
* a real cut (the window repainted in other colours) resets exactly one
  frame, and the reply says so; frames around it do not;
* a frame sent without the flag carries no score.

What it is worth is a benchmark, not a check, and is not run here: measured
at 2560x1440, +3.3% at full resolution and +3.6% with Boost and two passes.

A light test: frames are paced to 60 fps at 1280x720, a few seconds in all.
Nothing shows: the worker's window is far off every monitor, and the target
is a ghost (tests/offscreen_target.py) - it has to be on a monitor for its
repaints to reach WGC.

Run:  runtime\\python.exe tests\\test_worker_scene.py
"""
import ctypes
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from offscreen_target import Target  # noqa: E402
from paths import WORKER_EXE  # noqa: E402

W, H = 1280, 720
GW, GH = 320, 180
MAX_FPS = 60              # a light test: never an uncapped loop on the GPU
CUT = (0xFFFFFF, 0x000000, 0xFFFFFF, 0x000000)


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    os.environ["NS_WINDOW_POS"] = "-30000,-30000"
    os.environ["NS_NR_SMALL"] = "0"
    os.environ["NS_MOTION_BACKEND"] = "nvofa"
    from pipeline import shutdown_worker, start_worker
    from protocol import (SharedFrameBuffer, send_frame, send_gray,
                          send_motion_size, send_wgc, send_window)
    from settings_io import PROFILES

    failures: list = []
    target = Target(W, H, name="NsWorkerScene", ghost=True)
    params = dict(PROFILES["Natural"])
    params["style"] = 1
    shm = SharedFrameBuffer(W, H)
    worker, logs, reader, stop = start_worker(params, W, H, 2, 0, 0, shm)
    state = {"index": 0, "due": 0.0}
    motion = np.zeros((GH, GW, 2), np.float16)

    def frame(worker_scene: bool = True):
        wait = state["due"] - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        state["due"] = max(state["due"], time.perf_counter()) + 1.0 / MAX_FPS
        i = state["index"]
        state["index"] += 1
        send_frame(worker, i, None, motion, i == 0, i, no_color=True,
                   motion_small=True, early_reply=True,
                   worker_scene=worker_scene)
        reader.recv(i, timeout=10.0)
        return reader.last_scene, reader.last_scene_cut

    try:
        send_wgc(worker, target.hwnd)
        if reader.wait_wgak(15) != (W, H):
            failures.append("the capture target is not the expected size")
        shm.open_gray(GW, GH)
        send_gray(worker, GW, GH, shm.gray_name)
        reader.wait_gak(10)
        send_motion_size(worker, GW, GH)
        reader.wait_mack(10)
        send_window(worker, W, H)
        reader.wait_wack(10)
        for _ in range(30):
            frame()

        # 1. The worker's score and cut are the client's rule on the same gray.
        from scene_cut import SceneCutDetector
        client = SceneCutDetector()
        client.step(shm.read_gray().reshape(GH, GW))
        target.animate = True
        scored = 0
        worst = 0.0
        disagree = 0
        for _ in range(90):
            score, cut = frame()
            gray = shm.read_gray().reshape(GH, GW)
            mine_cut = client.step(gray)
            if score is not None:
                worst = max(worst, abs(client.last_score - score))
                disagree += int(bool(cut) != bool(mine_cut))
                scored += 1
        print(f"    {scored} of 90 frames scored by the worker; the largest "
              f"difference from the client's rule {worst * 65535:.2f}/65535")
        if scored < 30:
            failures.append(f"only {scored} of 90 animated frames carried a score")
        if worst > 1.5 / 65535:
            failures.append(f"the worker's score differs from the client's rule "
                            f"by {worst:.6f}")
        if disagree:
            failures.append(f"the worker and the client disagreed on {disagree} cuts")

        # 2. A cut resets exactly the frame that captured it.
        target.animate = False
        for _ in range(20):
            frame()
        before = [frame()[1] for _ in range(10)]
        target.set_bars(CUT)
        after = [frame() for _ in range(40)]
        cuts = [k for k, (_s, c) in enumerate(after) if c]
        top = max((s for s, _c in after if s is not None), default=None)
        print(f"    a black-and-white repaint: cut on frame {cuts} of the 40 after "
              f"it, top score {top if top is None else round(top, 3)}; "
              f"{sum(before)} cuts in the 10 before")
        if any(before):
            failures.append("a still window reported a scene cut")
        if len(cuts) != 1:
            failures.append(f"the repaint reset {len(cuts)} frames, not exactly one")
        elif after[cuts[0]][0] is None or after[cuts[0]][0] <= 0.40:
            failures.append(f"the cut came with a score of {after[cuts[0]][0]}")

        # 3. No flag, no score.
        target.animate = True
        frame(worker_scene=False)
        target.animate = False
        if reader.last_scene is not None or reader.last_scene_cut:
            failures.append("a frame sent without the flag carried a score")
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        shutdown_worker(worker, stop)
        shm.close()
        target.close()
    if not any("[nvofa] active" in ln for ln in logs):
        failures.append("NVOFA never came up - the frames carried no motion")
    if failures:
        for f in failures:
            print("FAIL:", f)
        print("worker log (tail):", *logs[-12:], sep="\n  ")
        return 1
    print("OK: the worker's scene score is the client's, a cut resets one frame, "
          "and a frame without the flag carries none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
