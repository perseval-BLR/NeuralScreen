"""The support bundle's log carries the session's startup, not just its last second.

log_tail.txt used to be the last 64 KB of NeuralScreen.log. Two kinds of
session lost everything a report is read for:

* NS_PHASE=1 writes a [phase]/[pw] profiler line per frame. A bundle from
  such a session held about 1.3 s of [phase] lines and nothing else: no [env]
  header (version, GPU, driver), no [compat] verdict, no adapter pick, and
  not the few non-profiler lines of its last minutes either.
* a crash and a relaunch. The relaunch moves an oversized log aside to
  NeuralScreen.log.1 before it writes a line, so the bundle read a log of a
  few dozen fresh lines and the session that crashed was not in it.

Checked, on synthetic logs and through diagnostics.create_diagnostic_bundle:

1. a long NS_PHASE session: the bundle has the session head ([env] header,
   driver, [compat], [host]) and the last lines, and the non-profiler lines
   of the tail survive while the [phase] lines are thinned; a private window
   title in the head is still scrubbed; the bundle stays within its bound;
2. a crash-relaunch: the current log is short, and the tail of
   NeuralScreen.log.1 - with the crashed session's [failure] line - comes
   before it; compatibility_runtime.create_support_bundle passes that file;
3. a short single-session log, with no NeuralScreen.log.1 on disk (the
   usual case), comes out whole.

Run:  runtime\\python.exe tests\\test_bundle_log_session.py
"""
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/

import diagnostics  # noqa: E402

SENTINEL = "Zebracorn"
SNAPSHOT = {"os": {}, "gpus": [], "displays": []}


def stamp(i: int) -> str:
    return f"21:{(i // 60000) % 60:02d}:{(i // 1000) % 60:02d}.{i % 1000:03d}  "


def session(start: int, frames: int, *, phase: bool, last: str) -> list:
    lines = [
        "[env] NeuralScreen 2.1.9 | Windows 10.0 (build 26200) | "
        "Windows-11-10.0.26200-SP0 | 2026-10-06 21:17:39",
        "[env] GPU: RTX 5070 Ti (50xx, arch 0x1B0)",
        "[env] driver: 32.0.16.1714",
        "[env] HDR: off",
        "[main] NeuralScreen - profile 'Natural', resolution 3840x2160, monitor 0",
        "[compat] pass: 3/3 (expected 3), stage=complete, reason=success, cached=yes",
        "[host] adapter 0: NVIDIA GeForce RTX 5070 Ti vendor=0x10DE vram=15995MB",
        "[host] adapter 0 runs the network and the capture",
        f"[z] foreign-above-hud (changed) top=hwnd=0x1 pid=7 class='Qt' "
        f"title='{SENTINEL} private chat' rect=(0,0,1,1) | hud=(0,0,1,1)",
    ]
    for frame in range(frames):
        if phase:
            lines.append(f"[phase] frame {frame}: capture 1.21 ms | send 0.40 ms "
                         f"| nr 6.02 ms | present 0.33 ms")
            lines.append(f"[pw] frame {frame}: exposure 1.00 dark=0.10 lit=0.40")
        if frame % 2000 == 1999:
            lines.append(f"[fg] heartbeat {frame}: generated 2 per real frame")
    lines.append(last)
    return [stamp(start + i) + line + "\n" for i, line in enumerate(lines)]


def bundle(work: Path, log: Path, *, limit=diagnostics.DEFAULT_LOG_BYTES,
           previous=None) -> tuple:
    out = work / "bundle.zip"
    kwargs = {}
    if previous is not None:
        kwargs["previous_log_path"] = previous
    diagnostics.create_diagnostic_bundle(out, diagnostics.DiagnosticBundleRequest(
        failure_stage="manual", log_path=log, max_log_bytes=limit,
        system_snapshot=SNAPSHOT, runtime_signature={"status": "skipped"},
        runtime_path=log, **kwargs))
    import json
    with zipfile.ZipFile(out) as archive:
        report = json.loads(archive.read("diagnostics.json"))
        return archive.read("log_tail.txt"), report


