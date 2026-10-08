"""Pure tests for the isolated v1.13 compatibility preflight."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest


BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

from compatibility import (  # noqa: E402
    CACHE_SCHEMA,
    CompatibilityCache,
    CompatibilityKey,
    CompatibilityPreflight,
    CompatibilityStatus,
    StageOutcome,
    StageStatus,
    UnsupportedError,
)


class MemoryFileSystem:
    def __init__(self):
        self.files = {}
        self.atomic_writes = []

    def read_bytes(self, path):
        key = str(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        value = self.files[key]
        return value if isinstance(value, bytes) else value.encode("utf-8")

    def read_text(self, path):
        key = str(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        value = self.files[key]
        return value.decode("utf-8") if isinstance(value, bytes) else value

    def atomic_write_text(self, path, text):
        self.atomic_writes.append(str(path))
        self.files[str(path)] = text


class Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeRunner:
    def __init__(self, create=None, evaluations=None, close_error=None):
        self.create_outcome = create or StageOutcome.success()
        self.evaluations = list(evaluations or [])
        self.create_requests = []
        self.evaluate_requests = []
        self.closed = False
        self.close_error = close_error

    def create(self, request):
        self.create_requests.append(request)
        if isinstance(self.create_outcome, BaseException):
            raise self.create_outcome
        return self.create_outcome

    def evaluate(self, request):
        self.evaluate_requests.append(request)
        outcome = self.evaluations.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "frame":
            # What the network does: every pixel moves a little.
            changed = bytes(value ^ 0x08 if (i % 4) != 3 else value
                            for i, value in enumerate(request.frame.pixels))
            return StageOutcome.frame(request.frame, changed)
        if outcome == "echo":
            # A runtime that answers success and hands the input back.
            return StageOutcome.frame(request.frame, bytes(request.frame.pixels))
        if outcome == "black":
            black = bytes(0 if (i % 4) != 3 else 255
                          for i in range(len(request.frame.pixels)))
            return StageOutcome.frame(request.frame, black)
        return outcome

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class RunnerFactory:
    def __init__(self, builders):
        self.builders = list(builders)
        self.instances = []

    def __call__(self):
        builder = self.builders.pop(0)
        runner = builder() if callable(builder) else builder
        self.instances.append(runner)
        return runner


def digest(data):
    return hashlib.sha256(data).hexdigest()


def make_key(
    fs, *, version="1.14.0", gpu=0, driver="600.01", hz=144,
    runtime=b"runtime-v1", worker=b"worker-v1",
):
    fs.files["runtime.dll"] = runtime
    fs.files["worker.exe"] = worker
    return CompatibilityKey.from_files(
        app_version=version,
        runtime_path="runtime.dll",
        worker_path="worker.exe",
        selected_gpu={"index": gpu, "luid": f"gpu-{gpu}", "name": "RTX test"},
        driver_version=driver,
        display_mode={"width": 2560, "height": 1440, "refresh_hz": hz,
                      "hdr": False, "format": "BGRA8"},
        filesystem=fs,
    )


class CompatibilityPreflightTests(unittest.TestCase):
    def setUp(self):
        self.fs = MemoryFileSystem()
        self.clock = Clock()
        self.key = make_key(self.fs)
        self.cache = CompatibilityCache(
            "compatibility.json", filesystem=self.fs, clock=self.clock,
        )

    def preflight(self, runners, *, cooldown=60):
        factory = RunnerFactory(runners)
        return CompatibilityPreflight(
            self.cache, factory, clock=self.clock,
            cooldown_seconds=cooldown,
        ), factory

    def test_key_hashes_runtime_worker_and_all_relevant_components(self):
        payload = self.key.payload()
        self.assertEqual(payload["runtime_sha256"], digest(b"runtime-v1"))
        self.assertEqual(payload["worker_sha256"], digest(b"worker-v1"))

        variants = [
            # A version change alone must move the digest: the variant uses
            # the previous release, which is exactly an app upgrade.
            make_key(self.fs, version="1.13.0"),
            make_key(self.fs, gpu=1),
            make_key(self.fs, driver="601.00"),
            make_key(self.fs, hz=60),
        ]
        variants.append(make_key(self.fs, runtime=b"runtime-v2"))
        variants.append(make_key(self.fs, worker=b"worker-v2"))
        self.assertTrue(all(item.digest != self.key.digest for item in variants))

    def test_success_without_any_change_is_not_a_pass(self):
        # A runtime that answers success and returns the picture untouched is
        # not running on this card (an unsupported card on a patched runtime
        # "starts without processing the picture"). It is a verdict, cached
        # like UNSUPPORTED - not a pass.
        for outcome in ("echo", "black"):
            with self.subTest(outcome=outcome):
                cache = CompatibilityCache(
                    f"compat-{outcome}.json", filesystem=self.fs, clock=self.clock)
                runner = FakeRunner(evaluations=[outcome, outcome, outcome])
                preflight = CompatibilityPreflight(
                    cache, RunnerFactory([runner]), clock=self.clock)
                result = preflight.run(self.key)
                self.assertFalse(result.is_pass)
                self.assertEqual(result.status, CompatibilityStatus.UNSUPPORTED)
                self.assertEqual(result.reason, StageStatus.NO_EFFECT.value)
                self.assertEqual(result.stage, "effect")
                self.assertTrue(runner.closed)

    def test_one_changed_frame_is_enough_for_a_pass(self):
        runner = FakeRunner(evaluations=["echo", "frame", "echo"])
        preflight, _ = self.preflight([runner])
        result = preflight.run(self.key)
        self.assertTrue(result.is_pass)

    def test_pass_requires_every_real_evaluate_frame_and_is_cached(self):
        runner = FakeRunner(evaluations=["frame", "frame", "frame"])
        preflight, factory = self.preflight([runner])
        result = preflight.run(self.key)

        self.assertTrue(result.is_pass)
        self.assertEqual(result.status, CompatibilityStatus.PASS)
        self.assertEqual(result.score, "3/3")
        self.assertEqual(result.stage, "complete")
        self.assertEqual(len(runner.evaluate_requests), 3)
        self.assertTrue(runner.closed)
        for request in runner.evaluate_requests:
            self.assertEqual(request.input_source, "synthetic")
            self.assertFalse(request.desktop_capture)
            self.assertFalse(request.presentation_window)
        create = runner.create_requests[0]
        self.assertEqual(create.input_source, "synthetic")
        self.assertFalse(create.desktop_capture)
        self.assertFalse(create.presentation_window)

        cached = preflight.run(self.key)
        self.assertTrue(cached.cached)
        self.assertTrue(cached.is_pass)
        self.assertEqual(len(factory.instances), 1)

    def test_pass_is_not_published_when_probe_cannot_be_reaped(self):
        runner = FakeRunner(
            evaluations=["frame", "frame", "frame"],
            close_error=RuntimeError("worker still alive"),
        )
        preflight, _ = self.preflight([runner])
        result = preflight.run(self.key)
        self.assertTrue(runner.closed)
        self.assertFalse(result.is_pass)
        self.assertEqual(result.status, CompatibilityStatus.QUARANTINED)
        self.assertEqual(result.stage, "close")
        cached = self.cache.get(self.key)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.status, CompatibilityStatus.QUARANTINED)

    def test_changed_key_runs_a_fresh_test(self):
        first = FakeRunner(evaluations=["frame"] * 3)
        second = FakeRunner(evaluations=["frame"] * 3)
        preflight, factory = self.preflight([first, second])
        self.assertTrue(preflight.run(self.key).is_pass)
        changed = make_key(self.fs, driver="601.00")
        self.assertTrue(preflight.run(changed).is_pass)
        self.assertEqual(len(factory.instances), 2)

    def test_skip_unknown_not_run_and_missing_frame_never_pass(self):
        bad_outcomes = [
            StageOutcome(StageStatus.SKIP),
            StageOutcome(StageStatus.UNKNOWN),
            StageOutcome(StageStatus.NOT_RUN),
            StageOutcome(StageStatus.SUCCESS),
        ]
        for bad in bad_outcomes:
            with self.subTest(status=bad.status):
                fs = MemoryFileSystem()
                clock = Clock()
                key = make_key(fs)
                cache = CompatibilityCache("cache", filesystem=fs, clock=clock)
                runner = FakeRunner(evaluations=["frame", bad])
                preflight = CompatibilityPreflight(
                    cache, lambda runner=runner: runner, clock=clock,
                )
                result = preflight.run(key)
                self.assertFalse(result.is_pass)
                self.assertEqual(result.status, CompatibilityStatus.FAILED)
                self.assertEqual(result.score, "1/2")
                self.assertEqual(result.stage, "evaluate[1]")
                # Ordinary failed/ambiguous results are deliberately not cached.
                self.assertIsNone(cache.get(key))

    def test_unsupported_is_deterministically_cached(self):
        runner = FakeRunner(create=UnsupportedError("unsupported"))
        preflight, factory = self.preflight([runner])
        result = preflight.run(self.key)
        self.assertEqual(result.status, CompatibilityStatus.UNSUPPORTED)
        self.assertEqual(result.stage, "create")
        self.assertEqual(result.score, "0/0")
        cached = preflight.run(self.key)
        self.assertTrue(cached.cached)
        self.assertEqual(cached.status, CompatibilityStatus.UNSUPPORTED)
        self.assertEqual(len(factory.instances), 1)

    def test_timeout_crash_device_loss_and_tdr_are_quarantined(self):
        failures = [
            TimeoutError("C:\\Users\\Alice\\private\\runtime.dll"),
            StageOutcome(StageStatus.CRASH),
            StageOutcome(StageStatus.DEVICE_LOST),
            StageOutcome(StageStatus.TDR),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                fs = MemoryFileSystem()
                clock = Clock()
                key = make_key(fs)
                cache = CompatibilityCache("cache", filesystem=fs, clock=clock)
                runner = FakeRunner(evaluations=[failure])
                preflight = CompatibilityPreflight(
                    cache, lambda runner=runner: runner, clock=clock,
                    cooldown_seconds=90,
                )
                result = preflight.run(key)
                self.assertEqual(result.status, CompatibilityStatus.QUARANTINED)
                self.assertEqual(result.score, "0/1")
                self.assertEqual(result.quarantine_until, 1090.0)
                self.assertTrue(cache.get(key).cached)
                serialized = fs.files["cache"]
                self.assertNotIn("Alice", serialized)
                self.assertNotIn("private", serialized)

    def test_runner_start_crash_is_create_stage_quarantine(self):
        def crashed_factory():
            raise RuntimeError("C:\\Users\\Alice\\worker launch failed")

        preflight = CompatibilityPreflight(
            self.cache, crashed_factory, clock=self.clock, cooldown_seconds=45,
        )
        result = preflight.run(self.key)
        self.assertEqual(result.status, CompatibilityStatus.QUARANTINED)
        self.assertEqual(result.stage, "create")
        self.assertEqual(result.score, "0/0")
        self.assertNotIn("Alice", self.fs.files["compatibility.json"])

    def test_quarantine_expiry_and_manual_retry(self):
        timeout_runner = FakeRunner(evaluations=[TimeoutError("timeout")])
        pass_after_expiry = FakeRunner(evaluations=["frame"] * 3)
        preflight, factory = self.preflight(
            [timeout_runner, pass_after_expiry], cooldown=30,
        )
        self.assertEqual(
            preflight.run(self.key).status, CompatibilityStatus.QUARANTINED,
        )
        self.clock.advance(29)
        self.assertTrue(preflight.run(self.key).cached)
        self.assertEqual(len(factory.instances), 1)
        self.clock.advance(1)
        self.assertTrue(preflight.run(self.key).is_pass)
        self.assertEqual(len(factory.instances), 2)

        # Manual retry removes even a durable PASS and immediately reruns.
        retry_runner = FakeRunner(evaluations=["frame"] * 3)
        preflight.runner_factory.builders.append(retry_runner)
        retried = preflight.manual_retry(self.key)
        self.assertTrue(retried.is_pass)
        self.assertFalse(retried.cached)
        self.assertEqual(len(factory.instances), 3)

    def test_manual_clear_removes_unsupported_record(self):
        unsupported = FakeRunner(create=StageOutcome(StageStatus.UNSUPPORTED))
        preflight, _ = self.preflight([unsupported])
        preflight.run(self.key)
        self.assertTrue(preflight.clear_for_retry(self.key))
        self.assertIsNone(self.cache.get(self.key))

    def test_broken_json_is_ignored_and_repaired_by_atomic_write(self):
        self.fs.files["compatibility.json"] = "{ definitely broken"
        runner = FakeRunner(evaluations=["frame"] * 3)
        preflight, _ = self.preflight([runner])
        result = preflight.run(self.key)
        self.assertTrue(result.is_pass)
        repaired = json.loads(self.fs.files["compatibility.json"])
        self.assertEqual(repaired["schema"], CACHE_SCHEMA)
        self.assertIn(self.key.digest, repaired["entries"])
        self.assertGreaterEqual(len(self.fs.atomic_writes), 1)

    def test_cache_contains_no_artifact_paths_or_exception_text(self):
        self.fs.files["C:\\private\\runtime.dll"] = b"runtime"
        self.fs.files["D:\\secret\\worker.exe"] = b"worker"
        key = CompatibilityKey.from_files(
            app_version="1.14.0",
            runtime_path="C:\\private\\runtime.dll",
            worker_path="D:\\secret\\worker.exe",
            selected_gpu={"index": 0, "name": "RTX test"},
            driver_version="600.01",
            display_mode={"width": 1920, "height": 1080, "hz": 60},
            filesystem=self.fs,
        )
        runner = FakeRunner(create=RuntimeError("C:\\Users\\Alice\\token.txt"))
        preflight, _ = self.preflight([runner])
        self.assertEqual(
            preflight.run(key).status, CompatibilityStatus.QUARANTINED,
        )
        text = self.fs.files["compatibility.json"]
        for private in ("private", "secret", "Alice", "token.txt", "runtime.dll", "worker.exe"):
            self.assertNotIn(private, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
