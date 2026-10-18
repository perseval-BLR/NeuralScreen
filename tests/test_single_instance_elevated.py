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
* the early gate with CreateMutexW answering NULL / ERROR_ACCESS_DENIED returns 1
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
import sys
import tempfile
import uuid
import ast
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import taskbar  # noqa: E402

_spec = importlib.util.spec_from_file_location("ns_main", str(BASE / "main.py"))
ns_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ns_main)


def _run_main(error: int, handle) -> object:
    """Probe only the early gate. No production mutex or real window APIs."""
    kernel = SimpleNamespace(CreateMutexW=mock.Mock(return_value=handle), CloseHandle=mock.Mock())
    user = SimpleNamespace(FindWindowW=lambda *a: 0)
    with mock.patch.object(ns_main, "_startup_claim", None), mock.patch.object(ns_main, "_startup_bindings", return_value=(kernel, user)), mock.patch.object(ns_main, "_startup_update_guard", return_value=None), mock.patch.object(ctypes, "get_last_error", return_value=error):
        result = ns_main._startup_instance_gate()
        if result == 0:
            assert ns_main._startup_instance_gate() == 0
            kernel.CreateMutexW.assert_called_once_with(None, False, "NeuralScreen_SingleInstance")
        elif handle:
            kernel.CloseHandle.assert_called_once_with(handle)
        return result


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
    for error, handle in ((0, None), (123, None), (123, 0x1234)):
        if _run_main(error, handle) != 1:
            failures.append(f"gate did not fail closed for {error}, {handle}")
    if _run_main(0, 0x1234) != 0:
        failures.append("fresh claim or idempotent reuse failed")
    if ns_main._startup_claim is not None:
        failures.append("non-main import claimed a production mutex")
    from updates import install_id_for
    from update_apply import install_id_for as helper_id, control_dir_for
    with tempfile.TemporaryDirectory(prefix="ns identity שלום ") as temp:
        for path in (temp, temp[0].upper() + temp[1:], temp[0].lower() + temp[1:]):
            expected = install_id_for(path)
            got_id, control = ns_main._startup_install_identity(path)
            assert got_id == expected == helper_id(path)
            assert control == control_dir_for(path)
    tree = ast.parse((BASE / "main.py").read_text(encoding="utf-8"))
    early = next(n for n in tree.body if isinstance(n, ast.If) and isinstance(n.test, ast.Compare))
    calls = [(n.lineno, n.func.id) for n in ast.walk(early) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert next(line for line, name in calls if name == "_startup_instance_gate") < next(line for line, name in calls if name == "_drop_stale_bytecode")

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
