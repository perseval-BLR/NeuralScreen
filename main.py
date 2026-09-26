"""DLSS 5 Desktop NR - the integration skeleton of the prototype.

The loop: desktop capture (capture.ScreenCapture) -> motion guides
(guides.TemporalGuideGenerator) -> the NGX worker (native/nvngx.dll in
--live mode) -> fullscreen output (display.Display).

Controls (global hotkeys, RegisterHotKey + a polling fallback - see
hotkeys.py). Num Lock must be on: the numpad sends Insert/End/arrows
without it:
    Num1          - NR on/off
    Num2          - the settings menu
    Num3          - screenshot
    Num0          - recording
    Num4 / Num6   - processing resolution down / up
    Num5          - process one window instead of the whole screen
    Ctrl+Alt+Q    - quit (the same as "Exit" in the tray)

Every key can be reassigned in the settings menu (config.json
"hotkeys").

Run:
    python main.py [--config config.json]
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path


def _startup_failure(kind, value, tb) -> None:
    """An import of ours failed before logging exists: say so, somewhere.

    The heavy imports below (cv2, numpy, pygame, every module of the program)
    run before _init_logging and outside main()'s own error box. Under
    pythonw - the .exe and .vbs launchers - a missing runtime package, a
    half-finished unpack or a NEURALSCREEN_PYTHON pointing at a plain Python
    showed nothing at all and wrote nothing anywhere. Installed only for the
    imports of a real launch, and put back once they are done.
    """
    import traceback
    text = "".join(traceback.format_exception(kind, value, tb))
    try:
        log = Path(__file__).resolve().parent / "NeuralScreen.log"
        with open(log, "a", encoding="utf-8") as handle:
            handle.write("\n[startup] NeuralScreen could not start:\n" + text)
    except OSError:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(
            None, f"NeuralScreen could not start:\n{value}\n\n"
                  f"Details in NeuralScreen.log next to the program.",
            "NeuralScreen", 0x10)
    except Exception:
        pass
    sys.__excepthook__(kind, value, tb)


def _drop_stale_bytecode(base: Path) -> None:
    """Throw the compiled-module cache away once per release installed over another.

    Every file in the release zip is dated 1980-01-01, and Python reuses a
    .pyc as long as the source's date and size match what it was compiled
    from. Unpacked over the previous release, every module that kept its size
    went on running the OLD code: settings_io.py is 73605 bytes in 2.1.0 and in
    2.1.1 (only APP_VERSION changed), so 2.1.1 on top of 2.1.0 still called
    itself 2.1.0 - and the compatibility cache, keyed by the version, reused
    2.1.0's verdict. A release carries VERSION.txt, which names its commit;
    when it differs from the one the cache was built under, the cache goes,
    before any module of ours is imported. A development tree has no
    VERSION.txt, and nothing happens there.

    Both caches: the modules' own in app/, and the root's (an install from
    before the modules moved to app/ left its compiled copies there).
    """
    try:
        stamp = (base / "VERSION.txt").read_bytes()
    except OSError:
        return
    caches = (base / "app" / "__pycache__", base / "__pycache__")
    marker = caches[0] / "release.stamp"
    try:
        if marker.read_bytes() == stamp:
            return
    except OSError:
        pass
    import shutil
    for cache in caches:
        shutil.rmtree(cache, ignore_errors=True)
    try:
        caches[0].mkdir(parents=True, exist_ok=True)
        marker.write_bytes(stamp)
    except OSError:
        pass


if __name__ == "__main__":
    sys.excepthook = _startup_failure
    _drop_stale_bytecode(Path(__file__).resolve().parent)


# DPI awareness BEFORE any import (cv2, capture, display, tray): if some
# module sets awareness first (dxcam, for instance, calls
# SetProcessDpiAwareness(2) when creating an Output), a second call returns
# ERROR_ACCESS_DENIED and the pygame window ends up scaled (125% ->
# 3072x1728). PER_MONITOR_AWARE_V2 = -4. Errors are ignored: display.py
# repeats the call.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# Embedded Python (python313._pth) does not add cwd to sys.path - we add the
# program's folders by hand: app/ holds the modules (capture, display,
# guides...), the root holds this entry point and the release tools.
_BASE = Path(__file__).resolve().parent
sys.path[:0] = [str(_BASE / "app"), str(_BASE)]

import cv2
import numpy as np
import pygame  # HUD overlay on the recorded frame (image.frombuffer)

from capture import ScreenCapture
from display import Display
from motion_backend import MotionBackendStatus
from i18n import STRINGS as UI_STRINGS

import channels
import commands
import compatibility_runtime
import startup
import settings_io
import pipeline
from pacing import FramePacer, FrameRateMeter
# The restart cooldown is the loop's business too: it is what the
# deferred apply waits for.
from pipeline import (AUTO_REVIVE_BACKOFF,  # noqa: F401
                      MAX_CONSECUTIVE_RESTARTS, RESTART_COOLDOWN)
# The pieces below live in their own modules now; re-exported because
# the rest of the program and the tests look them up in main.
from paths import BASE_DIR, NATIVE_DIR, WORKER_EXE  # noqa: F401
from pipeline import (restart_worker,  # noqa: F401
                      shutdown_worker, start_worker)
from startup import (_apply_nr_dll, _apply_spout_env, _init_logging,
                     _log_environment)
from settings_io import (load_config, load_presets,  # noqa: F401
                         resolve_params)
from settings_io import hotkey_labels  # noqa: F401
# The settings layer owns these now; re-exported because the rest
# of the program and the tests look them up in main.
from settings_io import (  # noqa: F401
    CHANNEL_URL, PRESET_NAME_PREFIX, REPO_URL, WORK_SCALE_MAX,
    WORK_SCALE_STEP, _next_preset_name, _set_autostart)
from settings_io import (  # noqa: F401
    APP_VERSION, CHANNEL_LABEL, PROFILES, WORK_MAX_W, WORK_MAX_H, WORK_SCALE_MIN, _atomic_write_json, _autostart_enabled, _menu_layout_payload)
# The Win32 window helpers live in winapi.py now. They are re-exported here
# on purpose: main is where the rest of the program - and the tests - look
# them up, and moving code must not move its callers.
from winapi import (_is_desktop_window, _is_our_window, _is_taskbar_window,
                    foreign_foreground, list_capturable_windows,
                    window_frame_rect, window_under_cursor)

# The worker protocol lives in protocol.py now - the magics, the formats, the
# senders and the reader thread. Re-exported here because main is where the
# rest of the program and the tests look them up, and because moving code
# must not move its callers.
# Named one by one rather than with a star: a star import would drag
# protocol's own imports into this namespace too.
from protocol import (  # noqa: F401
    CREATE_ACK_FMT, CREATE_ACK_MAGIC, CREATE_CATEGORY_FAILED,
    CREATE_CATEGORY_NONE, CREATE_CATEGORY_UNSUPPORTED,
    DDA_ACK_FMT, DDA_ACK_MAGIC, DDA_FMT, DDA_MAGIC, FRAME_FLAG_BYPASS,
    FRAME_FLAG_MOTION_SMALL, FRAME_FLAG_NO_COLOR, FRAME_FLAG_SHM,
    FRAME_FLAG_SPLIT, FRAME_FLAG_WANT_PIXELS,
    FRAME_FMT, FRAME_MAGIC,
    GRAY_ACK_FMT, GRAY_ACK_MAGIC, GRAY_FMT, GRAY_MAGIC, HEADER_FMT,
    MOTION_ACK_FMT, MOTION_ACK_MAGIC, MOTION_FMT, MOTION_MAGIC,
    OUTS_ACK_FMT, OUTS_ACK_MAGIC, OUTS_FMT, OUTS_MAGIC, OUT_BYTES_IN_SHM,
    AUDIO_RING_FMT, REC_DONE_FMT, REC_START_ACK_FMT, REC_START_FMT,
    REC_STOP_FMT,
    FrameReply, OUT_FMT, OUT_MAGIC, OUT_STATUS_OK, RACK_FMT,
    RESIZE_ACK_MAGIC, RESIZE_FLAG_NR_SMALL,
    RESIZE_FMT, RESIZE_MAGIC, SHM_ACK_FMT, SHM_ACK_MAGIC, SHM_FMT,
    SHM_MAGIC, VIDEO_MAGIC, WGC_ACK_FMT, WGC_ACK_MAGIC, WGC_FMT,
    WGC_MAGIC, WINDOW_ACK_FMT, WINDOW_ACK_MAGIC, WINDOW_FLAG_CAPTURABLE,
    WINDOW_FLAG_DISABLE, WINDOW_FMT, WINDOW_MAGIC, WorkerReader,
    _read_exact, prepare_capture, send_dda, send_frame, send_gray, send_motion_size,
    send_out, send_resize, send_wgc, send_window)

# The imports are done: from here on main()'s own handler reports failures.
if sys.excepthook is _startup_failure:
    sys.excepthook = sys.__excepthook__




#: Ask the worker to answer each frame as soon as it is queued on the GPU
#: (FRAME_FLAG_EARLY_REPLY) instead of once it is on screen, so this loop's own
#: work - the HUD, the commands, the next capture request - runs while the GPU
#: finishes the frame rather than between frames. NS_EARLY_REPLY=0 turns it
#: off, for comparing the two or ruling it out in a report.
EARLY_REPLY = os.environ.get("NS_EARLY_REPLY", "1") != "0"

#: Leave the scene cut to the worker wherever this loop has no other use for
#: the capture (FRAME_FLAG_WORKER_SCENE): with NVOFA, and with NR off. The
#: capture round trip before each frame (CAP1 -> the gray -> FRM1) then goes -
#: it left the GPU idle ~2 ms a frame. The CPU optical flow still needs the
#: gray first and keeps it. NS_WORKER_SCENE=0 turns it off.
WORKER_SCENE = os.environ.get("NS_WORKER_SCENE", "1") != "0"

#: How many consecutive frames without an NGX evaluation before the interface
#: stops claiming the picture is processed. Ten frames is a fraction of a second
#: at any rate the pass runs at, and long enough that a single skipped slot or a
#: stall reset cannot trip it.
NR_IDLE_STREAK_LIMIT = 10

FPS_LOG_INTERVAL = 2.0  # seconds, FPS log to the console
PERF_LOG_INTERVAL = 5.0  # seconds, log of the mean pipeline stage timings
PERF_KEYS = ("grab", "resize_full", "guides", "send", "recv", "show")
MAX_CONSECUTIVE_CAPTURE_FAILURES = 3
CAPTURE_RETRY_BACKOFF = 30.0
CAPTURE_FAILURE_IDLE = 0.05


def _next_capture_failure(failures: int, now: float) -> tuple[int, float]:
    """Advance the bounded capture-recovery state.

    A zero deadline means another immediate recreation is allowed. A nonzero
    deadline quarantines the capture until that monotonic time.
    """
    failures += 1
    if failures >= MAX_CONSECUTIVE_CAPTURE_FAILURES:
        return 0, now + CAPTURE_RETRY_BACKOFF
    return failures, 0.0


def _resize_interp(src: np.ndarray, dst_w: int, dst_h: int) -> int:
    """Which cv2 filter the fallback resize uses.

    Only the fallback path resizes at all: with the capture in the worker
    (DDA1/WGCW) the GPU hands over a frame that is already the right size.
    This runs when dxcam is doing the grabbing and its frame disagrees with
    the configured size - a hybrid laptop on the iGPU display, or a display
    mode change caught in flight.

    It used to be INTER_LANCZOS4 in either direction, which measured 11.76 ms
    for 2560x1440 -> 4K against 1.53 ms for INTER_AREA and 1.64 ms for
    INTER_LINEAR: ten milliseconds of the frame budget on the one path that
    exists BECAUSE the fast path was unavailable - i.e. on the slowest
    hardware in the fleet.

    AREA when shrinking, LINEAR when growing: AREA is a box filter and
    degenerates towards nearest neighbour on an upscale, while LINEAR aliases
    on a large downscale, and guides.py says what aliasing does to the flow
    field. A Lanczos kernel's extra sharpness was never going to survive NGX
    resampling the frame again anyway.
    """
    if dst_w * dst_h < src.shape[1] * src.shape[0]:
        return cv2.INTER_AREA
    return cv2.INTER_LINEAR
















            # A reply for a frame main no longer waits for (after a timeout) - skip


def check_worker(worker: subprocess.Popen, logs: list[str]) -> None:
    """If the worker died - print the last stderr lines and raise."""
    code = worker.poll()
    if code is not None:
        tail = "\n".join(logs[-40:]) or "(stderr empty)"
        raise RuntimeError(
            f"the NGX worker exited with code {code}.\n"
            f"last stderr lines:\n{tail}"
        )


def _hard_failure(logs: list[str]) -> bool:
    """Whether the worker's tail shows a HARD failure - 0xBAD00001.

    FeatureNotSupported (0xBAD00001) means the GPU cannot run the neural
    pass at all (Turing, a broken runtime build): no auto-recovery will
    ever clear it, and retrying only spins the restart loop. Transient
    failures (0x00000000 no-frame, timeouts, driver hiccups) can clear
    on their own - those are the ones worth an automatic revive.

    The whole log is scanned, newest line first, and the first decisive one
    wins. It used to be the last forty lines: the worker says this once, on
    frame 0, and then keeps running and keeps logging, so forty later
    diagnostics buried the verdict and a permanently broken card started
    reading as a transient failure - the restart storm this classifier
    exists to prevent (audit).

    Reading newest-first is what keeps the tail window's one good property:
    if the feature DID come up after the refusal (NGX is reinitialised in
    place after repeated failures), the success is the newer line and it
    wins. An unconditional "0xBAD00001 anywhere" would have lost that.

    The list is per worker process - a restart hands out a fresh one - and
    capped at 2000 lines, so "the whole log" is bounded and cannot carry a
    verdict across a restart that might have cleared it. This runs when a
    worker dies, not per frame.
    """
    for line in reversed(logs):
        if "0xBAD00001" in line:
            return True
        if "feature 18 ready" in line:
            return False
    return False




class _Pipeline:
    """Everything main() rebinds while the program runs.

    These 56 names used to be locals of main() reached through 39
    `nonlocal` statements and 1041 references: every nested function
    could rebind any of them, and nothing said which part of the pipeline
    owned what. They are fields of one object now, so a function that takes
    `st` declares by that alone that it touches the pipeline, and the reader
    can see where a value comes from.

    __slots__ is the point, not an optimisation: a typo in a field name
    raises AttributeError here instead of quietly creating a new attribute
    that nothing ever reads.
    """

    __slots__ = (
        "buf_full",
        "capture",
        "cfg",
        "consecutive_restarts",
        "dda_attempted",
        "dda_mode",
        "display",
        "follow_pos",
        "follow_resize",
        "follow_size",
        "mon_resize",
        #: The devicename whose absence has already been reported, so a
        #: monitor that stays gone does not repeat the line every 30 frames
        #: (#128). Cleared as soon as the monitor answers again.
        "mon_gone",
        "environment",
        "frame_index",
        "gpu_ok",
        "gpu_alerted",
        "gpu_switch_pending",
        "fg_alerted",
        "gray_active",
        "mon_origin",
        "guide_fails",
        "guides",
        "height",
        "hotkey_bindings",
        "hotkeys",
        # #93: the program is in the tray - the taskbar button is hidden and
        # the tray icon is the way back. Declared here because __slots__ turns
        # an undeclared field into an AttributeError at run time, which is its
        # purpose.
        "in_tray",
        "lang",
        "last_foreground",
        "window_list",
        "last_restart",
        "menu_opened_at",
        "mon_h",
        "mon_w",
        "monitor",
        "monitor_devicename",
        "motion_attempted",
        "motion_small",
        "next_auto_revive",
        "nr_direct",
        "nr_passes",
        "convert_busy",
        "convert_status",
        # The conversion queue (convert_jobs), the finished jobs waiting
        # for one combined alert, and the running file's fraction for
        # the main page's Convert cell.
        "convert_queue",
        "convert_batch",
        "convert_progress",
        # Which worker has been told the pass count: the cascade has to be
        # re-sent to every new one (see the main loop).
        "nr_passes_pid",
        #: What passes 2..N use, when the user gave them their own set. None
        #: by default, which means "the main set for every pass".
        "nr_pass_params",
        "nr_small",
        # The "NR ON but nothing is being processed" verdict: a streak of frames
        # the worker answered without evaluating, and the flag the HUD reads.
        "nr_idle_streak",
        "nr_not_evaluating",
        "out_attempted",
        "out_shm",
        "off_suspended",
        "output_rgba",
        "params",
        "paused",
        "pending_apply",
        #: When the queued apply is due: set on every request, so a drag ends
        #: with ONE rebuild instead of one per step (#115).
        "pending_apply_due",
        "pending_shot",
        "shot_rgba",
        # The screenshot timing marks. Declared here because __slots__ turns an
        # undeclared field into an AttributeError at run time - which is its
        # purpose, and which is how the missing declaration crashed the app on
        # the first Screenshot click.
        "shot_requested_at",
        "shot_dialog_started_at",
        "perf",
        "present_attempted",
        "present_mode",
        "presets",
        "pts",
        "reader",
        "record_audio",
        "recorder",
        "recording_finalizer",
        "recording_finalize_deadline",
        "last_recording",
        "running",
        "shm",
        "shot_dialog_open",
        "shot_paths",
        "split_pos",
        "taskbar",
        "startup_menu",
        "tray",
        "tray_commands",
        "width",
        "window_hwnd",
        "work_frame",
        "work_h",
        "work_scale",
        "work_w",
        "worker",
        "worker_failed",
        "worker_logs",
        "worker_stop",
        "want_dda",
        "want_motion_small",
        "want_out_shm",
        "want_present",
        "cfg_path",
        "gpu_text",
        "warmup",
        "effective_warmup",
        "hdr_alerted",
        "compatibility_key",
        "compatibility_result",
    )


def main() -> int:
    # The pipeline's mutable state (see _Pipeline): one object
    # instead of 56 closure variables.
    st = _Pipeline()
    parser = argparse.ArgumentParser(description="DLSS 5 Desktop NR prototype")
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.json",
                        help="path to config.json (defaults to next to main.py)")
    args = parser.parse_args()
    _init_logging()  # pythonw: stdout/stderr -> NeuralScreen.log

    # One instance only: two copies fight over the screen capture (the
    # second one gets a dead DDA and the first one loses frames). The
    # mutex is the standard Windows single-instance mechanism - it lives
    # in the kernel and dies with the process, so a crashed copy does not
    # block the next launch.
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "NeuralScreen_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        # The running copy is asked to show itself: this one used to exit
        # with only a log line, so a double-click on the program while it sat
        # in the tray - or on a new version while the old one ran - did
        # nothing anyone could see. Posted, not a message box: a launch from
        # a script must not block on a dialog.
        print("[main] another NeuralScreen is already running - this copy "
              "asks it to show its menu and exits", file=sys.stderr)
        try:
            from taskbar import WM_NS_SHOW, WM_NS_SHOW_LEGACY
            user32 = ctypes.windll.user32
            user32.FindWindowW.restype = ctypes.c_void_p
            running = user32.FindWindowW("NeuralScreenTaskbar", "NeuralScreen")
            if running:
                # Both numbers: the running copy may be an older one, which
                # listens only for the legacy one (a new copy ignores it).
                for message in (WM_NS_SHOW, WM_NS_SHOW_LEGACY):
                    user32.PostMessageW(ctypes.c_void_p(running), message, 0, 0)
        except Exception:
            pass
        return 1

    # The config file's path, kept in the state: the settings module writes
    # back into it and has no business knowing what argparse is.
    st.cfg_path = args.config
    startup.configure(st)
    # The compatibility worker receives synthetic RGBA frames only.  Desktop
    # capture and the presentation window are deliberately created after an
    # exact N/N PASS (or a cached PASS for the same key).
    if not compatibility_runtime.startup_gate(st):
        print("[main] compatibility preflight cancelled - capture was not opened")
        return 2
    try:
        startup.open_capture(st)
        startup.bring_up(st)

        capture_failures = 0
        capture_retry_at = 0.0
        capture_owner = st.capture

        def _recreate_capture() -> None:
            """Recreate the capture (a fresh DDA session) after a failure or mode change."""
            nonlocal capture_owner
            try:
                st.capture.close()
            except Exception:
                pass
            st.capture = ScreenCapture(monitor_idx=st.monitor)
            capture_owner = st.capture

        def _safe_grab() -> np.ndarray | None:
            """Grab with a bounded recreation budget and quarantine backoff.

            Launching a game in fullscreen invalidates Desktop Duplication
            (DXGI_ERROR_ACCESS_LOST / a mode change) - dxcam may raise instead
            of returning None. Two fresh sessions are tried immediately. A
            persistent failure then gets one probe per backoff instead of an
            allocation/logging loop that can exhaust memory. A monitor or
            window switch replaces the capture object and clears the gate.
            """
            nonlocal capture_failures, capture_retry_at, capture_owner
            now = time.monotonic()
            if st.capture is not capture_owner:
                capture_owner = st.capture
                capture_failures = 0
                capture_retry_at = 0.0

            probing = capture_retry_at > 0.0
            if probing:
                if now < capture_retry_at:
                    time.sleep(min(CAPTURE_FAILURE_IDLE, capture_retry_at - now))
                    return None
                print("[main] retrying capture after the recovery backoff")
                try:
                    _recreate_capture()
                except Exception as exc:
                    capture_retry_at = now + CAPTURE_RETRY_BACKOFF
                    print(f"[main] capture retry failed ({exc}) - next probe in "
                          f"{CAPTURE_RETRY_BACKOFF:.0f}s", file=sys.stderr)
                    time.sleep(CAPTURE_FAILURE_IDLE)
                    return None
                capture_retry_at = 0.0
                # A probe gets one real grab, not another burst of immediate
                # recreations when the underlying failure is still present.
                capture_failures = MAX_CONSECUTIVE_CAPTURE_FAILURES - 1

            try:
                frame = st.capture.grab()
            except Exception as exc:
                capture_failures, blocked_until = _next_capture_failure(
                    capture_failures, now)
                if blocked_until:
                    capture_retry_at = blocked_until
                    try:
                        st.capture.close()
                    except Exception:
                        pass
                    print(f"[main] capture failed repeatedly ({exc}) - paused for "
                          f"{CAPTURE_RETRY_BACKOFF:.0f}s", file=sys.stderr)
                else:
                    print(f"[main] capture failed ({exc}) - recreating the session "
                          f"({capture_failures}/{MAX_CONSECUTIVE_CAPTURE_FAILURES})")
                    try:
                        _recreate_capture()
                    except Exception as exc2:
                        print(f"[main] recreating the capture failed: {exc2}",
                              file=sys.stderr)
                time.sleep(CAPTURE_FAILURE_IDLE)
                return None
            if frame is not None:
                if capture_failures or probing:
                    print("[main] capture recovered")
                capture_failures = 0
                capture_retry_at = 0.0
            return frame


        # The loop's own state, next to the loop that owns it.
        guide = None  # Num1 before the first NR frame must not raise NameError
        worker_scene = False  # the same, for the status line's scene score
        startup_pending = True  # open the menu once the picture is alive
        nr_rate = FrameRateMeter(FPS_LOG_INTERVAL)
        frame_pacer = FramePacer()
        last_log = time.monotonic()
        last_fps = 0.0
        last_perf_log = time.monotonic()
        motion_status = MotionBackendStatus()
        # Stage timings: mean ms over PERF_LOG_INTERVAL (the [perf] log)
        st.perf = {k: [] for k in PERF_KEYS}

        def _perf(key: str, t0: float) -> None:
            """Record the stage duration (ms) into the timings dictionary."""
            st.perf[key].append((time.perf_counter() - t0) * 1000.0)

        def _service_idle_overlay() -> None:
            """Keep the settings menu usable without waking the frame loop.

            No frame will ever arrive in this state, so a mode-switch veil
            raised by a rebuild started from here (Spout2 / HDR / motion
            backend / monitor / GPU) has nothing to wait for - and while it
            is up draw_overlay refuses to paint anything, so the menu the
            user just used stops being drawn and the screen stays frozen
            under the assemble mark (issues #89/#96: "the window becomes
            invisible, then it may reappear to disappear"). Every other way
            down lives in the frame path, which this branch never reaches.
            Both calls below are no-ops when no veil is up.
            """
            if st.display.menu.visible:
                for ev in pygame.event.get():
                    for action in st.display.menu.handle_event(ev):
                        commands.run_contained(st, f"menu action {action!r}",
                                               commands.apply_menu_action, st, action)
                if st.display.menu.visible and not st.display.menu.dragging:
                    st.display.menu.set_state(settings_io.menu_payload(st))

            if st.display.menu.visible:
                st.display.set_hud_only(True)
                # A user may turn NR off before the first processed frame.  In
                # that case set_visible() intentionally refuses to reveal the
                # startup window; opening the menu is an explicit reason to
                # reveal the otherwise transparent HUD layer.
                if not st.display.is_visible():
                    st.display.reveal()
                    st.display.set_visible(True)
                st.display.raise_topmost()
                # With the menu up draw_overlay advances the veil's fade-out
                # itself, so a fade started here finishes in ~SWITCH_FADE_OUT.
                st.display.exit_switch_mode()
                st.display.draw_overlay()
            else:
                # Nothing is being drawn on this path, so a fade could never
                # advance: end the veil outright. Only reachable when a
                # rebuild raised it and the menu was closed in between.
                st.display.drop_switch_mode()
                st.display.set_visible(False)

        while st.running:
            loop_start = time.perf_counter()
            now = time.monotonic()

            # False only when the user asked to quit; a command that raised
            # returns None and the loop goes on (run_contained).
            if commands.run_contained(st, "a command", commands.drain_commands,
                                      st) is False:
                break

            # The answer from the "Save as" dialog (it runs in its own thread).
            commands.drain_save_dialog(st)
            # Conversions run on their own thread; what finished is
            # announced here, where the display may be touched.
            commands.service_conversions(st)
            commands.poll_recording_finalizer(st)
            # A GPU recording the worker closed by itself (its encoder failed)
            # or lost with it (the worker died): finalize it now, with what
            # reached the disk, rather than when the user next presses record.
            if (st.recorder is not None
                    and getattr(st.recorder, "stopped_elsewhere", lambda: False)()):
                print("[main] the recording ended in the worker - finalizing it",
                      file=sys.stderr)
                try:
                    commands.begin_recording_finalization(st)
                except Exception as exc:
                    print(f"[main] recording finalization could not start: {exc}",
                          file=sys.stderr)

            # NR OFF is a real idle state unless an explicit consumer still
            # needs bypass frames.  No code below this branch captures, sends,
            # receives or presents a frame.
            low_cost_off = pipeline.sync_low_cost_off(st)

            # The worker is gone (restart budget exhausted): the pipeline is
            # stopped. Commands still run (Num1 revives it), but no frame is
            # grabbed or sent - the worker is dead and would only be
            # restarted in vain (issue #3: endless restart loop on a GPU
            # where feature 18 cannot be created). A transient failure gets
            # one automatic revive after the backoff (recovery pattern from
            # dlss5-video-player 0.17.2: CreateFeature-once, retries as a
            # fallback - the revive is a fresh process, not a feature
            # recreation).
            if st.worker_failed:
                if st.next_auto_revive and time.monotonic() >= st.next_auto_revive:
                    st.next_auto_revive = 0.0
                    st.worker_failed = False
                    print("[main] auto-reviving the worker after the transient failure")
                    try:
                        pipeline.require_compatibility(st)
                        st.worker, st.worker_logs, st.reader, st.worker_stop = restart_worker(
                            st.worker, st.params, st.work_w, st.work_h,
                            st.effective_warmup,
                            st.width if (st.work_w != st.width or st.work_h != st.height) else 0,
                            st.height if (st.work_w != st.width or st.work_h != st.height) else 0,
                            st.worker_stop, st.shm)
                        channels.forget_present(st)
                        channels.forget_dda(st)
                        channels.forget_out(st)
                        channels.forget_verdict(st)
                        channels.sync_motion_size(st)
                        st.frame_index = 0
                        st.pts = 0
                        st.paused = False
                        st.display.set_visible(True)
                        st.display.alert(UI_STRINGS[st.lang]["nr_on"])
                        st.tray._set_state(nr=True)
                    except Exception as exc:
                        print(f"[main] auto-revive failed ({exc}) - staying NR OFF",
                              file=sys.stderr)
                        st.paused = True
                        st.worker_failed = True
                _service_idle_overlay()
                time.sleep(0.05)
                continue

            if low_cost_off:
                _service_idle_overlay()
                time.sleep(0.05)
                continue

            # Deferred apply (debounce + coalescing): the request lands once
            # the user has stopped moving the control for APPLY_DEBOUNCE and
            # the cooldown between two applies has passed. Deliberately after
            # the NR OFF branch, exactly as the old cooldown check was: while
            # the pipeline is asleep there is nothing to rebuild against, and
            # the queued change is applied when NR comes back.
            if (st.pending_apply is not None
                    and time.monotonic() >= st.pending_apply_due
                    and time.monotonic() - st.last_restart >= RESTART_COOLDOWN):
                p_scale, p_profile, p_params, p_small = st.pending_apply
                st.pending_apply = None
                st.pending_apply_due = 0.0
                print("[main] applying the queued settings")
                pipeline.do_restart(st, p_scale, p_profile, p_params, new_small=p_small)

            if not st.running:
                break

            # NR OFF reaches this path only while a recording, screenshot or
            # Frame Generation explicitly needs raw frames.  The neural pass
            # remains bypassed, but the frame producer stays paired with recv.
            bypass = st.paused
            # (for readability: send_frame is called with bypass=bypass)

            if st.want_present and not st.present_mode and not st.present_attempted:
                channels.enable_present(st)
            # Who has the focus, for the window-mode hotkey: by the time it
            # is pressed the menu may be in front, so the last window that was
            # not ours is remembered continuously.
            fg = foreign_foreground()
            if fg:
                st.last_foreground = fg
            # A game that goes fullscreen raises itself above every topmost
            # window, ours included, and then the menu is drawn but not on
            # screen. While it is open we keep coming back up.
            if st.display.menu.visible:
                # The menu is up: the worker's picture window is re-asserted
                # HWND_TOPMOST on every restart (NR off->on, a settings apply,
                # a mode switch), which lands the picture ABOVE the HUD and
                # hides the panel behind it - the "menu disappears while I am
                # using it" reports (#94, and the NR/FG toggle case). While
                # the menu is open the pair is re-asserted EVERY frame: the
                # call is idempotent (raise_topmost inserts the HUD above the
                # picture, or does nothing when it is already there), so the
                # steady state costs no SetWindowPos at all. The 30-frame
                # cadence below stays for the menu-closed HUD case.
                st.display.raise_topmost()
            # The same for the HUD even when the menu is closed: a borderless
            # game (Cyberpunk) keeps itself on top and our HUD stays
            # underneath it forever. Re-assert only when the topmost window
            # is NOT ours - in the steady state this is zero SetWindowPos
            # calls, so no DWM flicker (user: flicker + invisible HUD over
            # borderless games).
            if st.frame_index % 30 == 0:
                try:
                    top = ctypes.windll.user32.GetTopWindow(0)
                    if top and top != st.display.get_hwnd():
                        st.display.raise_topmost()
                except Exception:
                    pass
            if st.window_hwnd is not None:
                if not ctypes.windll.user32.IsWindow(ctypes.c_void_p(st.window_hwnd)):
                    print("[main] the captured window closed - back to full screen",
                          file=sys.stderr)
                    pipeline.switch_window(st, 0)
                    continue
                pipeline.follow_window(st)
            elif st.frame_index % 30 == 0:
                # Not in window mode: watch the monitor instead. Every
                # 30 frames - a mode change is not a per-frame event and
                # the query walks the monitor list.
                pipeline.follow_monitor(st)
            if st.want_dda and not st.dda_mode and not st.dda_attempted:
                if st.window_hwnd is not None:
                    # The channel module opens channels; deciding that the
                    # window is gone and the whole screen comes back is the
                    # pipeline's call, and it lives here.
                    if not channels.enable_wgc(st):
                        pipeline.switch_window(st, 0)
                else:
                    channels.enable_dda(st)
            if st.want_motion_small and not st.motion_small and not st.motion_attempted:
                st.motion_attempted = True
                channels.sync_motion_size(st)
            if st.want_out_shm and not st.out_shm and not st.out_attempted:
                channels.enable_out_shm(st)

            # Has the worker said whether the neural pass came up on this
            # card? The answer fires the "this GPU cannot run the neural
            # pass" alert, and it used to be asked only inside menu_payload
            # - which the loop builds only while the menu is OPEN. With the
            # menu closed (the usual state) a refused feature arrived in
            # total silence: the red dot was there for nobody to see. That
            # is the issue #29 gap the alert was added to close, still open
            # (audit F12).
            #
            # refresh_gpu_ok caches its verdict, so this costs one attribute
            # check once the worker has spoken; every thirtieth frame is
            # twice a second before that, which is soon enough for an alert
            # and far from the per-frame work that cost 29 FPS the last time
            # something was added to this loop.
            if st.frame_index % 30 == 0:
                settings_io.refresh_gpu_ok(st)
                # And whether Frame Generation came up at all (issue #76:
                # the switch used to stay ON after the runtime refused).
                settings_io.refresh_fg_ok(st)
                # And whether the display being captured is in HDR. The
                # network is trained on SDR: on an HDR desktop the result
                # reads as "everything is too bright and the sliders do
                # nothing", which is a report we have had (issue #27) and a
                # notice a user asked for (issue #33). Once per session.
                settings_io.warn_hdr(st)

            # --- Input for the overlay menu --------------------------
            # Events are read only while the menu is open: the rest of the
            # time the window is click-through, there are no events, and an
            # extra get() would eat the queue from pump() inside drawing.
            if st.display.menu.visible:
                for ev in pygame.event.get():
                    for action in st.display.menu.handle_event(ev):
                        commands.run_contained(st, f"menu action {action!r}",
                                               commands.apply_menu_action, st, action)
                if not st.display.menu.dragging:
                    st.display.menu.set_state(settings_io.menu_payload(st))

            # --- Grab ahead: while NGX computes frame N we grab N+1 -------
            # work_frame == None happens on the first frame, after a worker
            # restart (a work_scale change) and after grab()==None. Then the
            # grab happens at the start of the iteration, BEFORE send - the
            # synchronisation with the worker is not lost (send/recv are
            # always paired, recv is mandatory after any send).
            # The worker (v3, NGX Upscaling) resizes full->work->full on the
            # GPU itself: Python sends a full-res frame, motion at work-res
            # (guides is built with work_w/work_h and downsamples its own
            # input) and receives full-res back.
            if st.work_frame is None and not st.gray_active:
                t0 = time.perf_counter()
                frame = _safe_grab()
                _perf("grab", t0)
                if frame is None:
                    continue  # the frame is not ready yet - skip the iteration
                if frame.shape[1] != st.width or frame.shape[0] != st.height:
                    t0 = time.perf_counter()
                    try:
                        cv2.resize(frame, (st.width, st.height), interpolation=_resize_interp(frame, st.width, st.height), dst=st.buf_full)
                    except cv2.error:
                        # The monitor resolution changed: buf_full was
                        # preallocated for the old size - recreate and retry
                        st.buf_full = np.empty((st.height, st.width, 4), dtype=np.uint8)
                        cv2.resize(frame, (st.width, st.height), interpolation=_resize_interp(frame, st.width, st.height), dst=st.buf_full)
                    _perf("resize_full", t0)
                    frame = st.buf_full
                else:
                    frame = np.ascontiguousarray(frame, dtype=np.uint8)
                st.work_frame = frame

            # --- Sending the frame with auto-recovery ---
            # The worker can die or hang (NGX after RNSZ, a GPU conflict) -
            # instead of crashing, main restarts the worker with the current
            # parameters and carries on. This is the last line of defence:
            # the program does not fall over.
            try:
                check_worker(st.worker, st.worker_logs)
                plain_bypass = bypass and not st.cfg.get("frame_generation", False)
                hardware_motion = False
                if st.gray_active and not plain_bypass:
                    was_failed = motion_status.failed and motion_status.worker is st.worker
                    hardware_motion = motion_status.update(st.worker, st.worker_logs)
                    if motion_status.failed and not was_failed:
                        st.display.alert(UI_STRINGS[st.lang].get(
                            "motion_fallback", "NVOFA unavailable - using CPU DIS"))
                nvofa = st.cfg.get("motion_backend") == "nvofa" and hardware_motion
                # The capture round trip, only where this loop reads the gray:
                # the CPU optical flow. NVOFA makes its field in the worker and
                # the worker scores the scene from the same gray; NR off without
                # FG needs no guides at all.
                worker_scene = bool(st.gray_active) and WORKER_SCENE and (nvofa or plain_bypass)
                if st.gray_active and not worker_scene:
                    prepare_capture(st.worker, st.reader, st.frame_index, st.pts)
                try:
                    t0 = time.perf_counter()
                    if plain_bypass:
                        # NR OFF, and FG is not presenting these frames: the
                        # worker skips the NGX evaluate, so nothing ever reads
                        # this motion field. Computing it anyway cost 2.9 ms of
                        # DIS per frame (measured, 320x180 flow, moving
                        # content) - and it cost it on the mode that runs
                        # FASTEST, 121-133 FPS in bypass, where it came to about
                        # half a core spent filling a buffer the worker throws
                        # away. The frame still CARRIES a motion field: the
                        # header's size contract does not change just because the
                        # effect is off.
                        #
                        # previous_gray goes with it. Keeping the last pre-bypass
                        # frame as history would mean correlating against a
                        # screen that is minutes old the moment NR comes back on,
                        # and the first real flow field would be garbage.
                        # Cleared, the first NR frame reports a scene cut
                        # instead - which is what a resumed pipeline is.
                        #
                        # With FG on this branch is not taken: the presenter
                        # interpolates between the frames it is handed, so the
                        # field and its reset flag ARE read, and a per-frame
                        # reset is what left the feature with nothing to
                        # interpolate (#104). The guides below are then the
                        # ordinary ones - with the hardware backend that is a
                        # scene score, not a DIS call.
                        st.guides.forget()
                        guide = st.guides.zero_guide()
                    elif worker_scene:
                        guide = st.guides.handoff()
                    elif st.gray_active:
                        guide = st.guides.process(gray=st.shm.read_gray(),
                                                  compute_motion=not nvofa)
                    else:
                        guide = st.guides.process(st.work_frame)
                    _perf("guides", t0)
                except Exception as guide_exc:
                    # guides is not critical: ValueError/TypeError/cv2.error (the
                    # shape of the gray frame, a division by zero) must not take
                    # the process down. We skip the frame - the worker gets the
                    # next one. But a persistent error (an incompatible gray
                    # channel, a broken shape) would spin main at 100% CPU -
                    # after 5 failures in a row we fall back to zero motion: the
                    # frames keep flowing and the picture does not freeze.
                    print(f"[main] guides.process failed ({guide_exc}) - frame skipped",
                          file=sys.stderr)
                    st.guide_fails += 1
                    if st.guide_fails >= 5:
                        print(f"[main] guides.process is unstable - zero motion "
                              f"(frames keep flowing)", file=sys.stderr)
                        st.guide_fails = 0
                        guide = st.guides.zero_guide()
                    else:
                        continue
                t0 = time.perf_counter()
                send_frame(st.worker, st.frame_index, st.work_frame, guide.motion, guide.reset,
                           st.pts, st.shm, want_pixels=(st.pending_shot is not None
                                                   or (st.recorder is not None
                                                       and st.recorder.needs_frame())),
                           motion_small=st.motion_small,
                           no_color=bool(st.dda_mode),
                           bypass=bypass,
                           split=st.split_pos,
                           frame_generation=bool(st.cfg.get("frame_generation", False)),
                           frame_multiplier=int(st.cfg.get("frame_multiplier", 2)),
                           prepared=bool(st.gray_active) and not worker_scene,
                           early_reply=EARLY_REPLY, worker_scene=worker_scene)
                _perf("send", t0)
            except (BrokenPipeError, OSError, EOFError, RuntimeError) as exc:
                st.consecutive_restarts += 1
                if st.consecutive_restarts >= MAX_CONSECUTIVE_RESTARTS:
                    print(f"[main] the worker died {st.consecutive_restarts} times in a row - NR OFF")
                    st.paused = True
                    st.worker_failed = True
                    st.display.alert(UI_STRINGS[st.lang]["nr_off"])
                    st.tray._set_state(nr=False)
                    st.consecutive_restarts = 0
                    st.work_frame = None
                    # The worker is gone and will not come back on its own:
                    # stop hammering it, hide the overlay so the desktop is
                    # not covered by a black window (issue #3), and wait for
                    # the user to turn NR back on. A HARD failure
                    # (0xBAD00001 - the GPU cannot run the pass at all) is
                    # permanent; a transient one (no-frame, driver hiccup)
                    # gets one automatic revive after a backoff instead of
                    # leaving the user with NR off until they press Num1.
                    if not _hard_failure(st.worker_logs):
                        st.next_auto_revive = time.monotonic() + AUTO_REVIVE_BACKOFF
                        print(f"[main] transient worker failure - auto-revive "
                              f"in {AUTO_REVIVE_BACKOFF:.0f}s")
                    try:
                        shutdown_worker(st.worker, st.worker_stop)
                    except Exception:
                        pass
                    st.display.set_visible(False)
                    continue
                print(f"[main] worker lost while sending ({exc}) - restarting "
                      f"({st.consecutive_restarts}/{MAX_CONSECUTIVE_RESTARTS})")
                if st.worker_logs:
                    print("[main] worker stderr (tail):")
                    for line in st.worker_logs[-15:]:
                        print(f"  {line}")
                pipeline.require_compatibility(st)
                st.worker, st.worker_logs, st.reader, st.worker_stop = restart_worker(
                    st.worker, st.params, st.work_w, st.work_h, st.effective_warmup,
                    st.width if (st.work_w != st.width or st.work_h != st.height) else 0,
                    st.height if (st.work_w != st.width or st.work_h != st.height) else 0,
                    st.worker_stop, st.shm)
                channels.forget_present(st)
                channels.forget_dda(st)
                channels.forget_out(st)
                channels.forget_verdict(st)
                channels.sync_motion_size(st)
                st.frame_index = 0
                st.pts = 0
                st.work_frame = None
                continue

            # Grab the next frame WHILE the worker computes the current one
            # (NGX is ~70-100 ms/frame - the bottleneck). dxcam is thread-safe
            # within one thread - a second thread is unnecessary, we simply
            # move grab() between send and recv. Buffers: send_frame copies
            # the data into the pipe (tobytes) and guides.process keeps no
            # references to its input - buf_full can be reused right away.
            # In DDA mode the worker grabs the frame itself - Python does not.
            next_frame = None
            if not st.gray_active:
                t0 = time.perf_counter()
                next_frame = _safe_grab()
                _perf("grab", t0)
            if next_frame is not None:
                if next_frame.shape[1] != st.width or next_frame.shape[0] != st.height:
                    t0 = time.perf_counter()
                    try:
                        cv2.resize(next_frame, (st.width, st.height), interpolation=_resize_interp(next_frame, st.width, st.height), dst=st.buf_full)
                    except cv2.error:
                        st.buf_full = np.empty((st.height, st.width, 4), dtype=np.uint8)
                        cv2.resize(next_frame, (st.width, st.height), interpolation=_resize_interp(next_frame, st.width, st.height), dst=st.buf_full)
                    _perf("resize_full", t0)
                    next_frame = st.buf_full
                else:
                    next_frame = np.ascontiguousarray(next_frame, dtype=np.uint8)
            # next_frame == None: the frame is not ready - the start of the
            # next iteration will do the grab (work_frame = None). The
            # synchronisation with the worker is not lost: send has already
            # gone out and the recv below is mandatory.

            t0 = time.perf_counter()
            try:
                st.output_rgba = None
                recv_reader = st.reader
                recv_deadline = time.monotonic() + 5.0
                while time.monotonic() < recv_deadline:
                    try:
                        st.output_rgba = st.reader.recv(st.frame_index, timeout=0.05)
                        break
                    except TimeoutError:
                        # A heavy 4K scene can take ~1 s per NGX frame -
                        # keep the hotkeys alive while main waits (user:
                        # "NR toggle does not always fire in Cyberpunk").
                        # The switch veil's mark must keep animating
                        # while the new worker warms up.
                        if st.display.is_switch_active():
                            st.display.draw_overlay(0.0)
                        else:
                            # The window's own messages, and the z-order
                            # guard, while main waits: a heavy frame can take
                            # the whole 5 s, and for that long nothing pumped
                            # the SDL window (Windows marks it "Not Responding"
                            # after 5 s) and nothing put the panel back over a
                            # picture that had covered it.
                            try:
                                pygame.event.pump()
                                if st.display.menu.visible:
                                    st.display.raise_topmost()
                            except Exception:
                                pass
                        if commands.run_contained(st, "a command",
                                                  commands.drain_commands,
                                                  st) is False:
                            st.running = False
                            break
                        if st.reader is not recv_reader:
                            break  # a command restarted the worker
                        continue
                else:
                    raise TimeoutError(
                        f"the worker has been silent for 5s on frame {st.frame_index} - NGX did not answer after the restart")
                if not st.running:
                    break
                if st.reader is not recv_reader:
                    continue  # the worker was restarted by a command
            except (TimeoutError, EOFError, RuntimeError, OSError) as exc:
                st.consecutive_restarts += 1
                if st.consecutive_restarts >= MAX_CONSECUTIVE_RESTARTS:
                    print(f"[main] worker silent/dying {st.consecutive_restarts} times in a row - NR OFF")
                    st.paused = True
                    st.worker_failed = True
                    st.display.alert(UI_STRINGS[st.lang]["nr_off"])
                    st.tray._set_state(nr=False)
                    st.consecutive_restarts = 0
                    st.work_frame = None
                    # No frame will ever arrive - the switch overlay must not
                    # hang over the desktop forever (audit M2: the veil is
                    # removed only on a received frame).
                    st.display.exit_switch_mode()
                    # Same for the overlay itself: hide it so the desktop is
                    # not covered by a black window (issue #3). A HARD
                    # failure (0xBAD00001) is permanent; a transient one gets
                    # one automatic revive after a backoff.
                    if not _hard_failure(st.worker_logs):
                        st.next_auto_revive = time.monotonic() + AUTO_REVIVE_BACKOFF
                        print(f"[main] transient worker failure - auto-revive "
                              f"in {AUTO_REVIVE_BACKOFF:.0f}s")
                    try:
                        shutdown_worker(st.worker, st.worker_stop)
                    except Exception:
                        pass
                    st.display.set_visible(False)
                    continue
                print(f"[main] worker silent/dead on frame {st.frame_index} ({exc}) - restarting "
                      f"({st.consecutive_restarts}/{MAX_CONSECUTIVE_RESTARTS})")
                pipeline.require_compatibility(st)
                st.worker, st.worker_logs, st.reader, st.worker_stop = restart_worker(
                    st.worker, st.params, st.work_w, st.work_h, st.effective_warmup,
                    st.width if (st.work_w != st.width or st.work_h != st.height) else 0,
                    st.height if (st.work_w != st.width or st.work_h != st.height) else 0,
                    st.worker_stop, st.shm)
                channels.forget_present(st)
                channels.forget_dda(st)
                channels.forget_out(st)
                channels.forget_verdict(st)
                channels.sync_motion_size(st)
                st.frame_index = 0
                st.pts = 0
                st.work_frame = None
                continue
            _perf("recv", t0)
            # A frame arrived - the failure chain is broken. Without the reset
            # the counter accumulated across the whole session and three
            # unrelated failures (even an hour apart) turned NR off.
            st.consecutive_restarts = 0
            status = "NR OFF" if st.paused else "NR ON"
            # Did the worker actually EVALUATE this frame? `last_ngx_result` is
            # 0 when no evaluation happened - the state where the menu says NR ON
            # while the picture goes out raw. Kept as a short streak so one odd
            # frame (a stall reset, a bypass) is not reported as a fault.
            if not st.paused:
                ngx = int(getattr(st.reader, "last_ngx_result", 0) or 0)
                if ngx == 0:
                    st.nr_idle_streak += 1
                else:
                    st.nr_idle_streak = 0
            else:
                st.nr_idle_streak = 0
            st.nr_not_evaluating = st.nr_idle_streak >= NR_IDLE_STREAK_LIMIT
            st.pts += 1

            # A native Save As dialog is an ordinary desktop window, so DDA
            # can still return a buffered frame containing it after the user
            # closes it. Freeze the requested processed frame FIRST; only
            # then does commands open the dialog (#89). The copy is isolated
            # from the shared output slot the worker will reuse next.
            if st.output_rgba is not None:
                commands.freeze_screenshot_frame(st, st.output_rgba)

            t0 = time.perf_counter()
            try:
                if (st.recorder is not None
                        and getattr(st.recorder, "takes_pixels", True)
                        and st.output_rgba is not None):
                    # Our layer is excluded from capture
                    # (WDA_EXCLUDEFROMCAPTURE), so we bake the open menu onto
                    # the frame ourselves. frombuffer references the numpy
                    # buffer (no copy): the blit writes straight into
                    # output_rgba.
                    try:
                        surf = pygame.image.frombuffer(
                            st.output_rgba, (st.output_rgba.shape[1], st.output_rgba.shape[0]), "RGBX")
                        st.display.draw_capture_overlay(surf)
                    except Exception as menu_exc:
                        print(f"[main] menu was not baked into the recorded frame: {menu_exc}",
                              file=sys.stderr)
                    # The recording gets its own try: an encoder failure must
                    # NOT land in the "output failed" except (that one
                    # recreates the pygame window on every frame - an endless
                    # loop). A recording error stops the recording, not the
                    # window.
                    try:
                        st.recorder.write(st.output_rgba)
                    except Exception as rec_exc:
                        print(f"[main] frame write failed ({rec_exc}) - "
                              f"stopping the recording", file=sys.stderr)
                        try:
                            commands.begin_recording_finalization(st)
                        except Exception as finish_exc:
                            print(f"[main] recording finalization could not start: "
                                  f"{finish_exc}", file=sys.stderr)
                if st.present_mode:
                    # In WNDO mode the worker draws the frame on screen; in
                    # Python the pixels arrive ONLY on want_pixels
                    # (recording/screenshot). There is no need to show them in
                    # pygame: that is a pointless 4K blend (~22 ms) and a
                    # flicker of the frame in the HUD layer above the worker's
                    # window. The HUD is refreshed by draw_overlay() with
                    # throttling (not every frame).
                    st.display.exit_switch_mode()  # the new worker is presenting
                    st.display.reveal()  # a real frame exchange happened
                    st.display.draw_overlay()
                elif st.output_rgba is None:
                    # The frame is already on screen - the worker showed it, only the HUD here
                    # (WGCW/DDA without want_pixels: no colour reaches Python).
                    # This is still a live exchange with the rebuilt worker: the
                    # switch overlay must come down or the menu stays hidden
                    # behind the veil forever (user: clipped/blank after Num5).
                    st.display.exit_switch_mode()
                    # reveal() is THE only way to show the window while
                    # _reveal_pending is set (audit H1): this branch is hit on
                    # every frame when the WNDO window is unavailable and DDA/
                    # WGCW works (fallback config) - without the call the HUD
                    # and the menu stay invisible forever in that setup.
                    st.display.reveal()
                    # And the layer has to be colour-keyed here, because
                    # draw_overlay() clears it with CHROMA_KEY. In this
                    # configuration enable_present() failed, so set_hud_only
                    # was last called with False - an OPAQUE window - and the
                    # clear painted the whole screen magenta with the menu on
                    # top of it. Nothing else fills this layer in this branch:
                    # no frame reaches Python at all, which is why the desktop
                    # underneath has to show through (audit I1).
                    st.display.set_hud_only(True)
                    st.display.draw_overlay()
                else:
                    st.display.exit_switch_mode()  # the next frame replaces the overlay
                    st.display.reveal()  # a real frame exchange happened
                    # The layer's own state is decided inside show(): it knows
                    # whether the frame covers the layer (opaque) or only a
                    # window on it (the surround is keyed out). Asking for it
                    # here as well is how the key used to flip once per
                    # returned frame - the blink.
                    st.display.show(st.output_rgba)
            except Exception as exc:
                # A display mode change (entering/leaving a fullscreen game)
                # can kill the pygame/SDL context - recreate the window.
                print(f"[main] output failed ({exc}) - recreating the window")
                # Snapshot the live menu state before the window dies - the
                # restore below must pick up where the user left it, not the
                # stale cfg values (user rule 10.09: fixed position until
                # the user drags it).
                st.cfg["menu_offset"] = [int(st.display.menu.offset[0]),
                                      int(st.display.menu.offset[1])]
                st.cfg["menu_scale"] = round(st.display.menu.user_scale, 2)
                st.cfg["menu_height"] = (None if st.display.menu.user_height is None
                                      else int(st.display.menu.user_height))
                try:
                    st.display.close()
                except Exception:
                    pass
                st.display = Display(st.width, st.height, fullscreen=bool(st.cfg["fullscreen"]))
                # A fresh window starts at (0,0): put it back on the chosen
                # monitor (the origin belongs to the pipeline, not to SDL).
                st.display.set_origin(*getattr(st, "mon_origin", (0, 0)))
                st.display.set_lang(st.lang)
                # In one-window mode the overlay must stay visible to outside
                # recorders: the NEW window comes up with the WDA flag set
                # (the Display default), so state it explicitly here - the
                # same call _rebuild_pipeline makes. Without this, any
                # display-mode change while in window mode silently drops
                # the overlay from NVIDIA App / OBS capture until the next
                # pipeline rebuild (audit #4, F1).
                st.display.set_excluded_from_capture(st.window_hwnd is None)
                # The menu is created together with the window - we give it
                # back its size, position, theme and language, otherwise after
                # a game starts it jumps to the centre, turns light and
                # switches to en.
                st.display.menu.set_user_scale(float(st.cfg.get("menu_scale", 1.0)))
                st.display.menu.set_hotkeys(hotkey_labels(st.hotkey_bindings))
                saved_theme = st.cfg.get("theme")
                if isinstance(saved_theme, str) and saved_theme in settings_io.THEME_NAMES:
                    st.display.menu.set_state({"theme": saved_theme})
                st.display.menu.set_state({"lang": st.lang})
                saved = st.cfg.get("menu_offset")
                if isinstance(saved, (list, tuple)) and len(saved) == 2:
                    st.display.menu.offset = [int(saved[0]), int(saved[1])]
                # The height the snapshot above took, like rebuild_pipeline
                # restores it: without it the new menu came back at its
                # default height and the next save_menu_layout wrote
                # menu_height null over the user's.
                saved_height = st.cfg.get("menu_height")
                if isinstance(saved_height, (int, float)) and saved_height > 0:
                    st.display.menu.user_height = int(saved_height)
                if st.present_mode:
                    # The new window must become a transparent layer over the worker again
                    st.display.set_hud_only(True)
                    st.display.raise_topmost()
                # The old menu may have been waiting for a hotkey to rebind
                # (every global hotkey suspended until the field gets a key);
                # the new one is not, so nothing would ever resume them.
                st.hotkeys.resume()
                st.display.alert(UI_STRINGS[st.lang]["nr_on"])
            _perf("show", t0)
            completed_at = time.perf_counter()
            if not bypass:
                nr_rate.record(completed_at)
            last_fps = nr_rate.rate(completed_at)
            st.display.set_hud({
                "fps": last_fps,
                # NR ON but the worker is not evaluating: the picture is raw.
                # Without this the only symptom is a counter that runs too fast,
                # and the user has no way to know the pass stopped.
                "nr_not_evaluating": st.nr_not_evaluating,
                # What the presenter shows with Frame Generation on - the
                # worker reports it every two seconds. The HUD pairs the
                # network rate with it ("42 / 84 fps"); None while FG is off.
                "display_fps": settings_io._fg_displayed_fps(st),
                # And which multiplier is really running (issue #100): the
                # user's pick and the live step can differ after a step-down.
                "fg_multiplier_active": settings_io._fg_active_multiplier(st),
                "status": status,
                "resolution": f"{st.width}x{st.height}",
                "profile": st.cfg["profile"],
                "params": {k: v for k, v in st.params.items() if k not in ("profile", "preset", "style", "auto_mask", "ui_correction")},
                "frames": st.frame_index,
                # The recording indicator outside the menu: the HUD is drawn
                # over the worker's window, so the user sees the REC state
                # even with the menu closed (user 5080 request).
                "recording": st.recorder is not None,
                "rec_seconds": (st.recorder.duration_ms / 1000.0) if st.recorder else 0.0,
                "rec_indicator": bool(st.cfg.get("rec_indicator", True)),
                # Which corner the counter sits in, or "off" (#109).
                "fps_overlay": str(st.cfg.get("fps_overlay", "off")),
            })

            st.frame_index += 1
            if startup_pending and st.frame_index >= 2:
                # Wait for the first displayed frame: an open menu over a
                # window that is not filled yet flashes black.
                startup_pending = False
                if st.startup_menu:
                    st.display.menu.set_state(settings_io.menu_payload(st))
                    st.display.menu.visible = True
                    st.display.set_menu_opaque(True)
                    st.display.set_menu_input(True)
                    print("[main] menu opened at startup")
                else:
                    st.display.alert(UI_STRINGS[st.lang]["started"], 3.5)
            # The NR cascade has to be told to EVERY worker, not just the
            # first. The stream header has no field for a pass count (that
            # slot is frame_count), so a worker always starts at one pass and
            # the count travels with an RNSZ. Sending it once at startup
            # covered the config case and missed every restart: a revive
            # after a crash, a manual revive, a pipeline rebuild - the worker
            # comes back at ONE pass while the panel still says four, and
            # nothing says so. Measured 20.09: killing the worker of a
            # four-pass session took the rate from 28 to 91 fps with no line
            # about it.
            #
            # Keyed on the worker's pid, so it is one compare per frame and
            # fires exactly once per worker, whatever brought that worker up.
            # Not before the stream is running: `startup_pending` is cleared
            # on the second displayed frame, and an apply before that would
            # race the first RNSZ.
            if (not startup_pending and int(getattr(st, "nr_passes", 1)) > 1
                    and st.worker is not None
                    and getattr(st, "nr_passes_pid", None) != st.worker.pid):
                st.nr_passes_pid = st.worker.pid
                print(f"[main] NR cascade: telling worker {st.worker.pid} "
                      f"about the saved {st.nr_passes} passes")
                pipeline.request_apply(st, st.work_scale,
                                       st.cfg["profile"], st.params)
            st.work_frame = next_frame  # None -> grab at the start of the next iteration

            log_now = time.monotonic()
            if log_now - last_log >= FPS_LOG_INTERVAL:
                # The worker's score when it decided the cut, else the loop's.
                score = (st.reader.last_scene if worker_scene
                         else guide.scene_score if guide is not None else None)
                scene = f" | scene {score:.3f}" if score is not None else ""
                print(f"[main] {status} | NR {last_fps:5.1f} fps | "
                      f"frames {st.frame_index} | "
                      f"work {st.work_w}x{st.work_h}{scene}")
                last_log = log_now

            if log_now - last_perf_log >= PERF_LOG_INTERVAL:
                parts = []
                for key in PERF_KEYS:
                    samples = st.perf[key]
                    if samples:
                        parts.append(f"{key} {sum(samples) / len(samples):.1f}ms")
                    samples.clear()
                if parts:
                    print("[perf] " + " | ".join(parts))
                last_perf_log = log_now

            frame_pacer.wait(settings_io.frame_limit_fps(st.cfg), loop_start)

        print("[main] exiting at the user's request")
    except KeyboardInterrupt:
        print("\n[main] interrupted (Ctrl+C)")
    except Exception as exc:
        # With the traceback, not without it. A user on an RTX 3060 sent a
        # log whose entire account of the failure was
        #   [main] ERROR: <built-in function get> returned a result with an
        #   exception set
        # - a SystemError, which means some C call had already left an error
        # set and the next builtin tripped over it. Without a traceback
        # there is no way to say which builtin, in which function, and the
        # reporter ended up guessing at a fix (issue #41, PR #42). The frames
        # cost nothing on a path that runs once, at the end.
        import traceback
        print(f"[main] ERROR: {exc}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        if st.worker is not None and st.worker.poll() is not None:
            print("[main] the worker crashed; last stderr lines:", file=sys.stderr)
            for line in st.worker_logs[-40:]:
                print(f"  {line}", file=sys.stderr)
        return 1
    finally:
        # A conversion may be running on its own worker. Stop it first and
        # wait a bounded moment: the runner reaps its worker on the way out,
        # and the job object in pipeline covers the case where it cannot.
        if st.convert_queue is not None:
            try:
                st.convert_queue.shutdown(timeout=10.0)
            except Exception as exc:
                print(f"[main] failed to stop the conversions: {exc}",
                      file=sys.stderr)
        # A recording may have been running at exit. Begin the same asynchronous
        # path the UI uses, then wait here only because the UI is already gone.
        if st.recorder is not None:
            try:
                commands.begin_recording_finalization(st, announce=False)
            except Exception as exc:
                print(f"[main] failed to start recording finalization: {exc}",
                      file=sys.stderr)
        if st.recording_finalizer is not None:
            try:
                st.recording_finalizer.close()
            except Exception as exc:
                print(f"[main] failed to close the recording: {exc}", file=sys.stderr)
            try:
                commands.poll_recording_finalizer(st)
            except Exception as exc:
                print(f"[main] failed to poll the recording finalizer: {exc}",
                      file=sys.stderr)
        # The worker and the shared section are the two calls here without a
        # guard, and a raise from either skips every step below it - capture,
        # window, hotkeys, tray and taskbar all stay live, with the borderless
        # topmost overlay still on screen and no loop left to feed it (audit
        # H4). shutdown_worker prints and closes a pipe, and on a pythonw
        # process stdout is the log file: a write into a log that has become
        # unwritable raises out of print and took the whole teardown with it.
        # Keep the order, guard each call.
        if st.worker is not None:
            try:
                shutdown_worker(st.worker, st.worker_stop)
            except Exception as exc:
                print(f"[main] failed to shut down the worker: {exc}",
                      file=sys.stderr)
        if st.shm is not None:
            try:
                st.shm.close()
            except Exception as exc:
                print(f"[main] failed to close the shared memory: {exc}",
                      file=sys.stderr)
        try:
            settings_io.save_menu_layout(st)
        except Exception:
            pass
        if st.capture is not None:
            try:
                st.capture.close()
            except Exception as exc:
                print(f"[main] failed to close the capture: {exc}", file=sys.stderr)
        if st.display is not None:
            try:
                st.display.close()
            except Exception as exc:
                print(f"[main] failed to close the window: {exc}", file=sys.stderr)
        try:
            st.hotkeys.stop()
        except Exception:
            pass
        try:
            st.tray.stop()
        except Exception:
            pass
        try:
            st.taskbar.stop()
        except Exception:
            pass
        print("[main] resources released")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # pythonw: there is no console - show the reason in a message box and
        # keep the details in NeuralScreen.log.
        import traceback
        traceback.print_exc()
        try:
            import ctypes as _ct
            _ct.windll.user32.MessageBoxW(
                None,
                f"NeuralScreen failed to start: {exc}\n\nDetails in NeuralScreen.log next to the program.",
                "NeuralScreen", 0x10)  # MB_ICONERROR
        except Exception:
            pass
        sys.exit(1)
