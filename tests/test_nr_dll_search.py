r"""The worker loads its NR runtime from its own folder, nowhere else.

The bundled nvngx_dlssnr.dll was loaded by bare name. With the file present
next to the worker that is the one loaded, but a native\ folder without it
(a partial copy, an antivirus quarantine) sent LoadLibrary on through the
current directory, the system folders and PATH, and the worker mapped
whichever nvngx_dlssnr.dll it met first - a planted DLL running inside the
process (pre-release audit). The BYO folder and NS_NR_DLL already go
through the signature gate; the default copy is now loaded by full path.

Checked with a copy of the worker in a temp folder that has no
nvngx_dlssnr.dll, and a decoy of that name (a harmless DLL from this
interpreter) in the working directory and on PATH: the worker must fail to
load the runtime from its own folder and never map the decoy.

Run:  runtime\\python.exe tests\\test_nr_dll_search.py
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))

from paths import WORKER_EXE  # noqa: E402

#: Next to the worker; the runtime itself is deliberately not copied.
COMPANIONS = ("Spout.dll", "SpoutDX.dll", "nvngx.dll_ns-forwarder.dll")


def main() -> int:
    if not WORKER_EXE.is_file():
        print("SKIP: native/nvngx.dll is not built - the worker cannot run")
        return 0
    import protocol
    failures = []
    root = Path(tempfile.mkdtemp(prefix="ns-dllsearch-"))
    work = root / "native"
    decoys = root / "decoys"
    work.mkdir()
    decoys.mkdir()
    shutil.copyfile(WORKER_EXE, work / "nvngx.dll")
    for name in COMPANIONS:
        src = WORKER_EXE.parent / name
        if src.is_file():
            shutil.copyfile(src, work / name)
    shutil.copyfile(Path(sys.executable).with_name("python3.dll"),
                    decoys / "nvngx_dlssnr.dll")
    env = dict(os.environ)
    env["PATH"] = str(decoys) + os.pathsep + env.get("PATH", "")
    for key in ("NS_NR_DLL", "NS_FORWARDER"):
        env.pop(key, None)
    proc = subprocess.Popen([str(work / "nvngx.dll"), "--live"], cwd=str(decoys),
                            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    lines: list = []
    threading.Thread(target=lambda: lines.extend(
        ln.decode("utf-8", "replace") for ln in iter(proc.stderr.readline, b"")),
        daemon=True).start()
    try:
        header = struct.pack(protocol.HEADER_FMT, protocol.VIDEO_MAGIC, 640, 360, 0, 0,
                             0, 0, 1, 0, 0, 1.0, 0.5, 1.0, -1.0, 0, 0)
        proc.stdin.write(header)
        proc.stdin.flush()
        end = time.time() + 30
        while time.time() < end and proc.poll() is None:
            text = "".join(lines)
            if "LoadLibrary(" in text or "missing direct exports" in text \
                    or "direct feature 18" in text:
                break
            time.sleep(0.2)
    except OSError:
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    time.sleep(0.3)
    text = "".join(lines)
    if "missing direct exports" in text:
        failures.append("the worker mapped the decoy nvngx_dlssnr.dll from the "
                        "working directory / PATH")
    if str(work).lower() not in text.lower() or "LoadLibrary(" not in text:
        failures.append("the worker did not try (and fail) to load the runtime "
                        "from its own folder")
    for _ in range(20):
        try:
            shutil.rmtree(root)
            break
        except OSError:
            time.sleep(0.2)
    for f in failures:
        print("FAIL:", f)
    if failures:
        print("worker log (tail):", *lines[-8:], sep="\n  ")
        return 1
    print("OK: the NR runtime is loaded from the worker's folder only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
