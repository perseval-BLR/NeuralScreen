"""NeuralScreen self-checks - the static part (no GUI).

Run:  runtime\\python.exe tests\\autocheck.py
The GUI part (menu, recording) is run separately - see the end of the output.

Every check reports PASS / FAIL / SKIP plus a reason. Exit: 0 = all PASS,
1 = there is a FAIL.
"""
import re
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # the project root (tests/ lives inside it)
FAILS = []

# The live-loop line is also consumed by GUI/smoke checks.  Keep its contract
# in one place: v1.13 deliberately labels the neural-processing rate as
# ``NR ... fps`` instead of presenting it as an ambiguous display FPS.
NR_FRAME_MARKER = "NR ON | NR "
NR_STATS_RE = re.compile(
    r"NR ON \| NR\s+([\d.]+) fps \| frames (\d+)"
)

NATIVE = ROOT / "native"
NATIVE_INCLUDE_DIRS = (NATIVE, NATIVE / "include", NATIVE / "src")
NATIVE_INCLUDE_RE = re.compile(
    r'^\s*#\s*include\s*([<"])([^>"]+)[>"]', re.MULTILINE)


def check(name, fn):
    try:
        ok, detail = fn()
        status = "PASS" if ok else "FAIL"
        if not ok:
            FAILS.append(name)
        print(f"[{status}] {name}: {detail}")
    except Exception as exc:
        FAILS.append(name)
        print(f"[FAIL] {name}: exception {exc!r}")


def native_include_closure(entry_points):
    """Return every existing project-local file reached by quoted includes."""
    native_root = NATIVE.resolve()
    pending = [Path(path).resolve() for path in entry_points]
    seen = set()
    while pending:
        source = pending.pop()
        if source in seen or not source.is_file():
            continue
        seen.add(source)
        text = source.read_text(encoding="utf-8-sig", errors="replace")
        for delimiter, include in NATIVE_INCLUDE_RE.findall(text):
            candidates = [] if delimiter == "<" else [source.parent / include]
            candidates.extend(folder / include for folder in NATIVE_INCLUDE_DIRS)
            for candidate in candidates:
                resolved = candidate.resolve()
                try:
                    resolved.relative_to(native_root)
                except ValueError:
                    continue
                if resolved.is_file():
                    pending.append(resolved)
                    break
    return seen


def fresh_worker():
    """Native binaries are newer than all of their local build inputs."""
    build_script = NATIVE / "build-host.bat"
    host_roots = [NATIVE / "dlss5-feed-host64.cpp",
                  NATIVE / "spout_bridge.cpp"]
    forwarder_roots = [NATIVE / "ns_forwarder.cpp"]
    required_inputs = host_roots + forwarder_roots + [build_script]
    missing = [path.relative_to(ROOT).as_posix() for path in required_inputs
               if not path.is_file()]
    if missing:
        return False, f"missing native build inputs: {', '.join(missing)}"

    host_inputs = native_include_closure(host_roots)
    host_inputs.update({build_script,
                        NATIVE / "SpoutDX.lib",
                        NATIVE / "lib" / "Windows_x86_64" / "x64" /
                        "nvsdk_ngx_d.lib"})
    forwarder_inputs = native_include_closure(forwarder_roots)
    forwarder_inputs.add(build_script)

    # These used to be outside the hand-written freshness list. Keep a small
    # contract here as a regression guard for the scanner itself; future
    # quoted includes are picked up automatically.
    expected_host_inputs = {
        NATIVE / "dll_trust.h",
        NATIVE / "frame_generation.inl",
        NATIVE / "hdr_present.inl",
        NATIVE / "nvofa.inl",
        NATIVE / "quality_shaders.h",
        NATIVE / "src" / "feed_ipc.h",
    }
    missed = expected_host_inputs - host_inputs
    if missed:
        names = sorted(path.relative_to(ROOT).as_posix() for path in missed)
        return False, f"native dependency scan missed: {', '.join(names)}"

    outputs = [
        ("worker", NATIVE / "nvngx.dll", host_inputs),
        ("forwarder", NATIVE / "nvngx.dll_ns-forwarder.dll", forwarder_inputs),
    ]
    details = []
    for label, binary, dependencies in outputs:
        if not binary.is_file():
            return False, f"no {binary.relative_to(ROOT).as_posix()}"
        absent = [path.relative_to(ROOT).as_posix() for path in dependencies
                  if not path.is_file()]
        if absent:
            return False, f"missing {label} build inputs: {', '.join(sorted(absent))}"
        stale = [path.relative_to(ROOT).as_posix() for path in dependencies
                 if binary.stat().st_mtime_ns < path.stat().st_mtime_ns]
        if stale:
            return False, (f"{label} is older than {', '.join(sorted(stale))} "
                           "- rerun build-host.bat")
        details.append(f"{label}: {len(dependencies)} inputs")

    dll = NATIVE / "nvngx.dll"
    if b"NS_ARCH_SPOOF" not in dll.read_bytes():
        return False, "no NS_ARCH_SPOOF in the worker (an old build?)"
    return True, (f"{dll.stat().st_size} bytes, the hook is there; "
                  + ", ".join(details) + ", fresh")


