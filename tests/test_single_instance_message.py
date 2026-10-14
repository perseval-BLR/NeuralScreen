"""A second copy asks the running one to show itself - with a message no
other program can send it by accident.

The number was 0x8000 + 0x4E53 = 0xCE53, described as the WM_APP range. WM_APP
ends at 0xBFFF; 0xC000-0xFFFF is where RegisterWindowMessage hands out numbers
to every program in the session, so another program's broadcast message could
carry 0xCE53 and open the panel on its own. The message is a registered one of
our own now. The old number is still POSTED by a new copy - an older copy may
be the one running - and no longer ACCEPTED.

No window is created: the window procedure is called directly.

Run:  runtime\\python.exe tests\\test_single_instance_message.py
"""
import sys
import ctypes
import importlib.util
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import taskbar  # noqa: E402


class _Window:
    """Only what _wnd_proc touches for the two messages under test."""

    def __init__(self):
        self.emitted = []

    def _emit(self, what):
        self.emitted.append(what)


def main() -> int:
    failures = []
    show, legacy = taskbar.WM_NS_SHOW, taskbar.WM_NS_SHOW_LEGACY
    if not (0xC000 <= show <= 0xFFFF or 0x8000 <= show <= 0xBFFF):
        failures.append(f"WM_NS_SHOW is 0x{show:04X}: neither a registered "
                        f"message nor in the WM_APP range")
    if show == legacy:
        failures.append("WM_NS_SHOW is still the old number")
    if taskbar._registered_message("NeuralScreen.ShowSettings") != show:
        failures.append("a second copy would not get the same number")

    window = _Window()
    taskbar.TaskbarWindow._wnd_proc(window, 0, show, 0, 0)
    if window.emitted != ["show_settings"]:
        failures.append(f"WM_NS_SHOW does not show the settings: {window.emitted}")
    window = _Window()
    taskbar.TaskbarWindow._wnd_proc(window, 0, legacy, 0, 0)
    if window.emitted:
        failures.append("the old number still opens the panel - any program "
                        "broadcasting a registered message that got 0xCE53 "
                        "would open it")

    spec = importlib.util.spec_from_file_location("ns_message_main", str(BASE / "main.py"))
    main_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main_mod)
    posted = []
    closed = []
    kernel = SimpleNamespace(CreateMutexW=lambda *a: 0x1234, CloseHandle=closed.append)
    user = SimpleNamespace(FindWindowW=lambda *a: 99, RegisterWindowMessageW=lambda *a: show,
                           PostMessageW=lambda *a: posted.append(a))
    with mock.patch.object(main_mod, "_startup_claim", None), mock.patch.object(main_mod, "_startup_bindings", return_value=(kernel, user)), mock.patch.object(ctypes, "get_last_error", return_value=183):
        assert main_mod._startup_instance_gate() == 1
    assert posted == [(99, show, 0, 0), (99, legacy, 0, 0)]
    assert closed == [0x1234]

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print(f"OK: the show-yourself message is registered (0x{show:04X}), the old "
          f"0xCE53 is posted for older copies and accepted by none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
