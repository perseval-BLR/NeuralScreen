"""Backend selection, restart, fallback and scene tracking without NVIDIA hardware."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import numpy as np
import pygame
import commands
import pipeline
import settings_io
from guides import TemporalGuideGenerator
from motion_backend import MotionBackendStatus, normalize_backend
from test_ui_buttons import build, paint, find
from test_config_atomic import _payload, GOOD
import json
import re
import tempfile


def main():
    native = (ROOT / "native" / "nvofa.inl").read_text(encoding="utf-8-sig")
    dump = native.split("static void DumpNvofa(VideoState &v)", 2)[-1]
    assert "if (!pair.first) continue;" in dump, \
        "an ordinary NVOFA dump must skip the opt-in cost texture when absent"
    confidence = (ROOT / "tests" / "experiment_nvofa_confidence.py").read_text(
        encoding="utf-8")
    assert "NS_NVOFA_COST='1'" in confidence, \
        "the confidence experiment reads cost files and must request the channel"

    # NS_NVOFA_GRID: the driver's output grid, and the resolution of the
    # motion field. 4 is what shipped (NVOFA then hands back one vector per
    # 4x4 block and the expand shader stretches it bilinearly - the reason a
    # moving edge deforms where CPU DIS keeps it sharp, ROADMAP "NVOFA:
    # разбор пары логов"). 1 asks the driver for the pixel-level field DIS
    # gives, for more GPU. The switch must default to 4, accept only the three
    # grids the driver documents, and never force one the driver did not
    # offer - a silently different grid would make the A/B measurement a lie.
    body = re.search(r"static unsigned NvofaGridRequested\(\)\s*\{(.*?)\n\}",
                     native, re.S)
    assert body, "NvofaGridRequested is gone - re-check the grid switch"
    body = body.group(1)
    assert "NS_NVOFA_GRID" in body, "the grid switch reads the wrong variable"
    assert "return 4;" in body, "the grid switch no longer defaults to 4"
    for value in ("1", "2", "4"):
        assert f"value == {value}" in body, f"grid={value} is not accepted"
    assert "f.grid = grids.front()" in native, \
        "a grid the driver does not offer must fall back, not be forced"

    assert all(normalize_backend(v) == "nvofa" for v in [None, {}, [], 1, "gpu", "NVofa", "NVOFA", "CPU"])
    assert normalize_backend("cpu") == "cpu"
    assert _payload()["motion_backend"] == "nvofa"
    assert _payload(dict(GOOD, motion_backend="cpu"))["motion_backend"] == "cpu"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        for value in (None, "cpu", "obsolete"):
            path.write_text(json.dumps(dict(GOOD, motion_backend=value)), encoding="utf-8")
            assert settings_io.load_config(path)["motion_backend"] == normalize_backend(value)
    # A fresh install (no config at all) must come up on NVOFA: it is the
    # shipped default now, CPU stays the automatic fallback (user rule 16.09).
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "fresh" / "config.json"
        fresh.parent.mkdir()
        assert settings_io.load_config(fresh)["motion_backend"] == "nvofa"
    state = MotionBackendStatus()
    worker, replacement = object(), object()
    assert not state.update(worker, [])
    assert state.update(worker, ["[nvofa] active: driver"])
    assert state.update(worker, ["old logs evicted"])
    assert not state.update(worker, ["[nvofa] active: driver", "[nvofa] unavailable: execute"])
    assert state.failed
    assert not state.update(worker, []) and state.failed
    assert not state.update(replacement, []) and not state.failed

    guide = TemporalGuideGenerator(320, 180, emit_small=True)
    rng = np.random.default_rng(17)
    gray = rng.integers(60, 170, (180, 320), dtype=np.uint8)
    guide.process(gray=gray)
    original = guide.dis
    guide.dis = Mock(wraps=original)
    shifted = np.roll(gray, 3, axis=1)
    assert not guide.process(gray=shifted, compute_motion=False).motion.any()
    guide.dis.calc.assert_not_called()
    # Fallback resumes against the last captured image, not a stale CPU pair.
    assert np.array_equal(guide.previous_gray, shifted)
    guide.process(gray=np.roll(shifted, 2, axis=1))
    guide.dis.calc.assert_called_once()
    assert guide.process(gray=np.full_like(gray, 255), compute_motion=False).reset

    st = SimpleNamespace(cfg={}, lang="en")
    with patch.dict(os.environ), patch.object(settings_io, "save_menu_layout") as save, \
         patch.object(pipeline, "teardown_pipeline") as down, \
         patch.object(pipeline, "rebuild_pipeline") as up:
        commands.apply_menu_action(st, ("motion_backend", "nvofa"))
        assert st.cfg["motion_backend"] == os.environ["NS_MOTION_BACKEND"] == "nvofa"
        save.assert_called_once_with(st); down.assert_called_once_with(st)
        assert up.call_count == 1
        pipeline.apply_motion_backend(st, "nvofa")
        assert up.call_count == 1
        pipeline.apply_motion_backend(st, "cpu")
        assert os.environ["NS_MOTION_BACKEND"] == "cpu" and up.call_count == 2

    pygame.init()
    try:
        menu = build(); menu.page = "settings"; paint(menu)
        item = find(menu, "choice", "motion_backend")
        assert item and item.payload == ["nvofa", "cpu"]
        assert menu._pick("motion_backend", "nvofa") == [("motion_backend", "nvofa")]
    finally:
        pygame.quit()
    print("PASS: NVOFA selection/restart, fallback/restart tracking, CPU resume and scene cuts")


if __name__ == "__main__":
    main()
