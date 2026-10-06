"""A helper for tests/test_panel_raise_async.py, run as its own process.

A topmost, nearly transparent (1/255) window over the rectangle given on the
command line, pumping its messages for at most a minute. It has to be another
process: the worker leaves windows of the client's own process alone.

Not a test itself (the name does not start with test_).
"""
import ctypes
import sys
import time
from ctypes import wintypes

u = ctypes.WinDLL("user32")
PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                          wintypes.WPARAM, wintypes.LPARAM)
u.DefWindowProcW.restype = ctypes.c_ssize_t
u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                             wintypes.LPARAM]
u.CreateWindowExW.restype = wintypes.HWND
u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                              wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.HWND,
                              wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
u.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD,
                                         wintypes.BYTE, wintypes.DWORD]
u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                           wintypes.UINT, wintypes.UINT, wintypes.UINT]
u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]


class _WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", PROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


def main() -> None:
    proc = PROC(lambda h, m, w, l: u.DefWindowProcW(h, m, w, l))
    wc = _WNDCLASS()
    wc.lpfnWndProc = proc
    wc.lpszClassName = "NsCoverProbe"
    u.RegisterClassW(ctypes.byref(wc))
    x, y, w, h = map(int, sys.argv[1:5])
    # TOPMOST | LAYERED | TOOLWINDOW | TRANSPARENT; POPUP | VISIBLE
    hwnd = u.CreateWindowExW(0x08 | 0x80000 | 0x80 | 0x20, "NsCoverProbe", "cover",
                             0x80000000 | 0x10000000, x, y, w, h,
                             None, None, None, None)
    u.SetLayeredWindowAttributes(hwnd, 0, 1, 2)
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    msg = wintypes.MSG()
    end = time.time() + 60
    while time.time() < end:
        while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


if __name__ == "__main__":
    main()
