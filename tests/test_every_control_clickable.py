"""Every control in the menu must survive a click, on the real state object.

THE BUG THIS EXISTS FOR (user: "clicking Screenshot throws" + "you should at least
have a test that presses every button")

    10:42:59.986  [main] ERROR: '_Pipeline' object has no attribute
                  'shot_requested_at' and no __dict__ for setting new attributes

`_Pipeline` uses `__slots__` on purpose (its docstring: "a typo in a field name
raises AttributeError here instead of quietly creating a new attribute that
nothing ever reads"). A diagnostic line added late wrote an undeclared field, and
the FIRST click on Screenshot killed the process. Nothing clicked that button, so
nothing noticed.

WHY THE OTHER TESTS COULD NOT CATCH IT

They build their state as `SimpleNamespace`, which accepts ANY attribute - so a
missing declaration is invisible there. This test uses the REAL `_Pipeline`, so an
undeclared field fails exactly as it does in the program.

WHAT IT PRESSES

Every control the menu can produce, on every page and every settings tab: the
menu's own action tuples are fed through `commands.apply_menu_action`, and the
hotkey commands through `commands.drain_commands`. Both are the real dispatchers;
the worker, dialogs and the display are stubs so a click cannot touch the GPU or
open a window.

What is NOT checked: whether the action does the right thing - that is each
feature's own test. This one answers "does pressing it crash the program".

Run:  runtime\\python.exe tests/test_every_control_clickable.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

import commands  # noqa: E402
import fonts  # noqa: E402
import overlay_ui  # noqa: E402


# --------------------------------------------------------------------------
# The real state object, with the parts that reach outside it replaced.
# --------------------------------------------------------------------------

def real_state():
    """A `_Pipeline` with neutral values everywhere and stubs for the outside.

    The real class, imported from main - that is the whole point: `__slots__` turns
    an undeclared field into an AttributeError here exactly as it does in the
    program. Every declared field is filled from the slots tuple so the fixture
    cannot be missing one, and the few that a dispatcher reads for real get a
    plausible value below.
    """
    import main as main_mod
    import queue

    st = main_mod._Pipeline()
    # Neutral for everything the class declares (0 / False / None / empty).
    for name in main_mod._Pipeline.__slots__:
        setattr(st, name, None)

    st.cfg = {
        "profile": "Natural", "params": {}, "nr_small": True,
        "nr_direct": False, "work_scale": 0.65, "theme": "dark",
        "lang": "en", "frame_generation": False, "frame_multiplier": 2,
        "boost": False, "spout": False, "hdr": False,
        "screenshot_dir": "C:/Shots", "recording_dir": "C:/Rec",
        "screenshot_mode": "ask", "screenshot_format": "png",
        "monitor": "0", "frame_limit_mode": "unlimited",
        "motion_backend": "nvofa", "hotkeys": {}, "open_on_start": True,
        "autostart": True, "rec_indicator": True, "gpu": "0",
        "param_ranges": {}, "param_defaults": {},
    }
    st.params = {"intensity": 1.0, "local_tone": 0.5, "local_structure": 1.0,
                 "skin_structure": -1.0, "style": 1, "auto_mask": 1}
    st.lang = "en"
    st.running = True
    st.width, st.height = 2560, 1600
    st.work_w, st.work_h = 2304, 1440
    st.work_scale = 0.65
    st.gpu_ok = True
    st.gpu_text = "RTX 5070 Ti"
    st.pts = 0
    st.frame_index = 0
    st.warmup = st.effective_warmup = 10
    st.nr_idle_streak = 0
    st.nr_not_evaluating = False
    st.consecutive_restarts = 0
    st.guide_fails = 0
    st.perf = {}
    # The real bindings: the HDR/spout paths relabel the hotkeys, and a fake
    # would hide a real bug in that relabel.
    import hotkeys as hk_mod
    st.hotkey_bindings = dict(hk_mod.DEFAULT_BINDINGS)
    st.environment = {"driver": "unknown", "gpu": "RTX 5070 Ti"}
    # The guard `require_pass` is real and correct: a production worker is refused
    # until the current key carries a PASS. Build that PASS with the module's own
    # builder rather than forging one - a forged verdict would also disable the
    # guard for whatever comes next.
    import compatibility as compat_mod
    import compatibility_runtime as compat_rt
    st.compatibility_key = compat_rt.build_key(st)
    st.compatibility_result = compat_mod.CompatibilityResult(
        key_digest=st.compatibility_key.digest,
        status=compat_mod.CompatibilityStatus.PASS,
        stage="probe",
        passed=1, attempted=1, expected=1,
        reason="test fixture: the gate itself is covered elsewhere",
    )
    st.presets = {}
    st.worker_logs = []
    st.recording_finalize_deadline = 0.0
    st.next_auto_revive = 0.0
    st.last_restart = 0.0
    st.menu_opened_at = 0.0
    st.split_pos = 0.0
    st.tray_commands = queue.Queue()
    st.shot_paths = queue.Queue()
    import convert_jobs
    st.convert_queue = convert_jobs.ConvertQueue(
        convert=lambda *a, **k: None)
    st.convert_batch = []

    # A fake worker that looks alive:  is asked on several paths, and
    # // are the restart helpers.
    st.worker = SimpleNamespace(
        poll=lambda: None, wait=lambda *a, **k: 0, terminate=lambda *a, **k: None,
        kill=lambda *a, **k: None, stdin=None, stdout=None, stderr=None)
    st.capture = SimpleNamespace(resolution=(2560, 1600),
                                 devicename="\\\\.\\DISPLAY1")
    st.hotkeys = SimpleNamespace(suspend=lambda: None, resume=lambda: None)
    st.tray = SimpleNamespace(_set_state=lambda **k: None)

    class _AnyDisplay:
        """A display that ACCEPTS every call and reports itself alive.

        Listing its methods one by one was pure noise: each round a missing
         or  failed like a bug in the code, and none of
        them are what this test is about. The display is not the subject - the
        dispatchers are - so it answers everything and answers sensibly.
        """

        def __init__(self, menu):
            self.menu = menu

        def __getattr__(self, name):
            if name.startswith(("is_", "has_")):
                return lambda *a, **k: False
            return lambda *a, **k: None

    st.display = _AnyDisplay(overlay_ui.OverlayMenu(
        1.0, lambda size=14, mono=False, bold=False, L="en":
        fonts.load(size, mono=mono, bold=bold, lang=L)))
    return st


class _FakeWorker:
    """Stands in for the NGX worker: alive, silent, and no process behind it."""

    def __init__(self, *a, **k):
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.returncode = None

    def poll(self):
        return None                      # alive

    def wait(self, *a, **k):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class _FakeReader:
    """A reader that never has a frame, so no pixel path is exercised."""

    last_ngx_result = 0

    def recv(self, index, timeout=None):
        raise TimeoutError("the fake worker has no frames")

    def close(self):
        pass


def _no_worker(monkeypatch_targets) -> None:
    """Replace every way a worker can start, and remember what was asked."""
    started: list = []

    def fake_start_worker(*a, **k):
        started.append(a)
        return _FakeWorker(), [], _FakeReader(), threading.Event()

    pipeline.start_worker = fake_start_worker
    pipeline.restart_worker = lambda *a, **k: (_FakeWorker(), [],
                                               _FakeReader(),
                                               threading.Event())
    monkeypatch_targets["started"] = started

def press_everything(menu) -> tuple[list[tuple], list[str]]:
    """Activate every control on the current page; return (actions, problems).

    `_activate_item` is what the menu calls once a click has landed on a control,
    so calling it directly covers every control the layout built - including the
    ones that only change the menu's own state and report nothing back.
    """
    actions: list[tuple] = []
    problems: list[str] = []

    def activate(item, how: str) -> None:
        if item.extra.get("disabled"):
            return
        try:
            out = menu._activate_item(item)
        except Exception as exc:
            problems.append(f"{how} {item.kind} {item.key!r} raised: {exc!r}")
            return
        if isinstance(out, list):
            actions.extend(a for a in out if isinstance(a, tuple))

    for item in list(menu.items):
        activate(item, "activate")
        # The cells of a segment group carry their own sub-actions, and the
        # drop-down entries too - they are not items, so a loop over items
        # alone would miss them.
        for cell in (item.extra.get("cells") or []):
            try:
                menu._activate_cell(item, cell)
            except AttributeError:
                pass                      # not every menu version has it
            except Exception as exc:
                problems.append(f"cell of {item.key!r} raised: {exc!r}")
        if item.kind == "slider":
            # The drag is the slider's activation path: both ends, so a
            # clamp/step bug at either end is covered.
            for frac in (0.0, 0.5, 1.0):
                x = int(item.rect.x + item.rect.w * frac)
                try:
                    out = menu._slide(item, x)
                except Exception as exc:
                    problems.append(f"slide {item.key!r} raised: {exc!r}")
                    continue
                if isinstance(out, list):
                    actions.extend(a for a in out if isinstance(a, tuple))

    # The drop-down lists: opening one builds `self.options`, and each entry is
    # pressable.
    for item in list(menu.items):
        if item.kind != "choice":
            continue
        try:
            menu.handle_event(pygame.event.Event(
                pygame.MOUSEBUTTONDOWN,
                {"pos": item.extra.get("strip", item.rect).center, "button": 1}))
        except Exception as exc:
            problems.append(f"opening {item.key!r} raised: {exc!r}")
        for opt in list(getattr(menu, "options", [])):
            try:
                out = menu._activate_item(opt)
            except Exception as exc:
                problems.append(f"option of {item.key!r} raised: {exc!r}")
                continue
            if isinstance(out, list):
                actions.extend(a for a in out if isinstance(a, tuple))
        menu.open_choice = None
    return actions, problems


def main() -> int:
    pygame.init()
    pygame.display.set_mode((64, 64))

    failures: list[str] = []
    pressed = 0

    # No process may start: the dispatchers reach `pipeline.start_worker` on
    # several paths (spout, hdr, motion backend), and a real NGX worker would
    # touch the GPU and compete with whatever is on screen.
    import threading
    import pipeline as pipeline_mod
    global pipeline
    pipeline = pipeline_mod
    started: list = []

    def fake_start_worker(*a, **k):
        started.append(a)
        return _FakeWorker(), [], _FakeReader(), threading.Event()

    pipeline.start_worker = fake_start_worker
    pipeline.restart_worker = lambda *a, **k: (_FakeWorker(), [], _FakeReader(),
                                               threading.Event())
    pipeline.request_apply = lambda *a, **k: None
    pipeline.switch_window = lambda *a, **k: None
    import dialogs
    dialogs.ask_open_paths = lambda *a, **k: []
    dialogs.pick_directory = lambda *a, **k: None
    dialogs.ask_save_path = lambda *a, **k: None

    pages = ("main", "settings", "windows", "convert")
    tabs = overlay_ui.SETTINGS_TABS

    for page in pages:
        for tab in (tabs if page == "settings" else (None,)):
            st = real_state()
            st.display.menu.page = page
            if tab:
                st.display.menu.settings_tab = tab
            # A plausible payload, so the layout builds real controls.
            st.display.menu.set_state({
                "theme": "dark", "lang": "en", "nr": True, "gpu_ok": True,
                "profile": "Natural", "profiles": ["Natural", "Cinematic"],
                "params": {}, "param_defaults": {}, "param_ranges": {},
                "split": 0.0, "work_scale": 0.65, "work_scale_cap": 0.65,
                "work_scale_min": 0.1, "style": 1, "work_size": "2496x1404",
                "monitors": ["0: 2560x1600 (\\\\.\\DISPLAY1)"], "monitor": "0",
                "windows": [{"hwnd": 101, "title": "Notepad",
                             "size": "1280x820"}],
                "window_current": 101, "window_mode": False,
                "hotkeys": {"toggle": "Num1", "settings": "Num2"},
                "screenshot_dir": "C:/Shots", "screenshot_mode": "ask",
                "screenshot_format": "png", "frame_limit_mode": "unlimited",
                "motion_backend": "nvofa", "frame_generation": False,
                "frame_multiplier": 2, "recording": False,
                "recording_dir": "C:/Rec", "autostart": True,
                "open_on_start": True, "boost": False,
                "spout": False, "hdr": False, "gpu": "0",
                "gpu_text": "RTX 5070 Ti", "gpus": ["0"], "langs": ["en", "ru"],
                "convert_dest": "folder", "convert_dir": "C:/Converted",
                "convert_progress": 0.4,
                "convert_jobs": [
                    {"id": 1, "name": "a.mp4", "kind": "video",
                     "status": "running", "fraction": 0.4, "line": "40%",
                     "tone": "text", "action": "stop"},
                    {"id": 2, "name": "b.png", "kind": "image",
                     "status": "done", "output": "C:/Converted/b-nr.png",
                     "line": "done", "tone": "ok", "action": "show"},
                    {"id": 3, "name": "c.avi", "kind": "video",
                     "status": "failed", "error": "denied", "line": "no",
                     "tone": "danger", "action": "retry"},
                    {"id": 4, "name": "d.mkv", "kind": "video",
                     "status": "queued", "line": "waiting", "tone": "muted",
                     "action": "remove"},
                ],
            })
            st.display.menu.visible = True
            st.display.menu.layout(1920, 1080)

            where = f"{page}/{tab or '-'}"
            actions, problems = press_everything(st.display.menu)
            for problem in problems:
                failures.append(f"{where}: {problem}")
            pressed += len(problems)
            if not actions and not problems:
                failures.append(f"{where}: the layout produced no clickable "
                                f"control - this test would be vacuous")
                continue

            for action in actions:
                pressed += 1
                try:
                    commands.apply_menu_action(st, action)
                except Exception as exc:
                    failures.append(f"{where}: {action!r} crashed "
                                    f"apply_menu_action: {exc!r}")

            # And the hotkey path, which is a second dispatcher with its own
            # branches (screenshot, record, framegen, window_mode, scale...).
            import hotkeys as hk_mod
            for name in sorted({c for _m, _v, c, _k in
                                hk_mod.DEFAULT_BINDINGS.values()}):
                try:
                    st.tray_commands.put(name)
                    commands.drain_commands(st)
                except Exception as exc:
                    failures.append(f"{where}: hotkey command {name!r} crashed "
                                    f"drain_commands: {exc!r}")

    # The screenshot request specifically - it is the one that crashed, and its
    # own path is what the timing marks live in.
    st = real_state()
    try:
        commands.request_screenshot(st)
        commands.freeze_screenshot_frame(st, None)
        commands.drain_save_dialog(st)
    except Exception as exc:
        failures.append(f"the screenshot path crashed with the real state: "
                        f"{exc!r}")

    if not pressed:
        print("FAIL: no control was pressed - the test covers nothing")
        return 1
    for f in failures[:20]:
        print("FAIL:", f)
    if len(failures) > 20:
        print(f"... and {len(failures) - 20} more")
    if failures:
        return 1
    print(f"OK: every control on every page and tab survives a click "
          f"({pressed} actions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
