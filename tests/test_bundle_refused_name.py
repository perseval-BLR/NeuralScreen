"""A refused file's name leaves the diagnostic package whole, parentheses and all.

The conversion queue logs a file it refuses as "not queued for conversion:
<name> (<reason>)", and the package cuts the name out - the folder is already
gone as a path, the name is the user's. The match stopped at the FIRST " (",
so a name with parentheses of its own lost only its first part:
"Anna Petrova (wedding).mp4 (format)" went into the package as
"<FILE> (wedding).mp4 (format)".

Checked through diagnostics.sanitize_text on log text: names with and without
parentheses, several lines, the reason kept.

Run:  runtime\python.exe tests\test_bundle_refused_name.py
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

from diagnostics import sanitize_text  # noqa: E402

LOG = (
    "14:00:01.000  [main] not queued for conversion: Anna Petrova (wedding).mp4 (format)\n"
    "14:00:01.001  [main] not queued for conversion: plain name.png (queued)\n"
    "14:00:01.002  [main] not queued for conversion: a (1) (2).jpg (missing)\n"
    "14:00:01.003  [main] queued 0 file(s) for conversion\n"
)


def main() -> int:
    failures = []
    out = sanitize_text(LOG)
    print("    " + out.replace("\n", "\n    ").rstrip())
    for secret in ("Anna", "Petrova", "wedding", "plain name", "(1)", "(2).jpg"):
        if secret in out:
            failures.append(f"{secret!r} reached the package")
    for reason in ("(format)", "(queued)", "(missing)"):
        if f"<FILE> {reason}" not in out:
            failures.append(f"the reason {reason} was not kept next to <FILE>")
    if "queued 0 file(s) for conversion" not in out:
        failures.append("an unrelated line was changed")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: a refused file's whole name is cut out of the package")
    return 0


if __name__ == "__main__":
    sys.exit(main())
