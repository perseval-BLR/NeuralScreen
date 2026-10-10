"""An unavailable recording folder does not lock out every dialog for the session.

The "Add files" picker of the conversion page starts in the recording folder,
and creating that folder can fail - a drive that is gone, a share that is
offline, a path that names a file. The picker raised the shared dialog flag
first and created the folder second, so the failure left the flag up for the
rest of the session: screenshots, the folder pickers, the diagnostic package
and the picker itself were all refused without a word until a restart.

Checked through commands.pick_convert_files with the recording folder set to
a path under an existing FILE (mkdir must fail) and the dialog itself stubbed:
the picker still opens, and once it has answered the flag is down again.

Run:  runtime\python.exe tests\test_convert_picker_bad_folder.py
"""
import queue
import sys
import tempfile
import time
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import commands  # noqa: E402
import dialogs  # noqa: E402


def main() -> int:
    failures = []
    opened = []
    real = dialogs.ask_open_paths
    dialogs.ask_open_paths = lambda hwnd, start, title: opened.append(start) or []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "not-a-folder"
            blocker.write_text("x", encoding="utf-8")
            st = types.SimpleNamespace(
                shot_dialog_open=False, lang="en",
                cfg={"recording_dir": str(blocker / "recordings")},
                shot_paths=queue.Queue(),
                display=types.SimpleNamespace(get_hwnd=lambda: 0))
            try:
                commands.pick_convert_files(st)
            except Exception as exc:
                failures.append(f"the picker raised {type(exc).__name__}: {exc}")
            deadline = time.monotonic() + 5.0
            while not opened and time.monotonic() < deadline:
                time.sleep(0.02)
            print(f"    dialog opened: {bool(opened)}, flag up: {st.shot_dialog_open}")
            if not opened:
                failures.append("the picker never opened")
            # The flag is lowered where the answer is consumed.
            deadline = time.monotonic() + 2.0
            while st.shot_paths.empty() and time.monotonic() < deadline:
                time.sleep(0.02)
            commands.drain_save_dialog(st)
            if st.shot_dialog_open:
                failures.append("the dialog flag stays up after the picker answered")
    finally:
        dialogs.ask_open_paths = real
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: an unavailable recording folder does not lock the dialogs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
