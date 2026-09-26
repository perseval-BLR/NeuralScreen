"""Pure regressions for test-runner routing, status and aggregation."""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autocheck
import run_tests as runner


def result(group: str, status: str, label: str = "check") -> dict:
    return {"group": group, "status": status, "label": label, "took": 0.0}


class ResultClassificationTests(unittest.TestCase):
    def test_canonical_skip_is_not_pass(self) -> None:
        self.assertEqual(
            runner.classify_result(0, "setup\nSKIP: no compatible device\n"),
            runner.STATUS_SKIP,
        )
        self.assertEqual(
            runner.classify_result(0, stderr="  [SKIP] optional runtime absent"),
            runner.STATUS_SKIP,
        )
        self.assertEqual(
            runner.classify_result(0, "SKIP\n"),
            runner.STATUS_SKIP,
        )

    def test_noncanonical_skip_words_remain_pass(self) -> None:
        for output in (
            "OK: optional branch skipped",
            "prefix SKIP: this is not a result line",
            "all checks PASS",
        ):
            with self.subTest(output=output):
                self.assertEqual(
                    runner.classify_result(0, output), runner.STATUS_PASS)

    def test_failure_timeout_and_error_take_precedence(self) -> None:
        self.assertEqual(
            runner.classify_result(3, "SKIP: followed by a crash"),
            runner.STATUS_FAIL,
        )
        self.assertEqual(
            runner.classify_result(None, timed_out=True),
            runner.STATUS_TIMEOUT,
        )
        self.assertEqual(
            runner.classify_result(None, error=True),
            runner.STATUS_ERROR,
        )


class AutocheckLogContractTests(unittest.TestCase):
    def test_honest_nr_rate_line_is_the_gui_liveness_contract(self) -> None:
        line = ("[main] NR ON | NR  59.8 fps | frames 321 | "
                "work 1664x936 | scene 0.012")
        self.assertIn(autocheck.NR_FRAME_MARKER, line)
        self.assertEqual(autocheck.nr_stats(line), [(59.8, 321)])
        self.assertEqual(autocheck.nr_stats(
            "[main] NR ON | FPS 59.8 | frames 321"), [])


class AggregationTests(unittest.TestCase):
    def test_exact_per_group_and_global_counts(self) -> None:
        results = [
            result(runner.GROUP_UNIT_STATIC, runner.STATUS_PASS, "unit-pass"),
            result(runner.GROUP_UNIT_STATIC, runner.STATUS_SKIP, "unit-skip"),
            result(runner.GROUP_WARP, runner.STATUS_SKIP, "warp-skip"),
            result(runner.GROUP_GPU, runner.STATUS_FAIL, "gpu-fail"),
            result(runner.GROUP_GPU, runner.STATUS_TIMEOUT, "gpu-timeout"),
            result(runner.GROUP_GUI_E2E, runner.STATUS_ERROR, "gui-error"),
        ]

        summary = runner.aggregate_results(results)
        self.assertEqual(
            summary["groups"][runner.GROUP_UNIT_STATIC],
            {"PASS": 1, "FAIL": 0, "SKIP": 1, "ERROR": 0, "TIMEOUT": 0},
        )
        self.assertEqual(
            summary["groups"][runner.GROUP_WARP],
            {"PASS": 0, "FAIL": 0, "SKIP": 1, "ERROR": 0, "TIMEOUT": 0},
        )
        self.assertEqual(
            summary["groups"][runner.GROUP_GPU],
            {"PASS": 0, "FAIL": 1, "SKIP": 0, "ERROR": 0, "TIMEOUT": 1},
        )
        self.assertEqual(
            summary["groups"][runner.GROUP_GUI_E2E],
            {"PASS": 0, "FAIL": 0, "SKIP": 0, "ERROR": 1, "TIMEOUT": 0},
        )
        self.assertEqual(
            summary["global"],
            {"PASS": 1, "FAIL": 1, "SKIP": 2, "ERROR": 1, "TIMEOUT": 1},
        )

    def test_exit_is_fail_closed_but_skip_is_not_failure(self) -> None:
        self.assertEqual(runner.exit_code_for_results([
            result(runner.GROUP_WARP, runner.STATUS_SKIP),
        ]), 0)
        for status in runner.BLOCKING_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(runner.exit_code_for_results([
                    result(runner.GROUP_UNIT_STATIC, status),
                ]), 1)
        self.assertEqual(runner.exit_code_for_results([]), 1)

    def test_rendered_summary_has_every_group_and_global_counts(self) -> None:
        results = [
            result(runner.GROUP_UNIT_STATIC, runner.STATUS_PASS, "unit"),
            result(runner.GROUP_WARP, runner.STATUS_SKIP, "warp"),
            result(runner.GROUP_GPU, runner.STATUS_FAIL, "gpu"),
            result(runner.GROUP_GUI_E2E, runner.STATUS_TIMEOUT, "gui"),
        ]
        saved_settles = list(runner.SETTLES)
        runner.SETTLES.clear()
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                runner.print_summary(results)
        finally:
            runner.SETTLES[:] = saved_settles

        report = output.getvalue()
        self.assertIn(
            "unit/static PASS=1 FAIL=0 SKIP=0 ERROR=0 TIMEOUT=0", report)
        self.assertIn(
            "WARP        PASS=0 FAIL=0 SKIP=1 ERROR=0 TIMEOUT=0", report)
        self.assertIn(
            "GPU         PASS=0 FAIL=1 SKIP=0 ERROR=0 TIMEOUT=0", report)
        self.assertIn(
            "GUI-E2E     PASS=0 FAIL=0 SKIP=0 ERROR=0 TIMEOUT=1", report)
        self.assertIn(
            "GLOBAL      PASS=1 FAIL=1 SKIP=1 ERROR=0 TIMEOUT=1", report)
        self.assertIn("RESULT: FAIL - 2 blocking result(s)", report)


