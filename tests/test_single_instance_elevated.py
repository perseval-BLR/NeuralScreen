r"""A second copy stays out when the running one is elevated.

The single-instance check was "CreateMutexW, then GetLastError() == 183
(ERROR_ALREADY_EXISTS)". A copy started as administrator creates the mutex
with an elevated security descriptor, and a normal launch then cannot open
it at all: CreateMutexW returns NULL with ERROR_ACCESS_DENIED (5). The NULL
read as "I am the first" and the second copy started next to the running
one - the two fight over the screen capture, which is what the mutex is for.
The running copy's "show yourself" message was also dropped on the way in:
UIPI keeps a normal-integrity process from posting to an elevated window.

Checked:
* main() with CreateMutexW answering NULL / ERROR_ACCESS_DENIED returns 1
  before the configuration is even read (the real main, stubs only at the
  Win32 boundary; FindWindowW finds nothing, so no real copy is disturbed);
* the same with ERROR_ALREADY_EXISTS (the ordinary second copy);
* a real mutex, created twice in this process, reads as running the second
  time and not the first;
* the taskbar window lets WM_NS_SHOW through the message filter.

Run:  runtime\python.exe tests\test_single_instance_elevated.py
"""
import ctypes
import importlib.util
import os
import sys
import tempfile
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import taskbar  # noqa: E402

_spec = importlib.util.spec_from_file_location("ns_main", str(BASE / "main.py"))
ns_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ns_main)


class _Reached(Exception):
    pass


class _Kernel32:
    """CreateMutexW answers like the OS does for the case under test."""

    def __init__(self, real, handle):
        self._real = real
        self._handle = handle

    def CreateMutexW(self, *_a):
        return self._handle

    def __getattr__(self, name):
        return getattr(self._real, name)


def _run_main(error: int, handle) -> object:
    """main()'s answer when CreateMutexW gives `handle` and `error`."""
    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32
    saved = (k32.CreateMutexW, k32.GetLastError, u32.FindWindowW,
             taskbar.kernel32, getattr(taskbar, "_last_error", None),
             ns_main._init_logging, ns_main.startup.configure, sys.argv)
    # Both the old route (ctypes.windll) and the typed one (taskbar's private
    # kernel32) answer the same, so the test measures the decision only.
    k32.CreateMutexW = lambda *_a: handle or 0
    k32.GetLastError = lambda: error
    u32.FindWindowW = lambda *_a: 0          # never touch a real running copy
    taskbar.kernel32 = _Kernel32(saved[3], handle)
    taskbar._last_error = lambda: error
    ns_main._init_logging = lambda: None

    def reached(_st):
        raise _Reached()

    ns_main.startup.configure = reached
    sys.argv = ["main.py", "--config",
                os.path.join(tempfile.mkdtemp(), "config.json")]
    try:
        return ns_main.main()
    except _Reached:
        return "started"
    finally:
        (k32.CreateMutexW, k32.GetLastError, u32.FindWindowW,
         taskbar.kernel32, last_error, ns_main._init_logging,
         ns_main.startup.configure, sys.argv) = saved
        if last_error is not None:
            taskbar._last_error = last_error
        else:
            del taskbar._last_error


def main() -> int:
    failures = []

    got = _run_main(5, None)
    if got != 1:
        failures.append(f"with the running copy elevated (NULL, "
                        f"ERROR_ACCESS_DENIED) main() answered {got!r}: a "
                        f"second copy starts next to the first")
    got = _run_main(183, 0x1234)
    if got != 1:
        failures.append(f"with ERROR_ALREADY_EXISTS main() answered {got!r}")

    claim = getattr(taskbar, "claim_single_instance", None)
    if claim is None:
        failures.append("taskbar has no claim_single_instance")
    else:
        name = f"NeuralScreen_test_{uuid.uuid4().hex}"
        first, running1 = claim(name)
        second, running2 = claim(name)
        if running1 or not running2:
            failures.append(f"a real mutex: first claim running={running1}, "
                            f"second running={running2}")
        for h in (first, second):
            if h:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))

    allow = getattr(taskbar, "allow_show_message", None)
    calls = []
    real_filter = taskbar.user32.ChangeWindowMessageFilterEx
    try:
        taskbar.user32.ChangeWindowMessageFilterEx = \
            lambda *a: calls.append(a) or 1
        if allow is None or not allow(0x1_8000_0010):
            failures.append("the taskbar window does not open its message "
                            "filter for WM_NS_SHOW")
        elif calls[0][1:3] != (taskbar.WM_NS_SHOW, 1):
            failures.append(f"the filter was opened for {calls}")
    finally:
        taskbar.user32.ChangeWindowMessageFilterEx = real_filter

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: an elevated running copy keeps the second one out, and can be "
          "asked to show itself")
    return 0


if __name__ == "__main__":
    sys.exit(main())
