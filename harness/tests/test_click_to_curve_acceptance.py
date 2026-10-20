from __future__ import annotations

import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness" / "tools"))

from accept_click_to_curve import (  # noqa: E402
    build_endurance_snapshot,
    formal_acceptance_status,
    idle_wait_seconds,
    listening_runtime_ports,
    statistics,
    validate_endurance,
    validate_configuration,
    validate_readiness_reuse,
    validate_round_trace,
    validate_stop_status,
)


EXPECTED_BY_INTERFACE = {
    "thermocouple_64ch": [f"温度{index}" for index in range(1, 65)],
    "plc_process": ["温度", "压力", "张力"],
    "abb_motion": ["线速度", "ABB_X", "ABB_Y", "ABB_Z"],
    "esr1001_strain": [f"CH{index}_ADC_signed" for index in range(1, 5)],
}
EXPECTED_CHANNELS = [
    *EXPECTED_BY_INTERFACE["plc_process"],
    *EXPECTED_BY_INTERFACE["abb_motion"],
    *EXPECTED_BY_INTERFACE["thermocouple_64ch"],
    *EXPECTED_BY_INTERFACE["esr1001_strain"],
]


def valid_fixture() -> tuple[dict, dict]:
    generation = "generation"
    request_id = "click"
    observations = {}
    presentations = {}
    for interface_id, names in EXPECTED_BY_INTERFACE.items():
        for index, name in enumerate(names):
            observations[name] = [
                {
                    "interface_id": interface_id,
                    "source_sequence": index * 2 + offset,
                    "received_monotonic": 101.0 + offset / 10,
                    "capture_generation": generation,
                    "start_request_id": request_id,
                    "quality": "measured_new",
                    "after_start": True,
                    "protocol_ok": True,
                    "stable": True,
                }
                for offset in (1, 2)
            ]
            if interface_id == "thermocouple_64ch":
                group = (int(name.removeprefix("温度")) - 1) // 8
                presentation_channels = [f"温度{group * 8 + item}" for item in range(1, 9)]
                surface = "thermocouple64_group"
            else:
                presentation_channels = [name]
                surface = "sensor_card"
            presentations[name] = {
                "surface": surface,
                "channels": presentation_channels,
                "canvas_width": 640,
                "canvas_height": 300,
                "client_width": 320,
                "client_height": 150,
            }
    trace = {
        "start_request_id": request_id,
        "capture_generation": generation,
        "selected_channels": EXPECTED_CHANNELS.copy(),
        "complete": True,
        "observations": observations,
        "presentations": presentations,
    }
    backend = {
        "start_request_id": request_id,
        "capture_generation": generation,
        "capture_timeline": {"events": [{"stage": "capture_prepare", "server_monotonic": 100.0}]},
    }
    return trace, backend


