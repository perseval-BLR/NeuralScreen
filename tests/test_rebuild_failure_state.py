"""A rebuild whose worker cannot start leaves a failed pipeline, not a stale one.

rebuild_pipeline (the monitor and window switches, Spout, HDR, the motion
backend, a GPU change) runs after the old pipeline was torn down. It started
the new worker first and reset everything else afterwards - the per-worker
flags (DDA, gray, picture window, motion, pixel section), the guides and the
frame buffer sized to the new frame. When the start raised - the
compatibility check after a DLL changed, start_worker, the shared section -
none of that happened: the flags still said the old worker's channels were
open, the guides and the buffer kept the old size next to the new work size,
and nothing said the worker was gone. The next worker inherited all of it
and was never asked for its channels.

What this pins, on the real rebuild_pipeline with the worker start made to
fail at each step: the exception still reaches the caller, and by then the
flags are reset, the guides and the buffer have the new size, and the
pipeline stands failed - worker_failed, NR OFF, the automatic revive armed.

Run:  runtime\\python.exe tests\\test_rebuild_failure_state.py
"""
import sys
import types
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import pipeline  # noqa: E402

FLAGS = ("present_mode", "present_attempted", "dda_mode", "dda_attempted",
         "gray_active", "motion_small", "motion_attempted", "out_shm",
         "out_attempted")


def _state():
    st = types.SimpleNamespace(
        width=1280, height=720, work_w=640, work_h=360,
        params={}, effective_warmup=4, cfg={"flow_preset": "fast"},
        guides=types.SimpleNamespace(width=1920, height=1080),
        buf_full=np.empty((1080, 1920, 4), np.uint8),
        worker=types.SimpleNamespace(poll=lambda: 0), worker_stop=None,
        worker_failed=False, paused=False, next_auto_revive=0.0,
        frame_index=99, pts=99, work_frame=object(), output_rgba=None,
        gpu_ok=True, gpu_alerted=True,
        tray=types.SimpleNamespace(_set_state=lambda **k: None),
        capture=types.SimpleNamespace(resolution=(1920, 1080)),
        display=types.SimpleNamespace(
            enter_switch_mode=lambda *a, **k: None,
            menu=types.SimpleNamespace(visible=False)))
    for name in FLAGS:
        setattr(st, name, True)
    return st


def _refuse(*a, **k):
    raise RuntimeError("production worker blocked: compatibility key changed")


def main() -> int:
    failures = []
    saved = {name: getattr(pipeline, name) for name in
             ("SharedFrameBuffer", "require_compatibility", "start_worker",
              "shutdown_worker")}
    try:
        pipeline.shutdown_worker = lambda *a, **k: None
        for label, where in (("the shared section", "SharedFrameBuffer"),
                             ("the compatibility check", "require_compatibility"),
                             ("start_worker", "start_worker")):
            pipeline.SharedFrameBuffer = lambda w, h: types.SimpleNamespace()
            pipeline.require_compatibility = lambda st: None
            pipeline.start_worker = lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("the test refuses earlier"))
            setattr(pipeline, where, _refuse)
            st = _state()
            try:
                pipeline.rebuild_pipeline(st, "test")
                failures.append(f"{label} failed and the rebuild reported "
                                f"success")
            except RuntimeError:
                pass
            stale = [name for name in FLAGS if getattr(st, name)]
            if stale:
                failures.append(f"{label} failed: the old worker's flags are "
                                f"still set {stale} - the next worker is never "
                                f"asked for those channels")
            if (st.guides.width, st.guides.height) != (st.work_w, st.work_h):
                failures.append(f"{label} failed: the guides stayed "
                                f"{st.guides.width}x{st.guides.height} next to "
                                f"a {st.work_w}x{st.work_h} work size")
            if st.buf_full.shape[:2] != (st.height, st.width):
                failures.append(f"{label} failed: the frame buffer stayed "
                                f"{st.buf_full.shape[1]}x{st.buf_full.shape[0]}")
            if not (st.worker_failed and st.paused and st.next_auto_revive):
                failures.append(f"{label} failed: the pipeline does not stand "
                                f"failed (worker_failed={st.worker_failed}, "
                                f"paused={st.paused}, revive="
                                f"{st.next_auto_revive})")
    finally:
        for name, fn in saved.items():
            setattr(pipeline, name, fn)

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a rebuild whose worker cannot start leaves the flags reset, the "
          "buffers at the new size and the pipeline failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
