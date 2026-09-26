"""The pipeline's lifecycle: build it, tear it down, point it somewhere else.

Everything the program does between frames. A pipeline is built for ONE frame
size, and the worker, the shared memory, the overlay and every negotiated
channel are built for that size - so changing what is captured (another
monitor, one window, a different work resolution) means tearing the whole
thing down and building it again. That is why these six belong together, and
why they were the hardest part of main() to reach: they rebind everything.

Two rules the hard way:

  * the work resolution and the guides change TOGETHER. Assigning one and not
    the other sent motion of the old size to a worker reading exactly
    new_w*new_h*4 bytes, and the pipeline hung waiting for bytes that never
    came.
  * a window's size and its position are different problems. A move is a
    SetWindowPos; a resize is a rebuild, and it waits for the size to settle
    first - rebuilding on every pixel of a drag would be unusable.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import struct
import subprocess
import sys
import threading
import time

import numpy as np

import channels
import settings_io
from capture import (ScreenCapture, _refresh_dxcam_factory,
                     list_monitors, monitor_origin, monitor_size,
                     resolve_output_idx)
from display import Display
from guides import TemporalGuideGenerator
from i18n import STRINGS as UI_STRINGS
from paths import NATIVE_DIR, WORKER_EXE
from protocol import (HEADER_FMT, VIDEO_MAGIC, SharedFrameBuffer,
                      WorkerReader, _negotiate_shm, send_dda, send_per_pass,
                      send_resize)
from settings_io import (THEME_NAMES, _work_size, cascade_passes, hotkey_labels,
                         nr_verdict)
from winapi import window_frame_rect


# Worker lines that always reach the shared log. These are the ones a user
# needs to answer "which card is running the network, and did it come up":
# the adapter list and the NS_GPU pick ([host]), the NGX create/init result
# ([pure]), the architecture spoof ([arch]), the capture path ([cap]) and
# the overlay/spout lifecycle. Before this they were gated behind
# NS_PHASE=1 and a user's log could not tell a working GPU from a Turing
# card that silently fell into SAFE PASSTHROUGH (issue #29: the 2060 Super
# case - the network never came up and nothing said so).
#: Always let through: the pipeline diagnostics. NS_PHASE=1 adds the
#: per-frame profiler lines ([phase]/[pw]) on top of these.
_LOG_ALWAYS = ("[host]", "[pure]", "[arch]", "[cap]", "[dda]", "[present]",
               "[spout]", "[wgc]", "[video]", "[hdr]", "[nvofa]", "[fg]",
               # Added in 2.0.1. These nine prefixes were emitted by the
               # worker and dropped HERE - not by the worker, which filters
               # nothing. Nine of them, and they are not decoration:
               # `[failure]` is the failure report itself; `[scale]`,
               # `[gray]`, `[residual]` and `[outs]` are every shader and
               # pipeline setup failure in the worker; `[nr]` carries
               # "working textures failed - staying at full resolution",
               # which is Boost silently not happening. None of it had ever
               # reached a log or a diagnostic package, and three packages
               # were read in this project without anyone noticing, because
               # a line that is never printed looks exactly like a line that
               # was never reached.
               "[nr]", "[scale]", "[gray]", "[residual]", "[outs]",
               "[failure]", "[reset]", "[live]", "[test]",
               # [z]: the worker's own z-order decision - which window took
               # the top and why the picture was or was not raised over it.
               # The client logs its half under the same tag, and the two
               # halves are read together in #96/#107; a worker half dropped
               # here would leave a user's log looking like the worker never
               # made the decision at all.
               "[z]",
               # The GPU recorder (native/gpu_recorder.cpp): which encoder
               # opened, what was refused, the frames written and dropped.
               "[grec]")
#: [video] lines that are a heartbeat rather than a diagnostic: the "delivered
#: frame N" line is printed every 30 frames and would bury the log.
_LOG_SKIP = ("delivered frame",)


def _log_wanted(line: str) -> bool:
    """Whether a worker stderr line goes into the shared log.

    The diagnostics above always do; the per-frame profiler only under
    NS_PHASE=1 (a line per frame would otherwise bury the log). [video]
    carries the one line that says the network never came up - "NR feature
    unavailable (0xBAD00001) - SAFE PASSTHROUGH" - and in issue #29 a user's
    log had no way to show it.
    """
    for skip in _LOG_SKIP:
        if skip in line:
            return False
    for tag in _LOG_ALWAYS:
        if tag in line:
            return True
    if os.environ.get("NS_PHASE") == "1":
        return "[phase]" in line or "[pw]" in line
    return False


def _drain_stderr(worker, logs: list[str], stop: threading.Event) -> None:
    """Background drain of the worker's stderr (otherwise the buffer fills up
    and the worker hangs).

    One thread per worker; it finishes on EOF (the process died) or on the
    stop event (shutdown_worker). After a restart the old thread reads from
    the CLOSED stderr of the old worker: readline() returns b"" (EOF) and the
    thread exits - it does not hang and does not read the new worker's stderr.

    shutdown_worker sets the stop event only once the process is gone, so a
    worker's last lines - the recording it finalised, "input stream closed" -
    still reach the log, and its stderr never fills up while it is exiting.
    """
    try:
        for raw in iter(worker.stderr.readline, b""):
            if stop.is_set():
                break
            line = raw.decode("utf-8", "replace").rstrip()
            logs.append(line)
            # The list grows without bound (NS_PHASE=1 adds a line per frame)
            # - every consumer reads only the tail, so we keep 2000.
            if len(logs) > 2000:
                del logs[: len(logs) - 2000]
            if _log_wanted(line):
                print(line)
    except Exception:
        pass


# --- A Job Object so a dead parent cannot orphan a worker ----------------
# The worker owns a D3D12 swap-chain window and presents to the screen on its
# own. If Python dies WITHOUT running shutdown_worker - a force-kill, an
# unhandled crash, the GPU-access-loss cascade that ends a session with
# exit 0x40010004 - stdin never closes cleanly and the worker keeps
# presenting. On an HDR session it is presenting HDR10 PQ, so the leftover
# window sits over the desktop as a washed-out layer that survives every app
# restart and clears only on reboot. The tell is Windows' own "waiting for
# nvngx.dll to close" at shutdown: a process nothing owns any more, reported
# by a user who saw exactly that dialog (20.09).
#
# A Job Object with KILL_ON_JOB_CLOSE closes the hole at the OS level. The
# handle lives for the life of THIS process and is never closed by us; when
# Python exits for ANY reason the OS closes it and the kernel terminates
# every worker still in the job. Graceful shutdown is unchanged and remains
# the normal path - this is the net under it, and it also covers the
# converter's transient workers, which take the same start_worker road.
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_k32.SetInformationJobObject.restype = wintypes.BOOL
_k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                         wintypes.LPVOID, wintypes.DWORD]
_k32.AssignProcessToJobObject.restype = wintypes.BOOL
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


#: The job every worker is bound to. None means the platform refused it and
#: the program runs exactly as before - best-effort, never fatal.
_WORKER_JOB = None
_WORKER_JOB_TRIED = False


def _ensure_worker_job():
    """Create the kill-on-close job once; return its handle or None."""
    global _WORKER_JOB, _WORKER_JOB_TRIED
    if _WORKER_JOB_TRIED:
        return _WORKER_JOB
    _WORKER_JOB_TRIED = True
    try:
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(
                job, _JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            _k32.CloseHandle(job)
            raise ctypes.WinError(err)
        _WORKER_JOB = job
        print("[main] worker job object armed (KILL_ON_JOB_CLOSE) - a crashed "
              "NeuralScreen cannot leave the worker running")
    except Exception as exc:
        print(f"[main] worker job object unavailable ({exc}) - a hard crash "
              f"could leave the worker running until reboot", file=sys.stderr)
        _WORKER_JOB = None
    return _WORKER_JOB


def bind_worker_to_job(worker: subprocess.Popen) -> bool:
    """Put a freshly spawned worker into the kill-on-close job. Best-effort."""
    job = _ensure_worker_job()
    if job is None:
        return False
    try:
        if not _k32.AssignProcessToJobObject(job, int(worker._handle)):
            err = ctypes.get_last_error()
            # ERROR_ACCESS_DENIED (5) usually means the parent is itself in a
            # job that forbids nesting - rare on Win8+. The graceful path
            # still runs on a normal exit; only a hard crash stays exposed.
            print(f"[main] worker not bound to the job (err {err}); graceful "
                  f"shutdown still applies", file=sys.stderr)
            return False
        return True
    except Exception as exc:
        print(f"[main] worker job bind failed ({exc})", file=sys.stderr)
        return False


def start_worker(params: dict, width: int, height: int, warmup: int,
                 full_w: int = 0, full_h: int = 0,
                 shm: "SharedFrameBuffer | None" = None) -> tuple[
                     subprocess.Popen, list[str], WorkerReader, threading.Event]:
    """Start the NGX worker in --live mode and send the header.

    width/height is the work resolution (the NGX feature), full_w/full_h is
    the size of the input frames coming from Python (the worker resizes them
    on the GPU through NGX Upscaling; full_w=0 -> the old 1:1 mode).

    Returns (worker, logs, reader, stop): reader is the permanent stdout
    reader thread (see WorkerReader), stop is the event used to finish
    _drain_stderr on shutdown.
    """
    if not WORKER_EXE.is_file():
        raise FileNotFoundError(
            f"worker not found: {WORKER_EXE}\n"
            "Copy nvngx.dll (the built worker) and nvngx_dlssnr.dll into native/."
        )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    worker = subprocess.Popen(
        [str(WORKER_EXE), "--live"],
        cwd=str(NATIVE_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation_flags,
    )
    # Bound before it has run long enough to open a window or a swap chain,
    # so nothing it creates can outlive a parent that dies right after.
    bind_worker_to_job(worker)
    logs: list[str] = []
    stop = threading.Event()
    try:
        threading.Thread(
            target=_drain_stderr, args=(worker, logs, stop), daemon=True,
        ).start()
        # In upscale mode (full_w>0) the worker returns full-res frames - the
        # reader must expect the full sizes, otherwise byte_count will not match.
        out_w = full_w if full_w else width
        out_h = full_h if full_h else height
        reader = WorkerReader(worker, out_w, out_h, shm)

        header = struct.pack(
            HEADER_FMT,
            VIDEO_MAGIC, width, height, int(warmup), 0,  # frame_count=0 -> an endless loop
            # profile, preset and ui_correction: sent, and sent as zero. All
            # three are dead in the 310.8.0 runtime - every value gives a
            # byte-identical frame - so they are not carried in the profiles
            # any more. The wire keeps its shape because the resize command
            # shares this layout and a hundred tests build it by position.
            0, 0, params["style"],
            params["auto_mask"], 0,
            params["intensity"], params["local_tone"],
            params["local_structure"], params["skin_structure"],
            int(full_w), int(full_h),
        )
        worker.stdin.write(header)
        worker.stdin.flush()
        if shm is not None:
            _negotiate_shm(worker, reader, shm)
        return worker, logs, reader, stop
    except BaseException:
        # A failed header/reader/SHM negotiation must not orphan the process.
        shutdown_worker(worker, stop)
        raise


def restart_worker(worker: subprocess.Popen, params: dict, width: int, height: int,
                   warmup: int, full_w: int = 0, full_h: int = 0,
                   stop: threading.Event | None = None,
                   shm: "SharedFrameBuffer | None" = None) -> tuple[subprocess.Popen, list[str], WorkerReader, threading.Event]:
    """Restart the worker at a new resolution (a work_scale change).

    The worker creates the NGX feature from the header sizes and reads exactly
    w*h*4 bytes per frame - the resolution cannot be changed on the fly, only
    by a restart. Warmup on a restart is smaller (30) so the screen does not
    freeze.

    A 2 s pause between shutdown and start: the old worker holds the NGX GPU
    resources (nvngx_dlssnr.dll, 165 MB + a D3D12 device) - initialising a new
    process concurrently on the same GPU hangs or kills it (observed: exit 127
    and a hung recv after apply_settings).

    The old reader/drain die on EOF of the old worker's closed pipes
    (shutdown_worker terminates the process) - there is no read race with the
    new worker: the pipes are different and the old thread physically cannot
    read the stdout of the new process.
    """
    shutdown_worker(worker, stop)
    time.sleep(2.0)
    return start_worker(params, width, height, warmup, full_w, full_h, shm)


def shutdown_worker(worker: subprocess.Popen, stop: threading.Event | None = None) -> None:
    """Graceful shutdown: close stdin (EOF -> the worker exits with code 0), wait 10 s.

    stop is the finish event for _drain_stderr (from start_worker). It is set
    once the process is gone, not before: set first, as it used to be, the
    drain stopped after the next line - the worker's shutdown lines (a
    finalised recording among them) never reached the log, and a worker whose
    last words filled the pipe blocked on them until the 10 s ran out and it
    was killed. The drain still cannot hang: the process's exit is the EOF
    that ends its readline().
    """
    if worker.poll() is not None:
        if stop is not None:
            stop.set()
        return
    try:
        if worker.stdin and not worker.stdin.closed:
            worker.stdin.close()
    except OSError:
        pass
    try:
        code = worker.wait(timeout=10)
        print(f"[main] worker exited cleanly (code {code})")
    except subprocess.TimeoutExpired:
        print("[main] worker did not exit within 10 s - forcing termination")
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("[main] worker could not be reaped after kill",
                      file=sys.stderr)
    finally:
        if stop is not None:
            stop.set()


def require_compatibility(st) -> None:
    """Fail closed before any production worker process is created."""
    # Local import avoids a module cycle: the isolated preflight adapter uses
    # start_worker(), while production lifecycle code calls this guard.
    from compatibility_runtime import require_pass

    require_pass(st)


def wants_low_cost_off(st) -> bool:
    """Whether NR OFF may put the frame pipeline fully to sleep.

    Frame Generation still needs a stream of bypass frames.  Recording and a
    pending screenshot are explicit frame consumers too, so they temporarily
    wake the bypass path even while the NR toggle itself remains off.
    """
    return (bool(st.paused)
            and not bool(st.cfg.get("frame_generation", False))
            and st.recorder is None
            and st.pending_shot is None)


def sync_low_cost_off(st) -> bool:
    """Enter or leave the idle NR OFF state; return its resulting state.

    Normal OFF keeps the worker process warm but closes its capture/present
    channels and stops sending frames.  If that handshake fails, the only
    honest low-cost fallback is to stop the worker: a later NR ON command uses
    the existing worker-failure recovery path to start a clean one.

    This function does NOT own `next_auto_revive` (audit H1). The transient
    failure handler arms exactly one automatic revive and announces it in the
    log; main reads that deadline one line after the call below. Zeroing it
    here disarmed the revive before it was ever read, so in the default
    configuration (FG off, no recording) the promised recovery never came and
    NR stayed off until the user pressed Num1 - the one case the feature
    exists for. A dead worker is still marked failed; the arm is left alone.
    """
    want_idle = wants_low_cost_off(st)
    if want_idle == st.off_suspended:
        return want_idle

    if want_idle:
        st.work_frame = None
        st.output_rgba = None
        try:
            st.guides.previous_gray = None
        except Exception:
            pass

        worker_alive = (st.worker is not None
                        and getattr(st.worker, "poll", lambda: None)() is None)
        if worker_alive and not st.worker_failed:
            try:
                channels.suspend_for_off(st)
            except Exception as exc:
                print(f"[main] low-cost OFF handshake failed ({exc}) - "
                      f"stopping the worker", file=sys.stderr)
                shutdown_worker(st.worker, st.worker_stop)
                st.worker_failed = True
                channels.forget_present(st)
                channels.forget_dda(st)
                channels.forget_out(st)
        elif not worker_alive:
            st.worker_failed = True

        # A transparent HUD layer is enough for the menu while OFF.  The idle
        # branch in main hides it completely whenever the menu is closed.
        st.display.set_hud_only(True)
        st.off_suspended = True
        print("[main] NR OFF: capture, processing and presentation suspended")
        return True

    # A consumer appeared (recording/screenshot/FG) or NR was turned on.
    # Re-arm the channels and insist on a fresh source frame/history.
    st.off_suspended = False
    st.present_mode = False
    st.present_attempted = False
    st.dda_mode = False
    st.dda_attempted = False
    st.gray_active = False
    st.work_frame = None
    st.output_rgba = None
    try:
        st.guides.previous_gray = None
    except Exception:
        pass
    st.display.set_hud_only(True)
    st.display.set_visible(True)
    print("[main] frame pipeline resumed")
    return False


# Applying settings is coalesced: a slider dragged across its range would
# otherwise restart the pipeline on every step. The intermediate values are
# dropped and only the last one is applied.
# 0.5 s rather than 2 s: the change goes through RNSZ inside the live worker
# process, not through a restart with an NGX init/shutdown plus sleep(2) -
# the expensive path is only a fallback now.
RESTART_COOLDOWN = 0.5  # seconds
# How long the settings wait for the user to stop moving a control before
# they are applied (issue #115). The apply itself blocks the frame loop
# while the worker rebuilds, so applying on the first step of a drag
# freezes the panel under the hand that is still dragging; one rebuild at
# the end of the movement is both cheaper and smoother.
APPLY_DEBOUNCE = 0.3  # seconds
RESTART_WARMUP = 10     # warmup after a resolution change (do not freeze the screen)
RACK_TIMEOUT = 20.0     # seconds to wait for RACK after RNSZ


# How many restarts in a row are allowed before NR is turned off: past this
# the worker is not coming back on its own, and spinning through restarts
# only keeps the screen frozen. The user gets an alert instead.
MAX_CONSECUTIVE_RESTARTS = 3
# A transient failure (no-frame, driver hiccup) gets ONE automatic revive
# after this backoff instead of leaving NR off until the user presses Num1.
# A hard failure (0xBAD00001) never auto-revives.
AUTO_REVIVE_BACKOFF = 30.0  # seconds


def teardown_pipeline(st) -> None:
    """Stop everything that is sized to the current width/height.

    Shared by the monitor switch and the window switch: the worker,
    the shared memory and a running recording are all built for one
    frame size and cannot survive a change of it.
    """
    if st.recorder is not None:
        rec = st.recorder
        st.recorder = None
        try:
            if st.recording_finalizer is not None:
                raise RuntimeError("a previous recording is still finalizing")
            rec.finish()
            st.recording_finalizer = rec
            st.recording_finalize_deadline = (
                time.monotonic() + float(rec.FINISH_TIMEOUT_S))
            st.display.alert(UI_STRINGS[st.lang].get(
                "record_finalizing", "Finalizing recording..."))
        except Exception as exc:
            print(f"[main] failed to start recording finalization: {exc}",
                  file=sys.stderr)
    st.pending_shot = None
    st.shot_rgba = None
    shutdown_worker(st.worker, st.worker_stop)
    try:
        st.shm.close()
    except Exception:
        pass


def rebuild_pipeline(st, note: str) -> None:
    """Build the worker, the shm and the overlay for the current size.

    The second half of what used to be _switch_monitor: it reads
    st.width/height/work_w/work_h and rebuilds everything that depends
    on them, resetting the per-worker flags so the main loop
    negotiates DDA1/WGCW, GRAY, OUTS and the window again.
    """
    # Freeze the last picture under the veil before the old worker
    # dies: the rebuild takes ~1 s (new worker, NGX warm-up) and the
    # bare desktop would flash underneath (user: mode-switch flashes).
    # The veil spans the whole monitor even when the next mode is
    # one window - no bare desktop at its edges.
    st.display.enter_switch_mode(st.output_rgba, *st.capture.resolution)
    menu_was_open = st.display.menu.visible
    full_w = st.width if (st.work_w != st.width or st.work_h != st.height) else 0
    full_h = st.height if (st.work_w != st.width or st.work_h != st.height) else 0
    st.shm = SharedFrameBuffer(st.width, st.height)
    # The launch warm-up, not the rebuild's. st.effective_warmup is 120
    # frames - it exists because a COLD card can take seconds to produce
    # its first NGX frame and the frame watchdog would kill the worker on
    # frame 0. By the time anything calls rebuild_pipeline the card has
    # already been running the network, so those 120 frames are a second
    # of veil for nothing: measured on a window resize, 1.85 s from the
    # capture closing to the first real frame, of which ~1 s was the
    # warm-up. do_restart has used RESTART_WARMUP for a full process
    # restart since 1.6 and that is the same feature being recreated.
    # min(), not the constant: a pre-Blackwell card gets 4 and must keep
    # it (audit F3 - the restart storm the shortening exists to prevent).
    warmup = min(st.effective_warmup, RESTART_WARMUP)
    require_compatibility(st)
    st.worker, st.worker_logs, st.reader, st.worker_stop = start_worker(
        st.params, st.work_w, st.work_h, warmup, full_w, full_h,
        st.shm)
    if getattr(st, "worker_failed", False):
        # Rebuilt while the worker stood failed: a monitor, GPU, HDR, Spout or
        # motion change made from the menu of a dead worker. The fresh one IS
        # the revive. The failed state used to survive it, so the loop never
        # fed the new worker - and the armed auto-revive, or Num1, then killed
        # it and started another (with a 2 s pause), or left a card the user
        # had just picked dark until Num1. The pause was the failure's, not
        # the user's (a failure sets both), so it goes with it.
        print("[main] the rebuild replaces the failed worker - NR resumes")
        st.worker_failed = False
        st.next_auto_revive = 0.0
        st.consecutive_restarts = 0
        st.paused = False
        tray = getattr(st, "tray", None)
        if tray is not None:
            try:
                tray._set_state(nr=True)
            except Exception:
                pass
    # The window and the menu are rebuilt, keeping the user settings.
    # A soft resize instead of close()+recreate: the old code went
    # through pygame.quit() and built a fresh window - the screen went
    # black for a moment on every Num5 (user: screen flashes on mode
    # switches). The worker and the shm MUST be torn down and rebuilt
    # (new size), the SDL window does not have to be.
    recreated = False
    try:
        st.display.resize(st.width, st.height)
        recreated = False
    except Exception as exc:
        print(f"[main] soft resize failed ({exc}) - recreating the window")
        # The menu is recreated with the window: snapshot its live
        # state (position, scale, height) into cfg so the restore
        # below picks up where the user left it, not the stale
        # values from the last menu close (user rule 10.09: fixed
        # position until the user drags it).
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
        # A fresh window starts at (0,0) - the primary monitor. The origin
        # belongs to the CHOSEN monitor and must survive the rebuild.
        st.display.set_origin(*getattr(st, "mon_origin", (0, 0)))
        recreated = True
    # In one-window mode the overlay stops hiding from screen capture:
    # the input is that window, not the desktop, so there is no
    # self-capture loop to break - and an outside recorder can see the
    # result. The worker does the same for its picture window.
    st.display.set_excluded_from_capture(st.window_hwnd is None)
    st.display.set_lang(st.lang)
    st.display.menu.set_hotkeys(hotkey_labels(st.hotkey_bindings))
    saved_theme = st.cfg.get("theme")
    if isinstance(saved_theme, str) and saved_theme in THEME_NAMES:
        st.display.menu.set_state({"theme": saved_theme})
    st.display.menu.set_state({"lang": st.lang})
    # The position/scale/height restore applies ONLY to a recreated
    # menu (the window was rebuilt). On a soft resize the menu is
    # alive and keeps exactly what the user set - re-applying the
    # cfg values here would snap it back to the last saved state on
    # every mode switch (user: menu returns to the launch position
    # and scale after picking a window).
    if recreated:
        st.display.menu.set_user_scale(float(st.cfg.get("menu_scale", 1.0)))
        saved_offset = st.cfg.get("menu_offset")
        if isinstance(saved_offset, (list, tuple)) and len(saved_offset) == 2:
            st.display.menu.offset = [int(saved_offset[0]), int(saved_offset[1])]
        saved_height = st.cfg.get("menu_height")
        if isinstance(saved_height, (int, float)) and saved_height > 0:
            st.display.menu.user_height = int(saved_height)
    if menu_was_open:
        st.display.menu.set_state(settings_io.menu_payload(st))
        st.display.menu.visible = True
        st.display.set_menu_opaque(True)
        st.display.set_menu_input(True)
        # The saved offset is honoured as-is: the panel stays where
        # the user left it, clamped to the screen by layout() (user
        # rule 10.09: fixed position until the user drags it).
        if st.window_hwnd is not None:
            st.display.set_fullscreen_layer(st.mon_w, st.mon_h)
    # guides and the buffers follow the new resolution.
    st.guides = TemporalGuideGenerator(
        st.work_w, st.work_h, emit_small=st.motion_small,
        preset=st.cfg.get("flow_preset", "fast"))
    st.buf_full = np.empty((st.height, st.width, 4), dtype=np.uint8)
    # Pipeline flags - the new worker knows nothing.
    st.present_mode = False
    st.present_attempted = False
    st.dda_mode = False
    st.dda_attempted = False
    st.gray_active = False
    st.motion_small = False
    st.motion_attempted = False
    st.out_shm = False
    st.out_attempted = False
    channels.forget_verdict(st)  # a new worker means a new verdict on feature 18
    st.frame_index = 0
    st.pts = 0
    st.work_frame = None
    # The last NR frame belongs to the previous monitor and size.
    # Without the reset a screenshot right after the switch would
    # save it.
    st.output_rgba = None
    settings_io.save_menu_layout(st)
    print(f"[main] pipeline rebuilt: {st.width}x{st.height}, "
          f"work {st.work_w}x{st.work_h} - {note}")
    # The mode-change alert must survive a rebuild: the pipeline
    # teardown clears the alert list, and in a game the user has no
    # time to read a 2.5 s toast. 6 s is long enough to read while
    # the game keeps running (user: "the Num5 alert disappears too
    # fast").
    st.display.alert(note, duration=6.0)


def switch_monitor(st, new_monitor: int | str) -> None:
    """Switch the capture/output monitor - a full pipeline restart.

    The resolution, the capture, the window, the worker and the shm
    are all tied to the monitor - it cannot be switched on the fly.
    Recording stops (the frame size changes). The menu is recreated
    with its theme/language/layout preserved.

    new_monitor is the dxcam output index, or a DXGI devicename
    ('\\\\.\\DISPLAY1') - the menu hands over the devicename so the
    switch is by identity, not by position.
    """
    # Everything downstream of the size - the worker, the shm, the
    # overlay, the flags - is rebuilt by _rebuild_pipeline, which owns
    # those names; this function only picks the monitor and the size.
    if isinstance(new_monitor, str):
        resolved = resolve_output_idx(new_monitor)
        if resolved is None:
            # The menu lists monitors live, the dxcam factory enumerated them
            # at import: a monitor plugged in after launch was offered but not
            # selectable until a restart. Ask the factory again first.
            _refresh_dxcam_factory()
            resolved = resolve_output_idx(new_monitor)
        if resolved is None:
            print(f"[main] monitor {new_monitor!r} is not connected",
                  file=sys.stderr)
            st.display.alert(UI_STRINGS[st.lang]["mon_fail"])
            return
        new_monitor = resolved
    if new_monitor == st.monitor:
        return
    print(f"[main] monitor change: {st.monitor} -> {new_monitor}")
    teardown_pipeline(st)
    try:
        st.capture.close()
    except Exception:
        pass
    # The new monitor: its real resolution, and where it sits.
    st.monitor = new_monitor
    st.cfg["monitor"] = st.monitor
    try:
        st.capture = ScreenCapture(monitor_idx=st.monitor)
    except Exception as exc:
        # The chosen output is gone (unplugged between the menu
        # render and the click, dock changed, driver reset) - the
        # capture must never take the app down. Fall back to the
        # primary output and tell the user.
        print(f"[main] monitor {st.monitor} failed to open: {exc}",
              file=sys.stderr)
        st.capture = ScreenCapture(monitor_idx=0)
        st.monitor = st.capture.monitor_idx
        st.cfg["monitor"] = st.monitor
        st.display.alert(UI_STRINGS[st.lang]["mon_fail"])
    st.width, st.height = st.capture.resolution
    # The monitor's own size goes with it: the menu's input layer in window
    # mode is sized from mon_w/mon_h, and after a switch it kept the old
    # monitor's - spilling onto the neighbour or clipping the panel.
    st.mon_w, st.mon_h = st.width, st.height
    st.work_w, st.work_h = _work_size(st.width, st.height, st.work_scale,
                                      cascade_passes(st))
    # A monitor change IS a capture-source change, so the window target
    # goes with it. It used to survive: only leaving window mode cleared
    # it, so after a switch the program still held the handle of the old
    # window, the main loop's next negotiation found it alive, and the
    # worker was told to capture that window on top of a pipeline built
    # for the whole monitor - the window AND the monitor's desktop in one
    # frame (reported in #96, Codemned: "will capture the window and the
    # entire desktop of the capture monitor"). Clearing it is the same
    # thing leaving window mode does; pointing at a window again is Num5
    # or the window list, which is how it was set the first time.
    if st.window_hwnd is not None:
        print("[main] monitor change clears the window target "
              f"(was hwnd=0x{st.window_hwnd:X})")
        st.window_hwnd = None
        # The follow state describes a window that is no longer the
        # source: a stale follow_size makes follow_window skip the size
        # check for a window nobody is capturing.
        st.follow_size = None
        st.follow_pos = None
        st.follow_resize = None
    # The worker reads NS_OUTPUT / NS_WINDOW_POS at every OpenDda/OpenPresent,
    # and the overlay is rebuilt below - so the new monitor's identity goes
    # out before the rebuild (issues #28, #33).
    from startup import _apply_monitor_env
    st.mon_origin = _apply_monitor_env(st.capture)
    st.display.set_origin(*st.mon_origin)
    rebuild_pipeline(st, f"Monitor {st.monitor}: {st.width}x{st.height}")


def _restore_after_failed_probe(st, hwnd: int) -> None:
    """The probe of `hwnd` failed: put the worker's source back as it was.

    The probe re-points the running worker at the window as a side effect
    (the worker closes its current capture first), so a refusal leaves it on
    nothing, or on a window that is not the picture. What goes back is the
    source this pipeline was built for:

      * the desktop - DDA1 at the monitor's size, and its DACK awaited so it
        cannot answer somebody else's DDA1 later (audit #4, F2);
      * another window, the one being processed - pointed at again, and kept
        only if it still captures at the size the pipeline has. It used to go
        to the desktop at THAT window's size, which is a crop of the monitor's
        top-left corner shown where the window was, while everything else
        still believed in window mode;
      * the processed window itself (the follow path re-probing it) - it can
        no longer be captured, so window mode ends honestly.
    """
    current = st.window_hwnd
    try:
        if current is None:
            send_dda(st.worker, st.width, st.height)
            st.reader.wait_dack(timeout=5.0)
            restored = True
        elif int(current) != int(hwnd):
            aw, ah = channels.probe_window_capture(st, current)
            restored = (int(aw), int(ah)) == (int(st.width), int(st.height))
        else:
            restored = False
    except Exception as exc:
        print(f"[main] the previous source did not come back ({exc})",
              file=sys.stderr)
        restored = False
    if restored or current is None:
        st.display.exit_switch_mode()  # the overlay was raised before the probe
        if current is None:
            # The size that was refused is remembered, or follow_window would
            # ask for the same switch again in half a second and keep asking
            # (user, 13.09: the alert kept coming back). Only in desktop mode:
            # in window mode follow_size is the processed window's.
            rect = window_frame_rect(hwnd) if hwnd else None
            if rect is not None:
                st.follow_size = rect[2:]
        return
    print("[main] the processed window cannot be captured any more - "
          "back to the desktop", file=sys.stderr)
    switch_window(st, 0)


def switch_window(st, hwnd: int) -> None:
    """Point the capture at one window (hwnd) or back at the desktop (0).

    The window's capture size is not something to guess: GetWindowRect
    includes the invisible resize borders and the DWM frame, while the
    capture produces the compositor's own surface. So the running
    worker is asked first (WGCW answers with the real size), and the
    pipeline is rebuilt for exactly that.
    """
    if hwnd and not st.want_dda:
        st.display.alert(UI_STRINGS[st.lang]["win_fail"])
        print("[main] window mode needs capture in the worker "
              "(capture_in_worker is off)", file=sys.stderr)
        return
    # The switch overlay goes up BEFORE the probe: the probe can take
    # ~1 s (WGCW round trip with the running worker) and the old
    # pipeline is already dead by then - without the overlay the
    # desktop sits bare (user: black gap on one-window mode switch).
    # enter_switch_mode is idempotent and covers the rebuild too.
    st.display.enter_switch_mode(st.output_rgba, *st.capture.resolution)
    if hwnd:
        # A minimised window has nothing to capture: WGC would answer with
        # the last size it had and then go silent. The list offers them
        # (a menu showing one of five open programs reads as broken), so
        # picking one restores it first and lets the frame arrive.
        if ctypes.windll.user32.IsIconic(ctypes.c_void_p(hwnd)):
            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), 9)  # SW_RESTORE
            time.sleep(0.25)  # the window has to be drawn before it is measured
        try:
            aw, ah = channels.probe_window_capture(st, hwnd)
        except Exception as exc:
            st.display.alert(UI_STRINGS[st.lang]["win_fail"])
            print(f"[main] the worker cannot capture that window: {exc}",
                  file=sys.stderr)
            _restore_after_failed_probe(st, hwnd)
            return
        if aw < 64 or ah < 64:
            # Below the work-resolution floor there is nothing to
            # process - and a work size larger than the frame is how
            # the worker gets killed.
            print(f"[main] the window is {aw}x{ah} - too small to process",
                  file=sys.stderr)
            st.display.alert(UI_STRINGS[st.lang]["win_fail"])
            _restore_after_failed_probe(st, hwnd)
            return
        teardown_pipeline(st)
        st.window_hwnd = int(hwnd)
        st.width, st.height = int(aw), int(ah)
        note = UI_STRINGS[st.lang]["win_mode_on"]
    else:
        teardown_pipeline(st)
        st.window_hwnd = None
        st.width, st.height = st.capture.resolution
        note = UI_STRINGS[st.lang]["win_mode_off"]
    st.work_w, st.work_h = _work_size(st.width, st.height, st.work_scale,
                                       cascade_passes(st))
    st.follow_pos = None        # a fresh overlay starts at (0,0)
    st.follow_resize = None
    # The window size this pipeline was built for, as WE measure it. It is
    # NOT st.width/height: that is the size the WORKER's capture reported,
    # and on Windows 10 the two are different numbers for the same window
    # (see follow_window).
    rect = window_frame_rect(hwnd) if hwnd else None
    st.follow_size = rect[2:] if rect else None
    rebuild_pipeline(st, note)


def apply_spout(st, enabled: bool) -> None:
    """Toggle the Spout2 bridge: the worker must be restarted.

    The bridge is initialised once inside the worker process
    (SpoutBridgeInit reads NS_SPOUT at startup) - there is no protocol
    message for it, so the only way in or out is a fresh worker. The
    same path the monitor switch takes: teardown, set the environment,
    rebuild. The picture size does not change, so the overlay, the
    menu and the capture source survive.
    """
    st.cfg["spout"] = bool(enabled)
    os.environ["NS_SPOUT"] = "1" if enabled else "0"
    settings_io.save_menu_layout(st)
    print(f"[main] Spout2 output: {'on' if enabled else 'off'} - restarting the worker")
    teardown_pipeline(st)
    rebuild_pipeline(st, UI_STRINGS[st.lang].get(
        "spout_on" if enabled else "spout_off",
        "Spout2 output ON" if enabled else "Spout2 output OFF"))


def resize_window_live(st, frame_w: int, frame_h: int) -> bool:
    """The followed window changed size: reconfigure, do not restart.

    A resize used to go through switch_window, which kills the worker and
    starts a new one. Measured on a browser leaving fullscreen: 1.85 s of
    veil, brought down to 0.97 s by not paying the cold-start warm-up, and
    the rest is the process itself. Every fullscreen toggle in a captured
    window cost that, which is what "it dims and animates while I watch a
    video" turned out to be.

    Nothing about a resize needs a new process. Every piece is already a
    live command with an ack:

      * WGCW re-points the worker's capture at the window and answers with
        the size it negotiated - switch_window already sends this one to
        the LIVE worker, before it tears it down;
      * RNSZ rebuilds the feature and the textures at the new work and full
        size inside the running process (~150 ms);
      * OUTS re-opens the result section - open_out creates a new section
        under a new name whenever the size changes;
      * WNDO re-opens the worker's own window, and OpenPresent starts with
        ClosePresent, so it is a resize;
      * MOTS and GRAY follow the guides, which follow the work size.

    Returns False when it will not do it, and the caller falls back to the
    full rebuild. The one that matters: only when the WORKER is capturing.
    Otherwise the colour still travels through the shared section, whose
    colour region is sized for the window the pipeline was built for, and a
    larger window would run off the end of it.
    """
    if st.window_hwnd is None or not st.dda_mode:
        return False
    try:
        aw, ah = channels.probe_window_capture(st, st.window_hwnd)
    except Exception as exc:
        print(f"[main] live resize: the worker would not re-point at the "
              f"window ({exc})", file=sys.stderr)
        return False
    if aw < 64 or ah < 64:
        return False
    new_w, new_h = _work_size(int(aw), int(ah), st.work_scale,
                              cascade_passes(st))
    new_full_w = int(aw) if (new_w != aw or new_h != ah) else 0
    new_full_h = int(ah) if (new_w != aw or new_h != ah) else 0
    try:
        t0 = time.perf_counter()
        send_resize(st.worker, st.params, new_w, new_h, RESTART_WARMUP,
                    new_full_w, new_full_h, st.nr_small, st.nr_direct,
                    getattr(st, "nr_passes", 1))
        st.reader.wait_rack(timeout=RACK_TIMEOUT)
        st.reader.set_output_size(new_full_w or new_w, new_full_h or new_h)
        # A resize rebuilds the cascade, and a rebuilt cascade reads the main
        # set - so the second set has to follow every RNSZ, not just the ones
        # the menu sends.
        _send_per_pass_if_any(st)
    except Exception as exc:
        print(f"[main] live resize: RNSZ did not go through ({exc})",
              file=sys.stderr)
        return False
    st.width, st.height = int(aw), int(ah)
    st.work_w, st.work_h = new_w, new_h
    # The guides carry the work size in their buffers, and the worker reads
    # exactly that many bytes of motion - they change together or the stream
    # desynchronises (see do_restart).
    st.guides = TemporalGuideGenerator(
        new_w, new_h, emit_small=st.motion_small,
        preset=st.cfg.get("flow_preset", "fast"))
    channels.sync_motion_size(st)
    channels.sync_gray(st)
    # Both of these are sized for the old frame. Re-negotiated here rather
    # than through the forget_* flags: those exist for a worker that died,
    # and forget_present would drop the display out of HUD-only mode for a
    # frame on the way.
    channels.forget_out(st)
    channels.enable_out_shm(st)
    if st.present_mode:
        channels.enable_present(st)
    st.display.resize(st.width, st.height)
    st.follow_pos = None
    st.follow_size = (frame_w, frame_h)
    st.follow_resize = None
    st.frame_index = 0
    st.pts = 0
    st.work_frame = None
    st.last_restart = time.monotonic()
    print(f"[main] the window is now {frame_w}x{frame_h} - reconfigured live "
          f"in {(time.perf_counter() - t0) * 1000:.0f} ms "
          f"(capture {st.width}x{st.height}, work {new_w}x{new_h})")
    return True


def apply_hdr(st, enabled: bool) -> None:
    """Toggle HDR compatibility: the worker must be restarted.

    HdrEnabled() is read once per worker process, and the whole chain
    hangs off it: DuplicateOutput1 with an FP16 format list instead of
    DuplicateOutput, a WGC pool in R16G16B16A16Float instead of BGRA, an
    scRGB swap chain instead of an 8-bit one. None of that can be changed
    under a running capture, so the switch takes the same road as the
    Spout bridge: teardown, set the environment, rebuild.

    The mode is experimental and off by default. On an SDR display it
    changes nothing that can be seen - the capture comes back 8-bit and
    the worker says so in its "[hdr] capture=" line.
    """
    st.cfg["hdr"] = bool(enabled)
    os.environ["NS_HDR"] = "1" if enabled else "0"
    settings_io.save_menu_layout(st)
    print(f"[main] HDR compatibility: {'on' if enabled else 'off'} - "
          f"restarting the worker")
    teardown_pipeline(st)
    # A fresh worker has said nothing about HDR yet, and the notice is
    # about what the capture actually did - so let it speak again.
    st.hdr_alerted = False
    rebuild_pipeline(st, UI_STRINGS[st.lang].get(
        "hdr_mode_on" if enabled else "hdr_mode_off",
        "HDR compatibility ON" if enabled else "HDR compatibility OFF"))


def apply_motion_backend(st, value: str) -> None:
    from motion_backend import normalize_backend
    value = normalize_backend(value)
    # Only a recorded choice that already matches is a no-op. A config that
    # has no key yet (an old file, a synthetic state) must still take the
    # value and persist it: with NVOFA as the shipped default, comparing a
    # missing key against it made the first explicit selection do nothing.
    current = st.cfg.get("motion_backend")
    if current is not None and normalize_backend(current) == value:
        return
    st.cfg["motion_backend"] = value
    os.environ["NS_MOTION_BACKEND"] = value
    settings_io.save_menu_layout(st)
    teardown_pipeline(st)
    rebuild_pipeline(st, UI_STRINGS[st.lang].get(
        "motion_restarted", "Motion backend changed - worker restarted"))


def follow_monitor(st) -> None:
    """Rebuild when the desktop resolution changes under a running pipeline.

    Everything downstream of the size is built once: the worker's NGX
    feature, the shared memory, the overlay window. Nothing was watching
    the monitor itself, so switching the desktop from 1440p to 4K left the
    program processing a 2560x1440 island in the corner of a 4K screen,
    with the overlay unable to grow past its old bounds (user report).

    st.capture.resolution is no help - it is what the monitor was when the
    capture session opened. The size is asked of Windows directly, and the
    rebuild waits half a second for it to settle: a mode change goes
    through intermediate sizes, and rebuilding on each one would mean
    several worker restarts for one switch.

    Window mode is not affected: there the frame follows the window, and
    follow_window already owns that.
    """
    if st.window_hwnd is not None or st.worker_failed or not st.running:
        return
    devicename = st.capture.devicename
    size = monitor_size(devicename)
    if size is None:
        # The monitor this pipeline was built on is GONE - unplugged, a dock
        # removed, the display switched off in Windows. The capture is pointed
        # at a devicename DXGI no longer exposes, so nothing it produces can
        # be right, and the worker refuses the output on its next reopen
        # ("NS_OUTPUT is not an output of this adapter"). That refusal used to
        # be the end of it: the pipeline kept running against a display that
        # does not exist until the user quit (#128). Move to a monitor that IS
        # there, through the same switch a user's choice goes through.
        #
        # One report per absence: this runs every 30 frames, and a monitor
        # that stays gone would otherwise fill the log with the same line.
        if getattr(st, "mon_gone", None) == devicename:
            return
        # Debounced, like a size change: a mode change hides the display for a
        # moment, and rebuilding on that would be paid for nothing.
        now = time.monotonic()
        if st.mon_resize is None or st.mon_resize[0] != "monitor-gone":
            st.mon_resize = ("monitor-gone", now)
            return
        if now - st.mon_resize[1] < 0.5:
            return
        st.mon_resize = None
        st.mon_gone = devicename
        # A live monitor, by identity: the switch must not be a no-op when the
        # one that vanished was output 0, and it must be a monitor Windows
        # actually reports.
        live = None
        try:
            for _idx, _w, _h, name in list_monitors():
                if name and name != devicename:
                    live = name
                    break
        except Exception:
            live = None
        if live is None:
            print(f"[main] the captured monitor {devicename!r} is gone and no "
                  f"other monitor is connected - nothing to switch to",
                  file=sys.stderr)
            return
        print(f"[main] the captured monitor {devicename!r} is gone - switching "
              f"to {live!r}", file=sys.stderr)
        st.display.alert(UI_STRINGS[st.lang]["mon_fail"])
        switch_monitor(st, live)
        return
    # The monitor is back (or was never gone): forget the absence, so the
    # same monitor vanishing again is reported again.
    st.mon_gone = None
    # Where the monitor sits counts as much as its size. Making another
    # display the main one - on Windows 11 the only way to move the taskbar to
    # it - moves every monitor's corner on the virtual desktop without
    # resizing any of them, and the overlay and the worker's picture stayed
    # at the old corner, off by the difference (#96).
    origin = monitor_origin(devicename)
    current_origin = tuple(getattr(st, "mon_origin", None) or (0, 0))
    if origin is None:
        origin = current_origin
    origin = tuple(origin)
    if size == (st.width, st.height) and origin == current_origin:
        st.mon_resize = None
        return
    key = (size, origin)
    now = time.monotonic()
    if st.mon_resize is None or st.mon_resize[0] != key:
        st.mon_resize = (key, now)
        return
    if now - st.mon_resize[1] < 0.5:
        return
    st.mon_resize = None
    print(f"[main] the monitor is now {size[0]}x{size[1]} at {origin} "
          f"(was {st.width}x{st.height} at {current_origin}) - rebuilding "
          f"the pipeline")
    teardown_pipeline(st)
    # The dxcam factory caches the outputs it enumerated at import, and a
    # mode change is exactly what makes that cache wrong - a fresh capture
    # built on it would come back at the old size.
    try:
        st.capture.close()
    except Exception:
        pass
    _refresh_dxcam_factory()
    # By name, not by the old flat index: a new main display renumbers the
    # outputs (output 0 is always the primary), and the old index would open
    # the other monitor.
    idx = resolve_output_idx(devicename)
    try:
        st.capture = ScreenCapture(monitor_idx=st.monitor if idx is None else idx)
    except Exception as exc:
        print(f"[main] the capture did not survive the mode change: {exc}",
              file=sys.stderr)
        st.capture = ScreenCapture(monitor_idx=0)
        st.display.alert(UI_STRINGS[st.lang]["mon_fail"])
    st.monitor = st.capture.monitor_idx
    st.cfg["monitor"] = st.monitor
    st.width, st.height = st.capture.resolution
    st.mon_w, st.mon_h = st.width, st.height
    st.work_w, st.work_h = _work_size(st.width, st.height, st.work_scale,
                                       cascade_passes(st))
    # The new corner reaches the overlay now and the worker at its spawn
    # (NS_WINDOW_POS is read from the worker's own environment).
    from startup import _apply_monitor_env
    st.mon_origin = _apply_monitor_env(st.capture)
    st.display.set_origin(*st.mon_origin)
    rebuild_pipeline(st, f"{st.width}x{st.height}")


def apply_gpu(st, index: int) -> None:
    """Run the worker on another card: a restart, like the Spout toggle.

    The adapter is chosen before the D3D12 device exists, so there is no
    way to move a running worker - NS_GPU is read once at process start.
    The frame size does not change, so the overlay, the menu and the
    capture source survive the restart.

    Both the network and the capture move together: the captured frame
    reaches D3D12 through an NT-shared texture, and a shared handle does
    not cross adapters. Choosing a card that drives no display therefore
    fails in the worker, with the reason in the log, rather than showing
    a black picture.
    """
    # null ("the worker chooses") is a legitimate previous value, not 0.
    previous = settings_io._optional_index(st.cfg.get("gpu"))
    if int(index) == previous:
        return
    previous_key = getattr(st, "compatibility_key", None)
    previous_result = getattr(st, "compatibility_result", None)
    st.cfg["gpu"] = int(index)
    os.environ["NS_GPU"] = str(int(index))
    # If the chosen card turns out to drive no display, the capture stays on
    # the display card and every frame crosses through shared memory - a
    # split pipeline. That is worth one alert, and only after a deliberate
    # switch (on an Optimus laptop it is the normal state from launch).
    st.gpu_switch_pending = True
    print(f"[main] GPU: adapter {index} - running compatibility preflight")
    teardown_pipeline(st)
    candidate_ok = False
    try:
        from compatibility_runtime import run_preflight

        candidate_ok = run_preflight(st).is_pass
    except Exception as exc:
        print(f"[main] GPU compatibility preflight failed ({exc})",
              file=sys.stderr)

    if candidate_ok:
        try:
            rebuild_pipeline(
                st, UI_STRINGS[st.lang].get("gpu_switched", "GPU switched"))
            candidate_ok = gpu_came_up(st)
        except Exception as exc:
            print(f"[main] adapter {index} production start failed ({exc})",
                  file=sys.stderr)
            candidate_ok = False

    if candidate_ok:
        # It works - so if it was marked as refusing the pass before, that
        # mark is stale (a driver update is the usual reason) and goes.
        marked = [i for i in st.cfg.get("gpu_no_nr") or [] if int(i) != int(index)]
        st.cfg["gpu_no_nr"] = marked
        # Only now: a config that remembers a card the network cannot use
        # comes back on the same dead card at the next launch, and the menu
        # to change it back is inside the overlay that a dead worker hides
        # (issue #33).
        settings_io.save_menu_layout(st)
        return
    print(f"[main] adapter {index} cannot run the network - back to {previous}",
          file=sys.stderr)
    # Remember it, so the picker can say so instead of offering two entries
    # that look identical - DXGI lists some cards twice and only one of the
    # two can run the pass (issue #33). A note, not a lock: the entry stays
    # selectable, and the mark is dropped the moment it does work.
    marked = sorted({int(i) for i in (st.cfg.get("gpu_no_nr") or [])} | {int(index)})
    st.cfg["gpu_no_nr"] = marked
    st.cfg["gpu"] = previous
    if previous is None:
        os.environ.pop("NS_GPU", None)
    else:
        os.environ["NS_GPU"] = str(previous)
    st.compatibility_key = previous_key
    st.compatibility_result = previous_result
    st.gpu_switch_pending = False
    teardown_pipeline(st)
    rebuild_pipeline(st, UI_STRINGS[st.lang].get(
        "gpu_reverted", "That GPU cannot run the neural pass - previous card"))
    st.display.alert(UI_STRINGS[st.lang].get(
        "gpu_reverted", "That GPU cannot run the neural pass - previous card"))


#: How long a fresh worker is given to say whether the network came up on
#: the card just chosen. NGX answers in well under a second; five seconds
#: is the margin for a cold driver, and a slower one is not called failed.
GPU_VERDICT_TIMEOUT = 5.0


def gpu_came_up(st, timeout: float = GPU_VERDICT_TIMEOUT) -> bool:
    """Did the network come up on the card the worker was just started on?

    The worker says so itself, and the lines are named in exactly one
    place - settings_io.nr_verdict, which the menu's own verdict reads too.
    A process that exited says it without words.

    Silence is NOT failure. A card that takes its time still works, and
    turning a slow start into an automatic revert would be worse than the
    bug this guards against.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker = getattr(st, "worker", None)
        if worker is not None and worker.poll() is not None:
            return False
        verdict = nr_verdict(reversed(st.worker_logs[-120:]))
        if verdict is not None:
            return verdict
        time.sleep(0.1)
    return True


