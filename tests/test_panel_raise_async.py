r"""A client that is not pumping cannot stall the worker's frame thread.

Every 300 frames the worker checks whether a foreign window covers the
picture, and if so raises the panel (the client's window) and inserts the
picture under it. SetWindowPos on another thread's window SENDS it
WM_WINDOWPOSCHANGING and waits until that thread pumps. The client pumps
between frames - but not while it waits for one of the worker's acks, and
the worker cannot read the command it waits on while its frame thread stands
in SetWindowPos: the client's 15-20 s ack timeout then declared a healthy
worker dead (pre-release audit).

Checked on the worker: a "pygame" panel in this process that stops pumping,
a nearly transparent topmost window from a child process over the picture,
and 330 frames - so the raise runs while the panel is not pumping. No frame
may wait for the panel. Needs an RTX GPU.

Run:  runtime\\python.exe tests\\test_panel_raise_async.py
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from offscreen_target import Target  # noqa: E402
from paths import WORKER_EXE  # noqa: E402
from test_fg_mode_change import _overlay_rect  # noqa: E402

W, H = 640, 360
GW, GH = 160, 90

user32 = ctypes.WinDLL("user32", use_last_error=True)
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                  wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DestroyWindow.argtypes = [wintypes.HWND]


class _WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


#: The covering window: another process, topmost, 1/255 opaque, over the rect
#: given on the command line. A window of ours would be read as the program's
#: own and left alone.
COVER = Path(__file__).with_name("_cover_probe_window.py")


class _Panel:
    """A topmost "pygame" window whose thread can stop pumping on demand."""

    def __init__(self):
        self.hwnd = None
        self.pause = threading.Event()
        self.resume = threading.Event()
        self.done = threading.Event()
        ready = threading.Event()

        def run():
            self._proc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
            wc = _WNDCLASS()
            wc.lpfnWndProc = self._proc
            wc.lpszClassName = "pygame"
            user32.RegisterClassW(ctypes.byref(wc))
            self.hwnd = user32.CreateWindowExW(
                0x08 | 0x80 | 0x08000000, "pygame", "panel probe",
                0x80000000 | 0x10000000, -32000, -32000, 64, 64,
                None, None, None, None)
            ready.set()
            msg = wintypes.MSG()
            while not self.done.is_set():
                if self.pause.is_set():
                    self.resume.wait(30)       # NOT pumping
                    self.pause.clear()
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.005)
            user32.DestroyWindow(self.hwnd)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(5)


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    os.environ["NS_WINDOW_POS"] = "-30000,-30000"
    os.environ["NS_NR_SMALL"] = "0"
    os.environ["NS_HDR"] = "0"
    panel = _Panel()
    os.environ["NS_HUD_HWND"] = str(int(panel.hwnd))
    from pipeline import shutdown_worker, start_worker
    from protocol import send_frame, send_motion_size, send_wgc, send_window
    from settings_io import PROFILES

    failures: list = []
    target = Target(W, H, name="NsPanelRaise", ghost=True)
    target.animate_interval = 1.0 / 60.0
    target.animate = True
    worker, logs, reader, stop = start_worker(dict(PROFILES["Natural"]), W, H, 2,
                                              0, 0, None)
    os.environ.pop("NS_HUD_HWND", None)
    cover = None
    motion = np.zeros((GH, GW, 2), np.float16)
    slowest = 0.0
    try:
        send_wgc(worker, target.hwnd)
        reader.wait_wgak(15)
        send_motion_size(worker, GW, GH)
        reader.wait_mack(10)
        send_window(worker, W, H)
        reader.wait_wack(10)
        for i in range(20):
            send_frame(worker, i, None, motion, i == 0, i, no_color=True, motion_small=True)
            reader.recv(i, timeout=10.0)
        rect = _overlay_rect(worker.pid)
        if rect is None:
            raise RuntimeError("no picture window")
        cover = subprocess.Popen([sys.executable, str(COVER), str(rect[0]), str(rect[1]),
                                  str(rect[2] - rect[0]), str(rect[3] - rect[1])],
                                 stdout=subprocess.PIPE, text=True)
        cover.stdout.readline()
        panel.pause.set()                       # the client stops pumping
        time.sleep(0.1)
        for i in range(20, 350):
            t = time.perf_counter()
            send_frame(worker, i, None, motion, False, i, no_color=True, motion_small=True)
            reader.recv(i, timeout=12.0)
            slowest = max(slowest, time.perf_counter() - t)
    except Exception as exc:
        failures.append(f"the run raised {type(exc).__name__}: {exc}")
    finally:
        panel.resume.set()
        panel.done.set()
        shutdown_worker(worker, stop)
        target.close()
        if cover is not None:
            cover.kill()
            cover.wait(10)
    raised = any("picture raised" in ln for ln in logs)
    print(f"    slowest frame {slowest * 1000:.0f} ms, raise logged: {raised}")
    if not raised:
        failures.append("the raise never ran - the check proves nothing")
    if slowest > 2.0:
        failures.append(f"a frame waited {slowest:.1f} s for a panel that was not "
                        f"pumping")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the panel raise does not hold the frame thread")
    return 0


if __name__ == "__main__":
    sys.exit(main())
