"""A screenshot named with a dot in it is saved, and a failed write leaves no
broken file.

Two faults in the "Save as" path:

  * A name like "скрин 06.10.2026" has the "suffix" .2026. The dialog adds
    its default extension only to a name with none, ask_save_path checked
    only for no suffix at all, and save_image then refused ".2026" - after
    the frozen frame had already been let go, so the screenshot was lost.
    The default extension is now APPENDED to any name that does not end in
    .png/.jpg/.jpeg.
  * save_image wrote straight into the chosen name: a write cut short (a
    full disk) left a truncated picture there, over whatever was there
    before. It writes a temporary file and replaces the target with it.

The modal dialog is replaced by a stand-in that "types" the name into the
real struct's buffer, so nothing opens on screen.

Run:  runtime\\python.exe tests\\test_screenshot_dotted_name.py
"""
import ctypes
import os
import pathlib
import queue
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import numpy as np  # noqa: E402

import commands  # noqa: E402
import dialogs  # noqa: E402


def typed(name: str, default_name: str) -> Path | None:
    """ask_save_path as if the user typed `name` and pressed Save."""
    real_struct = dialogs._save_dialog_struct
    dll = ctypes.windll.comdlg32
    real_call = dll.GetSaveFileNameW
    held = {}

    def struct(parent, default, initial):
        ofn, buf = real_struct(parent, default, initial)
        held["buf"] = buf
        return ofn, buf

    def dialog(_ref):
        held["buf"].value = name
        return 1

    dialogs._save_dialog_struct = struct
    dll.GetSaveFileNameW = dialog
    try:
        return dialogs.ask_save_path(0, default_name)
    finally:
        dialogs._save_dialog_struct = real_struct
        dll.GetSaveFileNameW = real_call


def frame() -> np.ndarray:
    pixels = np.zeros((48, 64, 4), np.uint8)
    pixels[..., 0] = 200
    pixels[..., 3] = 255
    return pixels


def check_names(folder: Path, failures: list) -> None:
    cases = (("скрин 06.10.2026", "neuralscreen.jpg", "скрин 06.10.2026.jpg"),
             ("скрин 06.10.2026", "neuralscreen.png", "скрин 06.10.2026.png"),
             ("v1.2 final", "neuralscreen.png", "v1.2 final.png"),
             ("shot", "neuralscreen.jpg", "shot.jpg"),
             ("shot.PNG", "neuralscreen.jpg", "shot.PNG"),
             ("shot.jpeg", "neuralscreen.png", "shot.jpeg"))
    for name, default, want in cases:
        got = typed(str(folder / name), default)
        print(f"typed {name!r} (default {default}) -> {got.name if got else None!r}")
        if got is None or got.name != want:
            failures.append(f"{name!r} with default {default}: saved as "
                            f"{got.name if got else None!r}, not {want!r}")


def check_screenshot_saved(folder: Path, failures: list) -> None:
    """The whole path: the dialog's answer reaches the file through commands."""
    alerts = []
    st = types.SimpleNamespace(
        shot_paths=queue.Queue(), shot_dialog_open=True, shot_rgba=frame(),
        lang="en", shot_requested_at=None,
        display=types.SimpleNamespace(
            alert=lambda message, *a, **k: alerts.append(str(message))))
    path = typed(str(folder / "скрин 06.10.2026"), "neuralscreen.png")
    st.shot_paths.put(("save", path))
    commands.drain_save_dialog(st)
    saved = [p.name for p in folder.iterdir() if p.name.startswith("скрин")]
    print(f"saved: {saved}, alerts: {alerts}")
    if saved != ["скрин 06.10.2026.png"]:
        failures.append(f"the screenshot was not saved under its name: {saved}")
    if not alerts or "saved" not in alerts[-1].lower():
        failures.append(f"the user was not told it was saved: {alerts}")


def check_disk_full(folder: Path, failures: list) -> None:
    target = folder / "kept.png"
    target.write_bytes(b"the picture that was here before")
    before = target.read_bytes()
    real_write = pathlib.Path.write_bytes

    def half_then_full(self, data):
        real_write(self, bytes(data)[:len(data) // 2])
        raise OSError(28, "No space left on device")

    pathlib.Path.write_bytes = half_then_full
    try:
        try:
            dialogs.save_image(target, frame())
            failures.append("disk full: save_image did not report the failure")
        except OSError:
            pass
    finally:
        pathlib.Path.write_bytes = real_write
    left = sorted(p.name for p in folder.iterdir() if "kept" in p.name)
    print(f"disk full: files {left}, target intact "
          f"{target.read_bytes() == before}")
    if target.read_bytes() != before:
        failures.append("disk full: the existing file was overwritten with a "
                        "truncated picture")
    if left != ["kept.png"]:
        failures.append(f"disk full: files left behind: {left}")


def main() -> int:
    failures: list = []
    folder = Path(tempfile.mkdtemp(prefix="ns-shot-dotted-"))
    check_names(folder, failures)
    check_screenshot_saved(folder, failures)
    check_disk_full(folder, failures)
    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: a dotted screenshot name gets its extension, and a failed write "
          "leaves the old file whole")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
