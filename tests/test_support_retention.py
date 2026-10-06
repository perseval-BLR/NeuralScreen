"""compatibility-cache.json and support-bundles/ stay bounded.

Neither was ever pruned. Every driver, app or runtime update is a new
compatibility key, and an expired quarantine was dropped only when its own
key was read again - which after an update it never is - so the cache grew
an entry per update for the life of the install. Every blocked start (and
every Retry that failed again) wrote a support bundle, and nothing removed
those either.

Checked in a temp dir:

1. 40 verdicts put one after another leave the newest CACHE_MAX_ENTRIES in
   the file, the newest still readable, the oldest gone;
2. a put drops an expired quarantine of another key and keeps one that is
   still running;
3. create_support_bundle with 15 older bundles in the folder leaves the
   newest SUPPORT_KEEP, the new one among them, and leaves files that are
   not bundles alone.

Run:  runtime\\python.exe tests\\test_support_retention.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))  # the modules live in app/

from compatibility import (  # noqa: E402
    CompatibilityCache, CompatibilityKey, CompatibilityResult,
    CompatibilityStatus,
)

KEEP_ENTRIES = 32
KEEP_BUNDLES = 10


class Clock:
    def __init__(self):
        self.value = 1_000_000.0

    def __call__(self):
        return self.value


def key(n: int) -> CompatibilityKey:
    return CompatibilityKey(
        app_version="2.1.9", runtime_sha256="a" * 64, worker_sha256="b" * 64,
        selected_gpu={"name": "RTX 5070 Ti"}, driver_version=f"32.0.16.{n}",
        display_mode={"width": 640})


def result(k: CompatibilityKey, status=CompatibilityStatus.PASS, until=None):
    return CompatibilityResult(
        key_digest=k.digest, status=status, stage="complete", passed=3,
        attempted=3, expected=3, reason="success", quarantine_until=until)


def entries(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["entries"]


def check_cache(work: Path, failures: list) -> None:
    path = work / "compatibility-cache.json"
    clock = Clock()
    cache = CompatibilityCache(path, clock=clock)
    for n in range(40):
        clock.value += 60
        cache.put(key(n), result(key(n)))
    kept = entries(path)
    if len(kept) != KEEP_ENTRIES:
        failures.append(f"40 verdicts left {len(kept)} entries in the cache, "
                        f"want {KEEP_ENTRIES}")
    if cache.get(key(39)) is None:
        failures.append("the newest verdict is not readable after pruning")
    if key(0).digest in kept or key(7).digest in kept:
        failures.append("the oldest verdicts were kept and newer ones dropped")
    if key(8).digest not in kept:
        failures.append("a verdict among the newest 32 was dropped")

    # 2. An expired quarantine of another key goes on the next put.
    path = work / "quarantine.json"
    clock = Clock()
    cache = CompatibilityCache(path, clock=clock)
    cache.put(key(100), result(key(100), CompatibilityStatus.QUARANTINED,
                               until=clock.value + 600))
    cache.put(key(101), result(key(101), CompatibilityStatus.QUARANTINED,
                               until=clock.value + 3600))
    clock.value += 1800
    cache.put(key(102), result(key(102)))
    kept = entries(path)
    if key(100).digest in kept:
        failures.append("an expired quarantine stayed in the cache after a put")
    if key(101).digest not in kept:
        failures.append("a running quarantine was dropped")
    if key(102).digest not in kept:
        failures.append("the verdict just put is missing")


def check_bundles(work: Path, failures: list) -> None:
    import compatibility_runtime as runtime
    import diagnostics

    support = work / "support-bundles"
    support.mkdir()
    now = time.time()
    for i in range(15):
        old = support / f"NeuralScreen-diagnostics-202601{i + 10:02d}-120000.zip"
        old.write_bytes(b"old bundle")
        os.utime(old, (now - 86400 * (20 - i), now - 86400 * (20 - i)))
    unrelated = support / "notes.txt"
    unrelated.write_text("mine", encoding="utf-8")
    (work / "NeuralScreen.log").write_text("21:00:00.000  [main] hello\n",
                                           encoding="utf-8")
    st = type("St", (), {"cfg": {}, "worker": None, "worker_logs": [],
                         "compatibility_result": None})()
    with mock.patch.object(runtime, "BASE_DIR", work), \
            mock.patch.object(runtime, "SUPPORT_DIR", support), \
            mock.patch.object(diagnostics, "collect_system_snapshot",
                              return_value={"os": {}, "gpus": [], "displays": []}), \
            mock.patch.object(diagnostics, "_authenticode_signature",
                              return_value={"status": "skipped"}):
        made = runtime.create_support_bundle(st, stage="manual")
    left = sorted(support.glob("NeuralScreen-diagnostics-*.zip"))
    if len(left) != KEEP_BUNDLES:
        failures.append(f"{len(left)} bundles left in support-bundles/, "
                        f"want {KEEP_BUNDLES}")
    if not made.is_file():
        failures.append("the bundle just made was deleted")
    if (support / "NeuralScreen-diagnostics-20260110-120000.zip").exists():
        failures.append("the oldest bundle was kept")
    if not (support / "NeuralScreen-diagnostics-20260124-120000.zip").exists():
        failures.append("the newest old bundle was deleted")
    if not unrelated.exists():
        failures.append("a file that is not a bundle was deleted")


def main() -> int:
    failures: list = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        check_cache(work, failures)
        check_bundles(work, failures)
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("OK: the compatibility cache keeps its newest verdicts and drops "
          "expired quarantines; support-bundles/ keeps the newest bundles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