def follow_window(st) -> None:
    """Keep the HUD layer on the window being processed.

    Position every frame - it is one DWM call and a SetWindowPos only
    when the window actually moved. A SIZE change is a different
    animal: the worker, the shared memory and every texture are built
    for one frame size, so it means rebuilding the pipeline - and doing
    that on every pixel while someone drags a resize handle would be
    unusable. The new size has to hold still for half a second first.
    """
    if st.window_hwnd is None:
        return
    # The mode-switch veil owns the layer until the first frame of the new
    # pipeline arrives: it spans the whole monitor, and moving or resizing
    # it now slides the veil off the desktop - the bare desktop showed
    # along the top and left edges and the mark drifted with it (the
    # reported window-mode switch glitch). switch_window resets follow_pos,
    # so the position re-syncs on the first frame after the veil is down.
    if getattr(st.display, "is_switch_active", None) and st.display.is_switch_active():
        return
    # The worker is dead: the overlay must stay hidden (issue #3) -
    # nothing would fill it, and showing it covers the desktop with
    # a black window.
    if st.worker_failed:
        return
    rect = window_frame_rect(st.window_hwnd)
    if rect is None:
        return
    x, y, w, h = rect
    if ctypes.windll.user32.IsIconic(ctypes.c_void_p(st.window_hwnd)):
        # Minimised: the capture goes silent (the worker hides its own
        # window for the same reason), so the HUD goes with it rather
        # than floating over whatever is underneath.
        #
        # EXCEPT while the menu is open. The layer IS the menu then, and
        # hiding it makes the app look dead: the click on the taskbar
        # button logs "menu opened", and the very next frame hides the
        # window it just showed, so the button appears to do nothing at
        # all. Measured on the bench: six "overlay menu opened" against
        # zero visible menus, with "[wgc] the window is minimised - the
        # overlay is hidden" in between, until the captured window was
        # restored and the menu came back by itself ("the window is back
        # - the overlay is shown").
        #
        # The menu is a window of OURS and its subject is our own
        # settings - it does not need the captured window to be alive.
        # The picture behind it stops (there is nothing to capture) and
        # that is correct; the panel itself must stay visible and
        # clickable so the user can switch window mode or the source off.
        if not st.display.menu.visible and st.display.is_visible():
            st.display.set_visible(False)
            st.follow_pos = None
        return
    if not st.display.is_visible():
        st.display.set_visible(True)
    moved = (x, y) != st.follow_pos
    # While the menu is open the user may be dragging it by its title
    # bar - following the captured window would yank the HUD (and the
    # menu with it) back onto the window every frame, which is the
    # "does not grab, stutters, flickers" report. The position is
    # re-synced on the first frame after the menu closes.
    if moved and not st.display.menu.visible:
        st.display.move_to(x, y)
        st.follow_pos = (x, y)
    # Both windows are topmost, and within that group the one raised
    # last is on top. The worker re-asserts its picture window every
    # time the target moves, so the HUD has to keep coming back up -
    # otherwise the menu ends up UNDER the picture, invisible both to
    # the user and to a recorder. Measured: without this the menu
    # changed 0% of what an outside capture saw.
    if moved or st.frame_index % 30 == 0:
        st.display.raise_topmost()
    # Against the size WE measured when the pipeline was built - not
    # against st.width/height, which is what the worker's capture
    # reported. On Windows 10 those are two different numbers for one
    # window: WGC hands back the GetWindowRect size, including the
    # invisible resize border, while this is the DWM extended frame.
    # A user's log (issue #30) showed capture 1354x853 against frame
    # 1340x846 - 14 and 7 pixels of border - so the comparison was never
    # equal, the pipeline rebuilt every half second, and the screen blinked
    # once every two seconds until window mode was turned off. On Windows 11
    # the two agree, which is why it never showed up here.
    if st.follow_size is None:
        st.follow_size = (w, h)
    # A couple of pixels is not a resize worth a rebuild. Two numbers
    # describe this window and they already disagree by a border on
    # Windows 10 (issue #30), and a 4K log shows the pair 3840x2160 /
    # 3840x2159 for one maximised browser. A rebuild costs a second of
    # veil; being two pixels stale costs the overlay two pixels, and the
    # worker re-opens its own capture when the window really changes size.
    #
    # Not the cause of the reported video dimming, though: watched at
    # 100 Hz, a browser leaving fullscreen moves the frame 72 px in one
    # step with no intermediate value at all. That one is a real resize
    # and it does rebuild - see the warm-up commit for what it now costs.
    if max(abs(w - st.follow_size[0]), abs(h - st.follow_size[1])) > 2:
        now = time.monotonic()
        if st.follow_resize is None or st.follow_resize[0] != (w, h):
            st.follow_resize = ((w, h), now)
        elif now - st.follow_resize[1] > 0.5:
            st.follow_resize = None
            # Below the floor there is nothing to process, and asking is not
            # free: switch_window probes the worker, puts the capture back on
            # the desktop and shows an alert. It also used to leave
            # follow_size alone, so the watcher tried again half a second
            # later, forever - four "the window is 3840x60 - too small to
            # process" in one session, with an alert each time (user, 13.09).
            # Remember the size and wait for it to become usable.
            if w < 64 or h < 64:
                if st.follow_size != (w, h):
                    print(f"[main] the window is {w}x{h} - too small to "
                          f"process, staying where we are")
                st.follow_size = (w, h)
                return
            # Live first: no new process, no veil, ~150 ms instead of ~1 s.
            # It refuses when it cannot do it safely, and then the old road
            # is still there.
            if resize_window_live(st, w, h):
                return
            print(f"[main] the window is now {w}x{h} - rebuilding the pipeline")
            switch_window(st, st.window_hwnd)
    else:
        st.follow_resize = None


