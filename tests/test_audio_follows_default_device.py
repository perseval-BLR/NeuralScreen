"""The loopback follows a switch of the default playback device.

Switching the output in Windows (speakers to headphones, a monitor's HDMI
sound) leaves the old endpoint working - it is no longer what is heard, but
WASAPI does not invalidate it. LoopbackCapture reopened only when GetBuffer
failed, so a recording went on capturing a device that had fallen silent.
It now compares the default endpoint with the open one about once a second
and reopens on the new one.

The device enumerator, the endpoints and the WASAPI clients are fakes; the
capture thread is the real one. No sound device is touched.

Run:  runtime\\python.exe tests\\test_audio_follows_default_device.py
"""
import ctypes
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import numpy as np  # noqa: E402

import audio  # noqa: E402

FMT = {"dtype": np.float32, "scale": 1.0, "src_channels": 2, "rate": 48_000,
       "block": 8}


def com_string(text: str) -> int:
    """A CoTaskMemAlloc'd wide string, as IMMDevice::GetId hands one out."""
    data = ctypes.create_unicode_buffer(text)
    ole32 = ctypes.windll.ole32
    ole32.CoTaskMemAlloc.restype = ctypes.c_void_p
    ptr = ole32.CoTaskMemAlloc(ctypes.sizeof(data))
    ctypes.memmove(ptr, data, ctypes.sizeof(data))
    return ptr


class FakeDevice:
    def __init__(self, name: str):
        self.name = name

    def GetId(self):
        return com_string(self.name)


class FakeEnumerator:
    def __init__(self):
        self.default = "{speakers}"

    def GetDefaultAudioEndpoint(self, flow, role):
        return FakeDevice(self.default)


class FakeClient:
    def Start(self):
        pass

    def Stop(self):
        pass


class IdleCapture:
    """An endpoint with nothing playing: no packets, and no error either."""

    def GetNextPacketSize(self):
        return 0


class Capture(audio.LoopbackCapture):
    REOPEN_WAIT_S = 0.05

    def __init__(self):
        super().__init__()
        self.opened: list = []

    def _activate(self, device):
        self.opened.append(device.name)
        return FakeClient(), IdleCapture(), dict(FMT)


def main() -> int:
    failures = []
    enumerator = FakeEnumerator()
    real = audio.CoCreateInstance
    audio.CoCreateInstance = lambda *a, **k: enumerator
    cap = Capture()
    try:
        if not cap.start(timeout=2.0):
            print(f"FAIL: the fake loopback did not start ({cap.error})")
            return 1
        time.sleep(1.5)
        steady = list(cap.opened)
        enumerator.default = "{headphones}"
        switched_at = time.monotonic()
        while time.monotonic() - switched_at < 3.0:
            if cap.opened[-1:] == ["{headphones}"]:
                break
            time.sleep(0.02)
        took = time.monotonic() - switched_at
        print(f"opened before the switch: {steady}; after: {cap.opened} "
              f"({took:.2f} s after the switch)")
        if steady != ["{speakers}"]:
            failures.append(f"the capture reopened while the default stayed: {steady}")
        if cap.opened[-1:] != ["{headphones}"]:
            failures.append("the capture stayed on the old device after the "
                            "default playback device changed")
        elif took > 2.0:
            failures.append(f"the switch was followed only after {took:.2f} s")
        if cap.error:
            failures.append(f"the capture reported an error: {cap.error}")
    finally:
        cap.close()
        audio.CoCreateInstance = real
    alive = [t.name for t in threading.enumerate() if t.name == "ns-audio"]
    if alive:
        failures.append("the capture thread did not stop")
    for failure in failures:
        print("FAIL:", failure)
    if failures:
        return 1
    print("OK: the loopback follows the default playback device")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
