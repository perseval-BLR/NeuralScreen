"""FG toggle, discrete multiplier, persisted config and frame-header wiring."""
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
import pygame
import commands
import protocol
import settings_io
from test_ui_buttons import build, find, paint, click


def main():
    pygame.init()
    menu = build()
    paint(menu)
    assert find(menu, "slider", "frame_multiplier") is None
    # v2.6.0: FG is a tile (on/off) and the multiplier its own row of steps.
    tile = find(menu, "tile", "frame_generation")
    assert tile is not None
    assert click(menu, tile) == [("toggle", "frame_generation")]
    # The menu reports a CLICK; what that click MEANS is decided in main.
    # Picking a step with FG off therefore posts a tray command, so the stub
    # needs the queue main would have.
    import queue as _queue
    st = SimpleNamespace(cfg={}, tray_commands=_queue.Queue())
    with patch.object(settings_io, "save_menu_layout") as save:
        # FG off, nothing chosen yet: the x2/x3/x4 group must already be
        # usable. Locking it behind the switch deadlocked a 40-series card -
        # it caps at 2x, the first switch-on refused x3 and flipped itself
        # back off before the user could ever pick 2 (user, 15.09).
        menu.set_state(st.cfg)
        paint(menu)
        btns = [i for i in menu.items
                if i.kind == "button" and i.key.startswith("frame_multiplier:")]
        assert len(btns) == protocol.FG_MULTIPLIER_MAX - 1, [i.extra["label"] for i in btns]
        assert not any(i.extra.get("disabled") for i in btns), \
            "the multiplier must be selectable while FG is off"
        # With FG off no step is lit and the tile says Off: the row never
        # claims a multiplier is running while none is.
        assert find(menu, "button", "frame_generation:off") is None
        assert find(menu, "tile", "frame_generation").value == 0.0
        active = [i for i in btns if i.extra["filled"]]
        assert not active, [i.key for i in active]
        # Picking a step with FG off asks for FG on as well - one click instead
        # of two, and the group cannot show a preference nobody can see.
        actions = click(menu, next(i for i in btns if i.key == "frame_multiplier:2"))
        assert actions == [("button", "frame_multiplier:2")], actions
        commands.apply_menu_action(st, actions[0])
        assert st.cfg["frame_multiplier"] == 2

        # Picking a step with FG off turns it on - one click, as before.
        actions = click(menu, next(i for i in btns if i.key == "frame_multiplier:2"))
        assert actions == [("button", "frame_multiplier:2")], actions
        commands.apply_menu_action(st, actions[0])
        assert st.cfg["frame_generation"], \
            "picking a step with FG off turns it on"
        menu.set_state(st.cfg)
        paint(menu)
        # The steps, the selected one filled, the tile showing it.
        btns = [i for i in menu.items
                if i.kind == "button" and i.key.startswith("frame_multiplier:")]
        assert len(btns) == protocol.FG_MULTIPLIER_MAX - 1, [i.extra["label"] for i in btns]
        active = [i for i in btns if i.extra["filled"]]
        assert len(active) == 1 and active[0].key == "frame_multiplier:2"
        assert find(menu, "tile", "frame_generation").extra["value_text"] == "×2"
        # Up to 6x (#163): DLSS 4.5's steps, the two above 4x carried by the
        # header's third multiplier bit.
        for value in range(2, protocol.FG_MULTIPLIER_MAX + 1):
            key = f"frame_multiplier:{value}"
            btn = next(i for i in btns if i.key == key)
            actions = click(menu, btn)
            assert actions == [("button", key)], actions
            commands.apply_menu_action(st, actions[0])
            assert st.cfg["frame_multiplier"] == value
            menu.set_state(st.cfg)
            paint(menu)
            worker = SimpleNamespace(stdin=io.BytesIO())
            protocol.send_frame(worker, 0, None, np.zeros((2, 2, 2), np.float16), False, 0,
                                no_color=True, split=0.5, frame_generation=True, frame_multiplier=value)
            header = struct.unpack(protocol.FRAME_FMT, worker.stdin.getvalue()[:struct.calcsize(protocol.FRAME_FMT)])
            flags = header[3]
            assert flags & 0x800 and flags & 0x100
            step = ((flags >> 9) & 3) | (4 if flags & protocol.FRAME_FLAG_FG_MULT_HIGH else 0)
            assert step + 2 == value, (value, hex(flags))
            assert flags >> 16 == 32768, "FG flags changed the comparison wipe"
        commands.apply_menu_action(st, ("toggle", "frame_generation"))
        menu.set_state(st.cfg)
        paint(menu)
        assert not st.cfg["frame_generation"]
        # With FG off the selected step STAYS lit - a preference for the
        # next attempt - and nothing is disabled.
        btns = [i for i in menu.items
                if i.kind == "button" and i.key.startswith("frame_multiplier:")]
        filled = [i for i in btns if i.extra["filled"]]
        assert not filled, [i.key for i in filled]
        assert find(menu, "tile", "frame_generation").value == 0.0
        assert not any(i.extra.get("disabled") for i in btns)
        assert save.call_count == 3 + protocol.FG_MULTIPLIER_MAX - 1
    # The prepared-capture flag rides the same header, independent of FG.
    worker = SimpleNamespace(stdin=io.BytesIO())
    protocol.send_frame(worker, 9, None, np.zeros((2, 2, 2), np.float16), False, 0,
                        no_color=True, split=.5, prepared=True,
                        frame_generation=True, frame_multiplier=4)
    flags = struct.unpack(protocol.FRAME_FMT, worker.stdin.getvalue()[:24])[3]
    assert flags & 0x1000 == 0x1000, "prepared rides the header"
    assert flags & 0x4000 == 0 and flags & 0x2000 == 0, "the removed SR flags stay unset"
    assert flags >> 16 == 32768
    assert ((flags >> 9) & 3) == 2
    # The resolution slider stays Boost-only: no SR to keep it company.
    menu.set_state({"nr_small": False, "work_scale": .8})
    paint(menu)
    assert find(menu, "slider", "nr_res") is None
    menu.set_state({"nr_small": True, "work_scale": .8})
    paint(menu)
    assert find(menu, "slider", "nr_res")
    # The real serializer must retain the FG settings.
    # This is a serializer contract, not a test of the user's live settings.
    # Loading ROOT/config.json would migrate and rewrite that personal file as
    # soon as a new product-default field is added.
    cfg = settings_io.load_config(ROOT / "config.default.json")
    cfg.update(frame_generation=True, frame_multiplier=4, ui_detection=True)
    params = settings_io.resolve_params(cfg)
    data = dict(cfg)
    data.update(settings_io._menu_layout_payload(cfg, params,
        cfg["monitor"] if isinstance(cfg["monitor"], int) else 0,
        cfg["lang"], cfg["work_scale"], 0.0, True, bool(cfg.get("nr_small")), menu))
    assert data["frame_generation"] and data["frame_multiplier"] == 4
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        restored = settings_io.load_config(path)
        assert restored["ui_detection"]
        assert restored["frame_generation"] and restored["frame_multiplier"] == 4
    pygame.quit()
    print("OK: FG controls, multiplier steps, persistence and wire flags")


