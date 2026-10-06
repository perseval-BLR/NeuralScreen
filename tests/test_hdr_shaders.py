"""The HDR shaders, compiled and run - on WARP, so anywhere.

hdr_gpu.cpp (PR #36) takes the two shader sources the worker actually
compiles - kHdrCaptureHlsl and kHdrCompositeHlsl, out of native/hdr_shaders.h -
runs them on the WARP software device and reads the pixels back. No HDR
monitor, no NVIDIA runtime, no NGX: it checks the arithmetic of the HDR path,
which is the part that decides whether the picture is right.

It was reachable only by hand (native\\test-hdr.bat), and a check nobody runs
is a check nobody has. This wrapper puts it in the suite: the suite already
insists on a freshly built worker, so the machine running it has the compiler.

What the WARP run covers: shader compilation, highlights above 1.0 surviving,
the signed part of the gamut surviving, a zero neural edit reproducing the
original EXACTLY (the residual must not tint an untouched frame), bypass, the
before/after wipe, no infinities or NaNs, the SDR export and channel order.

Run:  runtime\\python.exe tests\\test_hdr_shaders.py
"""
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BAT = BASE / "native" / "test-hdr.bat"


def main() -> int:
    source = (BASE / "native" / "dlss5-feed-host64.cpp").read_text(encoding="utf-8")
    capture = source.split("static bool SwizzleCaptureIntoColor(VideoState &v)", 1)[1]
    constants = capture.split("struct { UINT is_float;", 1)[1].split("};", 1)[0]
    # Whether the capture is converted at all follows the SOURCE (#112); how
    # it is mapped follows where it goes: tone-mapped for the HDR composite
    # to invert (1), or kept at SDR white when it is shown as SDR (2).
    if "(g_capture_float && g_capture_display.enabled) ? (g_hdr_capture ? 1u : 2u) : 0u" not in constants:
        print("FAIL: capture shader HDR flag must follow source HDR, not presentation preference")
        return 1
    if not BAT.exists():
        print(f"FAIL: no {BAT}")
        return 1
    try:
        r = subprocess.run(["cmd", "/c", str(BAT)], cwd=str(BAT.parent),
                           capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        print("FAIL: the WARP shader run did not finish in 300s")
        return 1
    out = (r.stdout or "") + (r.returncode and (r.stderr or "") or "")
    for line in (r.stdout or "").splitlines():
        if line.strip() and not line.startswith("hdr_gpu.cpp"):
            print(f"    {line.strip()}")
    # Only what vcvars.bat says when there really is no compiler. A bare "is
    # not recognized" is in stderr on EVERY run - vswhere.bat tries
    # vswhere.exe on PATH before looking elsewhere - and stderr is read only
    # when the run failed, so a shader that failed its checks was reported
    # as missing build tools, and the check that failed never showed.
    no_tools = ("vswhere not found" in out or "build tools not found" in out
                or "'cl' is not recognized" in out)
    if no_tools and r.returncode != 0:
        print("FAIL: no Visual Studio build tools - the same ones the worker "
              "is built with (native\\build-host.bat)")
        return 1
    if r.returncode != 0 or "PASS" not in (r.stdout or ""):
        print((r.stderr or "").strip()[-600:])
        print(f"FAIL: the HDR shaders did not pass on WARP (exit {r.returncode})")
        return 1
    print("OK: the HDR capture and composite shaders are right on WARP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
