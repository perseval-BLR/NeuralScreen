"""The rate badge goes to the worker, not onto the HUD layer (v2.6.0, #166).

With the on-screen counter on, the HUD layer - a full-screen layered window -
was flipped at every new reading, ten times a second, and each flip costs the
compositor a frame: on the bench a quarter of a video's frames came a refresh
early or late. The client now renders the badge into a shared bitmap
(app/badge.py) that the worker blends over what it presents (badge.inl).

Checked without a window or a GPU:

1. the bitmap's layout is the worker's BadgeHeader - magic, a non-zero
   sequence once written, size, corner, margin, row - and the pixels follow
   at WIDTH x 4 per row; the same badge again is not rewritten, a hidden one
   reads w = h = 0;
2. on a real Display (SDL's dummy driver) with the channel attached, the
   counter on and changing: the badge reaches the channel at every change and
   the HUD layer is flipped once, empty, and never again;
3. the command layer names the bitmap to each new worker once (BADG, 88
   bytes) and attaches the channel to the display only while the worker
   presents.

Fails before the change: the badge was drawn on the HUD layer, which was
flipped with every reading, and there was no channel or BADG.

Run:  runtime\\python.exe tests\\test_badge_channel.py
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

import pygame  # noqa: E402


def main() -> int:
    failures = []
    pygame.init()
    import badge
    import commands
    import display as display_mod
    import protocol

    # 1. The layout.
    ch = badge.BadgeChannel()
    try:
        pix = bytes(range(256)) * (3 * 4 * 10 // 256 + 1)
        pix = pix[:30 * 4 * 2]
        if not ch.publish(pix, 30, 2, "bl", 14, 0):
            failures.append("a new badge was not written")
        head = struct.unpack(badge.HEADER_FMT, ch._mm[:badge.HEADER_SIZE])
        if head[0] != badge.MAGIC or head[1] == 0 or head[2:7] != (30, 2, 2, 14, 0):
            failures.append(f"the header reads {head}")
        row1 = ch._mm[badge.HEADER_SIZE + badge.WIDTH * 4:
                      badge.HEADER_SIZE + badge.WIDTH * 4 + 120]
        if bytes(row1) != pix[120:240]:
            failures.append("the second row is not at WIDTH x 4 bytes")
        seq = head[1]
        if ch.publish(pix, 30, 2, "bl", 14, 0):
            failures.append("the same badge was written again")
        ch.hide()
        head = struct.unpack(badge.HEADER_FMT, ch._mm[:badge.HEADER_SIZE])
        if head[2:4] != (0, 0) or head[1] == seq:
            failures.append(f"a hidden badge reads {head}")
    finally:
        ch.close()

    # 2. The Display hands it over and leaves its layer alone.
    flips = [0]
    real_flip = pygame.display.flip
    clock = [100.0]
    real_monotonic = display_mod.time.monotonic

    def counting_flip():
        flips[0] += 1
        real_flip()

    display_mod.pygame.display.flip = counting_flip
    display_mod.time.monotonic = lambda: clock[0]
    sent = []
    try:
        disp = display_mod.Display(800, 450)
        disp.menu.visible = False

        class Sink:
            def publish(self, pixels, w, h, corner, margin, row):
                sent.append((len(pixels), w, h, corner))
                return True

            def hide(self):
                sent.append("hide")
                return True

        disp.badge_out = Sink()
        for fps in (59.0, 60.0, 61.0, 62.0):
            disp.set_hud({"fps_overlay": "tr", "fps": fps, "nr": True,
                          "gpu_ok": True, "status": "NR ON"})
            for _ in range(15):
                disp.draw_overlay()
                clock[0] += 0.007
        published = [s for s in sent if s != "hide"]
        print(f"    badges handed over: {len(published)}, HUD flips: {flips[0]}")
        if len(published) < 4:
            failures.append(f"the badge reached the channel {len(published)} times "
                            f"for four readings")
        if published and (published[0][0] != published[0][1] * published[0][2] * 4
                          or published[0][3] != "tr"):
            failures.append(f"the bitmap handed over is {published[0]}")
        if flips[0] > 1:
            failures.append(f"the HUD layer was flipped {flips[0]} times while the "
                            f"worker draws the badge (expected once, empty)")
    finally:
        display_mod.pygame.display.flip = real_flip
        display_mod.time.monotonic = real_monotonic

    # 3. Named once per worker, attached only while presenting.
    class Worker:
        def __init__(self, pid):
            self.pid = pid
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    shown = types.SimpleNamespace(badge_out=None)
    st = types.SimpleNamespace(present_mode=True, worker=Worker(11), display=shown)
    commands.send_badge_state(st)
    commands.send_badge_state(st)
    data = st.worker.stdin.getvalue()
    if len(data) != struct.calcsize(protocol.BADGE_FMT):
        failures.append(f"BADG wrote {len(data)} bytes for two passes (expected one 88)")
    else:
        magic, *_r, pts, name = struct.unpack(protocol.BADGE_FMT, data)
        if magic != protocol.BADGE_MAGIC or name.rstrip(b"\0").decode() != st.badge.name:
            failures.append("BADG does not name the channel")
    if shown.badge_out is not st.badge:
        failures.append("the display did not get the channel while presenting")
    st.worker = Worker(12)
    commands.send_badge_state(st)
    if len(st.worker.stdin.getvalue()) != 88:
        failures.append("a new worker was not told about the badge")
    st.present_mode = False
    commands.send_badge_state(st)
    if shown.badge_out is not None:
        failures.append("the display kept the channel without a presenting worker")
    st.badge.close()
    pygame.quit()

    for f in failures:
        print("  FAIL:", f)
    print("PASS" if not failures else "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
