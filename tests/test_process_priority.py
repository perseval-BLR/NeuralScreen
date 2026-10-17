"""The Windows priority class of both processes is a setting (#170).

The report: on a stripped-down home-cinema PC, NeuralScreen at High priority in
Task Manager played smoother - and the change had to be made by hand on every
start, for the program and for its worker. The Program tab now has a
Normal / Above normal / High row that does it, and keeps it.

Checked without a window, on real processes:

* the handler sets the class of THIS process and of a running child (the
  stand-in for the worker), and reads them back - Windows' own answer;
* the frame loop's re-assert (power.apply_priority) puts a restarted child at
  the chosen class, and "normal" puts both back;
* the menu has the row, a pick asks for ("process_priority", value);
* an unknown value in the config falls back to "normal", the choice is saved.

Fails before the change: there was no setting, the handler ignored the action
and both processes stayed at Normal.

Run:  runtime\\python.exe tests\\test_process_priority.py
"""
import contextlib
import ctypes
import io
import json
import os
import subprocess
import sys
import tempfile
import types
from ctypes import wintypes
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))

import pygame  # noqa: E402

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
k32.GetPriorityClass.restype = wintypes.DWORD
k32.GetCurrentProcess.restype = wintypes.HANDLE
k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.CloseHandle.argtypes = [wintypes.HANDLE]

CLASSES = {"normal": 0x20, "above_normal": 0x8000, "high": 0x80}


def class_of(pid=None) -> int:
    if pid is None:
        return k32.GetPriorityClass(k32.GetCurrentProcess())
    h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    try:
        return k32.GetPriorityClass(h)
    finally:
        k32.CloseHandle(h)


def child() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _check_processes(failures: list) -> None:
    import commands
    worker = child()
    st = types.SimpleNamespace(cfg={}, worker=worker)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            commands.apply_menu_action(st, ("process_priority", "high"))
        if st.cfg.get("process_priority") != "high":
            failures.append(f"the handler stored {st.cfg}")
        if class_of() != CLASSES["high"]:
            failures.append(f"this process reads back 0x{class_of():X}, not High")
        if class_of(worker.pid) != CLASSES["high"]:
            failures.append(f"the worker reads back 0x{class_of(worker.pid):X}, not High")
        if "process priority: high" not in out.getvalue():
            failures.append(f"nothing in the log said what was set: {out.getvalue()!r}")

        # A restarted worker comes up at Normal; the loop's re-assert lifts it.
        worker.kill()
        worker.wait()
        worker = st.worker = child()
        import power
        with contextlib.redirect_stdout(io.StringIO()):
            power.apply_priority(st)
        if class_of(worker.pid) != CLASSES["high"]:
            failures.append(f"a restarted worker stayed at 0x{class_of(worker.pid):X}")

        with contextlib.redirect_stdout(io.StringIO()):
            commands.apply_menu_action(st, ("process_priority", "above_normal"))
        if class_of(worker.pid) != CLASSES["above_normal"]:
            failures.append("Above normal did not reach the worker")
        with contextlib.redirect_stdout(io.StringIO()):
            commands.apply_menu_action(st, ("process_priority", "normal"))
        if class_of() != CLASSES["normal"] or class_of(worker.pid) != CLASSES["normal"]:
            failures.append("Normal did not put both processes back")

        # Realtime is not offered: the handler refuses it and changes nothing.
        with contextlib.redirect_stderr(io.StringIO()):
            commands.apply_menu_action(st, ("process_priority", "realtime"))
        if st.cfg.get("process_priority") != "normal" or class_of() != CLASSES["normal"]:
            failures.append("an unknown level was accepted")
    finally:
        worker.kill()
        worker.wait()
        k32.SetPriorityClass(k32.GetCurrentProcess(), CLASSES["normal"])
    print("    processes: High, Above normal and Normal reach both, a restart is re-asserted")


def _check_menu(failures: list) -> None:
    import fonts
    import overlay_ui as ui
    menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
    menu.visible = True
    menu.page = "settings"
    menu.settings_tab = "app"
    menu.set_state({"nr": True, "gpu_ok": True, "profile": "Natural",
                    "profiles": ["Natural"], "lang": "en", "process_priority": "high"})
    menu.layout(1920, 1080)
    row = next((it for it in menu.items
                if it.kind == "segmented" and it.key == "process_priority"), None)
    if row is None or list(row.payload or []) != ["normal", "above_normal", "high"]:
        failures.append(f"the Program tab has no Normal/Above normal/High row "
                        f"({row and row.payload})")
        return
    actions = menu._pick("process_priority", "above_normal")
    if actions != [("process_priority", "above_normal")]:
        failures.append(f"picking Above normal asked for {actions}")


def _check_config(failures: list) -> None:
    import settings_io
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text('{"process_priority": "realtime"}', encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            got = settings_io.load_config(path).get("process_priority")
        if got != "normal":
            failures.append(f"an unknown priority loaded as {got!r}, not 'normal'")
        path.write_text("{}", encoding="utf-8")
        menu = types.SimpleNamespace(user_scale=1.0, user_height=None, state={}, mini=False,
                                     mini_rows=(), offset=(0, 0))
        st = types.SimpleNamespace(
            cfg_path=path, cfg={"profile": "Natural", "process_priority": "high"},
            params=dict(settings_io.PROFILES["Natural"], style=1), monitor=0, lang="en",
            work_scale=1.0, split_pos=0.0, startup_menu=False, nr_small=True,
            display=types.SimpleNamespace(menu=menu))
        settings_io.save_menu_layout(st)
        if json.loads(path.read_text(encoding="utf-8")).get("process_priority") != "high":
            failures.append("the priority was not saved")


def main() -> int:
    failures: list = []
    pygame.init()
    pygame.display.set_mode((64, 64))
    try:
        _check_processes(failures)
        _check_menu(failures)
    finally:
        pygame.quit()
    _check_config(failures)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the process priority is a setting that reaches both processes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
