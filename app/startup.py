"""Getting the program ready: from a config file to a running pipeline.

A straight line that runs once, kept away from the loop that runs sixty times
a second. Two halves, in the order they happen:

  configure()  decides what the program is going to do - the config and the
               NR parameters, which monitor, and at what resolution. The
               resolution comes from the REAL monitor rather than from the
               config: a display switched to 1440p while config.json still
               says 4K would leave the overlay, the recording and the worker
               window drifting away from the screen.
  bring_up()   makes it exist - the shared memory, the worker, the overlay,
               the tray icon, the taskbar button, the hotkeys, the guides,
               and every default on the state object the loop then reads.

Order is the whole content of this module. The worker is started before the
overlay because it is the slow part; the menu captions are set only after the
hotkeys are registered, because before that there are no bindings to name;
and the state defaults come last, so nothing the loop reads is missing when
the first frame arrives.
"""
from __future__ import annotations

import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

import convert_jobs
from capture import (ScreenCapture, list_adapters, list_monitors,
                     monitor_origin, monitor_work_size,
                     resolve_output_idx)
from display import Display
from gpuinfo import describe as gpu_describe, probe as gpu_probe
from guides import TemporalGuideGenerator
from hotkeys import (HotkeyController, build_bindings,
                     describe as describe_hotkeys, numlock_needed,
                     numlock_on)
from i18n import STRINGS as UI_STRINGS
from paths import BASE_DIR
from pipeline import require_compatibility, start_worker
from protocol import SharedFrameBuffer, WorkerReader
from recorder import VideoRecorder
from settings_io import (APP_VERSION, THEME_NAMES, _work_size, cascade_passes,
                         clean_per_pass, hotkey_labels,
                         load_config, load_presets, resolve_params)
from taskbar import TaskbarWindow
from tray import TrayController


# --- Log to a file instead of the console --------------------------------
# The release is launched through pythonw.exe (no console window): stdout and
# stderr are None there and any print would fail. We redirect them into
# NeuralScreen.log next to main.py - every print keeps working and the user
# reads the log as a file rather than a window. Startup errors (a missing DLL
# and the like) are additionally shown in a message box (see the bottom of
# this file).
LOG_PATH = BASE_DIR / "NeuralScreen.log"

#: NeuralScreen.log is appended to by every run and was never trimmed: one
#: user's reached 7.8 MB in two and a half days, most of it the NR and FG
#: heartbeats. Past this size a start moves it aside to NeuralScreen.log.1
#: (replacing the previous one) and begins a new file - two files at most,
#: and the current one is the one a support bundle reads.
LOG_ROTATE_BYTES = 8 * 1024 * 1024


def _rotate_log(path: Path = LOG_PATH, limit: int = LOG_ROTATE_BYTES) -> bool:
    """Move an oversized log aside before it is opened; True if it was moved.

    A second copy of the program gets here too - the single-instance check
    comes after the log - while the first copy holds the file open. Windows
    refuses the rename then, and the file is simply kept.
    """
    try:
        if path.stat().st_size < limit:
            return False
        os.replace(path, path.with_name(path.name + ".1"))
        return True
    except OSError:
        return False


# Every line in the log carries the time it was written.
#
# The worker stamps its own lines ("16:03:11.482  [fg] ..."); the Python side
# did not, and half of a real user's log came out untimed - measured on the
# four diagnostic packages of 19.09: 499 of 916 lines (raycornea), 477 of 966
# (saymoin), 145 of 243 (codemned), 111 of 220 (saymoin), 76 of 153 (manik).
# That is exactly the half a report needs: "the menu opened 21 times" cannot be
# placed against "the user minimised a window" when neither line carries a
# time, and a z-order decision could only be dated by its neighbouring line.
# The [z] lines and the menu open/close lines are both in that untimed half.
#
# Only unstamped lines get a prefix, so a worker line is not stamped twice, and
# the stamp is put on when the line is written rather than when it was queued.
_TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s")


