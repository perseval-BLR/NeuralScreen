"""NR starts the way it was left (#166).

The #166 reporter asked for NeuralScreen to start with NR and FG as they were
left (or off). Frame Generation already was - its switch is saved in
config.json - but NR always started on. Num1 and the NR tile now save the
state (nr_on), and the next start comes up with it.

Checked:

1. the start's decision: on by default, off only when nr_on is false;
2. Num1 (the "toggle" command) flips NR, saves nr_on and asks for the config
   to be written, both ways;
3. nr_on is a validated boolean and is written with the rest of the layout;
4. with NR off at start the panel opens at once - it waited for a second
   frame that never comes, and the program looked minimised.

Fails before the change: NR always started on and nothing was saved.

Run:  runtime\python.exe tests\test_nr_remembered.py
"""
import contextlib
import io
import os
import queue
import sys
import types
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))


def main() -> int:
    failures = []
    import startup
    import commands
    import settings_io

    for cfg, want in (({}, True), ({"nr_on": True}, True), ({"nr_on": False}, False)):
        if startup.nr_starts_on(cfg) is not want:
            failures.append(f"NR at start with {cfg}: {startup.nr_starts_on(cfg)}")

    saves = []
    real_save = settings_io.save_menu_layout
    settings_io.save_menu_layout = lambda st: saves.append(dict(st.cfg))
    try:
        st = types.SimpleNamespace(
            cfg={}, paused=False, worker_failed=False, work_frame=None, lang="en", frame_index=0,
            running=True, tray_commands=queue.Queue(),
            display=types.SimpleNamespace(alert=lambda *a, **k: None, set_visible=lambda v: None),
            tray=types.SimpleNamespace(_set_state=lambda **k: None))
        with contextlib.redirect_stdout(io.StringIO()):
            st.tray_commands.put("toggle")
            commands.drain_commands(st)
            off = (st.paused, st.cfg.get("nr_on"))
            st.tray_commands.put("toggle")
            commands.drain_commands(st)
            on = (st.paused, st.cfg.get("nr_on"))
    finally:
        settings_io.save_menu_layout = real_save
    if off != (True, False) or on != (False, True) or len(saves) != 2:
        failures.append(f"Num1 off {off}, on {on}, saves {len(saves)}")

    if "nr_on" not in settings_io._BOOL_KEYS:
        failures.append("nr_on is not validated as a boolean")
    import inspect
    sig = inspect.signature(settings_io._menu_layout_payload)
    from unittest import mock
    args = {name: mock.MagicMock() for name in sig.parameters}
    cfg = settings_io.load_config(Path(BASE) / "config.default.json")
    cfg["nr_on"] = False
    args.update(cfg=cfg, params=dict(settings_io.PROFILES["Natural"]), monitor=0, lang="en")
    try:
        payload = settings_io._menu_layout_payload(**args)
        if payload.get("nr_on") is not False:
            failures.append(f"the saved layout carries nr_on={payload.get('nr_on')}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"the layout payload could not be built: {exc!r}")

    # #166: with NR off at start no frame arrives, and the panel waited for
    # the second one - the program looked as if it had started minimised.
    import main as app
    got = [app.startup_menu_due(p, f) for p, f in ((False, 0), (False, 1), (False, 2),
                                                  (True, 0))]
    if got != [False, False, True, True]:
        failures.append(f"the start's panel opens at {got} (expected only after two "
                        f"frames, or at once with NR off)")

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
