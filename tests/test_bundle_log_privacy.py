"""What the support bundle's log tail must not carry: the user's own names.

The bundle is meant to be attached to a public issue. Its scrubber cut out
paths and other programs' window titles, and three things still got through:

1. a converted file's name. The conversion queue logs every job as
   "[convert] <name>: started|failed|done|stopped ..." and only the folder
   was cut (as a path) - "Holiday with Anna - private.mp4" reached the bundle.
2. the second half of a line. The tail was a byte window, so its first line
   started wherever the cut fell; the title and path rules are anchored on
   "title='" and on the drive letter, both on the far side of the cut, and
   the rest of another program's window title or of a path went out as
   plain text.
3. a window title with a secret-looking word in it. The secret rules ran
   first; "token=abc" or "password: x" inside title='...' took the closing
   quote with the value, the title rule then saw no title, and the whole
   title - "Divorce lawyer chat" - went out.

Each check builds the line exactly as the program writes it, with a sentinel
in the private part, runs it through the scrubber (or a whole bundle, for
the cut) and asserts the sentinel is gone while the line still says what
happened.

Run:  runtime\\python.exe tests\\test_bundle_log_privacy.py
"""
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/

import diagnostics  # noqa: E402

SENTINEL = "Zebracorn"
STAMP = "12:00:00.000  "


def check_convert_names(failures: list) -> None:
    name = f"Holiday with {SENTINEL} - private.mp4"
    # The four shapes app/convert_jobs.py prints, with their real tails.
    lines = {
        "started": (f"[convert] {name}: started (video, codec auto, quality "
                    f"high, beside the source)"),
        "failed": (f"[convert] {name}: failed - RuntimeError: could not "
                   f"decode {name} (moov atom not found)"),
        "done": (f"[convert] {name}: done -> D:\\Media\\{SENTINEL} "
                 f"Holiday_NR.mp4 (120 frame(s), 4.2s, av1, audio copied)"),
        "stopped": f"[convert] {name}: stopped",
    }
    for verb, line in lines.items():
        out = diagnostics.sanitize_text(STAMP + line)
        if SENTINEL in out:
            failures.append(f"[convert] {verb}: the file name reached the "
                            f"bundle: {out!r}")
        if f": {verb}" not in out or "[convert] " not in out:
            failures.append(f"[convert] {verb}: the line no longer says what "
                            f"happened: {out!r}")
    # A [convert] line that is not a job line keeps its text.
    plain = "[convert] colour metadata not set: av1 has no colr box"
    if diagnostics.sanitize_text(plain) != plain:
        failures.append("a [convert] diagnostic line that names no file was "
                        "rewritten")


def check_tail_cut(failures: list) -> None:
    window = 4096
    # Not a profiler line: those are thinned out of a long tail on purpose
    # (test_bundle_log_session.py), and this check is about the cut alone.
    filler = STAMP + "[main] frame pacing: 60.0 fps, 0 late\n"
    lines = {
        "title": (STAMP + "[z] foreign-above-hud (changed) top=hwnd=0x1 "
                  f"pid=7 class='Qt' title='Anna ({SENTINEL} private chat) - "
                  "Telegram' rect=(0,0,1,1) | hud=(0,0,1,1)\n"),
        "path": (STAMP + "[main] recording published: D:\\Media\\Anna "
                 f"Smith\\{SENTINEL}\\clip.mp4 | 12.0 s\n"),
    }
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        log = work / "NeuralScreen.log"
        for label, line in lines.items():
            # The window starts a few characters into the line, before the
            # sentinel and after the anchor the scrubber looks for.
            cut = line.index("Anna") + 2
            after = (filler * 200)[: window - (len(line) - cut)]
            log.write_text(filler * 300 + line + after, encoding="utf-8",
                           newline="\n")
            out = work / f"bundle-{label}.zip"
            diagnostics.create_diagnostic_bundle(
                out, diagnostics.DiagnosticBundleRequest(
                    failure_stage="manual", log_path=log,
                    max_log_bytes=window,
                    system_snapshot={"os": {}, "gpus": [], "displays": []},
                    runtime_signature={"status": "skipped"},
                    runtime_path=log))
            with zipfile.ZipFile(out) as archive:
                tail = archive.read("log_tail.txt").decode("utf-8")
            if SENTINEL in tail:
                first = tail.splitlines()[0] if tail else ""
                failures.append(f"a cut inside a {label} line kept its private "
                                f"half: {first!r}")
            if not tail.startswith(STAMP):
                failures.append(f"the {label} tail does not start on a whole "
                                f"line: {tail[:60]!r}")
        # A window that starts exactly on a line keeps that line.
        log.write_text(filler * 300, encoding="utf-8", newline="\n")
        out = work / "bundle-aligned.zip"
        diagnostics.create_diagnostic_bundle(
            out, diagnostics.DiagnosticBundleRequest(
                failure_stage="manual", log_path=log,
                max_log_bytes=len(filler) * 10,
                system_snapshot={"os": {}, "gpus": [], "displays": []},
                runtime_signature={"status": "skipped"}, runtime_path=log))
        with zipfile.ZipFile(out) as archive:
            kept = archive.read("log_tail.txt").decode("utf-8").count("[main]")
        if kept != 10:
            failures.append(f"a window aligned on a line start kept {kept} of "
                            f"its 10 lines")


def check_title_with_secret_word(failures: list) -> None:
    for title in (f"{SENTINEL} token=y",
                  f"Divorce lawyer {SENTINEL} chat - password: x",
                  f"acme/reset?token=abc123 {SENTINEL}"):
        line = (STAMP + "[z] foreign-above-hud (changed) top=hwnd=0x1 pid=7 "
                f"class='Chrome_WidgetWin_1' title={title!r} "
                "rect=(0,0,1,1) | hud=(0,0,1,1)")
        out = diagnostics.sanitize_text(line)
        if SENTINEL in out or "Divorce" in out:
            failures.append(f"a title with a secret word in it leaked: {out!r}")
        if f"title=<{len(title)} chars>" not in out:
            failures.append(f"the title was not replaced by its length: {out!r}")
        if "rect=(0,0,1,1) | hud=(0,0,1,1)" not in out:
            failures.append(f"the rest of the [z] line was lost: {out!r}")


def main() -> int:
    failures: list = []
    check_convert_names(failures)
    check_tail_cut(failures)
    check_title_with_secret_word(failures)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the bundle's log tail carries no converted file names, no "
          "half lines and no window titles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
