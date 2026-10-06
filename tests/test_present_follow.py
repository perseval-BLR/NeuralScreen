"""The follower's step decision, driven on the numbers real reports carry.

The overlay the worker presents into keeps the buffer's own size, and every
follow step asks whether that buffer can be shown inside the frame of the
window being captured. On Windows 10 the capture carries the invisible resize
border and the frame does not, so the two rectangles of ONE window differ by
14 and 7 pixels (issue #30's own log). Comparing the buffer against the frame
alone is true for every Windows 10 window, which hid the overlay permanently
and killed the effect in window mode (#139).

The rule lives in native/present_follow.h and the worker compiles that same
header, so this is not a description of the code - it is the code, driven.
Checks are run by the harness built below; each one fails on the pre-fix rule,
where the slack was ignored.

Run:  runtime\\python.exe tests\\test_present_follow.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native"
WORK = ROOT / "_work"
SOURCE = NATIVE / "present_follow.h"
HARNESS = ROOT / "tests" / "present_follow_check.cpp"
EXE = WORK / "present_follow_check.exe"

#: The two rectangles of one window, as Windows reports them. GetWindowRect
#: includes the invisible resize border, DWMWA_EXTENDED_FRAME_BOUNDS does not:
#: 1354x853 against 1340x846 for the same window (issue #30).
BORDER_CAPTURE = (1354, 853)
BORDER_FRAME = (1340, 846)


def check_header_contract() -> None:
    """The rule the worker compiles still says what the harness drives."""
    source = SOURCE.read_text(encoding="utf-8")
    for marker in (
        "DecideForStep",
        "CaptureSlack",
        "Verdict::Hide",
        "Verdict::Place",
    ):
        assert marker in source, f"present_follow.h lost {marker}"

    # The worker must use the shared header rather than keep its own copy of
    # the comparison: a second implementation is the one that drifts.
    worker = (NATIVE / "dlss5-feed-host64.cpp").read_text(
        encoding="utf-8", errors="surrogateescape")
    assert '#include "present_follow.h"' in worker, (
        "the worker does not include present_follow.h")
    assert "ns_present_follow::DecideForStep(" in worker, (
        "the worker does not call the shared decision")
    # The pre-fix shape: comparing the buffer against the raw frame.
    body = worker[worker.index("static void FollowCapturedWindow"):]
    body = body[:body.index("\n}\n")]
    assert "bw > rw ||" not in body and "bh > rh)" not in body, (
        "the follower still compares the buffer against the frame alone, "
        "without the capture's border")
    # The call has to carry the MEASUREMENT, not a constant: a call site that
    # passes zero slack compiles, agrees with the header and reintroduces
    # #139 exactly. Both axes must come from CaptureSlack.
    assert "ns_present_follow::CaptureSlack(" in body, (
        "the follower does not measure the capture's border")
    assert body.count("ns_present_follow::CaptureSlack(") == 2, (
        "both axes of the frame must be measured against the capture")
    assert "GetWindowRect(g_wgc_hwnd, &wr)" in body, (
        "the border has to come from the window's own GetWindowRect")
    for call in re.findall(r"CaptureSlack\(([^;]*?)\)", body):
        assert "wr." in call, (
            "the slack must be measured from GetWindowRect, not assumed")
    # #140: the window capture opens at the size the frames will have.
    opener = worker[worker.index("static bool OpenWgc"):]
    opener = opener[:opener.index("\n}\n")]
    assert "ns_present_follow::InitialCaptureSize(" in opener, (
        "OpenWgc does not open at the visible frame's size - on Windows 10 "
        "the buffer is built 14x7 px larger than every frame (#140)")


def build_harness() -> Path | None:
    """None with a SKIP only when there is no C++ compiler at all."""
    EXE.parent.mkdir(parents=True, exist_ok=True)
    vcvars = NATIVE / "vcvars.bat"
    # cmd /s with ONE string: a list would be quoted by Python and re-read by
    # cmd's own quote rule, which breaks on a space in the path.
    bat = (
        f'cmd /s /c ""{vcvars}" && cl /nologo /EHsc /W4 /WX /std:c++17 '
        f'/I"{NATIVE}" "{HARNESS}" /Fo:"{WORK}\\\\" '
        f'/Fe:"{EXE}" /link""'
    )
    proc = subprocess.run(bat, capture_output=True, timeout=300)
    # The compiler speaks the console codepage while the runner hands the
    # child PYTHONIOENCODING=utf-8: read bytes and decode leniently, or the
    # decode raises inside subprocess's reader thread where no test sees it.
    output = (proc.stdout or b"").decode("utf-8", errors="replace") + \
             (proc.stderr or b"").decode("utf-8", errors="replace")
    if proc.returncode == 0 and EXE.is_file():
        return EXE
    if "not recognized" in output or "NO_COMPILER" in output or \
            "vcvars64.bat" in output and "cannot find" in output:
        print("SKIP: no Visual Studio C++ tools to build "
              "present_follow_check.exe")
        return None
    assert False, "present_follow_check.exe did not build:\n" + output[-3000:]


def run_harness(exe: Path) -> dict:
    proc = subprocess.run([str(exe)], capture_output=True, timeout=60)
    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.startswith("{")]
    assert lines, f"the harness printed no JSON line:\n{text}\n" + \
                  (proc.stderr or b"").decode("utf-8", errors="replace")
    stats = json.loads(lines[-1])
    stats["exit"] = proc.returncode
    return stats


def main() -> int:
    check_header_contract()
    exe = build_harness()
    if exe is None:
        return 0
    stats = run_harness(exe)
    print(f"present_follow: {stats['checks']} checks, "
          f"{stats['failed']} failed")
    if stats["failed"]:
        for line in stats["failures"]:
            print(f"  FAIL: {line}")
        return 1
    assert stats["exit"] == 0, f"the harness exited {stats['exit']}"
    print("OK: the follower's step decision holds on real window rectangles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
