"""Full speed while the window is hidden: the opt-out is real and switchable (#137).

The report (joaquiros360-dot, independently confirmed by namesource on a 4070
Ti Super): smooth while the NeuralScreen window is open, a collapse the moment
it is minimised or sent to the background. Windows 11 does this on purpose -
Quality of Service classifies a window-owning process by its window's state
(High in focus, Medium visible, Low minimised or fully occluded) and the timer
resolution page states that a process whose window is invisible "does not
get a guaranteed higher resolution than the default system resolution". The
documented opt-out is SetProcessInformation(ProcessPowerThrottling) with the
EXECUTION_SPEED bit selected and cleared.

What this test locks, each a way the fix could silently not be a fix:

  * the mask really clears, read back from the OS - not just "the call
    returned true";
  * the opt-out works on ANOTHER process by pid, which is the only reason the
    worker needs no native change and no rebuild;
  * off restores the default - a switch that cannot be turned off is not a
    switch, and the option costs battery life by design;
  * the shipped default is ON, because the report came from a user who
    minimised the program expecting it to keep working;
  * the menu row exists in its state dict and its click reaches the config, so
    the choice survives a restart;
  * the row is really BUILT, on the tab it lives on, and fits its cell in
    all twelve languages - a key in the state dict proves none of that;
  * the config key is in the diagnostic list - "it is slow when minimised" is
    unanswerable in a support bundle without it.

Run:  runtime\\python.exe tests\\test_power_when_hidden.py
"""
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import power  # noqa: E402

#: The same bit the module uses, spelled out here so the test would notice if
#: the module's own constant drifted to something the OS treats differently.
EXECUTION_SPEED = 0x1

#: save_menu_layout writes the live parameter snapshot along with the switch,
#: so the stub state has to carry one or the config write fails and the
#: "does the choice survive a restart" check reads a file nobody wrote.
PARAMS = {"intensity": 1.0, "local_tone": 0.5, "local_structure": 1.0,
          "skin_structure": -1.0, "style": 1}


def _masks(handle) -> tuple | None:
    """What the OS reports for that process, straight from kernel32.

    Reimplemented rather than read from the module: a test that asks the
    module what the module did has checked nothing.
    """
    class STATE(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_ulong),
                    ("ControlMask", ctypes.c_ulong),
                    ("StateMask", ctypes.c_ulong)]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # The argtypes are load-bearing: without them ctypes passes -1 (the
    # GetCurrentProcess pseudo-handle) as a 32-bit int, the call fails, and a
    # broken read looks exactly like "the OS does not support this".
    k32.GetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_void_p, ctypes.c_ulong]
    k32.GetProcessInformation.restype = ctypes.c_int
    st = STATE()
    st.Version = 1
    ok = k32.GetProcessInformation(handle, 4, ctypes.byref(st), ctypes.sizeof(st))
    return (int(st.ControlMask), int(st.StateMask)) if ok else None


def check_own_process(failures: list) -> None:
    """The opt-out clears the mask on this process, and off puts it back."""
    before = _masks(-1)
    if before is None:
        failures.append("GetProcessInformation(ProcessPowerThrottling) is not "
                        "supported here - the whole fix rests on it")
        return

    verdict = power.apply_own(True)
    after = _masks(-1)
    if after is None:
        failures.append("the read-back stopped working after the write")
    else:
        if after[1] & EXECUTION_SPEED:
            failures.append(f"the opt-out did not clear the execution-speed "
                            f"throttle: StateMask={after[1]} still has it set")
        if not after[0] & EXECUTION_SPEED:
            failures.append(f"ControlMask={after[0]} does not select "
                            f"EXECUTION_SPEED, so the opt-out was never "
                            f"requested - the call may have succeeded and "
                            f"changed nothing")
    if "full speed" not in verdict and not verdict.startswith("off"):
        failures.append(f"apply_own said {verdict!r} after clearing the mask")

    verdict_off = power.apply_own(False)
    off = _masks(-1)
    if off is not None and off[1] & EXECUTION_SPEED:
        failures.append("switching the option off left the throttle cleared "
                        "(or worse, enabled it) - off must restore the default")
    # Off must hand the policy back, not only say so: with ControlMask still
    # selecting EXECUTION_SPEED the opt-out is in force until the process
    # exits, while the log and the support bundle report "the OS decides".
    if off is not None and off[0] & EXECUTION_SPEED:
        failures.append(f"switching the option off kept the opt-out "
                        f"(ControlMask={off[0]}) while apply_own reported "
                        f"{verdict_off!r}")
    # And no state is left behind for the next check to trip over.
    power.apply_own(False)


