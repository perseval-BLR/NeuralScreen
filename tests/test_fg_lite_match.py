"""FG Lite and FG matched to the display (scan 27.10, v2.7.0).

FG Lite asks DLSS-G for preset C (DLSSG.LiteCandidateBlendingEnabled, in the
310.9.16 runtime): cheaper generated frames. "Matched to the display" picks the
largest multiplier up to the user's own whose output still fits the screen's
refresh rate - a 30 fps video on 144 Hz runs x4, a 60 fps game x2 - and moves
only after a new choice has held, because a step change recreates FG.

Checked without a GPU:

1. the frame header carries the Lite bit only with FG explicitly set and Lite
   on; the multiplier bits are unchanged by it;
2. the step choice: table cases, the user's step as the ceiling, at least x2;
3. the hold: a short dip does not move the step, a lasting one does; a lower
   ceiling applies at once; a near-still source keeps the step;
4. main's per-frame choice: the user's step with the switch off, the matched
   one with it on, read against the monitor's refresh rate;
5. both switches: off by default, in the Network block of the panel, a press
   flips and saves them, the payloads carry them, every language has the text.

The worker side (the key set at FG create, "lite (preset C)" in the log) is
measured on the bench.

Run:  runtime\\python.exe tests\\test_fg_lite_match.py
"""
import contextlib
import io
import json
import os
import struct
import sys
import types

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))

import numpy as np  # noqa: E402


class _Pipe:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    def flush(self):
        pass


def _flags(**kw) -> int:
    import protocol
    worker = types.SimpleNamespace(stdin=_Pipe())
    protocol.send_frame(worker, 1, None, np.zeros((2, 2, 2), np.float16), False, 0,
                        no_color=True, **kw)
    return struct.unpack(protocol.FRAME_FMT, worker.stdin.data[:struct.calcsize(protocol.FRAME_FMT)])[3]


