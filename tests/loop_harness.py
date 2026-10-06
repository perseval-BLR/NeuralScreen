"""Run main.main()'s real frame loop against fakes, for the lifecycle tests.

The frame loop is one long function in main.py, and most of what goes wrong
in it goes wrong BETWEEN its steps: a rebuild in the middle of an iteration,
a command that eats the frame watchdog, a failure that escapes to main()'s
last handler and closes the program. Those are only visible by running the
loop itself, so this runs it - with no worker, no window, no capture and no
GPU. Everything the loop talks to is a fake that records what it was asked:

* the worker is an object whose poll() says it is alive, and `send_frame`
  records each frame together with the reader it went to;
* the reader answers recv() at once with no pixels (the worker presented the
  frame), unless a test hands it a script;
* the channels are opened by fakes that set the same flags the real ones do;
* the display, the tray, the hotkeys and the taskbar accept every call.

A test passes `on_pass(st, n)`, called at the top of every pass (from the
fake drain_commands, which is where the loop asks for commands); returning
False ends the run the way the user's quit does. `run()` returns main()'s
exit code, the state, and what the program printed.

Not a test - a helper the tests import (like worker_reply.py).
"""
from __future__ import annotations

import contextlib
import ctypes
import io
import sys
import types
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import channels  # noqa: E402
import commands  # noqa: E402
import compatibility_runtime  # noqa: E402
import foreign_hooks  # noqa: E402
import main as main_mod  # noqa: E402
import pipeline  # noqa: E402
import power  # noqa: E402
import protocol  # noqa: E402
import settings_io  # noqa: E402
import startup  # noqa: E402


class Anything:
    """Accepts every call and records its name; answers None."""

    def __init__(self, name: str = "fake", log: list | None = None):
        self._name = name
        self._log = log if log is not None else []

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)

        def call(*args, **kwargs):
            self._log.append(f"{self._name}.{attr}")
            return None
        return call


class FakeDisplay(Anything):
    """The overlay: every call accepted, the few answers the loop reads."""

    def __init__(self, log: list):
        super().__init__("display", log)
        self.menu = types.SimpleNamespace(
            visible=False, dragging=False, offset=[0, 0], user_scale=1.0,
            user_height=None, handle_event=lambda ev: [],
            set_state=lambda state: None)
        self.switch_active = False

    def is_switch_active(self):
        return self.switch_active

    def enter_switch_mode(self, *a, **k):
        self._log.append("display.enter_switch_mode")
        self.switch_active = True

    def exit_switch_mode(self):
        self._log.append("display.exit_switch_mode")
        self.switch_active = False

    def drop_switch_mode(self):
        self._log.append("display.drop_switch_mode")
        self.switch_active = False

    def is_visible(self):
        return True

    def get_hwnd(self):
        return 1


class FakeReader:
    """The worker's replies: every frame answered at once, no pixels."""

    def __init__(self, name: str = "reader"):
        self.name = name
        self.last_ngx_result = 1
        self.last_scene = None
        self.last_scene_cut = False
        self.recv_calls = 0
        # recv(index, timeout) -> pixels; a test replaces it to script replies.
        self.answer = lambda index, timeout: None

    def recv(self, index, timeout):
        self.recv_calls += 1
        return self.answer(index, timeout)


class FakeGuides:
    def __init__(self, w: int = 64, h: int = 36):
        self.w, self.h = w, h
        self.previous_gray = None
        self.processed = []          # what process() was given
        self.zeroed = 0
        self.emit_small = False

    def _frame(self, reset: bool):
        return types.SimpleNamespace(
            motion=np.zeros((self.h, self.w, 2), np.float16), reset=reset,
            scene_score=0.0)

    def process(self, rgba=None, gray=None, compute_motion=True):
        self.processed.append("gray" if gray is not None else
                              ("frame" if rgba is not None else "none"))
        return self._frame(False)

    def zero_guide(self):
        self.zeroed += 1
        return self._frame(True)

    def handoff(self):
        return self._frame(False)

    def forget(self):
        self.previous_gray = None


