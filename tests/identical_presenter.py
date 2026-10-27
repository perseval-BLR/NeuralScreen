"""A window that presents the same picture on every vsync, through a D3D11 swap chain.

What Chromium, Photos or Photoshop do over a still picture: the swap chain gets
a new present every frame and the window capture hands each one over as a new
frame, although not one pixel changed (#183). A GDI window that repaints the
same pixels does not do this - WGC delivers nothing for it - so the stand-in
has to present.

Run by test_identical_capture_kept.py as its own process (one SDL window per
process): argv = title width height seconds [vary]. With `vary` the picture
steps one bar's green on every present - a playing video. Prints the window
handle once it is up.
"""
import ctypes
import os
import sys
import time

os.environ["SDL_RENDER_DRIVER"] = "direct3d11"
os.environ["SDL_HINT_RENDER_DRIVER"] = "direct3d11"
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame  # noqa: E402
from pygame._sdl2 import video  # noqa: E402


def main() -> int:
    title, w, h, seconds = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
    vary = len(sys.argv) > 5 and sys.argv[5] == "vary"
    pygame.init()
    window = video.Window(title, size=(w, h), position=(0, 0), borderless=True)
    renderer = video.Renderer(window, accelerated=1, vsync=True)
    hwnd = window.id and ctypes.windll.user32.FindWindowW(None, title)
    # Under everything else: the capture reads the window's own surface, the
    # person at the machine keeps what was on top.
    ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(1), 0, 0, 0, 0,
                                      0x0001 | 0x0002 | 0x0010)   # NOSIZE|NOMOVE|NOACTIVATE
    print(f"hwnd {hwnd}", flush=True)
    bars = [(208, 32, 32), (32, 176, 32), (32, 32, 208), (128, 128, 128)]
    end = time.perf_counter() + seconds
    step = 0
    while time.perf_counter() < end:
        for _ in pygame.event.get():
            pass
        for k, colour in enumerate(bars):
            if vary and k == 3:
                colour = (128, 128 + 2 * (step % 8), 128)
            renderer.draw_color = (*colour, 255)
            renderer.fill_rect(pygame.Rect(k * w // 4, 0, (k + 1) * w // 4 - k * w // 4, h))
        renderer.present()
        step += 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
