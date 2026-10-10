"""A monitor standing in for a missing one is not saved over it (#158).

config.json names the monitor by its Windows device name. When that monitor
is not there at startup - a TV, a dock, a DisplayPort monitor still waking up
at logon - the program uses monitor 0 for the session, which is right. But the
first save (closing the menu, any setting, exit) wrote monitor 0's name over
the user's, and the choice was gone for good. The same happened when the
captured monitor vanished mid-session and the capture moved to another one.

Checked through the real startup.configure (monitor probes stubbed), the real
save_menu_layout on a temporary config, and the real menu handler:
* the saved monitor is missing at startup -> the session runs on monitor 0,
  and the save keeps the saved name;
* the user then picks a monitor in the menu -> that one is saved.

Run:  runtime\python.exe tests\test_monitor_wanted_kept.py
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import commands  # noqa: E402
import pipeline  # noqa: E402
import settings_io  # noqa: E402
import startup  # noqa: E402

WANTED = "\\.\DISPLAY3"
STAND_IN = "\\.\DISPLAY1"


def main() -> int:
    failures = []
    saved = {
        "startup._log_environment": startup._log_environment,
        "startup.resolve_output_idx": startup.resolve_output_idx,
        "startup.list_monitors": startup.list_monitors,
        "startup._apply_monitor_name": startup._apply_monitor_name,
        "settings_io.devicename_for_output_idx": settings_io.devicename_for_output_idx,
        "pipeline.switch_monitor": pipeline.switch_monitor,
    }
    startup._log_environment = lambda cfg: None
    startup.resolve_output_idx = lambda name: None          # DISPLAY3 is not up
    startup.list_monitors = lambda: [(0, 1920, 1080, STAND_IN)]
    startup._apply_monitor_name = lambda name: (0, 0)
    settings_io.devicename_for_output_idx = lambda idx: STAND_IN if idx == 0 else None
    switched = []
    pipeline.switch_monitor = lambda st, name: switched.append(name)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"monitor": WANTED}), encoding="utf-8")
            st = types.SimpleNamespace(cfg_path=path)
            startup.configure(st)
            print(f"    startup: monitor {st.monitor}, kept {getattr(st, "monitor_wanted", None)!r}")
            if st.monitor != 0:
                failures.append(f"the session did not fall back to monitor 0 ({st.monitor})")
            menu = types.SimpleNamespace(user_scale=1.0, user_height=None, state={},
                                         mini=False, mini_rows=(), offset=(0, 0))
            st.split_pos, st.startup_menu, st.nr_small = 0.0, False, True
            st.display = types.SimpleNamespace(menu=menu)
            settings_io.save_menu_layout(st)
            on_disk = json.loads(path.read_text(encoding="utf-8")).get("monitor")
            print(f"    saved after a start without it: {on_disk!r}")
            if on_disk != WANTED:
                failures.append(f"the stand-in replaced the saved monitor: {on_disk!r}")
            st.capture = types.SimpleNamespace(devicename=STAND_IN)
            commands.apply_menu_action(st, ("monitor", f"0: 1920x1080 ({STAND_IN})"))
            settings_io.save_menu_layout(st)
            on_disk = json.loads(path.read_text(encoding="utf-8")).get("monitor")
            print(f"    saved after the user picks monitor 0: {on_disk!r}")
            if on_disk != STAND_IN:
                failures.append(f"the monitor the user picked was not saved: {on_disk!r}")
    finally:
        for key, value in saved.items():
            mod, name = key.split(".")
            setattr({"startup": startup, "settings_io": settings_io,
                     "pipeline": pipeline}[mod], name, value)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a stand-in monitor is used for the session, not saved over the user's")
    return 0


if __name__ == "__main__":
    sys.exit(main())
