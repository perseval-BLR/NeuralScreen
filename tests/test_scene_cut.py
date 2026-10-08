r"""A scene cut is a real change of picture - not a fast pan, and not missed when dark.

On a cut the network's temporal history is thrown away and the picture
visibly pops; a missed cut warps one picture's history into another. The old
rule - mean(|grey - previous grey|) / 255 > 0.24, the same in guides.py and
the worker - was wrong both ways: a cel-shaded pan faster than its flat areas
scored 0.25-0.36 (a reporter elsewhere counted 42 resets in 120 frames of a
fast anime clip), while a cut between two dark or two text pictures scored
0.05-0.10 and was missed. app/scene_cut.py aligns the frames (global shift,
contrast) before it judges; native/scene_cut.h is its C++ twin in the worker.

Checked:
1. every regression vector (tests/scene_vectors.py): sequences of one scene
   (fast cel pans, shakes, zoom, fades, a flash, a dissolve) never reset after
   the first frame, and every cut resets - the old rule fails 9 of these;
2. the guides (the CPU motion path) reset on a cut and not on a fast pan;
3. the worker's C++ gives the same decision and the same integers on every
   frame of every vector (built here with MSVC; skipped where there is none).

Run:  runtime\python.exe tests\test_scene_cut.py
"""
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scene_vectors as V  # noqa: E402
from scene_cut import SceneCutDetector  # noqa: E402


def _decisions(frames):
    d = SceneCutDetector()
    out = []
    for f in frames:
        cut = d.step(f)
        det = d.last_detail
        out.append((int(cut), det.get("dx", 0), det.get("dy", 0), det.get("sad", 0),
                    det.get("n", 0), det.get("env", 0), det.get("mad_lo", 0),
                    det.get("mad_hi", 0)))
    return out


def _vectors():
    gens = dict(V.MUST_NOT_CUT)
    gens.update(V.MUST_CUT)
    return {name: gen() for name, gen in gens.items()}


def _check_rule(seqs, failures):
    for name, (frames, labels) in seqs.items():
        cuts = np.array([d[0] for d in _decisions(frames)], bool)
        false_cuts = np.where(cuts & (labels == 0))[0]
        missed = np.where(~cuts & (labels == 1))[0]
        if name in V.MUST_NOT_CUT and len(false_cuts):
            failures.append(f"{name}: reset inside one scene at frames {false_cuts.tolist()}")
        if name in V.MUST_CUT and (len(missed) or len(false_cuts)):
            failures.append(f"{name}: missed cuts {missed.tolist()}, "
                            f"extra resets {false_cuts.tolist()}")


def _check_guides(failures):
    """The CPU motion path resets on a cut and not on a fast pan."""
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import guides as guides_mod
    pan, pan_labels = V.MUST_NOT_CUT["cel_pan_h_40"]()
    cut, cut_labels = V.MUST_CUT["cut_dark_dark"]()
    for name, frames, labels in (("fast cel pan", pan, pan_labels),
                                 ("dark-to-dark cut", cut, cut_labels)):
        g = guides_mod.TemporalGuideGenerator(320, 180, flow_width=320, emit_small=True)
        resets = []
        for f in frames:
            resets.append(bool(g.process(gray=f).reset))
        resets = np.array(resets)
        if np.any(resets & (labels == 0)):
            failures.append(f"guides: a {name} reset at frames "
                            f"{np.where(resets & (labels == 0))[0].tolist()}")
        if np.any(~resets & (labels == 1)):
            failures.append(f"guides: a {name} was missed at frames "
                            f"{np.where(~resets & (labels == 1))[0].tolist()}")


def _vcvars():
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / \
        "Microsoft Visual Studio/Installer/vswhere.exe"
    query = [str(vswhere), "-latest", "-products", "*",
             "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationPath"]
    try:
        install = subprocess.check_output(query, text=True, timeout=120).strip()
        if not install:
            install = subprocess.check_output(query[:2] + ["-prerelease"] + query[2:],
                                              text=True, timeout=120).strip()
    except Exception:
        return None
    path = Path(install) / "VC/Auxiliary/Build/vcvars64.bat" if install else None
    return path if path and path.is_file() else None


def _check_native(seqs, failures):
    vcvars = _vcvars()
    if vcvars is None:
        print("    (MSVC not found - the C++ comparison is skipped)")
        return
    with tempfile.TemporaryDirectory(prefix="ns-scene-") as scratch:
        scratch = Path(scratch)
        exe = scratch / "scene_cut_check.exe"
        cmd = (f'"{vcvars}" >nul && cl /nologo /O2 /EHsc /W3 /std:c++17 '
               f'"{BASE / "tests" / "scene_cut_check.cpp"}" /Fe:"{exe}" /Fo:"{scratch}\\\\"')
        built = subprocess.run('cmd /d /s /c "' + cmd + '"', capture_output=True,
                               text=True, encoding="cp866", errors="replace", timeout=540)
        if built.returncode != 0 or not exe.is_file():
            failures.append("native/scene_cut.h does not compile: " +
                            " ".join(l for l in (built.stdout + built.stderr).splitlines()
                                     if "error" in l.lower())[:400])
            return
        data = scratch / "seqs.bin"
        names = list(seqs)
        with open(data, "wb") as f:
            f.write(struct.pack("<i", len(names)))
            for name in names:
                frames = np.ascontiguousarray(seqs[name][0], dtype=np.uint8)
                f.write(struct.pack("<3i", frames.shape[0], frames.shape[2], frames.shape[1]))
                f.write(frames.tobytes())
        run = subprocess.run([str(exe), str(data)], capture_output=True, text=True, timeout=300)
        if run.returncode != 0:
            failures.append(f"the C++ driver failed with exit {run.returncode}")
            return
        native = {}
        rows = [list(map(int, l.split())) for l in run.stdout.splitlines() if l.strip()]
        for row in rows:
            native.setdefault(row[0], []).append(tuple(row[2:]))
        mismatched = 0
        for i, name in enumerate(names):
            py = _decisions(seqs[name][0])
            cc = native.get(i, [])
            if len(cc) != len(py):
                failures.append(f"{name}: C++ gave {len(cc)} frames, Python {len(py)}")
                continue
            for k, (a, b) in enumerate(zip(py, cc)):
                if a != b:
                    mismatched += 1
                    if mismatched <= 3:
                        failures.append(f"{name} frame {k}: Python {a} != C++ {b}")
        frames_total = sum(len(v) for v in native.values())
        print(f"    C++ and Python compared on {frames_total} frames, {mismatched} differ")


def main() -> int:
    failures: list = []
    seqs = _vectors()
    _check_rule(seqs, failures)
    _check_guides(failures)
    extra = {"anime_clip_2": V.seq_anime_clip(seed=2),
             **{k: g() for k, g in V.BORDERLINE.items()}}
    _check_native({**seqs, **extra}, failures)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: cuts reset, one scene in motion does not, and the worker's C++ "
          "decides the same way")
    return 0


if __name__ == "__main__":
    sys.exit(main())
