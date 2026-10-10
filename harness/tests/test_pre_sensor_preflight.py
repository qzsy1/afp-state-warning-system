from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from visualization_app.pre_sensor_preflight import (
    ExternalEnvironmentBlocked,
    PhysicalSensorPending,
    Probe,
    ProbeOutcome,
    render_preflight_json,
    render_preflight_markdown,
    run_preflight,
    write_preflight_report,
)


ROOT = Path(__file__).resolve().parents[2]


class PreSensorPreflightOrchestrationTests(unittest.TestCase):
    def test_one_report_drives_stable_json_and_markdown(self) -> None:
        probes = (
            Probe(
                "release.manifest",
                "Candidate manifest",
                lambda _context: ProbeOutcome("passed", "sha256 verified"),
            ),
            Probe(
                "environment.target_mysql",
                "Target MySQL",
                lambda _context: (_ for _ in ()).throw(
                    ExternalEnvironmentBlocked(
                        "target instance was not configured",
                        "Provide AFP_MYSQL_PROFILE and rerun",
                    )
                ),
                external_environment=True,
            ),
            Probe(
                "physical.device_identity_ports",
                "Physical device identity",
                lambda _context: (_ for _ in ()).throw(
                    PhysicalSensorPending(
                        "sensor is not attached", "Verify the physical identity on site"
                    )
                ),
                physical_sensor=True,
            ),
        )
        report = run_preflight(
            probes,
            context={"root": "candidate"},
            candidate_identity="2.0.5-rc.1-test",
            mode="remote_helper",
        )
        payload = json.loads(render_preflight_json(report))
        markdown = render_preflight_markdown(report)

        self.assertEqual(payload["candidate_identity"], "2.0.5-rc.1-test")
        self.assertEqual(payload["mode"], "remote_helper")
        self.assertEqual(payload["summary"]["outcome"], "blocked")
        self.assertEqual(
            [item["check_id"] for item in payload["checks"]],
            [item.check_id for item in probes],
        )
        for item in payload["checks"]:
            self.assertIn(item["check_id"], markdown)
            self.assertIn(item["status"], markdown)
        blocked = payload["checks"][1]
        self.assertTrue(blocked["external_environment"])
        self.assertTrue(blocked["remediation"])

    def test_timeout_and_exception_are_isolated_and_failed(self) -> None:
        def slow(_context):
            time.sleep(0.2)
            return ProbeOutcome("passed", "too late")

        probes = (
            Probe("runtime.timeout", "Timeout", slow, timeout_seconds=0.01),
            Probe(
                "runtime.exception",
                "Exception",
                lambda _context: (_ for _ in ()).throw(RuntimeError("boom")),
            ),
            Probe(
                "runtime.after_failure",
                "After failure",
                lambda _context: ProbeOutcome("passed", "still ran"),
            ),
        )
        report = run_preflight(
            probes,
            context={},
            candidate_identity="candidate",
            mode="local_direct",
        )
        statuses = {item.check_id: item.status for item in report.checks}
        self.assertEqual(statuses["runtime.timeout"], "failed")
        self.assertEqual(statuses["runtime.exception"], "failed")
        self.assertEqual(statuses["runtime.after_failure"], "passed")
        self.assertLess(report.duration_seconds, 0.15)

    def test_invalid_status_and_physical_classification_fail_closed(self) -> None:
        invalid = Probe(
            "runtime.bad",
            "Bad outcome",
            lambda _context: ProbeOutcome("unknown", "bad"),
        )
        wrong_physical = Probe(
            "storage.save_directory",
            "Storage",
            lambda _context: (_ for _ in ()).throw(
                PhysicalSensorPending("missing", "connect it")
            ),
        )
        report = run_preflight(
            (invalid, wrong_physical),
            context={},
            candidate_identity="candidate",
            mode="local_direct",
        )
        self.assertEqual([item.status for item in report.checks], ["failed", "failed"])

    def test_report_is_written_as_an_atomic_json_markdown_pair(self) -> None:
        report = run_preflight(
            (
                Probe(
                    "release.manifest",
                    "Manifest",
                    lambda _context: ProbeOutcome("passed", "verified"),
                ),
            ),
            context={},
            candidate_identity="candidate",
            mode="local_direct",
        )
        with tempfile.TemporaryDirectory() as temporary:
            json_path, markdown_path = write_preflight_report(
                report, Path(temporary), basename="preflight"
            )
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["checks"][0]["check_id"], "release.manifest")
            self.assertIn("release.manifest", markdown_path.read_text(encoding="utf-8"))
            self.assertFalse(any(Path(temporary).glob("*.tmp")))

    def test_candidate_bootstrap_exposes_one_click_preflight_arguments(self) -> None:
        source = (ROOT / "modular_runtime" / "app" / "bootstrap.py").read_text(
            encoding="utf-8"
        )
        for argument in (
            "--pre-sensor-preflight",
            "--pre-sensor-mode",
            "--pre-sensor-config",
            "--pre-sensor-output",
        ):
            self.assertIn(argument, source)
        self.assertIn("raise SystemExit(1)", source)
        self.assertNotIn(
            "pre-sensor readiness did not pass; inspect the generated report",
            source,
        )
        wrapper = ROOT / "pre_sensor" / "run-preflight.ps1"
        self.assertTrue(wrapper.is_file())
        wrapper_source = wrapper.read_text(encoding="utf-8")
        self.assertIn("--pre-sensor-preflight", wrapper_source)
        self.assertIn("Start-Process", wrapper_source)
        self.assertIn(".ExitCode", wrapper_source)


if __name__ == "__main__":
    unittest.main()