class _StampedLog:
    """A text stream that puts HH:MM:SS.mmm in front of every untimed line."""

    def __init__(self, stream) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        if not text:
            return 0
        out = []
        for part in text.splitlines(True):
            body = part.rstrip("\r\n")
            newline = part[len(body):]
            if body and not _TIMESTAMP_RE.match(body):
                stamp = time.strftime("%H:%M:%S")
                millis = int(time.time() * 1000) % 1000
                body = f"{stamp}.{millis:03d}  {body}"
            out.append(body + newline)
        self._stream.write("".join(out))
        return len(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _init_logging() -> None:
    """Redirect stdout/stderr into NeuralScreen.log (utf-8), with timestamps."""
    _rotate_log()
    try:
        log_file = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = _StampedLog(log_file)
        sys.stderr = _StampedLog(log_file)
    except Exception:
        pass  # it did not work - the prints just vanish, we do not crash


def _apply_nr_dll(cfg: dict) -> None:
    """The swappable runtime: a configured nr_dll reaches the worker.

    The worker loads nvngx_dlssnr.dll by name; NS_NR_DLL lets a different
    build be loaded without rebuilding the worker (the RHI
    dlss_manifest.json pattern). The path is put into the environment,
    which subprocess inherits. Without the flag the bundled DLL stays the
    default.
    """
    if cfg.get("nr_dll"):
        os.environ["NS_NR_DLL"] = str(cfg["nr_dll"])



def _apply_spout_env(cfg: dict) -> None:
    """The Spout2 bridge flag reaches the worker through the environment.

    SpoutBridgeInit in the worker reads NS_SPOUT once, at process start -
    there is no protocol message for the bridge, so the config flag
    becomes the environment before the first worker is launched (and
    again on every restart, see pipeline.apply_spout). "0" and unset
    both mean off; the worker treats anything but "1" as disabled.
    """
    os.environ["NS_SPOUT"] = "1" if cfg.get("spout") else "0"


def _apply_hdr_env(cfg: dict) -> None:
    """The HDR compatibility flag reaches the worker the same way.

    HdrEnabled() in the worker reads NS_HDR once, at process start, and
    everything downstream of it is decided then: the duplication format,
    the swap chain format, the colour space. So the switch goes through a
    worker restart (pipeline.apply_hdr), exactly like the Spout bridge.
    Off unless the config says otherwise - the mode is experimental.
    """
    os.environ["NS_HDR"] = "1" if cfg.get("hdr") else "0"


def _apply_monitor_name(name: str) -> tuple[int, int]:
    """Publish a monitor identity without opening a capture session."""
    name = str(name or "")
    if name:
        os.environ["NS_OUTPUT"] = name
        origin = monitor_origin(name)
        if origin is not None:
            os.environ["NS_WINDOW_POS"] = f"{origin[0]},{origin[1]}"
            return origin
    os.environ.pop("NS_OUTPUT", None)
    os.environ.pop("NS_WINDOW_POS", None)
    print(f"[main] monitor identity unknown ({name!r}) - "
          f"output 0 and the primary position stay", file=sys.stderr)
    return (0, 0)


def _apply_monitor_env(capture) -> tuple[int, int]:
    """Publish the chosen monitor to the worker and return its origin.

    Two separate hand-offs for the same subject:

    * NS_OUTPUT - which DXGI output the worker's Desktop Duplication should
      duplicate, by device name. Without it the worker duplicated output 0
      (the primary monitor) while the client was built for the chosen one
      (issues #28, #33).
    * the origin - where the chosen monitor's corner sits on the virtual
      desktop. The overlay is created at (0,0), which IS the primary: on a
      second monitor the picture landed on the wrong screen. The caller
      feeds it to Display.set_origin.

    A monitor whose name cannot be resolved keeps both defaults - the old
    behaviour - and says so in the log.
    """
    name = getattr(capture, "devicename", "") or ""
    return _apply_monitor_name(name)


def _apply_residual_env(cfg: dict) -> None:
    """Hand the residual strength to the worker, if the user set one.

    The composite is `native + (nr_out - nr_in) * strength`, and with N
    passes the delta is roughly N times larger while strength stays where it
    is. Dividing it by the pass count holds tone, colour and motion where a
    single pass leaves them - measured, and the numbers are worth keeping:

        passes  strength   d-luma   d-sat   detail   mc-error
             1      1.00    -2.38  -10.62    0.92x     0.6641
             4      1.00    -8.88  -28.77    0.75x     1.5294
             4      0.25    -2.23   -7.53    0.93x     0.3781

    But holding the picture where one pass leaves it is NOT what somebody
    asking for four passes wants. They want more, and an automatic 1/passes
    gives them less: it made a two-pass cascade apply half the effect of one
    pass, which reads as the cascade doing nothing (user, 20.09). Stability
    is a real property and so is strength, and which one you want is a
    preference, not a correctness question - so this is a SETTING, empty by
    default, and the worker's own 1.0 stands unless it is set.

    `residual_strength` in config.json, or NS_NR_RESIDUAL_STRENGTH in the
    environment. Read once per worker process (from its own environment
    block, fixed at spawn), so a change to it needs a restart - which is why
    the pass count does NOT travel this way: that has to stay instant.
    """
    if os.environ.get("NS_NR_RESIDUAL_STRENGTH"):
        return  # a deliberate override, left alone
    value = cfg.get("residual_strength")
    if value is None:
        return  # the worker's own 1.0: more passes, more effect
    try:
        strength = float(value)
        if strength != strength:
            raise ValueError("NaN")
        strength = min(1.0, max(0.0, strength))
    except (TypeError, ValueError, OverflowError):
        # OverflowError: a huge integer does not fit a float, and it used to
        # raise out of the launch.
        print(f"[main] config.json: residual_strength {value!r} is not a "
              f"number - the worker's default stands", file=sys.stderr)
        return
    os.environ["NS_NR_RESIDUAL_STRENGTH"] = f"{strength:.4f}"


def _apply_gpu_env(cfg: dict) -> None:
    """Which card the worker runs on, through the environment.

    NS_GPU is read once per worker process - the adapter is chosen before
    the device exists - so the config flag becomes the environment before
    the first worker is launched, and again on every restart (see
    pipeline.apply_gpu). Unset means the worker's own default: the first
    NVIDIA adapter for the network, adapter 0 for the capture.
    """
    gpu = cfg.get("gpu")
    if gpu is None:
        os.environ.pop("NS_GPU", None)
    else:
        os.environ["NS_GPU"] = str(int(gpu))


def _working_card_name(cfg: dict) -> str:
    """The name of the card the worker will really run on, or "".

    NS_GPU is a wish, not an order: an index that is not a usable NVIDIA
    adapter falls back to the first NVIDIA card in DXGI order (the worker
    says so itself: "[host] NS_GPU=... is not a usable NVIDIA adapter").
    The fallback is mirrored here so the environment header and the GPU
    line name the SAME card the pipeline runs on - NVAPI enumerates in
    its own order, and its first card can be the one that does no work
    at all (issue #81: the panel said RTX 3050 while a 4070 Super did
    everything).
    """
    adapters = list_adapters()
    if not adapters:
        return ""
    try:
        wanted = int(cfg.get("gpu"))
    except (TypeError, ValueError):
        wanted = None
    for i, name in adapters:
        if i == wanted:
            return name
    return adapters[0][1]


#: What _log_environment found, kept for the About block. The log header
#: is what users are asked to paste into an issue, and the same four facts
#: belong where a user can read them without finding the log first.
ENVIRONMENT: dict = {}


def _log_environment(cfg: dict) -> None:
    """Print the environment header into the log: version, OS, HDR, driver.

    Users paste NeuralScreen.log into issues; the header answers the
    questions we would otherwise have to ask (which version, which
    Windows, is HDR on, which driver). Every probe is wrapped: a missing
    API or a stripped system must not crash the startup - the line is
    simply skipped.
    """
    try:
        import platform
        import sys as _sys
        win = _sys.getwindowsversion()
        ENVIRONMENT["version"] = APP_VERSION
        ENVIRONMENT["windows"] = f"{win.major}.{win.minor} ({win.build})"
        # The date belongs in the header: the line stamps are times of day, so
        # a log could not be placed in time at all - a bundle's date was only
        # readable from the screenshot file names, and two bundles from
        # different days could not be told apart.
        print(f"[env] NeuralScreen {APP_VERSION} | Windows {win.major}.{win.minor} "
              f"(build {win.build}) | {platform.platform()} | "
              f"{time.strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception:
        print(f"[env] NeuralScreen {APP_VERSION} | Windows unknown")
    try:
        import gpuinfo
        g = gpuinfo.probe(_working_card_name(cfg))
        print(f"[env] GPU: {g.get('name') or 'unknown'} "
              f"({g.get('family') or '?'}, arch 0x{g.get('arch_group', 0):X})")
    except Exception:
        pass
    try:
        # The NVIDIA driver version from the display-class registry key.
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        for idx in range(10):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    f"{base}\\{idx:04d}") as key:
                    desc, _ = winreg.QueryValueEx(key, "DriverDesc")
                    if "NVIDIA" in str(desc):
                        ver, _ = winreg.QueryValueEx(key, "DriverVersion")
                        ENVIRONMENT["driver"] = str(ver)
                        print(f"[env] driver: {ver}")
                        break
            except OSError:
                continue
    except Exception:
        pass
    try:
        # HDR: the monitor data store in the registry carries HDREnabled.
        # One read, no deep API digging - if the key is not there the
        # line just says unknown.
        import winreg
        base = (r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers"
                r"\MonitorDataStore")
        hdr = None
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        with winreg.OpenKey(root, winreg.EnumKey(root, i)) as mon:
                            try:
                                val, _ = winreg.QueryValueEx(mon, "HDREnabled")
                                hdr = bool(val)
                                break
                            except OSError:
                                continue
                    except OSError:
                        continue
        except OSError:
            pass
        print(f"[env] HDR: {'on' if hdr else 'off' if hdr is not None else 'unknown'}")
        # The effect switches this session starts with. Without them a report
        # cannot say whether "frame generation does not work" is about a
        # feature that was on from the start or one switched on halfway
        # through - and the log's own "[fg] UI: on" only marks the change.
        try:
            print("[env] switches: "
                  f"NR {'on' if not cfg.get('_paused', False) else 'off'} | "
                  f"FG {'on' if cfg.get('frame_generation') else 'off'}"
                  f" x{int(cfg.get('frame_multiplier', 2))} | "
                  f"motion {cfg.get('motion_backend', 'cpu')} | "
                  f"spout {'on' if cfg.get('spout') else 'off'}")
        except Exception:
            pass
    except Exception:
        pass
    try:
        numlock = bool(ctypes.windll.user32.GetKeyState(0x90) & 1)
        print(f"[env] Num Lock: {'on' if numlock else 'off'} | "
              f"lang: {cfg.get('lang', 'en')} | "
              f"profile: {cfg.get('profile', '?')} | "
              f"work_scale: {cfg.get('work_scale', '?')} | "
              f"flow: {cfg.get('flow_preset', 'fast')}")
    except Exception:
        pass


def configure(st) -> None:
    """Read the config and decide what the program is going to do.

    st.cfg_path is already set - the caller owns argparse, this module does
    not. Everything else lands on the state: the config, the NR parameters,
    the presets, the monitor and the resolution the pipeline will run at.
    """
    st.cfg = load_config(st.cfg_path)
    st.params = resolve_params(st.cfg)
    st.presets = load_presets(st.cfg)
    _apply_nr_dll(st.cfg)
    _log_environment(st.cfg)
    # A copy for the About block: the module-level dict is filled by the
    # probe above, and the menu reads it off the state like everything else.
    st.environment = dict(ENVIRONMENT)
    st.width, st.height = int(st.cfg["width"]), int(st.cfg["height"])
    monitor_cfg = st.cfg["monitor"]
    if isinstance(monitor_cfg, str):
        # New configs store the DXGI devicename - resolve it to the current
        # output index; a monitor that is not connected falls back to 0.
        st.monitor = resolve_output_idx(monitor_cfg)
        if st.monitor is None:
            print(f"[main] monitor {monitor_cfg!r} from config.json is not "
                  "connected - using monitor 0", file=sys.stderr)
            st.monitor = 0
    else:
        # Old configs store the positional index.
        st.monitor = int(monitor_cfg)
    st.warmup = int(st.cfg["warmup"])
    st.work_scale = float(st.cfg["work_scale"])
    # The worker reads NS_NR_SMALL once, at startup: with it on, Neural
    # Rendering runs at the work resolution and the result is scaled back up
    # instead of the network chewing the whole screen. Off by default - it is
    # faster but softer, and an update must not change how the picture looks
    # without being asked. Toggling it later restarts the worker, which is why
    # it lives in the environment rather than in the frame protocol.
    # Boost is ON unless a config says otherwise (user, 13.09). It was off
    # by default because it had been measured on still frames only; it has
    # been in a release since 1.7.0 now, and on a 5070 Ti at 4K it is
    # 45.7 -> 72.6 frames for a picture that is indistinguishable at 1:1 -
    # the residual composite puts the detail back off the native frame.
    st.nr_small = bool(st.cfg.get("nr_small", True))
    os.environ["NS_NR_SMALL"] = "1" if st.nr_small else "0"
    # Which composite Boost uses: the matched residual (the network's delta
    # laid over the native frame, so text and edges keep full resolution) or
    # direct reconstruction (the network's own output, stretched). Residual
    # is what ships; direct exists to be measured against it on real content,
    # which has never been done. No menu control on purpose - it becomes one
    # only if the A/B says it earns its place. Unlike Boost itself this one
    # travels with the resize, so flipping it costs no feature.
    st.nr_direct = bool(st.cfg.get("nr_direct", False))
    #: How many NR passes run over one frame (the cascade). Travels with every
    #: RNSZ, so changing it costs no rebuild once the features exist.
    st.nr_passes = int(st.cfg.get("nr_passes", 1))
    #: What passes 2..N should use, if the user set anything different from the
    #: main set. None means "every pass uses the main set" - the behaviour of
    #: every build before this one, so a config that never touched it is
    #: untouched. The rules live in settings_io.clean_per_pass, which the
    #: config validator uses as well: two copies would drift apart, and the
    #: one that drifted would be the one deciding what reaches the wire.
    st.nr_pass_params = None
    if int(st.nr_passes) >= 2:
        st.nr_pass_params = clean_per_pass(st.cfg.get("nr_pass_params"))
    #: Frames in a row the worker answered without an NGX evaluation.
    st.nr_idle_streak = 0
    #: A file conversion is running on its own worker. The loop
    #: only reads it; commands owns both fields.
    st.convert_busy = False
    st.convert_status = ""
    st.convert_queue = convert_jobs.ConvertQueue()
    st.convert_batch = []
    st.convert_progress = None
    #: The verdict the interface reads: NR is on, but nothing is processed.
    st.nr_not_evaluating = False
    os.environ["NS_NR_RESIDUAL"] = "0" if st.nr_direct else "1"
    # The composite is normalised by the pass count - see the helper.
    _apply_residual_env(st.cfg)
    # The Spout2 bridge is the same story: the worker reads NS_SPOUT once
    # at startup (SpoutBridgeInit), so the config flag becomes the
    # environment before the first worker is launched. Off by default -
    # the bridge costs a full-frame GPU copy on every Present, and it is
    # only useful to someone recording through OBS.
    _apply_spout_env(st.cfg)
    # And HDR compatibility, read once per worker process as well.
    _apply_hdr_env(st.cfg)
    from motion_backend import normalize_backend
    os.environ["NS_MOTION_BACKEND"] = normalize_backend(st.cfg.get("motion_backend"))
    # The same for the card: NS_GPU is read once per worker process.
    _apply_gpu_env(st.cfg)
    st.lang = str(st.cfg["lang"])

    # Resolve the real monitor dimensions before compatibility preflight,
    # but do not open Desktop Duplication yet.  list_monitors is Win32/DXGI
    # enumeration only: the isolated Create/Evaluate gate must run before a
    # ScreenCapture or presentation window exists.
    monitor_info = next(
        (item for item in list_monitors() if int(item[0]) == int(st.monitor)),
        None,
    )
    if monitor_info is None:
        monitor_info = next(iter(list_monitors()), None)
    if monitor_info is not None:
        st.monitor = int(monitor_info[0])
        st.mon_w, st.mon_h = int(monitor_info[1]), int(monitor_info[2])
        st.monitor_devicename = str(monitor_info[3] or "")
    else:
        st.mon_w, st.mon_h = st.width, st.height
        st.monitor_devicename = ""
    if st.mon_w > 0 and st.mon_h > 0 and (st.mon_w, st.mon_h) != (st.width, st.height):
        print(f"[main] monitor {st.monitor} is {st.mon_w}x{st.mon_h} (config: {st.width}x{st.height}), "
              f"taking the real resolution")
        st.width, st.height = st.mon_w, st.mon_h
    st.mon_origin = _apply_monitor_name(st.monitor_devicename)
    st.capture = None

    print(f"[main] NeuralScreen - profile {st.cfg['profile']!r}, "
          f"resolution {st.width}x{st.height}, monitor {st.monitor}")
    print(f"[main] NGX parameters: {st.params}")
    print(f"[main] work_scale {st.work_scale:.2f} (NGX resolution "
          f"{int(st.width * st.work_scale)}x{int(st.height * st.work_scale)})")

    st.worker: subprocess.Popen | None = None
    st.reader: WorkerReader | None = None
    st.worker_stop: threading.Event | None = None
    st.shm: SharedFrameBuffer | None = None
    st.display: Display | None = None
    st.tray: TrayController | None = None
    st.hotkeys: HotkeyController | None = None
    st.recorder: VideoRecorder | None = None
    st.recording_finalizer: VideoRecorder | None = None
    st.recording_finalize_deadline = 0.0
    st.last_recording = {}
    st.compatibility_key = None
    st.compatibility_result = None


def open_capture(st) -> None:
    """Open desktop capture only after the compatibility gate returned PASS."""
    st.capture = ScreenCapture(monitor_idx=st.monitor)
    st.monitor = int(getattr(st.capture, "monitor_idx", st.monitor))
    st.monitor_devicename = str(getattr(st.capture, "devicename", "") or "")
    st.mon_w, st.mon_h = st.capture.resolution
    if st.mon_w > 0 and st.mon_h > 0 and (st.mon_w, st.mon_h) != (st.width, st.height):
        print(f"[main] opened monitor {st.monitor} at {st.mon_w}x{st.mon_h} "
              f"(enumerated: {st.width}x{st.height}), taking the capture size")
        st.width, st.height = st.mon_w, st.mon_h
    st.mon_origin = _apply_monitor_env(st.capture)


def bring_up(st) -> None:
    """Make it exist: the worker, the overlay, the tray, the hotkeys.

    Runs inside main's try/finally - everything created here is torn down
    there, which is why the handles go onto the state as they appear rather
    than being returned in a bundle at the end.
    """
    # The worker and guides run at the work resolution (the NGX feature is
    # created from the header sizes; guides' assert requires them to match)
    st.work_w, st.work_h = _work_size(st.width, st.height, st.work_scale,
                                      cascade_passes(st))
    # The v3 protocol (full_w/full_h) ONLY when work != full: at work==full
    # (scale 1.0) the worker crashes or hangs in upscale mode (verified in
    # isolation) - we use legacy full_w=0, as in D5V2.
    full_w = st.width if (st.work_w != st.width or st.work_h != st.height) else 0
    full_h = st.height if (st.work_w != st.width or st.work_h != st.height) else 0
    # Shared memory for the input frame: its size does not depend on
    # work_scale (see SharedFrameBuffer), so it is created once per process.
    st.shm = SharedFrameBuffer(st.width, st.height)
    # Which card this is and whether NR works on it. The model comes from
    # nvapi, picked by the name of the adapter the worker will really run
    # on (NVAPI's own order is not DXGI's - issue #81); the support verdict
    # comes from the worker rather than the architecture, because only it
    # knows whether feature 18 was created.
    gpu_info = gpu_probe(_working_card_name(st.cfg))
    st.gpu_text = gpu_describe(gpu_info)
    st.gpu_ok: bool | None = None
    st.gpu_alerted = False          # the "cannot run the pass" alert, once per verdict
    st.fg_alerted = False           # the "FG could not start" alert, re-armed by the switch
    st.gpu_switch_pending = False   # set by apply_gpu: a split pipeline is worth an alert
    print(f"[main] GPU: {st.gpu_text or 'unknown'} "
          f"(group 0x{gpu_info['arch_group']:X}, officially supported: "
          f"{'yes' if gpu_info['official'] else 'no'})")
    # The stock warm-up is 120 discarded evaluations. On a fast Blackwell
    # card that is a second or two; on Turing/Ampere/Ada it can take far
    # longer than the frame watchdog, which then kills the worker on
    # frame 0 and starts a restart storm (seen on RTX 2070 at ~1 FPS and
    # on RTX 3060 Ti at ~18 FPS). Unsupported/pre-Blackwell cards get a
    # short warm-up; the actual effect is still evaluated normally
    # afterwards.
    effective_warmup = st.warmup
    if not gpu_info["official"] and st.warmup > 4:
        effective_warmup = 4
        print(f"[main] pre-Blackwell GPU: warmup {st.warmup} -> "
              f"{effective_warmup} to avoid a false frame-0 watchdog "
              f"timeout")
    # Every later (re)start has to use the same number. It used to read the
    # raw config value instead, so on a pre-Blackwell card the shortening
    # applied to the launch and to nothing else: the first revive brought
    # the 120-frame warm-up back, it outlived the 5 s watchdog, and the
    # restarts climbed to NR OFF - the exact storm the shortening exists to
    # prevent (audit F3).
    st.effective_warmup = effective_warmup
    # The overlay is built BEFORE the worker, and that order is load-bearing:
    # Display publishes its window handle in NS_HUD_HWND (display.py, in
    # _move_to_origin), and the worker reads it to show the picture window
    # BELOW the panel instead of on top of it. A worker started first inherits
    # an environment where the variable does not exist yet, and it reads it
    # once - so its reveal falls back to ShowWindow, which puts the picture
    # over the panel. That fallback is exactly the single-refresh flash v2.0.0
    # set out to remove, and a reporter's log shows the fallback line on a
    # plain launch (#107).
    st.display = Display(st.width, st.height, fullscreen=bool(st.cfg["fullscreen"]))
    # The overlay is the size of one monitor and must sit ON it: created at
    # (0,0) it covered the primary screen while the capture ran elsewhere.
    st.display.set_origin(*st.mon_origin)
    st.display.set_lang(st.lang)

    require_compatibility(st)
    st.worker, st.worker_logs, st.reader, st.worker_stop = start_worker(
        st.params, st.work_w, st.work_h, effective_warmup, full_w, full_h, st.shm)
    print(f"[main] worker started (pid {st.worker.pid}), header sent "
          f"({st.work_w}x{st.work_h})")

    print(f"[main] capturing monitor {st.monitor}: {st.capture.resolution}")

    # The program draws over the desktop and gives no sign of itself -
    # without this it is unclear after launch whether it is running.
    st.startup_menu = bool(st.cfg.get("open_menu_on_start", True))
    # The before/after wipe: the share of the frame the worker leaves raw.
    st.split_pos = min(1.0, max(0.0, float(st.cfg.get("split", 0.0))))
    # The menu size, position and theme - exactly as the user left them.
    st.display.menu.set_user_scale(float(st.cfg.get("menu_scale", 1.0)))
    # A first launch sizes the panel to the screen it landed on. At the
    # shipped 1.0 the main page is 1165 px of content, which does not fit a
    # 1080p desktop: the panel came up scrolled, with its last controls under
    # the taskbar (measured, and seen in a reporter's video - #107). The fit
    # runs ONLY while menu_scale_auto is set, so it can never argue with a
    # size the user picked.
    if st.cfg.get("menu_scale_auto") is True:
        work = None
        try:
            work = monitor_work_size(getattr(st, "monitor_devicename", "") or "")
        except Exception:
            work = None
        # The work area is the desktop minus the taskbar; without it, the
        # monitor's own height minus a taskbar's worth is a better guess than
        # the full height.
        fit_w, fit_h = (work if work else (st.width, max(1, st.height - 48)))
        try:
            step = st.display.menu.fit_user_scale(fit_w, fit_h)
        except Exception as exc:
            print(f"[main] the interface fit failed ({exc}) - keeping "
                  f"{st.cfg.get('menu_scale', 1.0)}", file=sys.stderr)
        else:
            if abs(step - float(st.cfg.get("menu_scale", 1.0))) > 0.001:
                print(f"[main] interface scale fitted to {step:g} for a "
                      f"{fit_w}x{fit_h} desktop")
            st.display.menu.set_user_scale(step)
            st.cfg["menu_scale"] = round(step, 2)
    saved_theme = st.cfg.get("theme")
    if isinstance(saved_theme, str) and saved_theme in THEME_NAMES:
        st.display.menu.set_state({"theme": saved_theme})
    # Mini mode, and what it keeps. A missing list is not an empty one:
    # it means nobody has chosen yet, so the default set stands.
    st.display.menu.mini = bool(st.cfg.get("menu_mini", False))
    saved_rows = st.cfg.get("menu_mini_rows")
    if isinstance(saved_rows, list):
        st.display.menu.mini_rows = {str(r) for r in saved_rows}

    saved_offset = st.cfg.get("menu_offset")
    if isinstance(saved_offset, (list, tuple)) and len(saved_offset) == 2:
        st.display.menu.offset = [int(saved_offset[0]), int(saved_offset[1])]
    saved_height = st.cfg.get("menu_height")
    if isinstance(saved_height, (int, float)) and saved_height > 0:
        st.display.menu.user_height = int(saved_height)
    print(f"[main] output window {st.display.width}x{st.display.height}")

    # Tray icon: commands go into a queue, the main loop reads them
    st.tray_commands = queue.Queue()
    # Answers from the "Save as" dialog. The dialog is modal and lives in
    # its own thread (see _open_save_dialog); the path arrives here.
    st.shot_paths = queue.Queue()
    st.shot_dialog_open = False
    st.tray = TrayController(st.tray_commands, labels={
        "settings": UI_STRINGS[st.lang].get("settings_title", "Settings"),
        "quit": UI_STRINGS[st.lang].get("exit", "Exit"),
    })
    st.tray._set_state(nr=True, scale=st.work_scale)
    st.tray.start()
    print("[main] tray icon started")

    # Taskbar button: the overlay and the worker window are tool
    # windows, so the program lived only in the tray. A 1x1 APPWINDOW
    # window gives the program a real taskbar button; clicking it sends
    # the same "settings" command as a left click on the tray (user
    # rule 2026-09-09: the program must always show in the taskbar).
    st.taskbar = TaskbarWindow(st.tray_commands, "NeuralScreen")
    # What the taskbar button's minimise and close mean (#93). The window
    # procedure runs on its own thread and must not read the config, so the
    # two answers are pushed into it here and again whenever they change.
    st.taskbar.to_tray_on_minimise = bool(st.cfg.get("tray_on_minimise", False))
    st.taskbar.to_tray_on_close = bool(st.cfg.get("tray_on_close", False))
    st.in_tray = False
    st.taskbar.start()
    print("[main] taskbar window started")

    # Global hotkeys: RegisterHotKey rather than polling the key state.
    # The system gives the keypress to us alone and does not pass it to the
    # active application - Num1 inside a game toggles NR and the game never
    # sees the key (the polling fallback does not swallow it, but the numpad
    # is free in games). The commands go into the same queue the tray uses. The
    # user's bindings come from config.json ("hotkeys": {"toggle": "Num1", ...}).
    hotkey_overrides = st.cfg.get("hotkeys")
    if not isinstance(hotkey_overrides, dict):
        hotkey_overrides = {}
    st.hotkey_bindings = build_bindings(hotkey_overrides)
    st.hotkeys = HotkeyController(st.tray_commands, st.hotkey_bindings)
    st.hotkeys.start()
    if st.hotkeys.registered:
        print(f"[main] hotkeys registered: {', '.join(st.hotkeys.registered)} "
              f"({describe_hotkeys(st.hotkey_bindings)})")
    if st.hotkeys.failed:
        print(f"[main] hotkeys taken by another program: {', '.join(st.hotkeys.failed)}",
              file=sys.stderr)
    # The numpad sends different key codes with Num Lock off, so those
    # bindings do not misbehave - they are simply absent. Say so, or it
    # looks like the program ignores the keyboard.
    numpad = numlock_needed(st.hotkey_bindings)
    if numpad and not numlock_on():
        print(f"[main] Num Lock is off: the numpad hotkeys "
              f"({', '.join(numpad)}) will not fire until it is on",
              file=sys.stderr)
        st.display.alert(UI_STRINGS[st.lang]["numlock_off"], duration=6.0)
    # The captions on the menu buttons come from the same bindings that were
    # registered. Strictly after build_bindings: before that they do not exist.
    st.display.menu.set_hotkeys(hotkey_labels(st.hotkey_bindings))

    # The settings live in the overlay menu (Num2). There is no separate
    # window any more: it was a second interface over the same fields, it
    # stole focus from the game and dragged the whole of tcl/tk into the
    # runtime.

    st.guides = TemporalGuideGenerator(
        st.work_w, st.work_h, preset=st.cfg.get("flow_preset", "fast"))

    # A reused buffer: every frame allocated ~100 MB (a 4K grab plus the
    # resizes plus flow), the GC could not keep up -> OOM around frame 1900.
    # The buffer is reused through cv2.resize(dst=...). work/out buffers are
    # not needed: in v3 the full->work->full resize is done by the worker on
    # the GPU (NGX Upscaling).
    st.buf_full = np.empty((st.height, st.width, 4), dtype=np.uint8)

    st.paused = False
    # True only after NR OFF has closed the worker's capture/present channels
    # and the main loop has stopped producing frames.  It is distinct from
    # paused: recording, screenshots and Frame Generation temporarily keep a
    # bypass stream alive while the NR toggle remains off.
    st.off_suspended = False
    # The worker died and exhausted the restart budget: the pipeline is
    # stopped (no send/recv, no more restarts) and the overlay is hidden
    # so the desktop is not covered by a black window (issue #3: black
    # screen on a GPU where feature 18 cannot be created). Cleared when
    # the user turns NR back on.
    st.worker_failed = False
    st.frame_index = 0
    st.pts = 0
    st.output_rgba = None  # the last NR frame (for a screenshot); None until the first one
    # WNDO mode: the worker shows the frame, no pixels come back to Python.
    st.want_present = bool(st.cfg.get("worker_present", True))
    st.want_motion_small = bool(st.cfg.get("motion_on_gpu", True))
    st.want_dda = bool(st.cfg.get("capture_in_worker", True))  # DDA: the worker takes the colour
    # The result pixels come back through shared memory, not the pipe.
    st.want_out_shm = bool(st.cfg.get("pixels_in_shm", True))
    # System audio ("what you hear") as a second track in the recording.
    # A config flag rather than a menu item: it is a decision made once,
    # not something to reach for while the overlay is up.
    st.record_audio = bool(st.cfg.get("record_audio", True))
    st.out_shm = False
    st.out_attempted = False
    st.motion_small = False  # the worker upscales the motion field itself
    st.motion_attempted = False  # already tried for the current worker
    st.present_mode = False      # the worker window is up right now
    st.present_attempted = False  # already tried for the current worker (do not spam)
    st.dda_mode = False          # the worker captures the screen itself
    st.dda_attempted = False     # already tried for the current worker (do not spam)
    st.window_hwnd = None        # WGCW target; None = the whole desktop (DDA1)
    st.last_foreground = 0       # the last focused window that was not ours
    #: The window list the picker shows, held still while that page is open.
    st.window_list: list = []
    st.follow_pos = None         # where the overlay currently sits (window mode)
    st.follow_resize = None      # a pending size change, waiting to settle
    # A pending MONITOR size change, same idea. follow_monitor assigns it on
    # the "nothing changed" path, so the field looked initialised - but the
    # very first call on a screen whose size already disagrees with the
    # config takes the other branch and READS it first. With __slots__ that
    # is an AttributeError, and the program leaves through main()'s
    # top-level handler (audit F2).
    st.mon_resize = None
    st.hdr_alerted = False       # the HDR notice is shown once per session
    st.mon_w, st.mon_h = st.width, st.height  # the full monitor size (for the menu layer)
    st.gray_active = False       # guides take luminance from the worker's gray channel
    st.pending_shot = None  # frame request before Save As, then cleared
    # When the overlay menu was opened, for the close line in the log
    # (time.monotonic()). 0.0 means "not open", so the close always logs.
    st.menu_opened_at = 0.0
    st.shot_rgba = None  # frozen before Save As, never a dialog-contaminated worker slot
    st.recorder: VideoRecorder | None = None  # recording (Num0), MP4 AV1 NVENC
    st.recording_finalizer: VideoRecorder | None = None
    st.recording_finalize_deadline = 0.0
    st.last_recording = {}
    st.work_frame = None  # the current work frame; None -> grab at the top of the loop


    st.running = True
    # Protection against rapid changes (arrow key repeat, a jerked slider):
    # the intermediate values are coalesced and only the last one is applied.
    # 0.5 s rather than 2 s: the change goes through RNSZ inside the live
    # worker process, not through a restart with an NGX init/shutdown plus
    # sleep(2) - the expensive path is only a fallback now.
    st.last_restart = 0.0
    st.pending_apply: tuple | None = None  # the deferred (scale, profile, params)
    # When that deferred apply is due. A monotonic deadline rather than a
    # flag: every request pushes it forward, so a drag ends with one rebuild
    # (issue #115 - the panel used to freeze under a hand that was still
    # moving a control).
    st.pending_apply_due = 0.0
    st.next_auto_revive = 0.0      # monotonic deadline; 0 = no revive pending
    st.consecutive_restarts = 0
    st.guide_fails = 0