def _fg_verdict_checks() -> None:
    """The switch follows reality (issue #76), pure unit level."""
    from types import SimpleNamespace

    # A refusal AFTER the enable marker -> off with the alert armed.
    alerts = []
    logs = ["[fg] UI: on, 2x", "[fg] Init_Ext -> 0xBAD00002",
            "[fg] CreateFeature failed 0xBAD00002"]
    st = SimpleNamespace(cfg={"frame_generation": True}, worker_logs=logs,
                         fg_alerted=False,
                         lang="en", display=SimpleNamespace(
                             alert=lambda *a, **k: alerts.append(a)))
    with patch.object(settings_io, "save_menu_layout"):
        settings_io.refresh_fg_ok(st)
    assert st.cfg["frame_generation"] is False, "the switch must flip back off"
    assert st.fg_alerted is True, "the alert must be armed once"
    assert len(alerts) == 1, f"exactly one alert, got {len(alerts)}"
    # The switch is off now: refresh is a no-op, nothing re-fires.
    settings_io.refresh_fg_ok(st)
    assert len(alerts) == 1, "the switch already off must not alert again"
    # Flipping it back on re-arms the alert (the user retries).
    st.cfg["frame_generation"] = True
    st.fg_alerted = False
    with patch.object(settings_io, "save_menu_layout"):
        settings_io.refresh_fg_ok(st)
    assert len(alerts) == 2, "a fresh attempt must be told again"

    # Success ("[fg] 2x enabled at ...") leaves the switch alone.
    logs_ok = ["[fg] UI: on, 2x", "[fg] 2x enabled at 3840x2160, format=28"]
    st2 = SimpleNamespace(cfg={"frame_generation": True}, worker_logs=logs_ok,
                          fg_alerted=False, lang="en",
                          display=SimpleNamespace(alert=lambda *a, **k: print("ALERT?")))
    settings_io.refresh_fg_ok(st2)
    assert st2.cfg["frame_generation"] is True, "a working FG must stay on"
    assert st2.fg_alerted is False

    # A stale refusal from BEFORE the last "UI: on" must not flip the switch.
    logs_stale = ["[fg] CreateFeature failed 0xBAD00002", "[fg] UI: off, 2x",
                  "[fg] UI: on, 2x"]
    st3 = SimpleNamespace(cfg={"frame_generation": True}, worker_logs=logs_stale,
                          fg_alerted=False, lang="en",
                          display=SimpleNamespace(alert=lambda *a, **k: None))
    settings_io.refresh_fg_ok(st3)
    assert st3.cfg["frame_generation"] is True, "a stale refusal must not flip it"
    # A refused multiplier that STEPPED DOWN and then built (issue #100) must
    # not flip the switch: the card runs Frame Generation, just at 2x.
    logs_step = ["[fg] UI: on, 4x",
                 "[fg] CreateFeature failed 0xBAD00005",
                 "[fg] 4x refused 0xBAD00005; stepping down to 2x",
                 "[fg] UI: on, 2x (capped by the runtime ceiling)",
                 "[fg] 2x enabled at 3840x2160, format=28"]
    st4 = SimpleNamespace(cfg={"frame_generation": True}, worker_logs=logs_step,
                          fg_alerted=False, lang="en",
                          display=SimpleNamespace(alert=lambda *a, **k: alerts.append(a)))
    settings_io.refresh_fg_ok(st4)
    assert st4.cfg["frame_generation"] is True, \
        "a step-down that landed on a working 2x must not flip the switch"
    assert st4.fg_alerted is False, "no alert: Frame Generation is running"
    # A step-down still in flight (no verdict yet) must not flip it either.
    st5 = SimpleNamespace(cfg={"frame_generation": True},
                          worker_logs=["[fg] UI: on, 4x",
                                       "[fg] CreateFeature failed 0xBAD00005",
                                       "[fg] 4x refused 0xBAD00005; stepping down to 2x"],
                          fg_alerted=False, lang="en",
                          display=SimpleNamespace(alert=lambda *a, **k: alerts.append(a)))
    settings_io.refresh_fg_ok(st5)
    assert st5.cfg["frame_generation"] is True, \
        "a retry in flight is not a verdict"
    print("    fg_verdict: refusal flips + alerts once; success, stale and "
          "a step-down do not")


