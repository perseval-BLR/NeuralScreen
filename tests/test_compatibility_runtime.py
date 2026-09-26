"""Production adapter: synthetic-only native preflight and startup ordering."""
from __future__ import annotations

import sys
import subprocess
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np


BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "app"))  # the modules live in app/

import compatibility_runtime as runtime  # noqa: E402
from compatibility import (  # noqa: E402
    CompatibilityKey, CreateRequest, EvaluateRequest, StageStatus,
)
from protocol import (  # noqa: E402
    CREATE_CATEGORY_FAILED, CREATE_CATEGORY_NONE,
    CREATE_CATEGORY_UNSUPPORTED,
)


KEY = CompatibilityKey(
    "1.14.0", "a" * 64, "b" * 64,
    {"index": 0}, "driver", {"width": 640, "height": 360},
)


class _Worker:
    def __init__(self, code=None, waits=None):
        self.code = code
        self.stdin = mock.Mock()
        self.stdin.closed = False
        self.waits = list(waits or [code or 0])
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.code

    def wait(self, timeout):
        value = self.waits.pop(0)
        if isinstance(value, BaseException):
            raise value
        self.code = value
        return value

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class _Reader:
    def __init__(self, pixels=None, *, error=None,
                 ack=(1, 1, CREATE_CATEGORY_NONE)):
        self.pixels = pixels
        self.last_ngx_result = 1
        self.error = error
        self.ack = ack

    def wait_create_ack(self, timeout):
        assert timeout == runtime.CREATE_TIMEOUT_SECONDS
        if self.error is not None:
            raise self.error
        return self.ack

    def recv(self, _index, timeout):
        assert timeout == runtime.EVALUATE_TIMEOUT_SECONDS
        if self.error is not None:
            raise self.error
        return self.pixels


