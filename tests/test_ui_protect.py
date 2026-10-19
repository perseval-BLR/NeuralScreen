"""Frame Generation's interface protection: finding a still HUD (v2.6.0).

DLSS-G on the desktop has flat depth and estimated motion, so a label over a
moving scene is warped with the scene around it. The worker can show the real
frame's pixels where it is told a still interface sits (UIRC); the client
finds those places on the gray luminance (app/ui_protect.py).

Checked without a GPU:

1. a textured box that stays still over a moving scene is found after the
   evidence frames, as one rectangle covering it, and nothing else is;
2. a still scene, a flat still area and a slow pan protect nothing;
   a repeated gray keeps the rectangles and adds no stillness;
3. the limits hold: at most 32 rectangles, at most 20% of the picture;
4. the command layer sends the rectangles only when they change, an empty
   list once when FG or the setting goes off, and nothing to a fresh worker
   that has nothing to protect; the menu has the switch in the Network block.

Fails before the change: there was no detector, no UIRC and no switch.
#177: the real gray section is flat - read as it was, the detector raised as
FG came on and the program ended; and a failure inside now stays inside.

Run:  runtime\\python.exe tests\\test_ui_protect.py
"""
import io
import os
import struct
import sys
import types

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "app"))

import numpy as np  # noqa: E402

H, W = 180, 320
rng = np.random.default_rng(7)
TEXTURE = rng.integers(0, 255, size=(H * 2, W * 2)).astype(np.uint8)
HUD = rng.integers(0, 255, size=(30, 60)).astype(np.uint8)


def moving_scene(t: int) -> np.ndarray:
    """A textured scene panning 4 px a frame - fast enough to read as motion."""
    return TEXTURE[: H, 4 * t % W: 4 * t % W + W].copy()


def with_hud(frame: np.ndarray) -> np.ndarray:
    frame[20:50, 240:300] = HUD
    return frame


