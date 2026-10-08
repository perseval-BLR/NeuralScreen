r"""Switching the per-pass set on does not change the picture by itself.

The set for passes 2..N is seeded from what those passes already ran with,
so flipping the switch moves nothing until a control in the set is moved.
Without a set, passes 2+ now run the main parameters with local tone 0 (the
tone stacked a darkening on every pass - test_cascade_tone); a seed that
copied the main tone would bring that darkening back the moment the switch
is flipped.

Checked on the real menu handler with a stand-in session: the seeded set
equals the main set except local tone, which is 0.

Run:  runtime\\python.exe tests\\test_per_pass_seed.py
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import commands  # noqa: E402
import settings_io  # noqa: E402


def main() -> int:
    main_set = {"style": 2, "auto_mask": 1, "intensity": 0.8, "local_tone": 0.5,
                "local_structure": 1.2, "skin_structure": -1.0}
    sent = []
    menu_state = {}
    st = SimpleNamespace(
        nr_pass_params=None, nr_passes=3, params=dict(main_set), cfg={},
        display=SimpleNamespace(menu=SimpleNamespace(set_state=menu_state.update)))
    saved_send = commands._send_per_pass_now
    saved_layout = settings_io.save_menu_layout
    commands._send_per_pass_now = lambda st_, params, enabled=True: sent.append(
        (dict(params or {}), enabled))
    settings_io.save_menu_layout = lambda st_: None
    try:
        commands.apply_menu_action(st, ("toggle", "pass_params"))
    finally:
        commands._send_per_pass_now = saved_send
        settings_io.save_menu_layout = saved_layout
    failures = []
    seed = st.nr_pass_params or {}
    if not seed:
        failures.append("switching the per-pass set on produced no set")
    else:
        if seed.get("local_tone") != 0.0:
            failures.append(f"the set was seeded with local tone {seed.get('local_tone')}, "
                            f"not the 0 that passes 2+ run with")
        for key in ("style", "intensity", "local_structure", "skin_structure"):
            if seed.get(key) != main_set[key]:
                failures.append(f"the set's {key} is {seed.get(key)}, the main set has "
                                f"{main_set[key]}")
        if not sent or sent[-1][0] != seed:
            failures.append("the seeded set did not go to the worker")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the per-pass set starts from what passes 2+ already ran with")
    return 0


if __name__ == "__main__":
    sys.exit(main())