def _fg_active_multiplier_checks() -> None:
    """Issue #100: the panel must be able to say which step really runs.

    A card whose runtime stops at 2x answers 3x/4x with a refusal; the worker
    steps down instead of failing, so the pick and the live step differ. The
    presenter names the step it ran in the FPS line, and this parser is what
    the panel reads - it must report what HAPPENED, never the request.
    """
    from types import SimpleNamespace

    def mult(lines):
        return settings_io._fg_active_multiplier(
            SimpleNamespace(worker_logs=list(lines)))

    cases = [
        (["[fg] displayed 149.7 FPS (real + generated, 4x); experimental"], 4),
        (["[fg] displayed 118.5 FPS (real + generated, 2x); experimental"], 2),
        (["[fg] displayed 90.0 FPS (real + generated, 3x); experimental"], 3),
        # FG off: no live step to report.
        (["[fg] UI: off, 2x"], None),
        # The newest line wins: the step-down story, as the worker logs it.
        (["[fg] UI: on, 4x",
          "[fg] 4x refused 0xBAD00005; stepping down to 2x",
          "[fg] UI: on, 2x (capped by the runtime ceiling)",
          "[fg] displayed 118.5 FPS (real + generated, 2x); experimental"], 2),
        # An older format (before the step was in the line) reads as unknown,
        # not as a wrong number.
        (["[fg] displayed 87.1 FPS (real + generated); experimental"], None),
    ]
    for lines, want in cases:
        got = mult(lines)
        assert got == want, (lines[-1], got, want)

    # The FPS parser must keep working beside it (same line, same contract).
    st = SimpleNamespace(worker_logs=[
        "[fg] displayed 149.7 FPS (real + generated, 4x); experimental"])
    assert settings_io._fg_displayed_fps(st) == 149.7, "the rate parser broke"
    print("    fg_multiplier: the live step is read from the presenter's own "
          "line, never from the request")


if __name__ == "__main__":
    main()
    _fg_verdict_checks()
    _fg_active_multiplier_checks()
