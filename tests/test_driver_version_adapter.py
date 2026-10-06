"""The driver version is the one of the adapter the worker runs on.

startup._log_environment read the NVIDIA driver version as the first NVIDIA
DriverDesc among the display-class subkeys 0000-0009. Windows keeps a subkey
for every card a machine ever had: a GTX 1060 swapped out years ago sits at
0000 with its old driver, while the card in use can be at 0012 - past the
tenth subkey, which was never read. That version is part of the
compatibility key and is what the "driver out of date" dialog (#145) tells
the user to update.

Checked with a fake registry (sys.modules["winreg"] replaced; the app reads
the registry only, and so does the fake):

1. a ghost GTX 1060 at 0000, a ghost of the same RTX 5070 Ti model at 0003
   with an older driver, an Intel iGPU at 0001 and the running RTX 5070 Ti
   at 0012: the header says 0012's driver;
2. two running NVIDIA cards (#81's multi-GPU case): the driver of the card
   the worker runs on is named, whichever subkey comes first;
3. no DEVICEMAP (presence unknown): the working card's name still decides.

Run:  runtime\\python.exe tests\\test_driver_version_adapter.py
"""
import io
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/

import startup  # noqa: E402

CLASS = (r"SYSTEM\CurrentControlSet\Control\Class"
         r"\{4d36e968-e325-11ce-bfc1-08002be10318}")
GUID_NV, GUID_NV2, GUID_INTEL = "{AAAA-1}", "{AAAA-2}", "{BBBB-1}"


class FakeWinreg:
    """The part of winreg the probe uses, over a dict of keys."""

    HKEY_LOCAL_MACHINE = "HKLM"

    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def __init__(self, keys: dict, denied=()):
        # path -> {value name: data}; subkeys follow from the paths.
        self.keys = {k.casefold(): (k, v) for k, v in keys.items()}
        self.denied = {d.casefold() for d in denied}

    def _full(self, key, sub):
        base = "" if key == self.HKEY_LOCAL_MACHINE else key.path
        return f"{base}\\{sub}" if base else sub

    def OpenKey(self, key, sub, *args, **kwargs):
        path = self._full(key, sub)
        if path.casefold() in self.denied:
            raise PermissionError(5, "Access is denied")
        if path.casefold() not in self.keys and not self._children(path):
            raise FileNotFoundError(2, "not found", path)
        return self.Key(self.keys.get(path.casefold(), (path, {}))[0])

    def _children(self, path):
        prefix = path.casefold() + "\\"
        names = []
        for low, (real, _) in self.keys.items():
            if low.startswith(prefix):
                name = real[len(prefix):].split("\\")[0]
                if name not in names:
                    names.append(name)
        return names + [d[len(prefix):] for d in self.denied
                        if d.startswith(prefix) and "\\" not in d[len(prefix):]]

    def EnumKey(self, key, index):
        children = self._children(key.path)
        if index >= len(children):
            raise OSError(259, "No more data")
        return children[index]

    def QueryValueEx(self, key, name):
        values = self.keys.get(key.path.casefold(), (None, {}))[1]
        if name not in values:
            raise FileNotFoundError(2, "no value", name)
        return values[name], 1

    def EnumValue(self, key, index):
        values = list(self.keys.get(key.path.casefold(), (None, {}))[1].items())
        if index >= len(values):
            raise OSError(259, "No more data")
        return values[index][0], values[index][1], 1

    def QueryInfoKey(self, key):
        return (len(self._children(key.path)),
                len(self.keys.get(key.path.casefold(), (None, {}))[1]), 0)


def card(desc, version):
    return {"DriverDesc": desc, "DriverVersion": version}


def video(guid, sub):
    return {f"SYSTEM\\CurrentControlSet\\Control\\Video\\{guid}\\Video": {
        "Driver": f"{{4d36e968-e325-11ce-bfc1-08002be10318}}\\{sub}"}}


def devicemap(*guids):
    values = {f"\\Device\\Video{i}":
              f"\\Registry\\Machine\\System\\CurrentControlSet\\Control\\Video\\{g}\\0000"
              for i, g in enumerate(guids)}
    values["MaxObjectNumber"] = len(guids)
    values["\\Device\\Video9"] = r"\REGISTRY\MACHINE\SYSTEM\ControlSet001\Services\BasicDisplay"
    return {r"HARDWARE\DEVICEMAP\VIDEO": values}


def header_driver(fake: FakeWinreg, working_card: str) -> str:
    """What _log_environment puts into ENVIRONMENT["driver"] on that registry."""
    startup.ENVIRONMENT.pop("driver", None)
    out = io.StringIO()
    with mock.patch.dict(sys.modules, {"winreg": fake}), \
            mock.patch.object(startup, "_working_card_name",
                              return_value=working_card), \
            mock.patch.object(sys, "stdout", out):
        startup._log_environment({"lang": "en"})
    return startup.ENVIRONMENT.get("driver", "")


def main() -> int:
    failures = []
    ghosts = {
        f"{CLASS}\\0000": card("NVIDIA GeForce GTX 1060 6GB", "27.21.14.5671"),
        f"{CLASS}\\0001": card("Intel(R) UHD Graphics 630", "31.0.101.2111"),
        f"{CLASS}\\0003": card("NVIDIA GeForce RTX 5070 Ti", "31.0.15.5222"),
        f"{CLASS}\\Configuration": {},
    }
    running = {f"{CLASS}\\0012": card("NVIDIA GeForce RTX 5070 Ti", "32.0.16.1714")}

    # 1. Ghosts first, the running card at 0012.
    fake = FakeWinreg({**ghosts, **running,
                       **devicemap(GUID_NV, GUID_INTEL),
                       **video(GUID_NV, "0012"), **video(GUID_INTEL, "0001")},
                      denied=[f"{CLASS}\\Properties"])
    got = header_driver(fake, "NVIDIA GeForce RTX 5070 Ti")
    if got != "32.0.16.1714":
        failures.append(f"ghost cards before the running one: driver {got!r}, "
                        f"want 0012's 32.0.16.1714")

    # 2. Two running NVIDIA cards: the working one decides.
    second = {f"{CLASS}\\0013": card("NVIDIA GeForce RTX 3050", "32.0.16.1700")}
    fake = FakeWinreg({**ghosts, **running, **second,
                       **devicemap(GUID_NV, GUID_NV2),
                       **video(GUID_NV, "0012"), **video(GUID_NV2, "0013")})
    for working, want in (("NVIDIA GeForce RTX 3050", "32.0.16.1700"),
                          ("NVIDIA GeForce RTX 5070 Ti", "32.0.16.1714")):
        got = header_driver(fake, working)
        if got != want:
            failures.append(f"two running cards, working {working}: driver "
                            f"{got!r}, want {want}")

    # 3. Presence unknown: the name decides, and the ghost GTX 1060 does not.
    fake = FakeWinreg({**ghosts, **running, **second})
    got = header_driver(fake, "NVIDIA GeForce RTX 3050")
    if got != "32.0.16.1700":
        failures.append(f"no DEVICEMAP: driver {got!r}, want the working "
                        f"card's 32.0.16.1700")

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the driver version is the running working adapter's, past "
          "subkey 0009 and over ghost cards")
    return 0


if __name__ == "__main__":
    sys.exit(main())