def _send_per_pass_if_any(st, wait: bool = True,
                          timeout: float | None = None) -> None:
    """Tell the worker what passes 2..N should use, if the user set anything.

    Silence means "every pass uses the main set", which is what a worker does
    on its own - so a config that never touched the second set sends nothing at
    all and the old behaviour is untouched.

    Called after every RNSZ, including a full restart: a fresh feature is built
    at ONE pass and knows nothing about a second set, so a cascade the user
    configured has to be re-stated to each new worker. Skipping that is the same
    class of bug the pass count itself once had - the panel says four, the
    worker runs one, and the log says nothing.

    `wait=False` for the path where the worker has just started and the stream
    is not flowing yet: the command is out, the ack is picked up by the reader
    on the next frame, and blocking here would stall the first RNSZ instead.
    """
    per_pass = getattr(st, "nr_pass_params", None)
    if not per_pass or int(getattr(st, "nr_passes", 1)) < 2:
        return
    try:
        send_per_pass(st.worker, per_pass, enabled=True)
        if wait:
            st.reader.wait_per_pass(timeout=timeout or RACK_TIMEOUT)
    except Exception as exc:
        # Not fatal: the frame is still correct, it just uses one set for
        # every pass. Say so rather than letting the panel disagree with the
        # picture in silence.
        print(f"[main] the per-pass parameters did not go through ({exc}) - "
              f"passes 2+ use the main set", file=sys.stderr)