class RoutingTests(unittest.TestCase):
    def test_inventory_is_explicit_and_disjoint(self) -> None:
        routed: list[str] = []
        for group, names in runner.TEST_GROUPS.items():
            self.assertIn(group, runner.GROUP_ORDER)
            for name in names:
                self.assertEqual(runner.group_for_test(f"tests/{name}"), group)
                routed.append(name)
        self.assertEqual(len(routed), len(set(routed)))
        with self.assertRaisesRegex(ValueError, "unrouted test"):
            runner.group_for_test("tests/test_not_classified.py")

    def test_representative_tests_reach_all_four_groups(self) -> None:
        self.assertEqual(runner.group_for_test("test_config.py"),
                         runner.GROUP_UNIT_STATIC)
        self.assertEqual(runner.group_for_test("test_hdr_shaders.py"),
                         runner.GROUP_WARP)
        self.assertEqual(runner.group_for_test("test_nr_small.py"),
                         runner.GROUP_GPU)
        self.assertEqual(runner.group_for_test("test_taskbar_window.py"),
                         runner.GROUP_GUI_E2E)

    def test_cli_switches_keep_their_existing_scope(self) -> None:
        tests = ["test_config.py", "test_hdr_shaders.py",
                 "test_nr_small.py", "test_taskbar_window.py"]

        default = runner.build_jobs(tests, [])
        self.assertEqual([job["label"] for job in default], [
            "static checks", "test_config.py", "test_hdr_shaders.py",
            "test_nr_small.py", "smoke", "test_taskbar_window.py",
        ])

        no_smoke = runner.build_jobs(tests, ["--no-smoke"])
        self.assertNotIn("smoke", [job["label"] for job in no_smoke])

        only = runner.build_jobs(tests, ["--only", "nr_small"])
        self.assertEqual([job["label"] for job in only], ["test_nr_small.py"])

        only_gui = runner.build_jobs(
            tests, ["--only", "nr_small", "--gui"])
        self.assertEqual([job["label"] for job in only_gui],
                         ["test_nr_small.py", "GUI cycle"])

        gui = runner.build_jobs(tests, ["--gui"])
        self.assertEqual(gui[-1]["label"], "GUI cycle")


if __name__ == "__main__":
    unittest.main(verbosity=2)
