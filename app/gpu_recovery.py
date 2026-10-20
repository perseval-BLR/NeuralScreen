"""After the driver resets the GPU under NR: wait for it, and know when to stop (#181).

An RTX 3090 user saw the D3D12 device removed (0x887A0007, a driver reset -
no page fault in DRED) 12 s to 48 min into NR, and three times the whole PC
froze on a black screen. Each time NeuralScreen restarted the worker within
two seconds, while the driver was still recovering (D3D12CreateDevice
0x887A0004), and the auto-revive tried again 30 s later.

Two rules, kept here so the main loop only asks:

* before a restart that follows a reset, wait until the driver answers
  again (D3D12CreateDevice with no device asked for - a support check that
  creates nothing), at most RECOVERY_TIMEOUT_S;
* on a card NVIDIA's runtime does not officially support (pre-Blackwell, run
  through the architecture patch), a second reset in one session leaves NR
  off for the session - no automatic restart, no auto-revive; Num1 stays the
  user's choice.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

#: The longest wait for the driver to come back after a reset.
RECOVERY_TIMEOUT_S = 30.0
#: Resets an unofficial card may have before NR stays off for the session.
UNOFFICIAL_RESETS_ALLOWED = 1

_MARKERS = ("kind=device-removed", "device removed at", "DXGI_ERROR_DEVICE_RESET")


def device_was_reset(logs) -> bool:
    """Whether the worker's log says the D3D12 device was removed or reset."""
    return any(marker in str(line) for line in list(logs or [])[-400:]
               for marker in _MARKERS)


def stand_down_for_session(official: bool, resets: int) -> bool:
    """True when NR must stay off for the rest of the session after `resets`
    driver resets on this card."""
    return not official and resets > UNOFFICIAL_RESETS_ALLOWED


class _GUID(ctypes.Structure):
    _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                ("d4", ctypes.c_ubyte * 8)]


# IID_ID3D12Device {189819f1-1db6-4b57-be54-1821339b85f7}
_IID_ID3D12_DEVICE = _GUID(0x189819F1, 0x1DB6, 0x4B57,
                           (ctypes.c_ubyte * 8)(0xBE, 0x54, 0x18, 0x21, 0x33, 0x9B, 0x85, 0xF7))


def driver_ready() -> bool:
    """Whether D3D12 can create a device on the default adapter right now.

    ppDevice NULL: the call checks support and creates nothing - S_FALSE when
    it would succeed, DXGI_ERROR_UNSUPPORTED (0x887A0004) while the driver is
    still recovering from a reset."""
    try:
        d3d12 = ctypes.WinDLL("d3d12")
        create = d3d12.D3D12CreateDevice
        create.restype = ctypes.c_long
        create.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(_GUID), ctypes.c_void_p]
        return create(None, 0xB000, ctypes.byref(_IID_ID3D12_DEVICE), None) >= 0
    except (OSError, AttributeError):
        return False


def wait_for_driver(timeout: float = RECOVERY_TIMEOUT_S, *, probe=driver_ready,
                    pump=None, clock=time.monotonic, sleep=time.sleep,
                    step: float = 0.5) -> float | None:
    """Wait until `probe` says the driver is back. Returns the seconds it
    took, or None after `timeout`. `pump` keeps the window alive meanwhile."""
    start = clock()
    while True:
        if probe():
            return clock() - start
        if clock() - start >= timeout:
            return None
        if pump is not None:
            try:
                pump()
            except Exception:
                pass
        sleep(step)
