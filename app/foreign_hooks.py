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

_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_LIST_MODULES_ALL = 0x03

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.HMODULE),
                                            wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD),
                                            wintypes.DWORD]
    _psapi.EnumProcessModulesEx.restype = wintypes.BOOL
    _psapi.GetModuleBaseNameW.argtypes = [wintypes.HANDLE, wintypes.HMODULE,
                                          wintypes.LPWSTR, wintypes.DWORD]
    _psapi.GetModuleBaseNameW.restype = wintypes.DWORD


def module_names(pid: int) -> list[str] | None:
    """Lower-case base names of the modules loaded in `pid`; None if unreadable."""
    if sys.platform != "win32":
        return None
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ,
                                   False, int(pid))
    if not handle:
        return None
    try:
        count = 512
        while True:
            mods = (wintypes.HMODULE * count)()
            needed = wintypes.DWORD()
            if not _psapi.EnumProcessModulesEx(handle, mods, ctypes.sizeof(mods),
                                               ctypes.byref(needed), _LIST_MODULES_ALL):
                return None
            got = needed.value // ctypes.sizeof(wintypes.HMODULE)
            if got <= count:
                break
            count = got + 16      # modules loaded between the two calls
        names = []
        buf = ctypes.create_unicode_buffer(260)
        for i in range(got):
            if mods[i] and _psapi.GetModuleBaseNameW(handle, mods[i], buf, len(buf)):
                names.append(buf.value.lower())
        return names
    finally:
        _kernel32.CloseHandle(handle)


def find(pid: int) -> list[str]:
    """The known pacing hooks loaded in `pid` (module names), possibly empty."""
    names = module_names(pid) or []
    return [name for name in names if name in KNOWN_HOOKS]


def check(st, log=print) -> None:
    """Name a known hook in our process or the worker, once per process.

    Called from the frame loop's housekeeping, but walks at most every
    CHECK_INTERVAL_S: a module walk is ~0.2 ms per 30 modules and the worker
    carries a few hundred. A hook is injected when the process starts
    presenting, so a few seconds of delay costs nothing; a process already
    named is not walked again.
    """
    import os
    import time
    now = time.monotonic()
    if now < getattr(st, "_foreign_hooks_due", 0.0):
        return
    st._foreign_hooks_due = now + CHECK_INTERVAL_S
    seen = getattr(st, "_foreign_hooks_seen", None)
    if seen is None:
        seen = st._foreign_hooks_seen = set()
    procs = [("ours", os.getpid())]
    worker = getattr(st, "worker", None)
    if worker is not None and getattr(worker, "poll", lambda: 0)() is None:
        procs.append(("worker", getattr(worker, "pid", None)))
    for who, pid in procs:
        if not pid or pid in seen:
            continue
        hooks = find(pid)
        if hooks:
            seen.add(pid)
            for name in hooks:
                log(f"[env] {name} is loaded in {who} (pid {pid}) - "
                    f"{KNOWN_HOOKS[name]}")