def do_restart(st, new_scale: float, new_profile: str, new_params: dict,
                full: bool = False, new_small: bool | None = None) -> None:
    """Change work_scale/profile/parameters WITHOUT recreating pygame or the capture.

    The main path is RNSZ: the worker recreates the NGX feature inside
    the same process and answers RACK (~0.3 s instead of ~3 s for a
    restart). If RNSZ did not go through - a full restart of the worker
    process.

    CRITICAL: guides is recreated at the NEW work resolution and
    stored back into the state (st.guides). It used to be assigned to
    a local by mistake - the outer guides stayed at the old size and
    main sent motion of the old size, while the worker reads exactly
    new_w*new_h*4 bytes:
      * scaling up   -> the worker waits for the missing bytes and goes
        silent, main hangs in reader.recv(60 s), the window stops
        pumping messages -> Application Hang (Event Id 1002) -> exit 127;
      * scaling down -> the extra bytes desynchronise the stream, the
        worker sees a foreign magic and exits -> BrokenPipe -> a restart
        loop.
    Reproduced in isolation: _work/test_stale_motion_repro.py (case A -
    TimeoutError, B - BrokenPipeError, C - the control, OK).
    pygame/D3D11 had nothing to do with the crashes.
    """
    st.work_scale = new_scale
    st.cfg["profile"] = new_profile
    st.params = new_params
    if new_small is not None and new_small != st.nr_small:
        st.nr_small = new_small
        st.cfg["nr_small"] = st.nr_small
        # The environment is what a freshly started worker reads; the
        # live one is told through the resize below.
        os.environ["NS_NR_SMALL"] = "1" if st.nr_small else "0"
        settings_io.save_menu_layout(st)
    new_w, new_h = _work_size(st.width, st.height, st.work_scale,
                                       cascade_passes(st))
    new_full_w = st.width if (new_w != st.width or new_h != st.height) else 0
    new_full_h = st.height if (new_w != st.width or new_h != st.height) else 0
    print(f"[main] applying: profile {new_profile!r}, "
          f"work_scale {st.work_scale:.2f} ({new_w}x{new_h}), params {st.params}")
    st.display.alert(UI_STRINGS[st.lang]["settings_applied"])

    applied = False
    if st.worker.poll() is None and not full:
        try:
            t_rnsz = time.perf_counter()
            send_resize(st.worker, st.params, new_w, new_h, RESTART_WARMUP,
                        new_full_w, new_full_h, st.nr_small, st.nr_direct,
                        getattr(st, "nr_passes", 1))
            st.reader.wait_rack(timeout=RACK_TIMEOUT)
            st.reader.set_output_size(new_full_w or new_w, new_full_h or new_h)
            # The per-pass set rides its own command, and it has to follow
            # every RNSZ: a resize rebuilds the cascade, and a rebuilt cascade
            # reads the main set unless it is told otherwise. Sending it here
            # rather than only on the menu's own path is what makes the second
            # set survive a resolution change - and a restart.
            _send_per_pass_if_any(st)
            applied = True
            print(f"[main] RNSZ applied: {new_w}x{new_h} in "
                  f"{(time.perf_counter() - t_rnsz) * 1000:.0f} ms")
        except Exception as exc:
            print(f"[main] RNSZ did not go through ({exc}) - full worker restart",
                  file=sys.stderr)
    if not applied:
        # The worker process is going down and a fresh one warms up for
        # seconds - the same full-screen veil the mode switches raise (user:
        # every long switch gets the veil and the mark, never a bare
        # desktop). The RNSZ path above is fast and keeps the picture, so
        # it does not raise one.
        st.display.enter_switch_mode(st.output_rgba, *st.capture.resolution)
        require_compatibility(st)
        st.worker, st.worker_logs, st.reader, st.worker_stop = restart_worker(
            st.worker, st.params, new_w, new_h, RESTART_WARMUP,
            new_full_w, new_full_h, st.worker_stop, st.shm)
        channels.forget_present(st)
        # The new worker knows nothing about DDA/gray: reset the flags
        # so the main loop sends DDA1/GRAY again. Otherwise the frames
        # go out with NO_COLOR to a worker that is not capturing - a
        # desync and a restart loop.
        channels.forget_dda(st)
        channels.forget_out(st)
        channels.forget_verdict(st)
        # And it knows nothing about a second parameter set either: a fresh
        # process always starts at one pass with the main set. The ack is
        # picked up by the reader on the next frame, so this does not block.
        _send_per_pass_if_any(st, wait=False)

    # The order matters: work_w/work_h and guides change TOGETHER,
    # otherwise the motion size drifts away from what the worker
    # expects (see the docstring).
    # A parameter change does not move the work size, and a fresh generator
    # would throw away the optical-flow history for nothing: the next frame
    # would come back as a scene cut and the network would start its
    # temporal accumulation again, which is visible as a small settle.
    if (new_w, new_h) != (st.work_w, st.work_h) or st.guides is None:
        st.guides = TemporalGuideGenerator(
            new_w, new_h, emit_small=st.motion_small,
            preset=st.cfg.get("flow_preset", "fast"))
    st.work_w, st.work_h = new_w, new_h
    channels.sync_motion_size(st)  # the flow resolution may have changed
    channels.sync_gray(st)         # the gray channel lives in the worker, size = guides flow
    st.frame_index = 0
    st.pts = 0
    st.work_frame = None  # the indices are reset - a fresh grab is needed
    st.tray._set_state(scale=st.work_scale)
    st.last_restart = time.monotonic()


