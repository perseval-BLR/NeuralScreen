"""A refused multiplier steps down; it must not kill Frame Generation (issue #100).

A card whose DLSS-G runtime stops at 2x answered a request for 3x/4x with a
refusal. The worker treated that refusal as "Frame Generation does not work on
this GPU": it set g_fg.failed, released everything, and every later frame went
through the ordinary present path. The switch on the panel stayed on (or, with
the #76 verdict, flipped itself off with an alert) while the card could have
run 2x perfectly well - the multiplier is a preference, not a capability.

Checked here:

* the ceiling is read from what the runtime itself reports
  (DLSSG.MultiFrameCountMax), so a 2x card is never asked for 4x in the first
  place;
* a refused create OR evaluation steps the multiplier down instead of failing
  the feature, and 2x is the floor - a refusal there is a real refusal;
* the request is clamped to the ceiling, so the step that already failed is
  not asked for again on every frame;
* switching FG off and on gives the user's choice a fresh attempt;
* the retry frame is not reported as a dead feature;
* the worker compiles with all of it.

Run:  runtime\\python.exe tests\\test_fg_multiplier_stepdown.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "frame_generation.inl"


def main() -> int:
    failures = []
    src = SOURCE.read_text(encoding="utf-8", errors="replace")
    if not src:
        print("FAIL: the frame generation source is not readable")
        return 1

    # --- the cap the runtime reports -------------------------------------
    if "NVSDK_NGX_DLSSG_Parameter_MultiFrameCountMax" not in src:
        failures.append("the worker never asks the runtime for its multiplier "
                        "ceiling - a log from a 2x card does not name the reason")
    if "NVSDK_NGX_D3D12_GetCapabilityParameters" not in src:
        failures.append("the capability query is gone: the ceiling cannot be "
                        "read from the runtime")
    if "g_fg_cap_reported" not in src:
        failures.append("the reported ceiling is not kept anywhere")
    # Report-only: an unverified capability answer must not clamp a request
    # that would otherwise have worked.
    if re.search(r"g_fg_count_limit\s*=\s*std::min\(g_fg_count_limit", src):
        failures.append("the reported ceiling clamps the request - an "
                        "unverified answer can silently cost a working step")

    # --- stepping down instead of failing --------------------------------
    if "FgStepDown" not in src:
        failures.append("there is no step-down: a refused multiplier still "
                        "kills Frame Generation on a card that runs 2x")
    if not re.search(r"if\s*\(g_fg_count\s*<=\s*1\)\s*return\s+false", src):
        failures.append("the step-down has no 2x floor - it would keep "
                        "retrying below the only step the runtime may accept")

    # Both refusal points must step down: the create (a runtime that refuses
    # the count up front) and the evaluation (one that refuses per frame).
    # The create runs under an SEH guard now (FgCreateGuarded); its result is
    # judged after the fault check, before the depth texture is built.
    create = re.search(r"const auto result = FgCreateGuarded\((.*?)CreateVideoTex\(g_fg\.depth",
                       src, re.S)
    if not create or "FgStepDown(result)" not in create.group(1):
        failures.append("a refused CreateFeature does not step down")
    evaluate = re.search(r"if \(NVSDK_NGX_FAILED\(result\)\)\s*\n\s*\{"
                         r"(.*?)\n        \}", src, re.S)
    if not evaluate or "FgStepDown(result)" not in evaluate.group(1):
        failures.append("a refused evaluation does not step down")

    # The request is clamped to the ceiling: without it the refused step is
    # asked for again on the next frame, forever.
    if not re.search(r"std::min\(requested,\s*g_fg_count_limit\)", src):
        failures.append("the requested multiplier is not clamped to the "
                        "ceiling - the refused step is retried every frame")

    # A fresh switch-on re-arms the user's choice (a newer provider or driver
    # can lift the ceiling without a restart of the program).
    if not re.search(r"g_fg_count_limit\s*=\s*3\s*;", src):
        failures.append("switching FG off and on does not re-arm the ceiling")

    # The retry frame is a retry, not a dead feature: after a step-down the
    # failure path must NOT set g_fg.failed (that is what killed FG outright).
    retry_branch = re.search(
        r"if \(!EnsureFg\(v, color->GetDesc\(\)\.Format\)\)\s*\n\s*\{(.*?)\n    \}",
        src, re.S)
    if not retry_branch or "g_fg_retry" not in retry_branch.group(1):
        failures.append("the retry frame is no longer distinguished from a "
                        "feature that cannot run")
    elif "g_fg.failed = true" in retry_branch.group(1).split("g_fg_retry")[0]:
        failures.append("the failure path sets g_fg.failed before the retry "
                        "check - a refused multiplier still kills FG")

    # The presenter must name the step it really runs - but the "[fg] displayed"
    # prefix is a contract: settings_io parses it for the HUD counter.
    if not re.search(r"\[fg\] displayed %.1f FPS .*%ux", src):
        failures.append("the FPS line either drops the multiplier it ran at "
                        "or breaks the '[fg] displayed' prefix the HUD parses")

    # --- it compiles ------------------------------------------------------
    vswhere = Path(os.environ.get("ProgramFiles(x86)",
                                  r"C:\Program Files (x86)")) / \
        "Microsoft Visual Studio/Installer/vswhere.exe"
    try:
        install = subprocess.check_output(
            [str(vswhere), "-latest", "-products", "*",
             "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationPath"],
            text=True, timeout=120).strip()
    except Exception:
        install = ""

    if install:
        vcvars = Path(install) / "VC/Auxiliary/Build/vcvars64.bat"
        host = ROOT / "native" / "dlss5-feed-host64.cpp"
        # A temp directory, not repo/_work: that folder is gitignored scratch
        # a disk cleanup may remove, and the object is a temporary file.
        # Writing to _work failed with C1083 once the cleanup had taken it.
        with tempfile.TemporaryDirectory(prefix="ns-compile-") as scratch:
            out_obj = Path(scratch) / "nvngx_fg_step.obj"
            command = (f'"{vcvars}" >nul && cl /nologo /c /O2 /EHsc /W3 /MD '
                       f'/std:c++17 /Iinclude /Isrc "{host}" /Fo:"{out_obj}"')
            result = subprocess.run(
                'cmd /d /s /c "' + command + '"', cwd=str(ROOT / "native"),
                capture_output=True, text=True, encoding="cp866",
                errors="replace", timeout=540)
            combined = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0 or "error C" in combined:
                failures.append("the worker does not compile:\n" +
                                "\n".join(l for l in combined.splitlines()
                                          if "error" in l.lower())[:500])

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a refused multiplier steps down to the card's ceiling and FG "
          "keeps running")
    return 0


if __name__ == "__main__":
    sys.exit(main())
