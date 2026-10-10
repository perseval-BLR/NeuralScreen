"""Changes made inside the debounce window add up instead of replacing each other.

The apply is debounced (#115): every request goes into one slot and is applied
0.3 s after the last change. Each request used to build its whole tuple -
scale, profile, parameters - from the RUNNING state, which only changes when
the slot is applied. So a second change inside the window threw the first one
away:

* two quick presses of the work-scale key from 0.50 queued 0.45, not 0.40 -
  holding the key walked one step in total;
* intensity, then local tone: the intensity change was lost;
* the resolution slider, then a tone slider: the resolution change was lost;
* a profile, then a slider: the profile change was lost;
* a preset saved inside the window stored the old numbers.

Checked through the real menu and hotkey handlers (commands.py) with the
apply itself stubbed out - nothing is launched.

Run:  runtime\\python.exe tests\\test_apply_debounce_merge.py
"""
import queue
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import commands  # noqa: E402
import pipeline  # noqa: E402
import settings_io  # noqa: E402
from settings_io import PROFILES  # noqa: E402


def _state():
    return types.SimpleNamespace(
        cfg={"profile": "Natural", "lang": "en", "presets": {}}, lang="en",
        params=dict(PROFILES["Natural"], style=1), presets={},
        nr_small=True, work_scale=0.50, nr_passes=1,
        width=3840, height=2160, running=True,
        pending_apply=None, pending_apply_due=0.0,
        tray_commands=queue.Queue(),
        display=types.SimpleNamespace(
            alert=lambda *a, **kw: None,
            menu=types.SimpleNamespace(set_state=lambda *a, **kw: None)))


def main() -> int:
    failures = []
    real_save = settings_io.save_menu_layout
    settings_io.save_menu_layout = lambda st: True
    try:
        st = _state()
        st.tray_commands.put("scale_down")
        st.tray_commands.put("scale_down")
        commands.drain_commands(st)
        got = round(st.pending_apply[0], 2)
        print(f"    two scale_down presses from 0.50 queue {got}")
        if abs(got - 0.40) > 1e-6:
            failures.append(f"two scale_down presses from 0.50 queued {got}, not 0.40")

        st = _state()
        commands.apply_menu_action(st, ("param", "intensity", 0.3))
        commands.apply_menu_action(st, ("param", "local_tone", 1.2))
        params = st.pending_apply[2]
        print(f"    intensity then tone queue intensity {params['intensity']}, "
              f"tone {params['local_tone']}")
        if params["intensity"] != 0.3 or params["local_tone"] != 1.2:
            failures.append(f"intensity then tone queued {params}")

        st = _state()
        commands.apply_menu_action(st, ("nr_res", 0.30))
        commands.apply_menu_action(st, ("param", "local_tone", 1.2))
        print(f"    resolution then tone queue scale {st.pending_apply[0]}")
        if abs(st.pending_apply[0] - 0.30) > 1e-6:
            failures.append(f"resolution then tone queued scale {st.pending_apply[0]}")

        st = _state()
        commands.apply_menu_action(st, ("profile", "Faithful"))
        commands.apply_menu_action(st, ("param", "local_tone", 0.1))
        profile, params = st.pending_apply[1], st.pending_apply[2]
        print(f"    profile then slider queue {profile!r}, intensity {params['intensity']}")
        if profile != "Faithful" or params["intensity"] != PROFILES["Faithful"]["intensity"]:
            failures.append(f"profile then slider queued {profile!r} with {params}")

        st = _state()
        commands.apply_menu_action(st, ("style", 2))
        commands.apply_menu_action(st, ("param", "intensity", 0.4))
        params = st.pending_apply[2]
        if params.get("style") != 2:
            failures.append(f"style then slider queued style {params.get('style')}")

        st = _state()
        commands.apply_menu_action(st, ("param", "intensity", 0.7))
        commands.apply_menu_action(st, ("button", "save_preset"))
        saved = next(iter(st.presets.values()), {})
        print(f"    preset saved inside the window holds intensity {saved.get('intensity')}")
        if saved.get("intensity") != 0.7:
            failures.append(f"a preset saved inside the window stored {saved}")
    finally:
        settings_io.save_menu_layout = real_save
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: changes inside the debounce window add up; the last value of each wins")
    return 0


if __name__ == "__main__":
    sys.exit(main())
