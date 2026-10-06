import os
import sys
import tempfile
import unittest
from pathlib import Path

from harness.engine.models import CheckSpec
from harness.engine.runner import OUTPUT_LIMIT, run_check


def command_check(command: list[str], **overrides: object) -> CheckSpec:
    values: dict[str, object] = {
        "id": "runner-check",
        "title": "Runner check",
        "kind": "command",
        "command": tuple(command),
        "cwd": ".",
        "timeout_seconds": 2.0,
        "profiles": ("quick",),
        "environments": ("local",),
        "blocking": True,
        "evidence_tier": "automated",
        "requirements": ("REQ-HARNESS",),
        "change_patterns": (),
    }
    values.update(overrides)
    return CheckSpec(**values)


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_success_records_output_and_zero_exit(self) -> None:
        check = command_check(["{python}", "-c", "print('ready')"])
        result = run_check(check, self.root, os.environ)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "ready")
        self.assertEqual(result.command[0], sys.executable)
        self.assertGreaterEqual(result.duration_seconds, 0)

    def test_nonzero_exit_is_failed(self) -> None:
        check = command_check(["{python}", "-c", "import sys; print('bad'); sys.exit(7)"])
        result = run_check(check, self.root, os.environ)
        self.assertEqual((result.status, result.exit_code), ("failed", 7))
        self.assertIn("bad", result.stdout)

    def test_timeout_is_recorded_and_fail_closed(self) -> None:
        check = command_check(
            ["{python}", "-c", "import time; print('start', flush=True); time.sleep(5)"],
            timeout_seconds=0.05,
        )
        result = run_check(check, self.root, os.environ)
        self.assertEqual(result.status, "timed_out")
        self.assertIsNone(result.exit_code)
        self.assertIn("timed out", result.error.lower())

    def test_missing_executable_is_not_started(self) -> None:
        check = command_check(["executable-that-does-not-exist-4f7f"])
        result = run_check(check, self.root, os.environ)
        self.assertEqual(result.status, "not_started")
        self.assertIsNone(result.exit_code)
        self.assertTrue(result.error)

    def test_invalid_working_directory_is_not_started(self) -> None:
        check = command_check(["{python}", "-c", "pass"], cwd="missing")
        result = run_check(check, self.root, os.environ)
        self.assertEqual(result.status, "not_started")
        self.assertIn("working directory", result.error)

    def test_non_utf8_output_is_replaced_instead_of_crashing(self) -> None:
        check = command_check(["{python}", "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"])
        result = run_check(check, self.root, os.environ)
        self.assertEqual(result.status, "passed")
        self.assertIn("\ufffd", result.stdout)

    def test_output_is_bounded(self) -> None:
        check = command_check(["{python}", "-c", f"print('x' * {OUTPUT_LIMIT + 1000})"])
        result = run_check(check, self.root, os.environ)
        self.assertLessEqual(len(result.stdout), OUTPUT_LIMIT + 100)
        self.assertIn("truncated", result.stdout)

    def test_field_check_is_pending_and_never_executes(self) -> None:
        check = command_check(
            [],
            kind="field",
            evidence_tier="field",
            title="Real hardware acceptance",
        )
        result = run_check(check, self.root, os.environ)
        self.assertEqual(result.status, "pending_field")
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.command, ())

    def test_inherited_environment_is_available_to_command(self) -> None:
        check = command_check(
            ["{python}", "-c", "import os; print(os.environ['HARNESS_TEST_VALUE'])"]
        )
        environment = dict(os.environ)
        environment["HARNESS_TEST_VALUE"] = "visible"
        result = run_check(check, self.root, environment)
        self.assertEqual(result.stdout.strip(), "visible")

    def test_command_variable_selects_the_requested_release_executable(self) -> None:
        check = command_check(["{baseline_exe}", "-c", "print('selected-baseline')"])
        result = run_check(
            check,
            self.root,
            os.environ,
            command_variables={"{baseline_exe}": sys.executable},
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.command[0], sys.executable)
        self.assertEqual(result.stdout.strip(), "selected-baseline")

    def test_unresolved_command_variable_fails_closed(self) -> None:
        check = command_check(["{baseline_exe}", "--self-test"])
        result = run_check(check, self.root, os.environ)
        self.assertEqual(result.status, "not_started")
        self.assertIn("unresolved command variable", result.error)


if __name__ == "__main__":
    unittest.main()
