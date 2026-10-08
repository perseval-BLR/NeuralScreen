r"""A failed worker build leaves the working nvngx.dll where it was.

build-host.bat linked straight to native\nvngx.dll, and MSVC's linker deletes
its output when the link fails: measured 2026-10-08 - one unresolved symbol
(LNK2019 / LNK1120) and the worker that had been sitting there was gone, so a
broken build also broke the program. (A running worker gives LNK1104 and
survives; any real link error does not.) build-clang.bat already links to a
temporary name and moves it into place only after success (PR #136).

Checked by running a copy of build-host.bat in a scratch folder with a
stand-in vcvars.bat and a stand-in cl.exe - built here with the .NET
Framework's csc.exe, present on every Windows 10/11; the script calls cl
without `call`, so it has to be a real program - that models the measured
behaviour: a failing link deletes whatever /Fe: names. A pre-existing
nvngx.dll must survive a failing build and be replaced by a successful one.

Run:  runtime\python.exe tests\test_build_host_keeps_worker.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SCRIPT = BASE / "native" / "build-host.bat"
CSC = (Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET" / "Framework64"
       / "v4.0.30319" / "csc.exe")

STUB = """
using System; using System.IO;
class Cl { static int Main(string[] args) {
    string output = null;
    foreach (var a in args)
        if (a.StartsWith("/Fe:", StringComparison.OrdinalIgnoreCase)) output = a.Substring(4);
    if (output == null) return 0;
    bool worker = !output.StartsWith("nvngx.dll_ns-forwarder", StringComparison.OrdinalIgnoreCase);
    if (worker && Environment.GetEnvironmentVariable("NS_STUB_FAIL") == "1") {
        // MSVC's linker on LNK2019/LNK1120: no output, and the old file is gone.
        if (File.Exists(output)) File.Delete(output);
        Console.WriteLine("stub: error LNK2019: unresolved external symbol");
        return 1;
    }
    File.WriteAllBytes(output, System.Text.Encoding.ASCII.GetBytes("NEW BUILD"));
    return 0;
} }
"""


def _run(folder: Path, fail: bool) -> int:
    env = dict(os.environ)
    env["PATH"] = str(folder) + os.pathsep + env.get("PATH", "")
    env["NS_STUB_FAIL"] = "1" if fail else "0"
    proc = subprocess.run(["cmd", "/c", str(folder / "build-host.bat")], cwd=str(folder),
                          env=env, capture_output=True, text=True)
    return proc.returncode


def main() -> int:
    if not SCRIPT.is_file():
        print("SKIP: native/build-host.bat is not here")
        return 0
    if not CSC.is_file():
        print("SKIP: no .NET Framework csc.exe to build the stand-in compiler")
        return 0
    failures = []
    folder = Path(tempfile.mkdtemp(prefix="ns-build-host-"))
    try:
        shutil.copyfile(SCRIPT, folder / "build-host.bat")
        (folder / "vcvars.bat").write_text("@exit /b 0\r\n", encoding="ascii")
        (folder / "cl_stub.cs").write_text(STUB, encoding="utf-8")
        built = subprocess.run([str(CSC), "/nologo", "/out:" + str(folder / "cl.exe"),
                                str(folder / "cl_stub.cs")], capture_output=True, text=True)
        if built.returncode != 0 or not (folder / "cl.exe").is_file():
            print("SKIP: csc.exe could not build the stand-in compiler")
            return 0
        worker = folder / "nvngx.dll"
        worker.write_bytes(b"WORKING WORKER")
        code = _run(folder, fail=True)
        if code == 0:
            failures.append("a failing link still reported success")
        if not worker.is_file():
            failures.append("a failing build deleted the working nvngx.dll")
        elif worker.read_bytes() != b"WORKING WORKER":
            failures.append("a failing build changed the working nvngx.dll")
        code = _run(folder, fail=False)
        if code != 0:
            failures.append(f"a successful build failed (exit {code})")
        elif not worker.is_file() or worker.read_bytes() != b"NEW BUILD":
            failures.append("a successful build did not put the new worker in place")
        leftovers = [p.name for p in folder.glob("nvngx.dll.*")]
        if leftovers:
            failures.append(f"the build left temporary files: {leftovers}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a failed build keeps the working worker; a good one replaces it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