def main() -> int:
    failures = []
    try:
        import fg_auto
    except ImportError:
        fg_auto = None
        failures.append("there is no step chooser")

    # 1. The header bit.
    try:
        on = _flags(frame_generation=True, frame_multiplier=4, fg_lite=True)
        off = _flags(frame_generation=True, frame_multiplier=4, fg_lite=False)
        none = _flags(frame_generation=None, fg_lite=True)
        if not on & 0x40 or off & 0x40 or none & 0x40 or (on & ~0x40) != off:
            failures.append(f"the Lite bit: on {on:#x}, off {off:#x}, no FG {none:#x}")
    except TypeError as exc:
        failures.append(f"send_frame takes no Lite flag: {exc}")

    if fg_auto is not None:
        # 2. The choice.
        for source, hz, ceiling, want in ((30, 144, 6, 4), (60, 144, 6, 2), (24, 144, 6, 6),
                                          (24, 144, 4, 4), (59.94, 119.88, 6, 2),
                                          (30, 240, 6, 6), (40, 240, 6, 6), (50, 240, 6, 4),
                                          (144, 144, 6, 2), (0, 144, 3, 3)):
            got = fg_auto.best_multiplier(source, hz, ceiling)
            if got != want:
                failures.append(f"{source} fps on {hz} Hz, up to x{ceiling}: x{got}, not x{want}")
        # 3. The hold.
        c = fg_auto.Chooser(hold_s=2.0)
        steps = [c.update(0.0, 30, 144, 6)]
        steps.append(c.update(0.5, 60, 144, 6))      # a dip starts
        steps.append(c.update(1.5, 30, 144, 6))      # and ends: no change
        steps.append(c.update(2.0, 60, 144, 6))      # a lasting change starts
        steps.append(c.update(3.0, 60, 144, 6))
        steps.append(c.update(4.1, 60, 144, 6))      # held 2.1 s: taken
        steps.append(c.update(4.2, 60, 144, 3))      # ceiling lowered below: at once
        steps.append(c.update(4.3, 2, 144, 6))       # near-still source: kept
        if steps != [4, 4, 4, 4, 4, 2, 2, 2]:
            failures.append(f"the hold gave {steps}")

        # 4. main's per-frame choice.
        import main as app
        real = fg_auto.refresh_hz
        fg_auto.refresh_hz = lambda hwnd=None: 144.0
        try:
            st = types.SimpleNamespace(cfg={"frame_generation": True, "frame_multiplier": 6,
                                            "fg_match_display": False},
                                       display=types.SimpleNamespace(get_hwnd=lambda: 0),
                                       fg_chooser=None, fg_refresh=0.0, fg_refresh_at=0.0)
            with contextlib.redirect_stdout(io.StringIO()):
                plain = app.fg_multiplier(st, 30.0, 10.0)
                st.cfg["fg_match_display"] = True
                matched = app.fg_multiplier(st, 30.0, 10.0)
                st.cfg["frame_generation"] = False
                fg_off = app.fg_multiplier(st, 30.0, 10.5)
            if (plain, matched, fg_off) != (6, 4, 6):
                failures.append(f"main's choice: off {plain}, matched {matched}, FG off {fg_off}")
        finally:
            fg_auto.refresh_hz = real

    # 5. The switches.
    defaults = json.load(open(os.path.join(BASE, "config.default.json"), encoding="utf-8"))
    if defaults.get("fg_lite") is not False or defaults.get("fg_match_display") is not False:
        failures.append("the switches are not off by default")
    import pygame
    pygame.init()
    pygame.display.set_mode((64, 64))
    import fonts
    import overlay_ui as ui
    from i18n import STRINGS
    menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
    menu.visible = True
    menu.page = "main"
    menu.set_state({"nr": True, "gpu_ok": True, "frame_generation": True, "frame_multiplier": 4,
                    "frame_multiplier_active": 2, "fg_match_display": True})
    menu.folds = {"net"}
    menu.layout(1920, 1080)
    for key in ("fg_lite", "fg_match_display"):
        item = next((i for i in menu.items if i.kind == "toggle" and i.key == key), None)
        if item is None:
            failures.append(f"the panel has no {key} switch")
        elif ("toggle", key) not in (menu._activate_item(item) or []):
            failures.append(f"pressing {key} does not send it")
    row = next((i for i in menu.items if i.key == "frame_multiplier"), None)
    if row is None or "x2" not in str(row.extra.get("hint", "")) or "x4" not in str(row.extra.get("hint", "")):
        failures.append(f"the FG row does not say it runs x2 matched, up to x4: "
                        f"{row and row.extra.get('hint')!r}")
    pygame.quit()

    import commands
    import settings_io
    saved = []
    real_save = settings_io.save_menu_layout
    settings_io.save_menu_layout = lambda st: saved.append(1)
    try:
        st = types.SimpleNamespace(cfg={}, display=types.SimpleNamespace(
            menu=types.SimpleNamespace(set_state=lambda d: None)))
        with contextlib.redirect_stdout(io.StringIO()):
            for key in ("fg_lite", "fg_match_display"):
                commands.apply_menu_action(st, ("toggle", key))
        if st.cfg != {"fg_lite": True, "fg_match_display": True} or len(saved) != 2:
            failures.append(f"pressing the switches gave {st.cfg}, saved {len(saved)}x")
    except Exception as exc:
        failures.append(f"the command raised {type(exc).__name__}: {exc}")
    finally:
        settings_io.save_menu_layout = real_save
    menu_stub = types.SimpleNamespace(user_scale=1.0, user_height=None, state={}, mini=False,
                                      offset=[0, 0])
    layout = settings_io._menu_layout_payload({"profile": "Natural", "gpu": 0, "presets": {},
                                               "fg_lite": True, "fg_match_display": True},
                                              {"intensity": 1.0, "local_tone": 1.0, "local_structure": 1.0,
                                               "skin_structure": -1.0},
                                              0, "en", 0.65, 0.0, False, False, menu_stub)
    if layout.get("fg_lite") is not True or layout.get("fg_match_display") is not True:
        failures.append("the saved layout drops the switches")
    for lang, s in sorted(STRINGS.items()):
        missing = [k for k in ("fg_lite", "fg_lite_hint", "fg_match_display",
                               "fg_match_display_hint", "fg_matched") if not s.get(k)]
        if missing or "{live}" not in s.get("fg_matched", "") or "{want}" not in s.get("fg_matched", ""):
            failures.append(f"{lang}: missing or broken {missing or 'fg_matched'}")

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