def request_apply(st, new_scale: float, new_profile: str, new_params: dict,
                  new_small: bool | None = None) -> None:
    """Ask for the settings to be applied once the user stops changing them.

    The apply is not instantaneous and cannot be: it sends RNSZ to the
    worker and waits for the ack (about 0.3 s), and while it waits the
    frame loop is not pumping the panel's events - the menu freezes under
    the hand that is still moving a control. That is what the reporter of
    #115 described ("it hangs a bit while changing") about the Boost
    switch and the resolution slider next to it.

    So every request goes into ONE deferred slot and is applied
    APPLY_DEBOUNCE seconds after the LAST change, with the cooldown
    between two applies still enforced by the main loop. Dragging a
    slider therefore rebuilds the feature once, at the value the user
    let go on, instead of once per step - and the first step no longer
    freezes the panel mid-drag.

    The single entry point for the settings window, the tray and the
    hotkeys: the cooldown check used to live only on the settings
    path, while the tray and the arrows called _do_restart directly -
    key repeat on an arrow produced a flood of RNSZ.
    """
    # new_small is the only optional field: None means "this request did
    # not touch the Boost switch", so a switch already waiting in the slot
    # keeps its value instead of being dropped by the next request.
    waiting = getattr(st, "pending_apply", None)
    if new_small is None and waiting is not None:
        new_small = waiting[3]
    st.pending_apply = (new_scale, new_profile, new_params, new_small)
    st.pending_apply_due = time.monotonic() + APPLY_DEBOUNCE
    print(f"[main] apply queued ({APPLY_DEBOUNCE:.1f} s after the last "
          f"change), scale {new_scale:.2f}, boost {new_small}")