class FakeCapture:
    def __init__(self, w: int, h: int):
        self.resolution = (w, h)
        self.devicename = r"\\.\DISPLAY1"
        self.monitor_idx = 0
        self.grabs = 0
        # grab() -> frame or None; a test replaces it.
        self.frame = lambda: np.zeros((h, w, 4), np.uint8)

    def grab(self):
        self.grabs += 1
        return self.frame()

    def close(self):
        pass


class FakeWorker:
    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.code = None

    def poll(self):
        return self.code


def make_state_filler(width=640, height=360, **overrides):
    """startup.configure + open_capture + bring_up, with fakes for everything."""

    def configure(st):
        log = []
        st.cfg = {"profile": "Natural", "frame_generation": False,
                  "frame_multiplier": 2, "fullscreen": False, "lang": "en",
                  "motion_backend": "dis", "flow_preset": "fast",
                  "fps_overlay": "off", "rec_indicator": True}
        st.params = {"style": 0, "auto_mask": 1, "intensity": 1.0,
                     "local_tone": 0.0, "local_structure": 1.0,
                     "skin_structure": -1.0}
        st.lang = "en"
        st.width, st.height = width, height
        st.mon_w, st.mon_h = width, height
        st.mon_origin = (0, 0)
        st.monitor = 0
        st.work_scale = 1.0
        st.work_w, st.work_h = width, height
        st.warmup = st.effective_warmup = 4
        st.nr_passes = 1
        st.nr_passes_pid = None
        st.nr_small = False
        st.nr_direct = False
        st.capture = FakeCapture(width, height)
        st.display = FakeDisplay(log)
        st.tray = Anything("tray", log)
        st.hotkeys = Anything("hotkeys", log)
        st.taskbar = Anything("taskbar", log)
        st.hotkey_bindings = {}
        st.shm = Anything("shm", log)
        st.worker = FakeWorker()
        st.worker_logs = []
        st.reader = FakeReader("first")
        st.worker_stop = None
        st.guides = FakeGuides()
        st.buf_full = np.empty((height, width, 4), np.uint8)
        st.convert_queue = None
        st.recorder = None
        st.recording_finalizer = None
        st.startup_menu = False
        st.split_pos = 0.0
        st.paused = False
        st.off_suspended = False
        st.worker_failed = False
        st.frame_index = 0
        st.pts = 0
        st.output_rgba = None
        st.want_present = True
        st.want_motion_small = False
        st.want_dda = True
        st.want_out_shm = False
        st.out_shm = st.out_attempted = False
        st.motion_small = st.motion_attempted = False
        st.present_mode = st.present_attempted = False
        st.dda_mode = st.dda_attempted = False
        st.gray_active = False
        st.window_hwnd = None
        st.last_foreground = 0
        st.follow_pos = st.follow_resize = st.follow_size = None
        st.mon_resize = None
        st.pending_shot = None
        st.shot_rgba = None
        st.shot_dialog_open = False
        st.work_frame = None
        st.running = True
        st.last_restart = 0.0
        st.pending_apply = None
        st.pending_apply_due = 0.0
        st.next_auto_revive = 0.0
        st.consecutive_restarts = 0
        st.guide_fails = 0
        st.gpu_ok = True
        st.nr_idle_streak = 0
        st.nr_not_evaluating = False
        st.recording_finalize_deadline = 0.0
        st.last_recording = {}
        st.compatibility_key = None
        st.compatibility_result = None
        for name, value in overrides.items():
            setattr(st, name, value)

    return configure


