"""Keep full speed while our window is hidden, minimised or fully occluded.

The report (#137, joaquiros360-dot, confirmed independently by namesource on a
4070 Ti Super): the picture is smooth while the NeuralScreen window is open or
in the foreground, and the frame rate collapses as soon as it is minimised or
sent to the background. Windows 11 documents both halves of the mechanism:

  * Quality of Service classifies a window-owning process by the state of its
    window - in focus is High, visible is Medium, minimised or fully occluded
    is Low (Quality of Service, win32/procthread/quality-of-service);
  * the timer-resolution page is sharper still: "Starting with Windows 11, if
    a window-owning process becomes fully occluded, minimized, or otherwise
    invisible or inaudible to the end user, Windows does not guarantee a
    higher resolution than the default system resolution" (timeBeginPeriod).

Neither rule is a bug and neither is aimed at us - they are how Windows keeps
a backgrounded app off the CPU. A real-time overlay is the case where they are
wrong, and the documented opt-out is per process:

    SetProcessInformation(GetCurrentProcess(), ProcessPowerThrottling, ...)
    with ControlMask = PROCESS_POWER_THROTTLING_EXECUTION_SPEED and
    StateMask = 0 - "ControlMask selects the mechanism, StateMask turns it
    off" (SetProcessInformation, PROCESS_POWER_THROTTLING_STATE).

Two facts decide the shape of this module, and both were measured on the bench
before it was written (probe in _work/):

  * the call needs only a process HANDLE, not the process itself - opening the
    worker with PROCESS_SET_INFORMATION and clearing the mask there works from
    this process, so the native side needs no change and no rebuild;
  * IGNORE_TIMER_RESOLUTION (0x4) is absent from older SDK headers but is
    documented and accepted here: the read-back came back with ControlMask 5.

Both processes matter and each must defend itself: this one owns the HUD window
and the frame loop, the worker owns the flip-model overlay window and the
capture/evaluate/present threads, and the QoS state machine keys on the process
that owns a window. A native-only or python-only fix would leave half of it
exposed.

Default is ON: a user who minimises the program is asking it to keep working,
and the option exists to be turned OFF by whoever would rather have the battery
life back. The price is real and is said in the menu hint - more power, more
heat, louder fans.
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

#: PROCESS_POWER_THROTTLING_STATE.Version - the only value the API accepts.
_CURRENT_VERSION = 1
#: The execution-speed throttle (EcoQoS). Clearing it is the documented opt-out.
_EXECUTION_SPEED = 0x1
#: Documented on the current SetProcessInformation page, absent from the
#: Windows 10 16299 SDK header this project builds against. Defined here by
#: value, and a refusal is treated as "this build does not support it" rather
#: than as an error - the EXECUTION_SPEED half still applies.
_IGNORE_TIMER_RESOLUTION = 0x4
_PROCESS_POWER_THROTTLING = 4  # PROCESS_INFORMATION_CLASS

_PROCESS_SET_INFORMATION = 0x0200
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _State(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG),
                ("ControlMask", wintypes.ULONG),
                ("StateMask", wintypes.ULONG)]


_kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD]
_kernel32.SetProcessInformation.restype = wintypes.BOOL
_kernel32.GetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD]
_kernel32.GetProcessInformation.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

#: What the last attempt reported, for the diagnostic bundle. A support report
#: has to be able to say whether the option took effect on that machine - the
#: OS may demote a process again after it is set, and only a read-back shows it.
_last: dict = {"ours": "not attempted", "worker": "not attempted"}


def _is_windows() -> bool:
    return sys.platform == "win32"


def _current() -> int:
    """A pseudo-handle for this process (works with Get/SetProcessInformation)."""
    return -1  # GetCurrentProcess() is the constant -1


def _read(handle) -> tuple | None:
    """(ControlMask, StateMask) or None when the query is not supported."""
    state = _State()
    state.Version = _CURRENT_VERSION
    if not _kernel32.GetProcessInformation(handle, _PROCESS_POWER_THROTTLING,
                                           ctypes.byref(state),
                                           ctypes.sizeof(state)):
        return None
    return (int(state.ControlMask), int(state.StateMask))


def _apply(handle, keep_timer_resolution: bool) -> bool:
    """Clear the execution-speed throttle on that process.

    ControlMask lists the mechanisms being configured, StateMask says which of
    them are ON. StateMask = 0 therefore turns everything named in ControlMask
    OFF, which is the opt-out. Setting StateMask = EXECUTION_SPEED instead
    would be the opposite call - opting INTO EcoQoS.
    """
    state = _State()
    state.Version = _CURRENT_VERSION
    control = _EXECUTION_SPEED
    if keep_timer_resolution:
        control |= _IGNORE_TIMER_RESOLUTION
    state.ControlMask = control
    state.StateMask = 0
    ok = bool(_kernel32.SetProcessInformation(handle, _PROCESS_POWER_THROTTLING,
                                              ctypes.byref(state),
                                              ctypes.sizeof(state)))
    if not ok and keep_timer_resolution:
        # The build may not know IGNORE_TIMER_RESOLUTION. The execution-speed
        # half is the one that matters most; retry without the flag rather
        # than losing the whole opt-out to an unsupported bit.
        state.ControlMask = _EXECUTION_SPEED
        ok = bool(_kernel32.SetProcessInformation(handle, _PROCESS_POWER_THROTTLING,
                                                  ctypes.byref(state),
                                                  ctypes.sizeof(state)))
    return ok


def _release(handle) -> bool:
    """Hand the policy back to the system: ControlMask = StateMask = 0.

    The documented "let the system manage all power throttling" call. Without
    it, turning the option off only changed what the log said - the opt-out
    set earlier stayed on the process until it exited.
    """
    state = _State()
    state.Version = _CURRENT_VERSION
    state.ControlMask = 0
    state.StateMask = 0
    return bool(_kernel32.SetProcessInformation(handle, _PROCESS_POWER_THROTTLING,
                                                ctypes.byref(state),
                                                ctypes.sizeof(state)))


def _describe_off(handle) -> str:
    """The OFF answer, from the read-back rather than from the request."""
    if not _release(handle):
        return "refused by the system"
    read_back = _read(handle)
    if read_back is not None and read_back[0] & _EXECUTION_SPEED:
        return "still opted out"
    return "off (the OS decides)"


def _describe(read_back: tuple | None) -> str:
    if read_back is None:
        return "unsupported"
    control, state = read_back
    if state & _EXECUTION_SPEED:
        return "throttled (EcoQoS)"
    return "full speed" if control & _EXECUTION_SPEED else "not requested"


def apply_own(enabled: bool, log=print) -> str:
    """Opt this process out of the background throttle, or restore the default.

    Idempotent: called on every state change and on a timer, it re-asserts and
    re-reads, because Windows may demote the process again after it is set.
    """
    global _last
    if not _is_windows():
        _last["ours"] = "not Windows"
        return _last["ours"]
    if not enabled:
        _last["ours"] = _describe_off(_current())
        return _last["ours"]
    if not _apply(_current(), keep_timer_resolution=True):
        # Once per refusal, not once per call: the loop re-asserts this
        # every 30 frames and a refusing system refuses every time.
        if _last["ours"] != "refused by the system":
            log(f"[main] power throttling: the system refused the request ({ctypes.get_last_error()})")
        _last["ours"] = "refused by the system"
        return _last["ours"]
    _last["ours"] = _describe(_read(_current()))
    return _last["ours"]


def apply_worker(worker, enabled: bool, log=print) -> str:
    """The same for the native worker, by pid - it owns the other window.

    Opened with PROCESS_SET_INFORMATION: setting a power-throttling policy is
    an information change on the target, which is exactly what that right
    means. A failure is reported once and never raised - the option is a
    speed-up, not a precondition for running.
    """
    global _last
    if not _is_windows():
        _last["worker"] = "not Windows"
        return _last["worker"]
    if worker is None or getattr(worker, "poll", lambda: None)() is not None:
        _last["worker"] = "no worker"
        return _last["worker"]
    pid = getattr(worker, "pid", None)
    if not pid:
        _last["worker"] = "no pid"
        return _last["worker"]
    handle = _kernel32.OpenProcess(
        _PROCESS_SET_INFORMATION | _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        _last["worker"] = "cannot open"
        return _last["worker"]
    try:
        if not enabled:
            _last["worker"] = _describe_off(handle)
            return _last["worker"]
        if not _apply(handle, keep_timer_resolution=True):
            _last["worker"] = "refused by the system"
            return _last["worker"]
        _last["worker"] = _describe(_read(handle))
        return _last["worker"]
    finally:
        _kernel32.CloseHandle(handle)


def apply_both(st, enabled: bool | None = None, log=print) -> None:
    """The one call the app makes: ours and the worker, on the same answer.

    ``enabled`` defaults to the config key, which is ON unless the user turned
    it off. Called when the setting changes and periodically from the frame
    loop, so a process the OS demoted after the fact is put back.
    """
    if enabled is None:
        enabled = bool(getattr(st, "cfg", {}).get("keep_speed_when_hidden", True))
    ours = apply_own(enabled, log=log)
    worker = apply_worker(getattr(st, "worker", None), enabled, log=log)
    # One line per change, not per call: the loop re-asserts and a line each
    # time would drown the log (the same reason display.py throttles its
    # z-order verdict).
    state = (enabled, ours, worker)
    if getattr(st, "_power_throttle_last", None) != state:
        st._power_throttle_last = state
        log(f"[main] keep speed while hidden: {'on' if enabled else 'off'} - "
            f"ours {ours}, worker {worker}")


def status() -> dict:
    """For the diagnostic bundle: what was asked and what the OS reports."""
    return dict(_last)