def personal_config_keys():
    """Every config key the program itself can write over a user's session.

    The archive's config.json comes from git HEAD, and this is what catches
    "a maintainer committed a personal value". It used to be a hand-written
    tuple, and it stopped growing: gpu, screenshot_dir, rec_indicator and
    monitor were all written by _menu_layout_payload and none of them was
    ever compared. Adding those would have restarted the same clock, so the
    set is asked of the payload itself - a key the menu learns to save is
    covered the day it is added.

    "hotkeys" is included by hand because it is saved on its own path, not
    through the menu payload.
    """
    # autocheck is run both through run_tests.py (which puts the project
    # root on the path) and on its own, where it is not there.
    import sys
    if str(ROOT / "app") not in sys.path:
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/
    import settings_io
    payload = settings_io._menu_layout_payload(
        {"profile": "Natural", "gpu": 0, "spout": False,
         "rec_indicator": True, "screenshot_dir": "", "presets": {}},
        {"intensity": 1.0, "local_tone": 1.0, "local_structure": 1.0,
         "skin_structure": -1.0},
        0, "en", 0.65, 0.0, True, False,
        type("M", (), {"user_scale": 1.0, "user_height": None,
                       "state": {"theme": "light"}, "offset": [0, 0]})())
    return sorted(set(payload) | {"hotkeys"})


def shipped_config_defaults():
    """The committed config.default.json is the product default.

    A maintainer's working values shipped inside an archive twice - the
    SR-removal merge left frame_generation true and motion_backend nvofa
    (1.10.0), and frame_multiplier 3 survived into 1.10.0 and 1.11.0 (a
    40-series card caps at 2x). The zip-vs-HEAD comparison cannot catch
    this class - both sides carry the same wrong values - so HEAD's default
    is checked against the profile-derived defaults directly.
    """
    import sys as _sys
    if str(ROOT / "app") not in _sys.path:
        _sys.path.insert(0, str(ROOT))
        _sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/
    import settings_io
    head = json.loads(subprocess.check_output(
        ["git", "show", "HEAD:config.default.json"], cwd=ROOT))
    natural = settings_io.PROFILES["Natural"]
    problems = []
    if head.get("frame_multiplier") != 2:
        problems.append(f"frame_multiplier={head.get('frame_multiplier')!r} "
                        f"- the safe floor is 2 (RTX 40 caps at 2x)")
    if head.get("frame_generation") is not False:
        problems.append(f"frame_generation={head.get('frame_generation')!r} "
                        f"- a fresh install must be opt-in")
    # NVOFA is the default since 1.13.1 (user rule 16.09): the driver path is
    # the one the program is built around, and CPU DIS stays as the automatic
    # fallback and an explicit choice.
    if head.get("motion_backend") != "nvofa":
        problems.append(f"motion_backend={head.get('motion_backend')!r} "
                        f"- nvofa is the default")
    for key in ("intensity", "local_tone", "local_structure", "skin_structure"):
        if key not in head or head[key] is None:
            continue
        try:
            same = abs(float(head[key]) - float(natural[key])) < 1e-9
        except (TypeError, ValueError):
            same = False
        if not same:
            problems.append(f"{key}={head[key]!r} - Natural says {natural[key]}")
    if problems:
        return False, "HEAD config is not the product default: " + "; ".join(problems)
    return True, "multiplier 2, FG off, NVOFA motion, Natural's four sliders"