def check_phase_session(work: Path, failures: list) -> None:
    log = work / "NeuralScreen.log"
    older = session(0, 2000, phase=False, last="[main] resources released")
    current = session(10_000, 20_000, phase=True,
                      last="[failure] stage=present kind=device-removed "
                           "code=0x887A0005")
    log.write_text("".join(older + current), encoding="utf-8", newline="\n")
    for limit in (diagnostics.DEFAULT_LOG_BYTES, 8 * 1024):
        try:
            raw, report = bundle(work, log, limit=limit)
        except Exception as exc:          # noqa: BLE001
            failures.append(f"NS_PHASE session, {limit} B: no bundle: "
                            f"{type(exc).__name__}: {exc}")
            continue
        text = raw.decode("utf-8")
        where = f"NS_PHASE session, {limit} B"
        if len(raw) > limit:
            failures.append(f"{where}: {len(raw)} bytes, over the bound")
        for needed in ("[env] NeuralScreen 2.1.9", "[env] driver: 32.0.16.1714",
                       "[compat] pass: 3/3", "[host] adapter 0 runs the network",
                       "[failure] stage=present kind=device-removed"):
            if needed not in text:
                failures.append(f"{where}: {needed!r} is not in the bundle")
        if SENTINEL in text:
            failures.append(f"{where}: the window title in the session head "
                            f"was not scrubbed")
        # The last two [fg] lines of the session are in its last 4000 frames,
        # i.e. some 600 KB of profiler lines before the end.
        for frame in (17999, 19999):
            if f"[fg] heartbeat {frame}:" not in text:
                failures.append(f"{where}: the non-profiler line of frame "
                                f"{frame} was crowded out by [phase] lines")
        if "[phase] frame 19999:" not in text:
            failures.append(f"{where}: the newest profiler lines are gone - "
                            f"they are thinned, not dropped")
        if ("[env] NeuralScreen" in text and "[failure] stage=" in text
                and text.index("[env] NeuralScreen") > text.index("[failure] stage=")):
            failures.append(f"{where}: the session head is not before its tail")
        if not report["log"].get("truncated"):
            failures.append(f"{where}: a thinned log is not marked truncated")


def check_crash_relaunch(work: Path, failures: list) -> None:
    log = work / "NeuralScreen.log"
    previous = work / "NeuralScreen.log.1"
    crashed = session(0, 40_000, phase=False,
                      last="[failure] stage=first-evaluate kind=device-hung "
                           "code=0x887A0006 crashed-session")
    previous.write_text("".join(crashed), encoding="utf-8", newline="\n")
    log.write_text("".join(session(50_000, 0, phase=False,
                                   last="[main] menu opened")),
                   encoding="utf-8", newline="\n")
    try:
        raw, report = bundle(work, log, previous=previous)
    except Exception as exc:              # noqa: BLE001
        failures.append(f"crash-relaunch: no bundle: {type(exc).__name__}: {exc}")
        return
    text = raw.decode("utf-8")
    if "crashed-session" not in text:
        failures.append("crash-relaunch: the crashed session's [failure] line "
                        "(in NeuralScreen.log.1) is not in the bundle")
    elif text.index("crashed-session") > text.index("[main] menu opened"):
        failures.append("crash-relaunch: the previous log is not before the "
                        "current one")
    if "[main] menu opened" not in text:
        failures.append("crash-relaunch: the current session is missing")
    if len(raw) > diagnostics.DEFAULT_LOG_BYTES:
        failures.append(f"crash-relaunch: {len(raw)} bytes, over the bound")
    if SENTINEL in text:
        failures.append("crash-relaunch: a window title was not scrubbed")

    # And the real caller passes NeuralScreen.log.1 next to the log.
    try:
        import compatibility_runtime as runtime
    except Exception as exc:              # noqa: BLE001
        failures.append(f"compatibility_runtime does not import: {exc}")
        return
    st = type("St", (), {"cfg": {}, "worker": None, "worker_logs": [],
                         "compatibility_result": None})()
    with mock.patch.object(runtime, "BASE_DIR", work), \
            mock.patch.object(runtime, "SUPPORT_DIR", work / "support-bundles"), \
            mock.patch.object(diagnostics, "collect_system_snapshot",
                              return_value=SNAPSHOT), \
            mock.patch.object(diagnostics, "_authenticode_signature",
                              return_value={"status": "skipped"}):
        path = runtime.create_support_bundle(st, stage="manual")
    with zipfile.ZipFile(path) as archive:
        if b"crashed-session" not in archive.read("log_tail.txt"):
            failures.append("create_support_bundle does not read "
                            "NeuralScreen.log.1 after a crash-relaunch")


def check_short_log_whole(work: Path, failures: list) -> None:
    log = work / "short.log"
    content = "".join(session(0, 10, phase=True, last="[main] resources released"))
    log.write_text(content, encoding="utf-8", newline="\n")
    # The usual case: there is no NeuralScreen.log.1 at all.
    try:
        raw, report = bundle(work, log, previous=work / "absent.log.1")
    except Exception as exc:              # noqa: BLE001
        failures.append(f"no previous log: no bundle: {type(exc).__name__}: {exc}")
        return
    # Whole, and only scrubbed: the title shrinks to its length, which is
    # not a cut.
    if raw.decode("utf-8") != diagnostics.sanitize_text(content):
        failures.append("a short log did not come out whole")
    if report["log"].get("truncated") or report["log"].get("previous_log"):
        failures.append("a short log is marked truncated or as carrying a "
                        "previous log")


def main() -> int:
    failures: list = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        check_phase_session(work, failures)
        check_crash_relaunch(work, failures)
        check_short_log_whole(work, failures)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the bundle's log keeps the session head and a tail [phase] "
          "lines cannot crowd out, and reaches into NeuralScreen.log.1 after "
          "a relaunch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