class Patches:
    """setattr with a record, undone in reverse."""

    def __init__(self):
        self._saved = []

    def set(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        while self._saved:
            obj, name, value = self._saved.pop()
            setattr(obj, name, value)


def default_channels(events: list, patches: Patches) -> None:
    """Fake channel negotiation: the flags the real ones set, and a record."""

    def present(st):
        st.present_attempted = True
        st.present_mode = True
        events.append(("present", st.reader))

    def dda(st):
        st.dda_attempted = True
        st.dda_mode = True
        st.gray_active = True
        events.append(("dda", st.reader))

    def wgc(st):
        st.dda_attempted = True
        st.dda_mode = True
        st.gray_active = True
        events.append(("wgc", st.reader))
        return True

    def motion(st):
        st.motion_attempted = True
        events.append(("motion", st.reader))

    def out(st):
        st.out_attempted = True
        events.append(("out", st.reader))

    patches.set(channels, "enable_present", present)
    patches.set(channels, "enable_dda", dda)
    patches.set(channels, "enable_wgc", wgc)
    patches.set(channels, "sync_motion_size", motion)
    patches.set(channels, "enable_out_shm", out)
    patches.set(channels, "sync_gray", lambda st: None)


def run(on_pass, *, state=None, patch=None, max_passes: int = 2000,
        events: list | None = None):
    """Run main.main() until on_pass returns False (or max_passes).

    state: overrides for the fake state (dict). patch(patches, events) may
    install more fakes before the loop starts. Returns (rc, st, log, events).
    """
    events = events if events is not None else []
    patches = Patches()
    captured = {}
    filler = make_state_filler(**(state or {}))

    def configure(st):
        filler(st)
        captured["st"] = st

    passes = {"n": 0}

    def drain_commands(st):
        passes["n"] += 1
        if passes["n"] > max_passes:
            return False
        return on_pass(st, passes["n"])

    def send_frame(worker, index, rgba, motion, reset, pts, shm=None, **kw):
        events.append(("send", captured["st"].reader,
                       {"index": index, "rgba": rgba, "reset": reset, **kw}))

    kernel32 = types.SimpleNamespace(CreateMutexW=lambda *a: 1,
                                     GetLastError=lambda: 0)
    user32 = types.SimpleNamespace(IsWindow=lambda h: 1,
                                   GetTopWindow=lambda h: 0)
    fake_ctypes = types.SimpleNamespace(
        windll=types.SimpleNamespace(kernel32=kernel32, user32=user32),
        c_void_p=ctypes.c_void_p)
    pumps = {"n": 0}

    def pump():
        pumps["n"] += 1
    fake_pygame = types.SimpleNamespace(
        event=types.SimpleNamespace(get=lambda: [], pump=pump),
        image=types.SimpleNamespace(frombuffer=lambda *a: None))

    patches.set(sys, "argv", ["main.py", "--config", str(BASE / "config.json")])
    patches.set(main_mod, "_init_logging", lambda: None)
    patches.set(main_mod, "ctypes", fake_ctypes)
    patches.set(main_mod, "pygame", fake_pygame)
    patches.set(main_mod, "foreign_foreground", lambda: 0)
    patches.set(main_mod, "send_frame", send_frame)
    patches.set(main_mod, "prepare_capture", lambda *a, **k: None)
    patches.set(main_mod, "shutdown_worker", lambda *a, **k: None)
    patches.set(pipeline, "shutdown_worker", lambda *a, **k: None)
    patches.set(startup, "configure", configure)
    patches.set(startup, "open_capture", lambda st: None)
    patches.set(startup, "bring_up", lambda st: None)
    patches.set(compatibility_runtime, "startup_gate", lambda st: True)
    patches.set(commands, "drain_commands", drain_commands)
    patches.set(commands, "drain_save_dialog", lambda st: None)
    patches.set(commands, "service_conversions", lambda st: None)
    patches.set(commands, "poll_recording_finalizer", lambda st: None)
    for name in ("refresh_gpu_ok", "refresh_fg_ok", "warn_hdr",
                 "save_menu_layout"):
        patches.set(settings_io, name, lambda st: None)
    patches.set(settings_io, "menu_payload", lambda st: {})
    patches.set(settings_io, "_fg_displayed_fps", lambda st: None)
    patches.set(settings_io, "_fg_active_multiplier", lambda st: None)
    patches.set(settings_io, "frame_limit_fps", lambda cfg: 0)
    patches.set(power, "apply_both", lambda st: events.append(("power",)))
    patches.set(foreign_hooks, "check", lambda st: None)
    patches.set(pipeline, "follow_monitor", lambda st: None)
    patches.set(pipeline, "follow_window", lambda st: None)
    default_channels(events, patches)
    if patch is not None:
        patch(patches, events)

    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                rc = main_mod.main()
            except SystemExit as exc:  # argparse, or a test that exits
                rc = exc.code
    finally:
        patches.undo()
        # A test may have left an idle hook installed through the program.
        setter = getattr(protocol, "set_idle_hook", None)
        if setter is not None:
            setter(None)
    st = captured.get("st")
    return rc, st, out.getvalue(), {"events": events, "passes": passes["n"],
                                    "pumps": pumps["n"]}
