r"""Every prefix the worker logs under reaches the shared log.

THE BUG (found 20.09.2026, while reading a reporter's package for #96)

The worker writes its diagnostics to stderr and the parent decides which ones
go into `NeuralScreen.log`, by an allow-list of prefixes (`_LOG_ALWAYS`).
Nothing ever checked that list against the prefixes the worker actually uses,
and NINE of them were missing:

    [nr] [scale] [gray] [residual] [outs] [failure] [reset] [live] [test]

They are not decoration. `[failure]` is the failure report itself. `[scale]`,
`[gray]`, `[residual]` and `[outs]` carry every shader and pipeline setup
failure in the worker. `[nr]` carries "working textures failed - staying at
full resolution", which is Boost silently not happening.

This survived because the symptom is nothing: three diagnostic packages were
read on this project while `[nr]` appeared zero times in all three, and the
conclusion drawn from that was "those call sites are never executed on the
live path". They were executed. The parent dropped them, and a line that is
never printed looks exactly like a line that was never reached.

WHAT THIS LOCKS

Every `Log("[tag] ...")` prefix in the worker's sources is either in
`_LOG_ALWAYS` or in GATED below, which is the per-frame profiler that is
deliberately behind NS_PHASE=1. A new prefix fails this test until someone
chooses - which costs one line, against a diagnostic that silently is not
there.

Run:  runtime\python.exe tests\test_worker_log_prefixes.py
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import pipeline  # noqa: E402

#: Behind NS_PHASE=1 on purpose: one line per frame would bury the log.
GATED = {"[phase]", "[pw]"}

#: The worker's sources. The .inl files are compiled into the same binary and
#: log through the same function, so they count.
SOURCES = ("native/dlss5-feed-host64.cpp", "native/frame_generation.inl",
           "native/hdr_present.inl", "native/spout_bridge.cpp",
           "native/gpu_recorder.cpp")

PREFIX = re.compile(r'Log(?:Once)?\(\s*"(\[[a-z]+\])')


def main() -> int:
    failures: list[str] = []
    allow = set(pipeline._LOG_ALWAYS)
    seen: dict[str, str] = {}

    for rel in SOURCES:
        path = BASE / rel
        if not path.is_file():
            continue            # an .inl may be folded away; not this test's business
        text = io.open(path, encoding="utf-8", errors="surrogateescape").read()
        for tag in PREFIX.findall(text):
            seen.setdefault(tag, rel)

    if not seen:
        failures.append("no Log() prefixes found at all - this test has "
                        "stopped looking at anything")

    for tag, rel in sorted(seen.items()):
        if tag in allow or tag in GATED:
            continue
        failures.append(
            f"the worker logs under {tag} ({rel}) and the parent drops it: "
            f"those lines reach no log and no diagnostic package. Add it to "
            f"pipeline._LOG_ALWAYS, or to GATED here if it is per-frame")

    # And the reverse: a prefix nobody emits is dead paperwork, EXCEPT the
    # ones that come from elsewhere in the program rather than the worker.
    FROM_ELSEWHERE = {"[nvofa]", "[fg]", "[spout]"}
    for tag in sorted(allow - set(seen) - FROM_ELSEWHERE):
        failures.append(f"_LOG_ALWAYS lets through {tag}, which nothing emits")

    # The nine that made this test necessary, by name.
    for tag in ("[nr]", "[failure]", "[scale]", "[gray]", "[residual]",
                "[outs]"):
        if tag not in allow:
            failures.append(f"{tag} is being dropped again")

    # The gate itself still works: a profiler line stays out by default.
    import os
    keep = os.environ.pop("NS_PHASE", None)
    try:
        if pipeline._log_wanted("[phase] frame 1 eval 16.6ms"):
            failures.append("the per-frame profiler is no longer gated - one "
                            "line per frame would bury the log it feeds")
        if not pipeline._log_wanted("[nr] working textures failed"):
            failures.append("[nr] still does not reach the log")
    finally:
        if keep is not None:
            os.environ["NS_PHASE"] = keep

    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print(f"OK: {len(seen)} worker prefixes, all reported or gated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