def main() -> int:
    failures = []
    import ui_protect as up

    # 1. A still HUD over a moving scene.
    det = up.UiDetector()
    rects = []
    for t in range(12):
        rects = det.update(with_hud(moving_scene(t)))
        if t < up.EVIDENCE - 1 and rects:
            failures.append(f"frame {t}: protected before the evidence ({rects})")
    print(f"    still HUD over a pan: {rects}")
    if len(rects) != 1:
        failures.append(f"expected one rectangle over the HUD, got {rects}")
    else:
        x, y, w, h = (v / 65535 for v in rects[0])
        if not (abs(x * W - 240) <= 10 and abs(y * H - 20) <= 10
                and abs(w * W - 60) <= 10 and abs(h * H - 30) <= 10):
            failures.append(f"the rectangle {rects[0]} is not the HUD at 240,20 60x30")

    # A repeated gray (the game drew slower than the loop) changes nothing:
    # the rectangles stay, and the scene's tiles earn no stillness from it.
    frame = with_hud(moving_scene(12))
    again = [det.update(frame), det.update(frame.copy()), det.update(frame.copy())]
    if any(r != rects for r in again[1:]) or not again[1]:
        failures.append(f"a repeated gray turned the protection {again} (was {rects})")
    det2 = up.UiDetector()
    early = []
    for t in range(up.EVIDENCE - 2):              # too few new frames for evidence
        f = with_hud(moving_scene(t))
        early = det2.update(f)
        early = early or det2.update(f.copy())    # every frame three times
        early = early or det2.update(f.copy())
    if early:
        failures.append(f"repeated frames counted as evidence: protected {early} "
                        f"after {up.EVIDENCE - 2} new frames")

    # 2. Nothing to protect.
    det = up.UiDetector()
    still = moving_scene(0)
    for _ in range(12):
        rects = det.update(with_hud(still.copy()))
    if rects:
        failures.append(f"a still scene was protected: {rects}")
    det = up.UiDetector()
    for t in range(12):
        f = moving_scene(t)
        f[20:50, 240:300] = 128      # flat and still: needs nothing
        rects = det.update(f)
    if rects:
        failures.append(f"a flat still area was protected: {rects}")
    det = up.UiDetector()
    for t in range(12):
        f = TEXTURE[:H, t % W: t % W + W].copy()   # 1 px a frame: slow, all of it
        f[:, :160] = TEXTURE[:H, 3 * t: 3 * t + 160]
        rects = det.update(f)
    if rects:
        failures.append(f"a slow pan was protected: {rects}")

    # 3. Limits.
    keep = np.zeros((18, 32), bool)
    keep[::2, ::2] = True                       # 144 single tiles
    boxes = up._rects(keep, w=W, h=H)
    area = sum(b[2] * b[3] for b in boxes) / 65535 / 65535
    if len(boxes) > up.MAX_RECTS or area > up.MAX_AREA + 1e-3:
        failures.append(f"{len(boxes)} rectangles over {area:.0%} - past the limits")

    # 4. The command layer and the menu.
    import commands
    import protocol

    class Worker:
        def __init__(self, pid):
            self.pid = pid
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    frames = [with_hud(moving_scene(t)) for t in range(14)]
    shm = types.SimpleNamespace(read_gray=lambda: frames.pop(0) if frames else None)
    st = types.SimpleNamespace(cfg={"fg_ui_protect": True, "frame_generation": True},
                               worker=Worker(5), gray_active=True, shm=shm)
    for _ in range(12):
        commands.update_ui_protection(st)
    size = struct.calcsize(protocol.UI_RECTS_FMT)
    data = st.worker.stdin.getvalue()
    sent = [struct.unpack(protocol.UI_RECTS_FMT, data[i:i + size])
            for i in range(0, len(data), size)]
    print(f"    UIRC sent: {[m[1] for m in sent]}")
    if len(data) % size or len(sent) != 1 or sent[0][0] != protocol.UI_RECTS_MAGIC \
            or sent[0][1] != 1:
        failures.append(f"expected one UIRC with one rectangle, got {[m[:2] for m in sent]}")
    st.cfg["frame_generation"] = False
    commands.update_ui_protection(st)
    commands.update_ui_protection(st)
    data = st.worker.stdin.getvalue()
    if len(data) != 2 * size or struct.unpack(protocol.UI_RECTS_FMT, data[size:])[1] != 0:
        failures.append("turning FG off did not send one empty list")
    fresh = types.SimpleNamespace(cfg={}, worker=Worker(6), gray_active=True, shm=shm,
                                  ui_rects_sent=st.ui_rects_sent)
    commands.update_ui_protection(fresh)
    if fresh.worker.stdin.getvalue():
        failures.append("a fresh worker with nothing to protect was sent a UIRC")

    # #177: the real shared gray section, not a hand-made 2-D array. Its
    # buffer is flat; read as it was, the detector raised the moment FG came
    # on and the program ended. And a failure inside does not reach the loop.
    shm2 = protocol.SharedFrameBuffer(64, 64)
    try:
        shm2.open_gray(W, H)
        shm2._gray_buf[:] = with_hud(moving_scene(0)).reshape(-1)
        if shm2.read_gray().shape != (H, W):
            failures.append(f"read_gray gave {shm2.read_gray().shape}, not {(H, W)}")
        real = types.SimpleNamespace(cfg={"fg_ui_protect": True, "frame_generation": True},
                                     worker=Worker(8), gray_active=True, shm=shm2, lang="en",
                                     display=types.SimpleNamespace(alert=lambda *a, **k: None))
        for t in range(3):
            shm2._gray_buf[:] = with_hud(moving_scene(t)).reshape(-1)
            commands.update_ui_protection(real)
        if getattr(real, "ui_protect_broken", False):
            failures.append("the detector failed on the real gray section")
    finally:
        shm2.close_gray()
    alerts = []
    broken = types.SimpleNamespace(
        cfg={"fg_ui_protect": True, "frame_generation": True}, worker=Worker(9),
        gray_active=True, lang="en", display=types.SimpleNamespace(alert=alerts.append),
        shm=types.SimpleNamespace(read_gray=lambda: np.zeros(7, np.uint8)),
        ui_detector=types.SimpleNamespace(update=lambda g: 1 / 0, reset=lambda: None))
    import contextlib
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            commands.update_ui_protection(broken)
            commands.update_ui_protection(broken)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"a failing detector reached the loop: {exc!r}")
    if len(alerts) != 1 or not getattr(broken, "ui_protect_broken", False):
        failures.append(f"a failing detector alerted {len(alerts)} times and was not "
                        f"turned off")

    import pygame
    pygame.init()
    import fonts
    import overlay_ui as ui
    menu = ui.OverlayMenu(1.0, lambda size, mono=False, bold=False: fonts.load(size, mono=mono))
    menu.visible = True
    menu.set_state({"nr": True, "gpu_ok": True, "profile": "Natural", "profiles": ["Natural"]})
    menu.folds = {"net"}
    menu.layout(1920, 1080)
    sw = next((i for i in menu.items if i.kind == "toggle" and i.key == "fg_ui_protect"), None)
    if sw is None or menu._activate_item(sw) != [("toggle", "fg_ui_protect")]:
        failures.append("the Network block has no interface-protection switch")
    pygame.quit()

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
