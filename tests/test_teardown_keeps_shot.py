"""A pipeline rebuild does not throw away a screenshot the user is in the middle of.

A screenshot is two steps: the next processed frame is requested
(pending_shot) and frozen, then the Save As dialog opens on the frozen copy
(shot_rgba) and the file is written when the user answers. teardown_pipeline
cleared both. A rebuild between the two - a window resize, a monitor mode
change, the window mode giving up, all of which can happen while the dialog
is up - left the dialog with nothing behind it: the user picked a file name
and got "No frame yet". A request that had no frame yet was dropped in
silence.

What this pins, on the real teardown_pipeline and the real screenshot code
(commands.py), with no worker:

* a frame frozen for an open Save As dialog survives the teardown, and the
  dialog's answer saves THAT frame;
* a request still waiting for its frame stays pending, and the first frame
  after the rebuild answers it;
* a frozen frame with no dialog to claim it still goes.

Run:  runtime\\python.exe tests\\test_teardown_keeps_shot.py
"""
import queue
import sys
import types
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import commands  # noqa: E402
import pipeline  # noqa: E402


def _state(**kw):
    st = types.SimpleNamespace(
        recorder=None, recording_finalizer=None, pending_shot=None,
        shot_rgba=None, shot_dialog_open=False, shot_paths=queue.Queue(),
        shot_requested_at=None, worker=None, worker_stop=None,
        shm=types.SimpleNamespace(close=lambda: None),
        cfg={"screenshot_mode": "ask", "screenshot_format": "png"},
        lang="en",
        display=types.SimpleNamespace(alert=lambda *a, **k: None))
    for name, value in kw.items():
        setattr(st, name, value)
    return st


def main() -> int:
    failures = []
    saved = (pipeline.shutdown_worker, commands.save_screenshot,
             commands.open_save_dialog)
    written = []
    opened = []
    pipeline.shutdown_worker = lambda *a, **k: None
    commands.save_screenshot = lambda st, path, rgba: written.append((path, rgba))

    def open_dialog(st):
        st.shot_dialog_open = True
        opened.append(True)
    commands.open_save_dialog = open_dialog
    try:
        # 1. The dialog is open on a frozen frame; the pipeline is rebuilt.
        frame = np.full((4, 4, 4), 7, np.uint8)
        st = _state(shot_rgba=frame, shot_dialog_open=True)
        pipeline.teardown_pipeline(st)
        if st.shot_rgba is not frame:
            failures.append("the frame frozen for the open Save As dialog was "
                            "thrown away by the rebuild")
        st.shot_paths.put(("save", Path("shot.png")))
        commands.drain_save_dialog(st)
        if not written or written[0][1] is not frame:
            failures.append("the dialog's answer did not save the frame it was "
                            "opened on")

        # 2. The request is still waiting for its frame.
        st = _state()
        commands.request_screenshot(st)
        pipeline.teardown_pipeline(st)
        if st.pending_shot is None:
            failures.append("a screenshot request was dropped by the rebuild "
                            "without a word")
        new_frame = np.full((2, 2, 4), 9, np.uint8)
        if not commands.freeze_screenshot_frame(st, new_frame) or not opened:
            failures.append("the first frame after the rebuild did not answer "
                            "the pending screenshot")

        # 3. A frozen frame nobody will claim still goes.
        st = _state(shot_rgba=frame, shot_dialog_open=False)
        pipeline.teardown_pipeline(st)
        if st.shot_rgba is not None:
            failures.append("a frozen frame with no dialog survived the "
                            "teardown")
    finally:
        (pipeline.shutdown_worker, commands.save_screenshot,
         commands.open_save_dialog) = saved

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a rebuild keeps the screenshot the user is saving, and a "
          "pending request is answered by the new pipeline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