def tests_isolate_user_config():
    """No automated test may load the ignored worktree config as live state."""
    pattern = re.compile(
        r'\b(?:ROOT|BASE)\s*/\s*["\']config\.json["\']'
    )
    offenders = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if pattern.search(path.read_text(encoding="utf-8-sig")):
            offenders.append(path.name)
    if offenders:
        return False, "tests touch the user's config.json: " + ", ".join(offenders)
    return True, "all tests use product defaults or disposable configs"


def zip_integrity():
    """Validate the pinned release contract, and a tagged ZIP when present.

    A production archive cannot exist before the release commit is tagged:
    the builder deliberately refuses any other state.  Development/static
    runs therefore prove that HEAD's manifest exactly describes the tracked
    payload and generated runtime.  Once the versioned ZIP exists, the same
    check escalates to the complete offline verifier (sidecars, exact member
    set, tagged blobs, checksums and deterministic metadata).
    """
    import sys as _sys
    if str(ROOT / "app") not in _sys.path:
        _sys.path.insert(0, str(ROOT))
        _sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/
    import build_release_zip as release
    import settings_io
    version = settings_io.APP_VERSION
    tag = f"v{version}"
    # The manifest pins the RELEASE, so it is checked against the tag - not
    # against HEAD. It lists every shipped file with its git blob, and a
    # checkout that has moved on since the release (a test fix, a translation,
    # a comment) differs from it by design; comparing with HEAD reported that
    # normal state as a broken contract. If the tag is missing, fall back to
    # HEAD so a pre-release tree is still validated.
    git_ref = subprocess.run(
        ["git", "rev-list", "-n", "1", tag],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout.strip()
    if not git_ref:
        git_ref = "HEAD"
    try:
        manifest, _raw = release.validate_runtime_manifest(
            ROOT,
            version=version,
            expected_tag=tag,
            git_ref=git_ref,
        )
    except release.ReleaseContractError as exc:
        return False, f"release manifest: {exc}"

    package_count = manifest["package"]["file_count"]
    runtime_count = manifest["runtime"]["file_count"]
    zpath = ROOT / f"neuralscreen-v{version}-full.zip"
    if not zpath.is_file():
        return True, (f"schema {manifest['schema_version']}, {package_count} payload / "
                      f"{runtime_count} runtime files pinned; ZIP waits for {tag}")

    import verify_github as verifier
    # The archive is built from the TAGGED commit - the builder refuses to run
    # anywhere else - so the tag is what it has to match. Resolving HEAD here
    # instead reported a fault on every working tree that had moved on since
    # the release (a test fix, a documentation line), because the commit
    # compiled into VERSION.txt is the tag's, not HEAD's. That is the normal
    # state of a checkout after a release, so the check read as broken while
    # the archive was perfectly correct.
    tag_commit = subprocess.run(
        ["git", "rev-list", "-n", "1", tag],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout.strip()
    if not tag_commit:
        return False, (f"a release ZIP exists but tag {tag} does not - the "
                       f"archive cannot be matched to a release")
    failures = verifier.validate_release_set(
        ROOT, tag=tag, tag_commit=tag_commit, repo=ROOT,
    )
    if failures:
        return False, "release ZIP: " + "; ".join(failures[:5])
    return True, (f"{zpath.stat().st_size} bytes, exact {package_count}-file "
                  f"payload, blobs and checksums verified against {tag}")


def gpuinfo_works():
    """gpuinfo.py answers: architecture plus official support."""
    py = ROOT / "runtime" / "python.exe"
    if not py.exists():
        return False, "no runtime/python.exe"
    code = (
        "import sys; sys.path[:0] = [r'%s', r'%s']; "
        "import gpuinfo; i = gpuinfo.probe(); "
        "print(gpuinfo.describe(i)); print('official:', i['official'])"
        % (ROOT / "app", ROOT)
    )
    # PYTHONIOENCODING: without it the child prints in the console codepage
    # and an em-dash in the GPU name decodes into garbage (or throws).
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True,
                       timeout=30, encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0:
        return False, f"gpuinfo crashed: {r.stderr.strip()[:200]}"
    out = r.stdout.strip()
    if "Blackwell" not in out:
        return False, f"odd answer: {out}"
    return True, out.replace("\n", " | ")


def spoof_default_on():
    """The spoof is on by default: ArchSpoofRequested has no =1 requirement."""
    cpp = (ROOT / "native" / "dlss5-feed-host64.cpp").read_text(encoding="utf-8-sig")
    start = cpp.find("static bool ArchSpoofRequested()")
    end = cpp.find("static int SetupArchSpoof()")
    body = cpp[start:end]
    if "buf[0] == '0'" not in body:
        return False, "the NS_ARCH_SPOOF=0 logic was not found in ArchSpoofRequested"
    if "buf[0] == '1'" in body:
        return False, "the old =1 logic is still in ArchSpoofRequested"
    return True, "on by default, NS_ARCH_SPOOF=0 turns it off"


def readme_consistency():
    """The four docs line up: two short READMEs, two technical ones.

    The READMEs are for someone installing the program, so the check that
    matters is that they stayed short and that every image and link in them
    resolves. The measurements live in the technical docs, and the one fact
    those must not lose is the spoof default - it decides whether a 20/30/40
    card works at all.
    """
    import re

    docs = {
        # The keys name where the files really live: each doc's links are
        # resolved from its own directory below. These two moved from docs/
        # to the root - the old keys kept resolving them against docs/.
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        "README.ru.md": (ROOT / "README.ru.md").read_text(encoding="utf-8"),
        "TECHNICAL.md": (ROOT / "TECHNICAL.md").read_text(encoding="utf-8"),
        "TECHNICAL.ru.md": (ROOT / "TECHNICAL.ru.md").read_text(encoding="utf-8"),
    }
    for name in ("README.md", "README.ru.md"):
        n = len(docs[name].splitlines())
        if n > 200:
            return False, f"{name} is {n} lines - it drifted back into a manual"
    # The Russian needle is the content of a translated doc and stays Russian.
    if "On by default" not in docs["TECHNICAL.md"]:
        return False, "TECHNICAL.md lost the spoof default"
    if "Включено по умолчанию" not in docs["TECHNICAL.ru.md"]:
        return False, "TECHNICAL.ru.md lost the spoof default"

    # Every relative link and image must resolve, in both directions.
    for name, text in docs.items():
        base = (ROOT / name).parent
        for target in re.findall(r"]\(([^)#][^)]*)\)", text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (base / target).exists():
                return False, f"{name} points at a missing {target}"
        for src in re.findall(r'<img src="([^"]+)"', text):
            # The screenshots are addressed absolutely on purpose: docs/ is
            # not in the release archive, and a relative link left every
            # image broken for anyone who unpacked the zip. Only a relative
            # path is ours to resolve.
            if src.startswith(("http://", "https://")):
                continue
            if not (base / src).exists():
                return False, f"{name} shows a missing {src}"
    return True, (f"READMEs {len(docs['README.md'].splitlines())}/"
                  f"{len(docs['README.ru.md'].splitlines())} lines, links resolve")


def git_clean():
    """The working copy is clean (apart from a personal config.json)."""
    r = subprocess.run(["git", "status", "--short"], cwd=ROOT,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    dirty = [l for l in r.stdout.splitlines() if l.strip() and "config.json" not in l]
    if dirty:
        return False, f"dirty: {dirty[:5]}"
    return True, "clean (config.json is personal, as expected)"


def release_notes_short():
    """The latest release notes are concise (the EN part is under 2 KB)."""
    from json import loads as _loads
    try:
        tag = _loads(subprocess.check_output(
            ["gh", "release", "list", "-R", "perseval-BLR/DLSS5-NeuralScreen",
             "--json", "tagName", "--limit", "1", "--exclude-drafts"],
            text=True, timeout=60, encoding="utf-8", errors="replace"))[0]["tagName"]
    except Exception:
        return False, "cannot resolve the latest release tag"
    r = subprocess.run(
        ["gh", "release", "view", tag, "-R", "perseval-BLR/DLSS5-NeuralScreen",
         "--json", "body", "--jq", ".body"],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return False, f"gh: {r.stderr.strip()[:100]}"
    body = r.stdout
    # The WHOLE body, not the part before the first "---". That split dates
    # from when the notes were written in two languages and the rule "one
    # language" (user, 12.09) retired it - and it had quietly made this
    # check vacuous: a markdown table starts with a |---| row, so the split
    # fired on the first table and measured 505 characters of a 4639
    # character body.
    #
    # 5000, and the purpose is unchanged: the notes must not drift into a
    # manual. 1.7.0 is the first release to carry a feature, an
    # architectural change and twenty fixes at once, and the user asked for
    # the fixes to be spelled out ("the things that were fixed - describe
    # them, definitely").
    if len(body) > 5000:
        return False, f"the release body is {len(body)} characters - too long"
    return True, f"the release body is {len(body)} characters"


# --- driving the running program (the GUI and smoke checks share this) ---
LOG = ROOT / "NeuralScreen.log"
_test_config_path = None
KEYEVENTF_KEYUP = 0x0002
_MOD_VK = {0x0002: 0x11, 0x0001: 0x12, 0x0004: 0x10}  # Ctrl / Alt / Shift


def binding_keys(command):
    """The default binding for a command as (vk, modifier vks).

    Taken from hotkeys.DEFAULT_BINDINGS rather than written out here: the
    defaults have moved twice already, and a check that presses yesterday's
    key tests nothing.
    """
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/
    from hotkeys import DEFAULT_BINDINGS
    for mods, vk, cmd, _name in DEFAULT_BINDINGS.values():
        if cmd == command:
            return vk, tuple(v for bit, v in _MOD_VK.items() if mods & bit)
    raise KeyError(command)


def send_key(vk, mods=()):
    import ctypes
    for m in mods:
        ctypes.windll.user32.keybd_event(m, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    for m in reversed(mods):
        ctypes.windll.user32.keybd_event(m, 0, KEYEVENTF_KEYUP, 0)


def log_offset():
    """Where the log ends right now.

    The log is appended to across runs, so a check that greps the whole file
    can pass on the previous launch's lines. Everything below reads from the
    offset taken before the launch.
    """
    return LOG.stat().st_size if LOG.exists() else 0


def log_since(offset):
    # main.py opens the log as utf-8; errors="replace" guards a truncated tail.
    if not LOG.exists():
        return ""
    with LOG.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        return fh.read()


def shipped_config(overrides: dict | None = None) -> dict:
    """Return the committed product defaults with optional test overrides."""
    head = subprocess.run(
        ["git", "show", "HEAD:config.default.json"], cwd=ROOT,
        capture_output=True,
    )
    if head.returncode != 0:
        detail = head.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"cannot read committed config.default.json: {detail or head.returncode}"
        )
    cfg = json.loads(head.stdout.decode("utf-8"))
    if overrides:
        cfg.update(overrides)
    return cfg


def nr_stats(text: str) -> list[tuple[float, int]]:
    """Extract honest neural-rate/frame counters from current live-loop logs."""
    return [(float(fps), int(frames))
            for fps, frames in NR_STATS_RE.findall(text)]


def running_instances():
    """Our processes that are already up.

    Nothing stops a second copy of the program from starting, and two copies
    fight over the screen capture - so a check that launches one has to know
    the field is clear, otherwise it measures the wrong process.
    """
    out = subprocess.run(["tasklist"], capture_output=True).stdout
    text = out.decode("cp1251", errors="replace")
    return [l.split()[0] for l in text.splitlines()
            if "pythonw.exe" in l or "nvngx.dll" in l]


def launch(overrides: dict | None = None):
    """Start the program with the DEFAULT config and return the log offset.

    The worktree config.json is the user's live file - whatever they tweaked
    last (split, theme, profile, work_scale) leaks straight into every smoke
    and GUI check that launches the app "the way a user does". Tests want the
    shipped defaults: git HEAD's config.default.json is copied to a disposable path
    and passed with --config. The path is cleaned up at quit_app time.

    overrides: a test that needs a specific launch state (menu open at
    start, a theme) passes {key: value} - applied on top of the defaults,
    so the user's file is never touched at all.
    """
    global _test_config_path
    offset = log_offset()
    cfg = shipped_config(overrides)
    path = ROOT / "_work" / "test-config.json"
    path.parent.mkdir(exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    _test_config_path = path
    subprocess.run(["cscript", "//nologo", "NeuralScreen.vbs", "--config", str(path)],
                   cwd=ROOT, capture_output=True)
    return offset


def wait_for(offset, needle, timeout):
    """Wait for a line to appear in the log written since `offset`."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = log_since(offset)
        if needle in text:
            return text
        time.sleep(0.5)
    return None


def quit_app(timeout=8.0):
    """Ctrl+Alt+Q, then confirm nothing of ours is left running."""
    import time
    send_key(*binding_keys("quit"))
    deadline = time.monotonic() + timeout
    left = []
    while time.monotonic() < deadline:
        out = subprocess.run(["tasklist"], capture_output=True).stdout
        text = out.decode("cp1251", errors="replace")
        left = [l for l in text.splitlines()
                if "pythonw.exe" in l or "nvngx.dll" in l]
        if not left:
            try:
                if _test_config_path and _test_config_path.is_file():
                    _test_config_path.unlink()
            except Exception:
                pass
            return []
        time.sleep(0.5)
    return left


def smoke_check():
    """The short one: launch -> is it processing -> exit. ~20 seconds.

    Everything a broken build fails at before recording even matters: the
    program comes up, the pipeline produces frames, the pixel channel is the
    shared-memory one (it silently fell back to the pipe once - see the OUTS
    regression), and Ctrl+Alt+Q leaves nothing running.
    """
    import time

    MIN_FRAMES, MIN_FPS = 60, 15.0
    busy = running_instances()
    if busy:
        return False, f"NeuralScreen is already running ({busy}) - stop it first"
    offset = launch()
    try:
        # The first FPS line reports the warm-up (frames 2, ~1 FPS), so wait
        # for the pipeline to have actually run for a while.
        fps, frames, deadline = 0.0, 0, time.monotonic() + 30.0
        while time.monotonic() < deadline:
            text = log_since(offset)
            stats = nr_stats(text)
            if stats:
                fps, frames = stats[-1]
                if frames >= MIN_FRAMES:
                    break
            time.sleep(0.5)
        text = log_since(offset)
        if frames == 0:
            return False, "NeuralScreen did not start processing (no FPS line in the log)"
        if frames < MIN_FRAMES or fps < MIN_FPS:
            return False, (f"the pipeline is barely alive: {fps:.1f} FPS, "
                           f"{frames} frames in 30 s")
        if "result pixels through shared memory" not in text:
            pipe = "shared memory for pixels unavailable" in text
            return False, ("the pixels are not going through shared memory"
                           + (" - the worker refused the section" if pipe else ""))
        # The capture must ALSO be the DDA one. A dead DDA1 makes the client
        # silently fall back to sending frames from Python - the pipeline
        # still runs, the FPS lines look fine, and only this log line tells
        # it apart (audit #4: DDA has no direct regression test).
        if "screen capture inside the worker (DDA1)" not in text:
            return False, "the worker is not capturing the screen (no DDA1 in the log)"
    finally:
        left = quit_app()
    if left:
        return False, f"processes left behind: {left}"
    return True, f"came up, {fps:.1f} FPS at {frames} frames, shared memory, clean exit"


def gui_check():
    """The full GUI cycle: launch -> record -> exit. Requires NeuralScreen not
    to be running. ~50 seconds."""
    import time

    busy = running_instances()
    if busy:
        return False, f"NeuralScreen is already running ({busy}) - stop it first"
    # 1. launch
    offset = launch()
    if wait_for(offset, "NR ON", timeout=25.0) is None:
        return False, "NeuralScreen did not come up (no 'NR ON' in the log)"
    # The first FPS lines report the warm-up (frames 2, ~1 FPS) - the NGX
    # feature discards 120 warmup frames and the pipeline needs several more
    # seconds to normalise (measured: 20 FPS at frame 82, 28 at 191, 38 at
    # 336). Recording into the warm-up would produce a short, slow file and
    # a false FAIL (75 frames / 4.8 s instead of 100+). Wait for the
    # pipeline to have actually normalised - frames >= 300 is ~8-10 s after
    # launch, past the warm-up.
    fps, frames, deadline = 0.0, 0, time.monotonic() + 40.0
    while time.monotonic() < deadline:
        text = log_since(offset)
        stats = nr_stats(text)
        if stats:
            fps, frames = stats[-1]
            if frames >= 300:
                break
        time.sleep(0.5)
    if frames < 300:
        quit_app()
        return False, (f"the pipeline did not normalise: {fps:.1f} FPS, "
                       f"{frames} frames in 40 s")
    # 2. record for 5 seconds - with the key the program actually binds.
    #
    # What the recording SHOULD hold is not a fixed 100 frames: the worker
    # presents whatever the capture gives it, and the DDA capture in the
    # worker asks for a frame with a 100 ms timeout - a desktop that is not
    # changing answers only when that wait runs out, so the pipeline runs at
    # about 15 FPS on a still screen and over 100 on a busy one. The GPU
    # recorder encodes the frames the worker presents and asks for nothing
    # (the old CPU recorder forced frames through with WANT_PIXELS), so the
    # recording follows the pipeline: 76 frames in 5 s with nobody touching
    # the machine, 293 with something moving. Demanding 100 frames regardless
    # failed a healthy recording for the crime of a still desktop.
    #
    # Judge the recorder by what it had to work with: the frames the pipeline
    # produced while the recording was open, capped by the stream's 60 fps.
    # The NR counter is logged every two seconds, so the window is measured
    # from the recording's own publish line - the worker reports exactly how
    # many frames it encoded and how long it ran, and those two numbers are
    # what the file has to match.
    record = binding_keys("record")
    send_key(*record)
    time.sleep(5)
    send_key(*record)
    time.sleep(3)
    recs = sorted((ROOT / "recordings").glob("neuralscreen-*.mp4"),
                  key=lambda p: p.stat().st_mtime)
    if not recs:
        quit_app()
        return False, "the recording file was not created"
    rec = recs[-1]
    import av
    c = av.open(str(rec))
    s = c.streams.video[0]
    dur = float(s.duration * s.time_base)
    # Count the frames by demuxing, not by reading s.frames. The GPU recorder
    # writes a fragmented MP4 (the worker encodes while it runs, so there is
    # no index to write at the end), and PyAV reports s.frames = 0 for that
    # container even though every packet is there - a healthy 292-frame file
    # read as "0 frames / 4.9 s" and failed the check. Demuxing is what the
    # file actually holds.
    frames = sum(1 for _ in c.demux(s))
    c.close()
    # The worker's own report: "finalized: N frames, M dropped, T s". It is
    # the honest source of what the recorder could have written, and the file
    # must hold it. On a still desktop the pipeline is slow (the DDA acquire
    # waits out its 100 ms timeout), which is a property of the capture, not
    # of the recorder - so the frames the worker counted, not a fixed 100, is
    # the bar.
    report = re.findall(r"\[grec\] finalized: (\d+) frames, (\d+) dropped, ([\d.]+) s",
                        log_since(offset))
    encoded = int(report[-1][0]) if report else 0
    if encoded:
        # The worker said how many frames it encoded: the file must hold them.
        bad = frames < int(0.9 * encoded)
    else:
        # No report (an older file, or the line was rotated away): fall back to
        # "something real was written".
        bad = frames < 20
    if dur < 4 or bad:
        quit_app()
        return False, (f"the recording does not match what the worker encoded: "
                       f"{frames} frames / {dur:.1f} s in the file, "
                       f"{encoded} encoded")
    # 3. exit
    left = quit_app()
    if left:
        return False, f"processes left behind: {left}"
    return True, (f"recording of {frames} frames / {dur:.1f} s, "
                  f"the worker encoded {encoded}, "
                  f"a clean exit, no processes left")


def main():
    print(f"NeuralScreen autocheck - {ROOT}")
    print("=" * 60)
    if "--gui" in sys.argv:
        check("GUI: launch -> record -> exit", gui_check)
    elif "--smoke" in sys.argv:
        check("smoke: launch -> processing -> exit", smoke_check)
    else:
        check("worker: fresh, with the hook", fresh_worker)
        check("zip: integrity and contents", zip_integrity)
        check("config: the shipped defaults", shipped_config_defaults)
        check("tests: user config isolated", tests_isolate_user_config)
        check("gpuinfo: answers", gpuinfo_works)
        check("spoof: on by default", spoof_default_on)
        check("README: EN/RU agree", readme_consistency)
        check("git: the working copy is clean", git_clean)
        check("release notes: concise", release_notes_short)
    print("=" * 60)
    if FAILS:
        print(f"RESULT: {len(FAILS)} FAIL - {FAILS}")
        return 1
    print("RESULT: all checks PASS")
    if "--gui" not in sys.argv and "--smoke" not in sys.argv:
        print()
        print("smoke (20 s):  runtime\\python.exe tests\\autocheck.py --smoke")
        print("GUI part:      runtime\\python.exe tests\\autocheck.py --gui")
    return 0


if __name__ == "__main__":
    sys.exit(main())
