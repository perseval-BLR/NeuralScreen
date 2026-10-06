"""Name the overlay tools that hook our processes and pace them.

The report (#143, Inconsumable): NR and Frame Generation both stuck at
exactly 30 FPS, the frame limit in the panel changing nothing - and gone the
moment MSI Afterburner / RivaTuner Statistics Server was closed. RTSS injects
RTSSHooks64.dll into every process that presents with Direct3D and applies
its own frame limiter (or scanline sync) inside IDXGISwapChain::Present. The
worker presents the picture and Frame Generation through one swap chain, so
a global 30 FPS profile there caps both, and nothing in our pacing can see it.

Nothing is changed here - RTSS is the user's tool and its profile is theirs.
The worker's module list is read and the hook is named once per process in
the log, which is what a support bundle carries: a "capped at 30" report
then answers itself instead of taking a round of questions.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

#: Module name (lower case) -> what to tell the user it does to us.
KNOWN_HOOKS = {
    "rtsshooks64.dll": "RivaTuner Statistics Server (MSI Afterburner): its "
                       "frame limit or scanline sync applies to NeuralScreen's "
                       "picture - set a profile for this process with "
                       "Application detection level None to lift it",
}

#: How often check() walks the module lists.
CHECK_INTERVAL_S = 5.0
#: Walks per process before it is left alone. RTSS injects its hook when the
#: process starts presenting; half a minute of looking covers that, and a
#: clean process is not walked for the rest of the session.
MAX_WALKS = 6

_TH32CS_SNAPMODULE = 0x00000008
_TH32CS_SNAPMODULE32 = 0x00000010
_INVALID_HANDLE = wintypes.HANDLE(-1).value
_ERROR_BAD_LENGTH = 24


class _ModuleEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD),
                ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD),
                ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD),
                ("hModule", wintypes.HMODULE),
                ("szModule", ctypes.c_wchar * 256),
                ("szExePath", ctypes.c_wchar * 260)]


if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ModuleEntry)]
    _kernel32.Module32FirstW.restype = wintypes.BOOL
    _kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ModuleEntry)]
    _kernel32.Module32NextW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


def module_names(pid: int) -> list[str] | None:
    """Lower-case base names of the modules loaded in `pid`; None if unreadable.

    One Toolhelp snapshot: a single pass over the loader list. Measured on the
    worker (84 modules) at 0.8 ms, where EnumProcessModulesEx plus one
    GetModuleBaseNameW per module took 3 ms - each of those re-walks the
    remote list, so the cost grows with the square of the module count.
    """
    if sys.platform != "win32":
        return None
    flags = _TH32CS_SNAPMODULE | _TH32CS_SNAPMODULE32
    for _attempt in range(2):     # ERROR_BAD_LENGTH: the list moved; retry
        snap = _kernel32.CreateToolhelp32Snapshot(flags, int(pid))
        if snap and snap != _INVALID_HANDLE:
            break
        if ctypes.get_last_error() != _ERROR_BAD_LENGTH:
            return None
    else:
        return None
    try:
        entry = _ModuleEntry()
        entry.dwSize = ctypes.sizeof(_ModuleEntry)
        names = []
        ok = _kernel32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            names.append(entry.szModule.lower())
            ok = _kernel32.Module32NextW(snap, ctypes.byref(entry))
        return names
    finally:
        _kernel32.CloseHandle(snap)


def find(pid: int) -> list[str]:
    """The known pacing hooks loaded in `pid` (module names), possibly empty."""
    names = module_names(pid) or []
    return [name for name in names if name in KNOWN_HOOKS]


def check(st, log=print) -> None:
    """Name a known hook in our process or the worker, once per process.

    Called from the frame loop's housekeeping, so it must stay cheap and must
    never raise: at most one walk per process every CHECK_INTERVAL_S, at most
    MAX_WALKS walks per process, and nothing at all once a process is named.
    """
    import os
    import time
    try:
        now = time.monotonic()
        if now < getattr(st, "_foreign_hooks_due", 0.0):
            return
        st._foreign_hooks_due = now + CHECK_INTERVAL_S
        walks = getattr(st, "_foreign_hooks_seen", None)
        if walks is None:
            walks = st._foreign_hooks_seen = {}
        procs = [("ours", os.getpid())]
        worker = getattr(st, "worker", None)
        if worker is not None and getattr(worker, "poll", lambda: 0)() is None:
            procs.append(("worker", getattr(worker, "pid", None)))
        for who, pid in procs:
            if not pid or walks.get(pid, 0) >= MAX_WALKS:
                continue
            walks[pid] = walks.get(pid, 0) + 1
            hooks = find(pid)
            if hooks:
                walks[pid] = MAX_WALKS     # named: never walked again
                for name in hooks:
                    log(f"[env] {name} is loaded in {who} (pid {pid}) - "
                        f"{KNOWN_HOOKS[name]}")
    except Exception as exc:          # a diagnostic must not end the frame loop
        log(f"[env] module check failed: {type(exc).__name__}: {exc}")
