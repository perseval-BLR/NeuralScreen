"""What the support bundle's log tail must not carry: the user's own names.

The bundle is meant to be attached to a public issue. Its scrubber cut out
paths and other programs' window titles, and three things still got through:

1. a converted file's name. The conversion queue logs every job as
   "[convert] <name>: started|failed|done|stopped ..." and only the folder
   was cut (as a path) - "Holiday with Anna - private.mp4" reached the bundle.

Each check builds the line exactly as the program writes it, with a sentinel
in the private part, runs it through diagnostics.sanitize_text and asserts
the sentinel is gone while the line still says what happened.

Run:  runtime\\python.exe tests\\test_bundle_log_privacy.py
"""
import sys
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


def main() -> int:
    failures: list = []
    check_convert_names(failures)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the bundle's log tail carries no converted file names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