class ClickToCurveAcceptanceTests(unittest.TestCase):
    def test_runtime_port_check_only_reports_required_listeners(self):
        connections = [
            SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(port=8771), pid=10),
            SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(port=9223), pid=11),
            SimpleNamespace(status="ESTABLISHED", laddr=SimpleNamespace(port=8770), pid=12),
            SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(port=9999), pid=13),
        ]
        self.assertEqual(
            listening_runtime_ports(connections),
            [{"port": 8771, "pid": 10}, {"port": 9223, "pid": 11}],
        )

    def test_formal_acceptance_requires_sixty_rounds_and_valid_ten_minute_endurance(self):
        rounds = [{"pass_performance": True, "pass_target": True} for _ in range(60)]
        endurance = {
            "pass_performance": True,
            "observed_seconds": 600.1,
            "endurance_validation": {"valid": True},
            "stop": {"valid": True},
        }
        result = formal_acceptance_status(
            rounds, cold=10, warm=40, idle=10,
            idle_minimum=120, idle_maximum=300,
            endurance=endurance, required_endurance_seconds=600,
            max_target_exceedances=3,
        )
        self.assertTrue(result["pass_performance"])
        self.assertTrue(result["pass_endurance"])
        self.assertTrue(result["pass_acceptance"])
        endurance["observed_seconds"] = 599.9
        result = formal_acceptance_status(
            rounds, cold=10, warm=40, idle=10,
            idle_minimum=120, idle_maximum=300,
            endurance=endurance, required_endurance_seconds=600,
            max_target_exceedances=3,
        )
        self.assertTrue(result["pass_performance"])
        self.assertFalse(result["pass_endurance"])
        self.assertFalse(result["pass_acceptance"])

    def test_target_tolerance_cannot_hide_a_hard_limit_failure(self):
        rounds = [{"pass_performance": True, "pass_target": index >= 3} for index in range(60)]
        arguments = dict(cold=10, warm=40, idle=10, idle_minimum=120, idle_maximum=300,
                         endurance=None, required_endurance_seconds=600)
        result = formal_acceptance_status(rounds, **arguments)
        self.assertTrue(result["pass_hard_limit"])
        self.assertFalse(result["pass_performance"])
        self.assertEqual(result["target_exceedances"], 3)
        self.assertTrue(formal_acceptance_status(rounds, **arguments, max_target_exceedances=3)["pass_performance"])
        self.assertFalse(formal_acceptance_status(rounds, **arguments, max_target_exceedances=2)["pass_performance"])
        rounds[0]["pass_performance"] = False
        result = formal_acceptance_status(rounds, **arguments, max_target_exceedances=3)
        self.assertFalse(result["pass_hard_limit"])
        self.assertFalse(result["pass_performance"])

    def test_endurance_snapshot_uses_latest_canonical_sources_and_backend_health(self):
        channels = []
        for interface_id, names in EXPECTED_BY_INTERFACE.items():
            for name in names:
                display_name = "压实力" if name == "压力" else name
                channels.append({
                    "name": display_name,
                    "source_channel_name": name,
                    "source_observations": [
                        {
                            "interface_id": interface_id,
                            "source_sequence": 1,
                            "received_monotonic": 100.0,
                            "quality": "measured_new",
                            "protocol_ok": True,
                            "stable": True,
                        },
                        {
                            "interface_id": interface_id,
                            "source_sequence": 2,
                            "received_monotonic": 101.0,
                            "quality": "measured_new",
                            "protocol_ok": True,
                            "stable": True,
                        },
                    ],
                })
        frontend = {"channels": channels}
        backend = {
            "running": True,
            "capture_generation": "generation",
            "sample_count": 42,
            "persistence_queue_depth": 1,
            "persistence_failed": False,
            "last_error": "",
            "interfaces": [
                {
                    "id": interface_id,
                    "ok": True,
                    "state": "ok",
                    "last_error": "",
                    "reconnect_attempts": 0,
                    "usb_evidence": ({"unit": "adc_counts", "writer_error": ""}
                                     if interface_id == "esr1001_strain" else None),
                }
                for interface_id in EXPECTED_BY_INTERFACE
            ],
        }
        snapshot = build_endurance_snapshot(frontend, backend, 5.0)
        self.assertEqual(snapshot["at"], 5.0)
        self.assertEqual(set(snapshot["sources"]), set(EXPECTED_CHANNELS))
        self.assertEqual(snapshot["sources"]["压力"]["source_sequence"], 2)
        self.assertEqual(snapshot["interfaces"]["esr1001_strain"]["state"], "ok")
        self.assertEqual(snapshot["esr_unit"], "adc_counts")
        self.assertEqual(snapshot["esr_writer_error"], "")

    def test_endurance_requires_all_sources_to_progress_without_queue_or_interface_failures(self):
        snapshots = []
        for at, sequence in ((0.0, 1), (600.0, 2)):
            sources = {
                name: {
                    "interface_id": interface_id,
                    "source_sequence": sequence,
                    "received_monotonic": 100 + at,
                    "quality": "measured_new",
                    "protocol_ok": True,
                    "stable": True,
                }
                for interface_id, names in EXPECTED_BY_INTERFACE.items()
                for name in names
            }
            snapshots.append({
                "at": at,
                "running": True,
                "capture_generation": "generation",
                "sample_count": 10 + int(at),
                "persistence_queue_depth": 0,
                "sources": sources,
                "interfaces": {
                    interface_id: {"ok": True, "state": "ok", "last_error": ""}
                    for interface_id in EXPECTED_BY_INTERFACE
                },
                "esr_unit": "adc_counts",
                "esr_writer_error": "",
            })
        self.assertTrue(validate_endurance(snapshots, 600)["valid"])
        snapshots[1]["sources"]["温度1"] = copy.deepcopy(snapshots[0]["sources"]["温度1"])
        snapshots[1]["persistence_queue_depth"] = 4
        result = validate_endurance(snapshots, 600)
        self.assertFalse(result["valid"])
        self.assertIn("source_progress:温度1", result["errors"])
        self.assertIn("persistence_queue_depth", result["errors"])

    def test_statistics_report_trace_physical_and_censored_failures(self):
        result = statistics([
            {"elapsed_ms": 2500, "physical_frame_upper_bound_ms": 2700, "pass_performance": True},
            {"pass_performance": False},
        ])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["censored_failures"], 1)
        self.assertEqual(result["trace"]["maximum_ms"], 2500)
        self.assertEqual(result["physical_upper_bound"]["p95_ms"], 2700)

    def test_idle_schedule_covers_two_through_five_minutes(self):
        waits = [idle_wait_seconds(index, 10, 120, 300) for index in range(10)]
        self.assertEqual(waits, [120, 140, 160, 180, 200, 220, 240, 260, 280, 300])

    def test_configuration_requires_the_exact_four_enabled_device_assignments(self):
        config = {
            "interfaces": [
                {"id": interface_id, "enabled": True}
                for interface_id in EXPECTED_BY_INTERFACE
            ] + [{"id": "disabled", "enabled": False}],
            "interface_channel_assignments": copy.deepcopy(EXPECTED_BY_INTERFACE),
        }
        self.assertTrue(validate_configuration(config)["valid"])
        config["interface_channel_assignments"]["plc_process"].pop()
        result = validate_configuration(config)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"], ["assignment:plc_process"])

    def test_requires_exact_75_channel_sources_and_original_canvas_presentations(self):
        trace, backend = valid_fixture()
        result = validate_round_trace(trace, backend)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["channel_count"], 75)
        self.assertTrue(result["source_provenance_valid"])
        self.assertTrue(result["presentation_valid"])

    def test_rejects_duplicate_wrong_interface_and_non_original_surface(self):
        trace, backend = valid_fixture()
        trace["selected_channels"][-1] = trace["selected_channels"][0]
        trace["observations"]["压力"][0]["interface_id"] = "abb_motion"
        trace["presentations"]["ABB_X"]["surface"] = "summary"
        result = validate_round_trace(trace, backend)
        self.assertFalse(result["valid"])
        self.assertIn("selected_channels_exact", result["errors"])
        self.assertIn("source_interface:压力", result["errors"])
        self.assertIn("presentation_surface:ABB_X", result["errors"])

    def test_rejects_held_old_or_duplicate_source_observations(self):
        trace, backend = valid_fixture()
        trace["observations"]["张力"][0]["quality"] = "held_within_freshness"
        trace["observations"]["ABB_Y"][0]["capture_generation"] = "old"
        trace["observations"]["CH1_ADC_signed"][1] = copy.deepcopy(
            trace["observations"]["CH1_ADC_signed"][0]
        )
        result = validate_round_trace(trace, backend)
        self.assertFalse(result["valid"])
        self.assertIn("source_observations:张力", result["errors"])
        self.assertIn("source_observations:ABB_Y", result["errors"])
        self.assertIn("source_observations:CH1_ADC_signed", result["errors"])

    def test_stop_requires_saved_files_empty_queue_and_matching_capture(self):
        status = {
            "running": False,
            "capture_phase": "completed",
            "capture_uuid": "capture",
            "capture_saved": True,
            "finalization_complete": True,
            "persistence_queue_depth": 0,
            "sample_count": 20,
            "last_error": "",
            "raw_file": "D:/AFP_Capture/layer.csv",
            "layer_file": "D:/AFP_Capture/layer.csv",
            "full_specimen_file": "D:/AFP_Capture/full.csv",
            "file_integrity": {
                "layer_file": {"bytes": 100, "sha256": "a" * 64},
                "whole_specimen_file": {"bytes": 100, "sha256": "b" * 64},
            },
        }
        self.assertTrue(validate_stop_status(status, "capture")["valid"])
        status["persistence_queue_depth"] = 1
        status["capture_uuid"] = "other"
        status["layer_file"] = None
        result = validate_stop_status(status, "capture")
        self.assertFalse(result["valid"])
        self.assertEqual(
            set(result["errors"]),
            {"capture_uuid", "persistence_queue_depth", "layer_file"},
        )

    def test_normal_start_reuses_the_passed_detection_session(self):
        before = {"session_id": "ready", "version": 3, "fingerprint": "config", "ok": True}
        backend = {
            "readiness_session_id": "ready",
            "readiness_version": 3,
            "readiness_session_valid": True,
            "readiness_invalid_reason": "",
        }
        self.assertTrue(validate_readiness_reuse(before, backend)["valid"])
        backend["readiness_version"] = 4
        result = validate_readiness_reuse(before, backend)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"], ["readiness_version"])


if __name__ == "__main__":
    unittest.main()
