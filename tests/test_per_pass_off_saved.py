"""Turning the per-pass set off is written to config.json.

Passes 2..N can have their own parameter set; "off" means no set at all, and
the file says so by not having the key. The toggle cleared it from the
running config, but save_menu_layout writes by reading the file and updating
it with the payload - and an absent key in the payload cannot delete one in
the file. So the old set stayed on disk, and with two or more passes the next
launch restored it: the worker ran it again and the panel showed it on.

Checked through the real save path on a temporary config file: a set that is
turned off is gone from the file after the save; a set that is on is written.

Run:  runtime\\python.exe tests\\test_per_pass_off_saved.py
"""
import json
import sys
import tempfile
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import settings_io  # noqa: E402

PP = {"style": 1, "intensity": 1.0, "local_tone": 0.0, "local_structure": 1.0,
      "skin_structure": -1.0}


def _save(path: Path, cfg: dict) -> dict:
    menu = types.SimpleNamespace(user_scale=1.0, user_height=None, state={}, mini=False,
                                 mini_rows=(), offset=(0, 0))
    st = types.SimpleNamespace(cfg_path=path, cfg=cfg,
                               params=dict(settings_io.PROFILES["Natural"], style=1),
                               monitor=0, lang="en", work_scale=1.0, split_pos=0.0,
                               startup_menu=False, nr_small=True,
                               display=types.SimpleNamespace(menu=menu))
    if not settings_io.save_menu_layout(st):
        raise RuntimeError("save_menu_layout failed")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps({"nr_passes": 2}), encoding="utf-8")
        on = _save(path, {"profile": "Natural", "nr_passes": 2, "nr_pass_params": dict(PP)})
        if on.get("nr_pass_params") != PP:
            failures.append(f"a set that is on was not written: {on.get('nr_pass_params')}")
        # The toggle pops the key from the running config, as commands.py does.
        off = _save(path, {"profile": "Natural", "nr_passes": 2})
        print(f"    after turning the set off the file holds "
              f"nr_pass_params={off.get('nr_pass_params')!r}")
        if "nr_pass_params" in off:
            failures.append("the set turned off is still in config.json")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: turning the per-pass set off reaches config.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