class RuntimeAdapterTests(unittest.TestCase):
    def request(self):
        frame = runtime.production_synthetic_frames()[0]
        return CreateRequest(KEY, frame.width, frame.height), EvaluateRequest(KEY, frame)

    def runner(self, logs, reader, worker=None):
        worker = worker or _Worker()
        stop = mock.Mock()
        start = mock.Mock(return_value=(worker, logs, reader, stop))
        sender = mock.Mock()
        patches = (
            mock.patch.object(runtime, "start_worker", start),
            mock.patch.object(runtime, "send_frame", sender),
        )
        return runtime.NativeSelfTestRunner({
            "style": 1, "auto_mask": 0, "intensity": 1.0,
            "local_tone": 0.5, "local_structure": 1.0,
            "skin_structure": -1.0,
        }), patches, start, stop, sender

    def test_full_create_and_evaluate(self):
        create, evaluate = self.request()
        pixels = np.zeros((evaluate.frame.height, evaluate.frame.width, 4), np.uint8)
        runner, patches, start, shutdown, sender = self.runner(
            ["[pure] direct feature 18 ready: 640x360"], _Reader(pixels))
        with patches[0], patches[1]:
            self.assertEqual(runner.create(create).status, StageStatus.SUCCESS)
            for frame in runtime.production_synthetic_frames():
                outcome = runner.evaluate(EvaluateRequest(KEY, frame))
                self.assertEqual(outcome.status, StageStatus.SUCCESS)
                self.assertEqual(len(outcome.output_pixels), 640 * 360 * 4)
                self.assertEqual(outcome.output_width, 640)
                self.assertEqual(outcome.output_height, 360)
            runner.close()
        start.assert_called_once()
        self.assertEqual(sender.call_count, 3)
        self.assertEqual([call.args[4] for call in sender.call_args_list],
                         [True, False, False])

    def test_passthrough_never_passes(self):
        create, evaluate = self.request()
        runner, patches, *_ = self.runner(
            ["[video] NR feature unavailable (0xBAD00001) - SAFE PASSTHROUGH"],
            _Reader(None, ack=(0, 0xBAD00001, CREATE_CATEGORY_UNSUPPORTED)))
        with patches[0], patches[1]:
            self.assertEqual(runner.create(create).status, StageStatus.UNSUPPORTED)
            runner.close()

        # Text resembling an unsupported verdict is never enough: only the
        # explicit native category plus the exact result can classify it.
        runner, patches, *_ = self.runner(
            ["[video] NR feature unavailable (0xBAD00001) - SAFE PASSTHROUGH"],
            _Reader(None, ack=(0, 0xBAD00002, CREATE_CATEGORY_FAILED)))
        with patches[0], patches[1]:
            self.assertEqual(runner.create(create).status, StageStatus.ERROR)
            runner.close()

        pixels = np.zeros((evaluate.frame.height, evaluate.frame.width, 4), np.uint8)
        runner, patches, *_ = self.runner(
            ["[pure] direct feature 18 ready: 640x360"],
            _Reader(pixels))
        with patches[0], patches[1]:
            self.assertEqual(runner.create(create).status, StageStatus.SUCCESS)
            self.assertEqual(runner.evaluate(evaluate).status, StageStatus.SUCCESS)
            runner.close()

    def test_device_loss_and_timeout_are_explicit(self):
        create, _evaluate = self.request()
        runner, patches, *_ = self.runner(
            ["[dred] device removed reason 0x887A0005"],
            _Reader(None, ack=(0, 0xBAD00002, CREATE_CATEGORY_FAILED)))
        with patches[0], patches[1]:
            self.assertEqual(runner.create(create).status, StageStatus.DEVICE_LOST)
            runner.close()

        runner, patches, *_ = self.runner([], _Reader(None, error=TimeoutError()))
        with patches[0], patches[1]:
            self.assertEqual(runner.create(create).status, StageStatus.TIMEOUT)
            runner.close()

    def test_probe_process_is_terminated_killed_and_reaped(self):
        worker = _Worker(waits=[
            subprocess.TimeoutExpired("worker", 2.0),
            subprocess.TimeoutExpired("worker", 2.0),
            9,
        ])
        stop = mock.Mock()
        runtime._reap_worker(worker, stop)
        stop.set.assert_called_once()
        worker.stdin.close.assert_called_once()
        self.assertTrue(worker.terminated)
        self.assertTrue(worker.killed)
        self.assertEqual(worker.code, 9)

    def test_synthetic_frames_are_deterministic_and_valid(self):
        first = runtime.production_synthetic_frames()
        second = runtime.production_synthetic_frames()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)
        self.assertEqual({len(frame.pixels) for frame in first}, {640 * 360 * 4})
        with self.assertRaises(ValueError):
            runtime.production_synthetic_frames(16, 12)

    def test_key_describes_the_canonical_probe_not_the_desktop(self):
        st = SimpleNamespace(
            cfg={"gpu": 2, "hdr": True, "width": 7680, "height": 4320},
            environment={"driver": "777.01"},
        )
        with mock.patch.object(runtime, "_selected_gpu", return_value={"index": 2}), \
                mock.patch.object(runtime, "_runtime_route", return_value={"route": "x"}), \
                mock.patch.object(runtime.CompatibilityKey, "from_files",
                                  return_value=KEY) as factory:
            self.assertIs(runtime.build_key(st), KEY)
        display = factory.call_args.kwargs["display_mode"]
        self.assertEqual(display["width"], runtime.PREFLIGHT_WIDTH)
        self.assertEqual(display["height"], runtime.PREFLIGHT_HEIGHT)
        self.assertEqual(display["pixel_format"], "RGBA8")
        self.assertNotIn("hdr", display)
        self.assertNotIn("monitor", display)

    def test_capture_opens_strictly_after_preflight(self):
        source = (BASE / "main.py").read_text(encoding="utf-8")
        order = [
            source.index("startup.configure(st)"),
            source.index("compatibility_runtime.startup_gate(st)"),
            source.index("startup.open_capture(st)"),
            source.index("startup.bring_up(st)"),
        ]
        self.assertEqual(order, sorted(order))
        startup_source = (BASE / "app" / "startup.py").read_text(encoding="utf-8")
        configure = startup_source.split("def configure(st)", 1)[1].split(
            "\ndef open_capture(st)", 1)[0]
        self.assertNotIn("ScreenCapture(", configure)
        self.assertIn("ScreenCapture(", startup_source.split(
            "def open_capture(st)", 1)[1].split("\ndef bring_up(st)", 1)[0])

    def test_every_production_worker_start_has_a_pass_guard(self):
        for name in ("startup.py", "pipeline.py", "commands.py", "main.py"):
            # main.py is the entry point at the root; the rest live in app/.
            where = BASE / name if name == "main.py" else BASE / "app" / name
            source = where.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(source):
                # A comment that mentions the call is not a call site: adding
                # an explanatory comment introduced a phantom site here and
                # failed the check on correct code.
                if line.lstrip().startswith("#"):
                    continue
                if ("start_worker(" not in line and "restart_worker(" not in line):
                    continue
                if line.lstrip().startswith(("def start_worker", "def restart_worker")):
                    continue
                # restart_worker's own tail calls start_worker after its
                # caller's guard and is not a production call site itself.
                #
                # Found by WHICH FUNCTION IT IS IN, not by line number. This
                # used to read `index < 210`, and adding a comment near the
                # top of pipeline.py pushed that tail call to line 211 and
                # failed a release on correct code. A magic line number is a
                # landmine under every edit above it.
                enclosing = ""
                for back in range(index, -1, -1):
                    stripped = source[back]
                    if stripped.startswith("def ") or stripped.startswith("async def "):
                        enclosing = stripped
                        break
                if name == "pipeline.py" and enclosing.startswith("def restart_worker"):
                    continue
                window = "\n".join(source[max(0, index - 14):index])
                self.assertRegex(
                    window, r"require_compatibility(?:_pass)?\(st\)",
                    f"{name}:{index + 1} starts a worker without PASS",
                )


if __name__ == "__main__":
    unittest.main()