def check_other_process(failures: list) -> None:
    """The worker's case: a pid we do not own, opened by handle.

    This is the fact that decides the shape of the fix - if the call only ever
    worked on the current process, the worker would need a native change and a
    rebuild, and the option would cost a release.

    The child must not outlive this check: run_tests.py runs the suite back to
    back and waits for our processes to settle before the next test, and a
    stray sleeper is one more process for that wait to trip over.
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        time.sleep(0.4)  # let it exist before opening it
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x0200 | 0x1000, False, child.pid)
        if not handle:
            failures.append("OpenProcess on another process failed - the "
                            "worker cannot be reached this way")
            return
        try:
            power._apply(handle, keep_timer_resolution=True)
            masks = _masks(handle)
            if masks is None:
                failures.append("could not read the other process's masks")
            elif masks[1] & EXECUTION_SPEED:
                failures.append(f"the opt-out did not reach the other "
                                f"process: StateMask={masks[1]}")
            # Through the call the app makes: on, then off, by pid.
            power.apply_worker(child, True)
            verdict_off = power.apply_worker(child, False)
            masks = _masks(handle)
            if masks is not None and masks[0] & EXECUTION_SPEED:
                failures.append(f"switching the option off kept the worker "
                                f"opted out (ControlMask={masks[0]}) while it "
                                f"reported {verdict_off!r}")
        finally:
            k32.CloseHandle(handle)
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
        # Prove it is gone rather than trusting terminate(): the next test's
        # settle() looks for leftovers, and a sleeping python is one.
        if child.poll() is None:
            failures.append("the probe process outlived its check")


def check_shipped_default(failures: list) -> None:
    """The shipped default is ON, and only an explicit false turns it off."""
    default = json.loads((BASE / "config.default.json").read_text(encoding="utf-8"))
    if default.get("keep_speed_when_hidden") is not True:
        failures.append(f"config.default.json ships "
                        f"keep_speed_when_hidden="
                        f"{default.get('keep_speed_when_hidden')!r}, expected "
                        f"True - the report asked for full speed when hidden")
    # _validate_config demands a whole config, so the shipped file is the base
    # for each case - the same shape test_hotkeys_master_switch uses.
    import settings_io
    for raw, expect in ((True, True), (False, False),
                        # The hostile shape: the string "false" is truthy, so a
                        # hand-edited config would leave the option ON while
                        # looking off. A word that spells a boolean is read as
                        # one; junk falls back to the shipped default.
                        ("false", False), ("no", False), ("off", False),
                        ("true", True), ("junk", True)):
        probe = dict(default)
        probe["keep_speed_when_hidden"] = raw
        got = settings_io._validate_config(probe).get("keep_speed_when_hidden",
                                                      "absent")
        if got is not expect:
            failures.append(f"a config with keep_speed_when_hidden={raw!r} "
                            f"validates to {got!r}, expected {expect}")
    # An old config.json written before the setting existed must keep the
    # speed, not quietly lose it on upgrade.
    probe = dict(default)
    probe.pop("keep_speed_when_hidden", None)
    if settings_io._validate_config(probe).get("keep_speed_when_hidden",
                                              True) is not True:
        failures.append("a config without the key does not read as ON")


def check_menu_row(failures: list) -> None:
    """The row is in the state dict and its click reaches the config.

    The state dict matters on its own: set_state drops an unknown key in
    silence, so a missing entry draws the toggle as off while the opt-out is
    in force - the exact failure #134 had with the hotkeys switch.
    """
    import commands
    from overlay_ui import OverlayMenu
    import pygame

    if not pygame.font.get_init():
        pygame.font.init()
    menu = OverlayMenu(1.0, lambda size=14: pygame.font.Font(None, size))
    menu.lang = "en"
    if "keep_speed_when_hidden" not in menu.state:
        failures.append("the menu has no keep_speed_when_hidden in its state - "
                        "set_state would drop the value in silence and the "
                        "toggle would always draw as off")
        return

    tmp = Path(tempfile.mkdtemp(prefix="ns137-"))
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps({"keep_speed_when_hidden": True,
                                    "profile": "Natural", "params": dict(PARAMS),
                                    "monitor": 0, "theme": "light"}),
                        encoding="utf-8")
    st = SimpleNamespace(
        cfg={"keep_speed_when_hidden": True, "profile": "Natural",
             "theme": "light"},
        worker=None, display=SimpleNamespace(menu=menu, alert=lambda *a, **k: None),
        lang="en", params=dict(PARAMS), cfg_path=cfg_path, presets={}, monitor=0,
        work_scale=1.0, split_pos=0.0, startup_menu=False, nr_small=True,
        window_hwnd=None, width=1920, height=1080, work_w=1920, work_h=1080,
    )
    for expect in (False, True):
        commands.apply_menu_action(st, ("toggle", "keep_speed_when_hidden"))
        if bool(st.cfg.get("keep_speed_when_hidden")) is not expect:
            failures.append(f"the toggle left keep_speed_when_hidden="
                            f"{st.cfg.get('keep_speed_when_hidden')!r}, "
                            f"expected {expect}")
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        if bool(saved.get("keep_speed_when_hidden")) is not expect:
            failures.append(f"the choice did not reach config.json "
                            f"({saved.get('keep_speed_when_hidden')!r}, "
                            f"expected {expect}) - it would not survive a "
                            f"restart")


def check_renders(failures: list) -> None:
    """The row is really built, on the tab it lives on, in every language.

    A key in the state dict is not enough: the builders only run for the tab
    section() selected, so a row added to the wrong tab is drawn never and the
    switch would be unreachable. And a caption wider than its cell is clipped,
    which reads as a truncated label rather than as a missing feature.
    """
    import json as _json
    import pygame
    from i18n import STRINGS
    from overlay_ui import OverlayMenu
    import settings_io

    if not pygame.font.get_init():
        pygame.font.init()
    base = _json.loads((BASE / "config.default.json").read_text(encoding="utf-8"))
    menu = OverlayMenu(1.0, lambda size=14: pygame.font.Font(None, size))
    missing = []
    for lang in sorted(STRINGS):
        menu.lang = lang
        menu.page = "settings"          # the Program page
        menu.settings_tab = "app"       # the Behaviour section lives here
        payload = settings_io._menu_layout_payload(
            base, settings_io.resolve_params(base), 0, lang, 1.0, 0.0,
            False, True, menu)
        menu.set_state(payload)
        menu.state["keep_speed_when_hidden"] = True
        menu.layout(2560, 1440)
        row = [i for i in menu.items if i.key == "keep_speed_when_hidden"]
        if not row:
            missing.append(lang)
            continue
        room = row[0].rect.w - menu._u(16)
        for label, font in ((STRINGS[lang].get("keep_speed_when_hidden", ""),
                             menu._font),
                            (STRINGS[lang].get("keep_speed_hint", ""),
                             menu._small_font)):
            if label and font.size(label)[0] > room:
                failures.append(f"{lang}: {label!r} is "
                                f"{font.size(label)[0]} of {room} px and "
                                f"would be clipped")
    if missing:
        failures.append(f"the keep_speed_when_hidden row is not built in "
                        f"{missing} - the switch is unreachable there")


def check_diagnostics(failures: list) -> None:
    """A support bundle must carry the switch and what the OS answered."""
    import diagnostics
    keys = diagnostics._SETTINGS_KEYS
    if "keep_speed_when_hidden" not in keys:
        failures.append("keep_speed_when_hidden is not in the diagnostic "
                        "config list - a report of 'slow when minimised' "
                        "cannot be answered without it")
    status = power.status()
    if "ours" not in status or "worker" not in status:
        failures.append(f"power.status() does not report both processes: "
                        f"{status!r}")


def main() -> int:
    failures: list = []
    check_shipped_default(failures)
    print("shipped default, the validator and the diagnostic list: checked")
    check_own_process(failures)
    print("the opt-out clears the mask on this process: checked")
    check_other_process(failures)
    print("and on another process by pid (the worker's case): checked")
    check_menu_row(failures)
    print("the menu row and its click: checked")
    check_renders(failures)
    print("the row is built and fits, in all twelve languages: checked")
    check_diagnostics(failures)

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: full speed while hidden is real, switchable, and on by default (#137)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
