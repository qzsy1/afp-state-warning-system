from __future__ import annotations

import ast
import sys
import tempfile
import threading
import time
import types
import unittest
import struct
from collections import deque
from pathlib import Path
from unittest.mock import Mock, patch

sys.modules.setdefault("pandas", types.ModuleType("pandas"))
from acquisition import (
    AcquisitionConfig,
    AcquisitionManager,
    MultiInterfaceDriver,
    ModbusTcpDriver,
    acquisition_config_from_payload,
)
try:
    from acquisition import BoundedCsvWriter
except ImportError:
    BoundedCsvWriter = None
from edge_capture import RemoteAcquisitionMirror, RemoteAcquisitionRegistry
from local_capture_agent import LocalCaptureAgent
from local_capture_helper_entry import HardwareCheckProcess, HelperCommandDispatcher
try:
    from local_direct_acquisition import LocalDirectAdapter, minimum_poll_interval
except ImportError:
    LocalDirectAdapter = None
    minimum_poll_interval = None
from real_acquisition import (
    ChannelSampleCache,
    ChannelQuality,
    ChannelSample,
    FrameAssembler,
    UnifiedFrame,
    readiness_config_fingerprint,
)
try:
    from real_acquisition import ReadinessSnapshot
except ImportError:
    ReadinessSnapshot = None


def load_config(payload):
    try:
        return acquisition_config_from_payload(payload)
    except TypeError as exc:
        raise AssertionError(f"mode configuration API is missing: {exc}") from exc


def real_payload(**overrides):
    payload = {
        "acquisition_mode": "real",
        "real_acquisition_mode": "local_direct",
        "dataset_schema": "new_collection_v11_3",
        "driver": "serial_json",
        "interfaces": [
            {
                "id": "temperature-device",
                "enabled": True,
                "driver": "serial_json",
                "endpoint": "COM9",
                "role": "custom",
                "physical_interface_id": "serial:COM9",
                "physical_port_id": "COM9",
                "physical_interface_kind": "serial",
                "physical_verified": True,
                "channel_map": {"temperature": "温度"},
            }
        ],
        "interface_channel_assignments": {"temperature-device": ["温度"]},
        "selected_sensors": ["温度"],
        "execution_host": "server",
    }
    payload.update(overrides)
    return payload


def _successful_readiness_worker(message, result_queue):
    """Spawn-safe fixture proving the real cross-process readiness path."""

    config = AcquisitionConfig(**dict(message.get("payload") or {}))
    snapshot = ReadinessSnapshot.create(
        config,
        {
            "ok": True,
            "interfaces": [
                {
                    "id": "temperature-device",
                    "physical_interface_id": "serial:COM9",
                    "state": "ready",
                }
            ],
            "sensors": [{"name": "温度", "state": "ready"}],
        },
        capabilities={
            "unified_frame_v1": True,
            "unified_frame_contract": "unified_frame_v1",
        },
    )
    result_queue.put(
        {
            "type": "result",
            "request_id": str(message.get("request_id") or ""),
            "payload": {"ok": True, "readiness_snapshot": snapshot.to_dict()},
        }
    )


class AcquisitionModeConfigurationTests(unittest.TestCase):
    def test_external_real_config_requires_explicit_mode(self) -> None:
        payload = real_payload()
        payload.pop("real_acquisition_mode")

        with self.assertRaisesRegex(ValueError, "real_acquisition_mode"):
            load_config(payload)

    def test_modes_are_explicit_and_do_not_follow_endpoint_shape(self) -> None:
        local = load_config(
            real_payload(endpoint="https://helper.invalid", real_acquisition_mode="local_direct")
        )
        remote = load_config(
            real_payload(
                endpoint="COM9",
                real_acquisition_mode="remote_helper",
                execution_host="helper_local",
                helper_session_id="session-1",
                helper_capabilities={"unified_frame_v1": True},
            )
        )

        self.assertEqual(local.real_acquisition_mode, "local_direct")
        self.assertEqual(remote.real_acquisition_mode, "remote_helper")

    def test_local_direct_rejects_remote_helper_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "mode_field_conflict"):
            load_config(
                real_payload(
                    helper_session_id="session-1",
                    helper_capabilities={"unified_frame_v1": True},
                )
            )

    def test_remote_helper_rejects_server_execution(self) -> None:
        with self.assertRaisesRegex(ValueError, "mode_field_conflict"):
            load_config(
                real_payload(
                    real_acquisition_mode="remote_helper",
                    execution_host="server",
                    helper_session_id="session-1",
                    helper_capabilities={"unified_frame_v1": True},
                )
            )

    def test_simulation_keeps_legacy_configuration_compatibility(self) -> None:
        config = load_config(
            {
                "acquisition_mode": "simulation",
                "dataset_schema": "new_collection_v11_3",
                "driver": "simulator",
                "selected_sensors": ["温度"],
                "interfaces": [
                    {
                        "id": "simulation",
                        "enabled": True,
                        "driver": "simulator",
                        "role": "custom",
                        "channel_map": {},
                    }
                ],
            }
        )

        self.assertEqual(config.acquisition_mode, "simulation")
        self.assertEqual(config.real_acquisition_mode, "")


class AcquisitionModeEquivalenceTests(unittest.TestCase):
    def test_local_adapter_and_remote_mirror_normalize_identical_input_equivalently(self) -> None:
        class Driver:
            protocol_verified = True
            quality_metadata = {}

        cache = ChannelSampleCache()
        adapter = LocalDirectAdapter(
            "temperature-device",
            Driver(),
            ["温度"],
            cache,
            clock=lambda: 10.0,
            wall_clock=lambda: 1000.0,
        )
        adapter._publish({"温度": 42.5})
        local_frame = FrameAssembler(
            ["温度"],
            10.0,
            {"温度": 0.2},
            clock=lambda: 10.0,
            wall_clock=lambda: 1000.0,
            start_monotonic=10.0,
            start_wall_time=1000.0,
        ).assemble(cache, 10.0)
        envelope = local_frame.to_envelope("capture-equivalent", {"温度": "°C"})
        mirror = RemoteAcquisitionMirror()
        mirror.configure_contract(["温度"], {"温度": "°C"})

        accepted = mirror.ingest(
            {
                "capture_uuid": "capture-equivalent",
                "sequence": 0,
                "required_frame_contract": "unified_frame_v1",
                "rows": [{"温度": 42.5}],
                "timestamps": [1000.0],
                "frames": [envelope],
            }
        )

        self.assertTrue(accepted["ok"])
        self.assertEqual(mirror.frame_envelopes(), [envelope])
        self.assertEqual(mirror.numeric_matrix(), ([{"温度": 42.5}], [1000.0]))

    def _frame(self, quality=ChannelQuality.MEASURED_NEW) -> UnifiedFrame:
        return UnifiedFrame(
            capture_sequence=17,
            target_monotonic=12.5,
            target_wall_time=1_700_000_000.25,
            channels={
                "温度": ChannelSample(
                    interface_id="temperature-device",
                    channel_name="温度",
                    value=42.5,
                    quality=quality,
                    device_timestamp=1_700_000_000.1,
                    received_wall_time=1_700_000_000.2,
                    received_monotonic=12.4,
                    source_sequence=9,
                    protocol_ok=True,
                    metadata={"age_seconds": 0.1},
                )
            },
            assembled_monotonic=12.55,
        )

    def test_shared_frame_envelope_is_complete_and_source_agnostic(self) -> None:
        frame = self._frame()
        self.assertTrue(
            hasattr(frame, "to_envelope"),
            "UnifiedFrame must expose the shared envelope contract",
        )

        envelope = frame.to_envelope("capture-1", {"温度": "°C"})

        self.assertEqual(envelope["contract_version"], "unified_frame_v1")
        self.assertEqual(envelope["capture_uuid"], "capture-1")
        self.assertEqual(envelope["frame_sequence"], 17)
        self.assertEqual(envelope["frame_time"], 1_700_000_000.25)
        self.assertNotIn("source_mode", envelope)
        self.assertEqual(
            envelope["channels"]["温度"],
            {
                "value": 42.5,
                "unit": "°C",
                "quality": "measured_new",
                "source_timestamp": 1_700_000_000.1,
                "received_timestamp": 1_700_000_000.2,
                "age_seconds": 0.1,
                "is_new": True,
                "interface_id": "temperature-device",
                "source_sequence": 9,
                "protocol_ok": True,
                "error": "",
            },
        )

    def test_remote_round_trip_preserves_shared_quality_without_reclassification(self) -> None:
        frame = self._frame(ChannelQuality.STALE)
        self.assertTrue(
            hasattr(UnifiedFrame, "from_envelope"),
            "remote_helper must consume the same shared envelope",
        )
        envelope = frame.to_envelope("capture-1", {"温度": "°C"})

        restored = UnifiedFrame.from_envelope(envelope)
        round_trip = restored.to_envelope("capture-1", {"温度": "°C"})

        self.assertEqual(round_trip, envelope)
        self.assertEqual(round_trip["channels"]["温度"]["quality"], "stale")

    def test_manager_stream_exposes_envelope_aligned_with_legacy_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = AcquisitionManager(Path(temporary) / "captures")
            config = AcquisitionConfig(**real_payload())
            driver = MultiInterfaceDriver([], ["温度"])
            driver.cache.publish(
                ChannelSample(
                    interface_id="temperature-device",
                    channel_name="温度",
                    value=42.5,
                    received_monotonic=time.monotonic(),
                    source_sequence=1,
                )
            )
            manager.config = config
            manager.driver = driver
            manager.capture_uuid = "capture-stream"
            worker = threading.Thread(target=manager._run, daemon=True)
            worker.start()
            deadline = time.monotonic() + 0.5
            while manager.total_sample_count < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            manager.stop_event.set()
            worker.join(1.0)

            streamed = manager.stream_rows_since(0, 1)

        self.assertEqual(len(streamed["rows"]), 1)
        self.assertEqual(len(streamed["timestamps"]), 1)
        self.assertIn("frames", streamed)
        self.assertEqual(len(streamed["frames"]), 1)
        self.assertEqual(streamed["frames"][0]["capture_uuid"], "capture-stream")
        self.assertEqual(streamed["frames"][0]["frame_sequence"], 0)
        self.assertEqual(streamed["frames"][0]["channels"]["温度"]["unit"], "°C")
        self.assertEqual(
            streamed["frames"][0]["channels"]["温度"]["value"],
            streamed["rows"][0]["温度"],
        )

    def test_helper_and_remote_mirror_preserve_exact_envelopes(self) -> None:
        envelope = self._frame().to_envelope("capture-1", {"温度": "°C"})
        envelope["frame_sequence"] = 0

        class FakeManager:
            def stream_rows_since(self, cursor, limit):
                return {
                    "rows": [{"温度": 42.5}],
                    "timestamps": [1_700_000_000.25],
                    "frames": [envelope],
                    "next_cursor": 1,
                    "total_count": 1,
                    "truncated": False,
                }

            def status(self):
                return {"config": {"sample_rate_hz": 10.0}}

            def numeric_matrix(self):
                return ([{"温度": 42.5}], [1_700_000_000.25])

        agent = LocalCaptureAgent(manager=FakeManager())
        agent._capture_uuid = "capture-1"
        batch = agent.next_sample_batch(limit=1)
        self.assertIn("frames", batch)
        self.assertEqual(batch["frames"], [envelope])

        mirror = RemoteAcquisitionMirror()
        accepted = mirror.ingest(batch)

        self.assertTrue(accepted["ok"])
        self.assertEqual(mirror.frame_envelopes(), [envelope])
        self.assertEqual(mirror.numeric_matrix(), ([{"温度": 42.5}], [1_700_000_000.25]))

    def test_absolute_frame_sequence_survives_memory_window_truncation(self) -> None:
        manager = AcquisitionManager()
        manager.rows = deque([{"温度": 3.0}, {"温度": 4.0}], maxlen=2)
        manager.timestamps = deque([1000.3, 1000.4], maxlen=2)
        manager.frame_envelopes = deque(
            [self.frame("capture-window", 3, 3.0), self.frame("capture-window", 4, 4.0)],
            maxlen=2,
        )
        manager.total_sample_count = 5

        window = manager.stream_rows_since(0, 10)

        self.assertTrue(window["truncated"])
        self.assertEqual(window["buffer_base"], 3)
        self.assertEqual(
            [item["frame_sequence"] for item in window["frames"]],
            [3, 4],
        )

    @staticmethod
    def frame(capture_uuid: str, sequence: int, value: float) -> dict:
        frame = UnifiedFrame(
            capture_sequence=sequence,
            target_monotonic=float(sequence),
            target_wall_time=1000.0 + sequence,
            channels={
                "温度": ChannelSample(
                    interface_id="fixture",
                    channel_name="温度",
                    value=value,
                    received_wall_time=1000.0 + sequence,
                    received_monotonic=float(sequence),
                    source_sequence=sequence,
                )
            },
            assembled_monotonic=float(sequence),
        )
        return frame.to_envelope(capture_uuid, {"温度": "°C"})


class LocalDirectNoHardwareIntegrationTests(unittest.TestCase):
    def test_empty_channel_assignment_cannot_publish_another_interfaces_value(self) -> None:
        cache = ChannelSampleCache()

        class Driver:
            def open(self):
                return None

            def read_sample(self):
                return {"温度": 999.0}

            def close(self):
                return None

        adapter = LocalDirectAdapter("unassigned", Driver(), [], cache)
        adapter._publish({"温度": 999.0})

        self.assertIsNone(cache.latest("温度"))
        self.assertFalse(adapter.status()["first_sample_received"])

    def test_local_adapter_recovers_one_device_without_any_helper_transport(self) -> None:
        self.assertIsNotNone(LocalDirectAdapter, "local_direct adapter is missing")

        class TransientDriver:
            def __init__(self):
                self.open_calls = 0
                self.read_calls = 0
                self.close_calls = 0

            def open(self):
                self.open_calls += 1

            def read_sample(self):
                self.read_calls += 1
                if self.read_calls == 1:
                    raise OSError("device-disconnected")
                return {"温度": 42.5}

            def close(self):
                self.close_calls += 1

        driver = TransientDriver()
        cache = __import__("real_acquisition").ChannelSampleCache()
        adapter = LocalDirectAdapter(
            "temperature-device",
            driver,
            ["温度"],
            cache,
            poll_interval_seconds=0.001,
            minimum_poll_seconds=0.005,
            reconnect_backoff_seconds=0.001,
            max_reconnect_attempts=2,
        )
        adapter.start()
        deadline = time.monotonic() + 0.5
        while cache.latest("温度") is None and time.monotonic() < deadline:
            time.sleep(0.005)
        stopped = adapter.stop(timeout_seconds=0.5)
        status = adapter.status()

        self.assertTrue(stopped)
        self.assertEqual(cache.latest("温度").value, 42.5)
        self.assertEqual(driver.open_calls, 2)
        self.assertGreaterEqual(driver.close_calls, 2)
        self.assertEqual(status["fault_domain"], "local_device")
        self.assertEqual(status["reconnect_attempts"], 1)
        self.assertEqual(status["actual_poll_interval_seconds"], 0.005)
        self.assertNotIn("helper", status)
        self.assertNotIn("network", status)

    def test_device_profiles_enforce_nonzero_polling_floors(self) -> None:
        self.assertIsNotNone(minimum_poll_interval, "device polling profiles are missing")

        self.assertGreaterEqual(minimum_poll_interval("modbus_tcp"), 0.05)
        self.assertGreaterEqual(minimum_poll_interval("abb_robot"), 0.1)
        self.assertGreaterEqual(minimum_poll_interval("m3232_pressure"), 0.005)

    def test_multi_interface_open_accepts_a_recovered_read_reconnect(self) -> None:
        class RecoveringDriver:
            protocol_verified = True

            def __init__(self):
                self.reads = 0

            def open(self):
                return None

            def read_sample(self):
                self.reads += 1
                if self.reads == 1:
                    raise IOError("transient-read")
                return {"温度": 33.0}

            def close(self):
                return None

        driver = RecoveringDriver()
        multi = MultiInterfaceDriver(
            [
                {
                    "id": "temperature-device",
                    "driver": "serial_json",
                    "endpoint": "COM9",
                    "poll_interval_seconds": 0.001,
                    "reconnect_backoff_seconds": 0.001,
                    "max_reconnect_attempts": 2,
                }
            ],
            ["温度"],
        )
        try:
            with patch("acquisition.SerialJsonDriver", return_value=driver):
                multi.open()
            deadline = time.monotonic() + 0.5
            while multi.cache.latest("温度") is None and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(multi.cache.latest("温度").value, 33.0)
        finally:
            multi.close()

    def test_local_adapter_exposes_first_sample_gate_and_persistent_disconnect(self) -> None:
        read_started = threading.Event()
        release_read = threading.Event()

        class DisconnectedDriver:
            def open(self):
                return None

            def read_sample(self):
                read_started.set()
                release_read.wait(0.2)
                raise IOError("device-disconnected")

            def close(self):
                return None

        cache = ChannelSampleCache()
        adapter = LocalDirectAdapter(
            "pressure-device",
            DisconnectedDriver(),
            ["压力"],
            cache,
            reconnect_backoff_seconds=0.001,
            max_reconnect_backoff_seconds=0.002,
            max_reconnect_attempts=2,
        )
        adapter.start()
        self.assertTrue(adapter.opened.wait(0.2))
        self.assertTrue(read_started.wait(0.2))
        initial = adapter.status()
        release_read.set()
        deadline = time.monotonic() + 0.5
        while adapter.status()["running"] and time.monotonic() < deadline:
            time.sleep(0.005)
        failed = adapter.status()

        self.assertEqual(initial["state"], "waiting_first_sample")
        self.assertFalse(initial["first_sample_received"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["reconnect_attempts"], 2)
        self.assertEqual(failed["last_error"], "device-disconnected")
        self.assertIsNone(cache.latest("压力"))

    def test_local_adapter_reports_close_timeout_without_silent_success(self) -> None:
        release = threading.Event()

        class BlockingDriver:
            def open(self):
                return None

            def read_sample(self):
                release.wait(1.0)
                return None

            def close(self):
                return None

        adapter = LocalDirectAdapter(
            "blocked-device",
            BlockingDriver(),
            ["温度"],
            ChannelSampleCache(),
            max_reconnect_attempts=0,
        )
        adapter.start()
        self.assertTrue(adapter.opened.wait(0.2))
        try:
            self.assertFalse(adapter.stop(timeout_seconds=0.001))
            self.assertEqual(adapter.status()["state"], "stop_timed_out")
        finally:
            release.set()
            adapter.stop(timeout_seconds=0.2)

    def test_successful_sample_resets_consecutive_reconnect_budget(self) -> None:
        class IntermittentDriver:
            def __init__(self):
                self.open_count = 0
                self.read_count = 0

            def open(self):
                self.open_count += 1

            def read_sample(self):
                self.read_count += 1
                if self.read_count in {1, 3, 5}:
                    raise IOError("isolated-transient")
                return {"温度": float(self.read_count)}

            def close(self):
                return None

        driver = IntermittentDriver()
        cache = ChannelSampleCache()
        adapter = LocalDirectAdapter(
            "temperature-device",
            driver,
            ["温度"],
            cache,
            poll_interval_seconds=0.001,
            reconnect_backoff_seconds=0.001,
            max_reconnect_attempts=1,
        )
        adapter.start()
        deadline = time.monotonic() + 0.5
        while driver.read_count < 6 and adapter.status()["running"] and time.monotonic() < deadline:
            time.sleep(0.005)
        status = adapter.status()
        adapter.stop(timeout_seconds=0.2)

        self.assertGreaterEqual(driver.read_count, 6)
        self.assertNotEqual(status["state"], "failed")
        self.assertGreaterEqual(status["reconnect_attempts"], 3)


class SharedAcquisitionCoreTests(unittest.TestCase):
    class FragmentedSocket:
        def __init__(self, payload: bytes, fragment: int = 2):
            self.payload = bytearray(payload)
            self.fragment = fragment
            self.sent = b""

        def sendall(self, payload: bytes):
            self.sent += payload

        def recv(self, size: int) -> bytes:
            if not self.payload:
                return b""
            count = min(size, self.fragment, len(self.payload))
            result = bytes(self.payload[:count])
            del self.payload[:count]
            return result

    @staticmethod
    def response(
        *,
        transaction=1,
        protocol=0,
        unit=255,
        function=3,
        data=b"\x00\x01\x00\x02",
        byte_count=None,
        length=None,
    ) -> bytes:
        count = len(data) if byte_count is None else byte_count
        pdu = bytes((function, count)) + data
        declared = 1 + len(pdu) if length is None else length
        return struct.pack(">HHHB", transaction, protocol, declared, unit) + pdu

    def test_modbus_fc03_fragmented_response_stops_at_declared_boundary(self) -> None:
        next_response = self.response(transaction=2, data=b"\x00\x03\x00\x04")
        sock = self.FragmentedSocket(self.response() + next_response, fragment=2)
        driver = ModbusTcpDriver("127.0.0.1:502", slave_id=255)
        driver.sock = sock

        values = driver._read_holding_registers(0, 2)

        self.assertEqual(values, [1, 2])
        self.assertEqual(bytes(sock.payload), next_response)

    def test_modbus_fc03_rejects_identity_length_and_register_mismatches(self) -> None:
        cases = {
            "transaction": self.response(transaction=9),
            "protocol": self.response(protocol=1),
            "unit": self.response(unit=1),
            "function": self.response(function=4),
            "odd byte count": self.response(data=b"\x00\x01\x02", byte_count=3),
            "quantity": self.response(data=b"\x00\x01", byte_count=2),
            "short pdu": self.response(length=8)[:-1],
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                driver = ModbusTcpDriver("127.0.0.1:502", slave_id=255)
                driver.sock = self.FragmentedSocket(payload, fragment=3)
                with self.assertRaisesRegex(IOError, "Modbus"):
                    driver._read_holding_registers(0, 2)

    def test_real_capture_fails_closed_when_required_persistence_is_unavailable(self) -> None:
        manager = AcquisitionManager()
        config = AcquisitionConfig(**real_payload(save_root=""))

        with patch("acquisition.build_driver") as build:
            with self.assertRaisesRegex(RuntimeError, "persistence_required"):
                manager.start(config)

        build.assert_not_called()

    def test_mysql_preflight_fails_before_local_driver_is_built(self) -> None:
        manager = AcquisitionManager()
        config = AcquisitionConfig(
            **real_payload(
                save_root="",
                mysql_enabled=True,
                mysql_host="127.0.0.1",
                mysql_database="afp_state_warning",
            )
        )
        store = Mock()
        store.preflight.return_value = {"ok": False, "error": "database-unreachable"}

        with (
            patch("acquisition.MySQLCaptureStore", return_value=store),
            patch("acquisition.build_driver") as build,
        ):
            with self.assertRaisesRegex(RuntimeError, "mysql_preflight_failed"):
                manager.start(config)

        store.preflight.assert_called_once_with(write_test=True)
        build.assert_not_called()

    def test_mysql_only_real_capture_uses_durable_local_retry_spool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = AcquisitionManager(Path(temporary))
            config = AcquisitionConfig(
                **real_payload(
                    save_root="relative-missing-save-root",
                    mysql_enabled=True,
                    mysql_host="127.0.0.1",
                    mysql_database="afp_state_warning",
                )
            )
            driver = MultiInterfaceDriver([], ["温度"])
            driver.cache.publish(
                ChannelSample(
                    interface_id="temperature-device",
                    channel_name="温度",
                    value=20.0,
                    source_sequence=1,
                )
            )
            store = Mock()
            store.preflight.return_value = {"ok": True}
            store.save_layer.return_value = {
                "ok": False,
                "error": "database-disconnected-after-start",
            }

            with (
                patch("acquisition.MySQLCaptureStore", return_value=store),
                patch("acquisition.build_driver", return_value=driver),
            ):
                started = manager.start(config)
                stopped = manager.stop()

            self.assertTrue(started["save_enabled"])
            self.assertTrue(started["save_status"]["retry_spool"])
            self.assertFalse(stopped["finalization_complete"])
            self.assertEqual(stopped["capture_phase"], "database_pending_retry")
            pending = list(Path(temporary).rglob("*_mysql*_pending.json"))
            self.assertEqual(len(pending), 1)
            self.assertTrue(Path(stopped["layer_file"]).is_file())

            retry_store = Mock()
            retry_store.verify_connection.return_value = {"ok": True}
            retry_store.save_layer.return_value = {"ok": True, "saved_rows": 1}
            with patch("acquisition.MySQLCaptureStore", return_value=retry_store):
                retried = manager.retry_pending_mysql(root=None)

            self.assertEqual(retried["succeeded"], 1)
            self.assertTrue(manager.status()["finalization_complete"])
            self.assertEqual(manager.status()["capture_phase"], "completed")
            self.assertTrue(manager.status()["mysql"]["ok"])

            next_driver = MultiInterfaceDriver([], ["温度"])
            next_driver.cache.publish(
                ChannelSample(
                    interface_id="temperature-device",
                    channel_name="温度",
                    value=21.0,
                    source_sequence=1,
                )
            )
            next_config = AcquisitionConfig(
                **real_payload(
                    save_root="",
                    persistence_policy="engineering_preview",
                )
            )
            with patch("acquisition.build_driver", return_value=next_driver):
                restarted = manager.start(next_config)
                manager.stop()
            self.assertTrue(restarted["running"])

    def test_real_start_fails_when_required_channel_never_emits_first_sample(self) -> None:
        manager = AcquisitionManager()
        config = AcquisitionConfig(
            **real_payload(
                save_root="",
                persistence_policy="engineering_preview",
                first_sample_timeout_seconds=0.03,
            )
        )
        driver = MultiInterfaceDriver([], ["温度"])

        with patch("acquisition.build_driver", return_value=driver):
            with self.assertRaisesRegex(RuntimeError, "first_sample_timeout"):
                manager.start(config)

        self.assertFalse(manager.status()["finalization_complete"])

    def test_explicit_engineering_preview_is_bounded_and_nonproduction(self) -> None:
        manager = AcquisitionManager()
        config = AcquisitionConfig(
            **real_payload(save_root="", persistence_policy="engineering_preview")
        )
        driver = MultiInterfaceDriver([], ["温度"])
        driver.cache.publish(
            ChannelSample(
                interface_id="temperature-device",
                channel_name="温度",
                value=20.0,
                source_sequence=1,
            )
        )

        with (
            patch("acquisition.build_driver", return_value=driver),
            patch("acquisition.io.StringIO", side_effect=AssertionError("unbounded-memory-sink")),
        ):
            started = manager.start(config)
            stopped = manager.stop()

        self.assertFalse(started["production_conclusion_enabled"])
        self.assertEqual(started["persistence_policy"], "engineering_preview")
        self.assertEqual(started["memory_buffer_limit_rows"], 200000)
        self.assertFalse(stopped["capture_saved"])

    def test_local_worker_fault_domain_is_exposed_in_interface_status(self) -> None:
        manager = AcquisitionManager()
        manager.config = AcquisitionConfig(
            **real_payload(save_root="", persistence_policy="engineering_preview")
        )
        manager.started_at = time.time() - 3.0
        manager.driver = Mock()
        manager.driver.worker_status.return_value = [
            {
                "interface_id": "temperature-device",
                "fault_domain": "local_device",
                "state": "failed",
                "last_error": "device-disconnected",
                "reconnect_attempts": 4,
            }
        ]

        status = manager.status()
        interface = status["interfaces"][0]

        self.assertEqual(interface["fault_domain"], "local_device")
        self.assertEqual(interface["adapter_state"], "failed")
        self.assertEqual(interface["last_error"], "device-disconnected")

    def test_bounded_writer_reports_overflow_and_write_failure(self) -> None:
        self.assertIsNotNone(BoundedCsvWriter, "bounded persistence writer is missing")
        entered = threading.Event()
        release = threading.Event()

        class BlockingWriter:
            def writerow(self, _row):
                entered.set()
                release.wait(1.0)

        writer = BoundedCsvWriter(
            BlockingWriter(), BlockingWriter(), max_queue_rows=1, submit_timeout_seconds=0.0
        )
        try:
            writer.submit({}, {})
            self.assertTrue(entered.wait(0.2))
            writer.submit({}, {})
            self.assertEqual(writer.high_water_rows, 1)
            with self.assertRaisesRegex(RuntimeError, "persistence_queue_overflow"):
                writer.submit({}, {})
        finally:
            release.set()
            writer.close(timeout_seconds=1.0)

        class FailingWriter:
            def writerow(self, _row):
                raise OSError("disk-full")

        failed = BoundedCsvWriter(FailingWriter(), BlockingWriter(), max_queue_rows=2)
        failed.submit({}, {})
        with self.assertRaisesRegex(RuntimeError, "persistence_write_failed.*disk-full"):
            failed.close(timeout_seconds=1.0)

    def test_mysql_failure_keeps_overall_finalization_pending(self) -> None:
        manager = AcquisitionManager()
        manager.config = AcquisitionConfig(
            acquisition_mode="simulation",
            driver="simulator",
            mysql_enabled=True,
        )
        manager.rows.append({"温度": 20.0})
        manager.total_sample_count = 1
        manager.save_enabled = False
        manager.session_dir = None
        manager.finalization_complete = False
        store = Mock()
        store.save_layer.return_value = {"ok": False, "error": "database-down"}

        with patch("acquisition.MySQLCaptureStore", return_value=store):
            status = manager.stop()
            repeated = manager.stop()

        self.assertFalse(status["finalization_complete"])
        self.assertEqual(status["capture_phase"], "database_pending_retry")
        self.assertTrue(status["database_pending_retry"])
        self.assertEqual(status["mysql"]["state"], "pending")
        self.assertEqual(repeated["capture_phase"], "database_pending_retry")
        self.assertFalse(repeated["finalization_complete"])
        store.save_layer.assert_called_once()

    def test_in_memory_mysql_finalize_rejects_truncated_live_buffer(self) -> None:
        manager = AcquisitionManager()
        manager.config = AcquisitionConfig(
            acquisition_mode="simulation",
            driver="simulator",
            mysql_enabled=True,
        )
        manager.rows.append({"温度": 20.0})
        manager.total_sample_count = 2
        manager.save_enabled = False
        manager.session_dir = None
        manager.finalization_complete = False
        store = Mock()

        with patch("acquisition.MySQLCaptureStore", return_value=store):
            status = manager.stop()

        self.assertFalse(status["finalization_complete"])
        self.assertEqual(status["capture_phase"], "finalization_failed")
        self.assertIn("buffer_reconciliation_failed", status["mysql"]["error"])
        store.save_layer.assert_not_called()

    def test_new_capture_is_blocked_while_database_retry_is_pending(self) -> None:
        manager = AcquisitionManager()
        manager.config = AcquisitionConfig(acquisition_mode="simulation", driver="simulator")
        manager.capture_phase = "database_pending_retry"
        manager.finalization_complete = False

        with self.assertRaisesRegex(RuntimeError, "database_pending_retry"):
            manager._start_new(
                AcquisitionConfig(acquisition_mode="simulation", driver="simulator")
            )

    def test_stop_timeout_is_explicit_and_never_finalized(self) -> None:
        class StuckThread:
            def join(self, timeout=None):
                return None

            def is_alive(self):
                return True

        class Driver:
            def close(self):
                return None

        manager = AcquisitionManager()
        manager.config = AcquisitionConfig(
            acquisition_mode="simulation",
            driver="simulator",
            stop_sampling_timeout_seconds=0.001,
            stop_force_timeout_seconds=0.001,
        )
        manager.thread = StuckThread()
        manager.driver = Driver()
        manager.finalization_complete = False

        result = manager.stop()

        self.assertEqual(result["capture_phase"], "stop_timed_out")
        self.assertFalse(result["finalization_complete"])
        self.assertIn("stop_timeout", result["last_error"])

    def test_shared_quality_contract_isolated_per_channel_and_recovers(self) -> None:
        cache = ChannelSampleCache()
        assembler = FrameAssembler(
            ["温度", "压力"],
            10.0,
            {"温度": 0.15, "压力": 0.15},
            clock=lambda: 1.0,
            wall_clock=lambda: 1000.0,
            start_monotonic=0.0,
            start_wall_time=1000.0,
        )

        missing = assembler.assemble(cache, 0.0)
        cache.publish(
            ChannelSample(
                interface_id="temperature-device",
                channel_name="温度",
                value=20.0,
                received_wall_time=1000.05,
                received_monotonic=0.05,
                source_sequence=1,
            )
        )
        fresh = assembler.assemble(cache, 0.1)
        held = assembler.assemble(cache, 0.15)
        stale = assembler.assemble(cache, 0.3)
        cache.publish(
            ChannelSample(
                interface_id="temperature-device",
                channel_name="温度",
                value=float("nan"),
                received_wall_time=1000.31,
                received_monotonic=0.31,
                source_sequence=2,
            )
        )
        invalid = assembler.assemble(cache, 0.32)
        cache.publish(
            ChannelSample(
                interface_id="temperature-device",
                channel_name="温度",
                value=21.0,
                received_wall_time=1000.33,
                received_monotonic=0.33,
                source_sequence=3,
            )
        )
        recovered = assembler.assemble(cache, 0.34)

        self.assertEqual(missing.quality(), {"温度": "missing", "压力": "missing"})
        self.assertEqual(fresh.quality(), {"温度": "measured_new", "压力": "missing"})
        self.assertEqual(held.channels["温度"].quality, ChannelQuality.HELD_WITHIN_FRESHNESS)
        self.assertEqual(stale.channels["温度"].quality, ChannelQuality.STALE)
        self.assertEqual(invalid.channels["温度"].quality, ChannelQuality.INVALID)
        self.assertEqual(recovered.channels["温度"].quality, ChannelQuality.MEASURED_NEW)
        self.assertEqual(recovered.channels["压力"].quality, ChannelQuality.MISSING)


class RemoteHelperNoHardwareIntegrationTests(unittest.TestCase):
    def remote_config(self, **overrides):
        endpoint = overrides.pop("endpoint", None)
        values = {
            "real_acquisition_mode": "remote_helper",
            "execution_host": "helper_local",
            "helper_session_id": "session-1",
            "helper_capabilities": {"unified_frame_v1": True},
        }
        values.update(overrides)
        payload = real_payload(**values)
        if endpoint is not None:
            payload["interfaces"][0]["endpoint"] = endpoint
        return acquisition_config_from_payload(
            payload
        )

    def test_readiness_snapshot_round_trip_excludes_secrets_and_revalidates(self) -> None:
        self.assertIsNotNone(ReadinessSnapshot, "readiness snapshot contract is missing")
        config = self.remote_config()
        snapshot = ReadinessSnapshot.create(
            config,
            {
                "ok": True,
                "checked_at": 100.0,
                "interfaces": [
                    {
                        "id": "temperature-device",
                        "physical_interface_id": "serial:COM9",
                        "state": "ok",
                    }
                ],
                "sensors": [{"name": "温度", "state": "ok"}],
                "password": "must-not-leak",
            },
            capabilities={
                "unified_frame_v1": True,
                "unified_frame_contract": "unified_frame_v1",
            },
            now=100.0,
            ttl_seconds=30.0,
        )

        payload = snapshot.to_dict()
        restored = ReadinessSnapshot.from_dict(payload)

        self.assertNotIn("must-not-leak", repr(payload))
        self.assertEqual(restored.config_fingerprint, readiness_config_fingerprint(config))
        restored.validate(config, now=120.0, required_capabilities=("unified_frame_v1",))
        with self.assertRaisesRegex(ValueError, "expired"):
            restored.validate(config, now=131.0)
        changed = self.remote_config(endpoint="COM10")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            restored.validate(changed, now=120.0)

        incomplete = ReadinessSnapshot.create(
            config,
            {"ok": True, "interfaces": [], "sensors": []},
            capabilities={
                "unified_frame_v1": True,
                "unified_frame_contract": "unified_frame_v1",
            },
            now=100.0,
        )
        with self.assertRaisesRegex(ValueError, "identity|channel"):
            incomplete.validate(
                config,
                now=110.0,
                required_capabilities=("unified_frame_v1",),
            )

    def test_dispatcher_imports_child_snapshot_before_returning_check_success(self) -> None:
        config = self.remote_config()
        snapshot = ReadinessSnapshot.create(
            config,
            {"ok": True, "checked_at": 100.0, "interfaces": [], "sensors": []},
            capabilities={"unified_frame_v1": True},
            now=100.0,
        ).to_dict()
        events = []

        class FakeAgent:
            def import_readiness_snapshot(self, payload):
                events.append(("import", payload["config_fingerprint"]))

        class FakeCheckRunner:
            def start(self, message, callback):
                callback(
                    {
                        "type": "result",
                        "request_id": message["request_id"],
                        "payload": {"ok": True, "readiness_snapshot": snapshot},
                    }
                )
                return True

            def cancel(self, **_kwargs):
                return False

            def close(self):
                return None

        dispatcher = HelperCommandDispatcher(FakeAgent(), check_runner=FakeCheckRunner())
        try:
            dispatcher.submit(
                {
                    "type": "command",
                    "request_id": "check-1",
                    "command": "check_capture",
                    "payload": {},
                },
                lambda response: events.append(("callback", response["payload"]["ok"])),
            )
        finally:
            dispatcher.close()

        self.assertEqual(
            events,
            [("import", snapshot["config_fingerprint"]), ("callback", True)],
        )

    def test_real_check_process_dispatcher_and_agent_share_readiness_gate(self) -> None:
        payload = real_payload(
            real_acquisition_mode="remote_helper",
            execution_host="helper_local",
            helper_session_id="session-1",
            helper_capabilities={
                "unified_frame_v1": True,
                "unified_frame_contract": "unified_frame_v1",
            },
        )
        config = acquisition_config_from_payload(payload)

        class FakeManager:
            def __init__(self):
                self.starts = 0

            def start(self, incoming):
                self.starts += 1
                return {"capture_uuid": incoming.capture_uuid, "running": True}

        manager = FakeManager()
        agent = LocalCaptureAgent(manager=manager)
        runner = HardwareCheckProcess(
            timeout_seconds=3.0,
            worker=_successful_readiness_worker,
        )
        dispatcher = HelperCommandDispatcher(agent, check_runner=runner)
        finished = threading.Event()
        responses = []
        try:
            dispatcher.submit(
                {
                    "type": "command",
                    "request_id": "check-cross-process",
                    "command": "check_capture",
                    "payload": payload,
                },
                lambda response: (responses.append(response), finished.set()),
            )
            self.assertTrue(finished.wait(5.0))
            self.assertTrue(responses[0]["payload"]["ok"])
            started = agent.start_capture(config)
        finally:
            dispatcher.close()

        self.assertTrue(started["running"])
        self.assertEqual(manager.starts, 1)

    def test_remote_start_requires_current_snapshot_and_frame_capability(self) -> None:
        config = self.remote_config()

        class FakeManager:
            def __init__(self):
                self.starts = 0

            def start(self, incoming):
                self.starts += 1
                return {"capture_uuid": incoming.capture_uuid, "running": True}

        manager = FakeManager()
        agent = LocalCaptureAgent(manager=manager)
        with self.assertRaisesRegex(RuntimeError, "remote_readiness_required"):
            agent.start_capture(config)

        snapshot = ReadinessSnapshot.create(
            config,
            {
                "ok": True,
                "checked_at": time.time(),
                "interfaces": [
                    {
                        "id": "temperature-device",
                        "physical_interface_id": "serial:COM9",
                        "state": "ready",
                    }
                ],
                "sensors": [{"name": "温度", "state": "ready"}],
            },
            capabilities={
                "unified_frame_v1": True,
                "unified_frame_contract": "unified_frame_v1",
            },
        )
        agent.import_readiness_snapshot(snapshot.to_dict())
        started = agent.start_capture(config)

        self.assertTrue(started["running"])
        self.assertEqual(manager.starts, 1)

        incompatible = self.remote_config(helper_capabilities={})
        agent.import_readiness_snapshot(snapshot.to_dict())
        with self.assertRaisesRegex(RuntimeError, "helper_capability_missing"):
            agent.start_capture(incompatible)

        wrong_revision = self.remote_config(
            helper_capabilities={
                "unified_frame_v1": True,
                "unified_frame_contract": "unified_frame_v2",
            }
        )
        agent.import_readiness_snapshot(snapshot.to_dict())
        with self.assertRaisesRegex(RuntimeError, "helper_capability_mismatch"):
            agent.start_capture(wrong_revision)

    @staticmethod
    def frame(capture_uuid: str, sequence: int, value: float) -> dict:
        return UnifiedFrame(
            capture_sequence=sequence,
            target_monotonic=20.0 + sequence / 10.0,
            target_wall_time=1000.0 + sequence / 10.0,
            channels={
                "温度": ChannelSample(
                    interface_id="temperature-device",
                    channel_name="温度",
                    value=value,
                    quality=ChannelQuality.MEASURED_NEW,
                    device_timestamp=1000.0 + sequence / 10.0,
                    received_wall_time=1000.0 + sequence / 10.0,
                    received_monotonic=20.0 + sequence / 10.0,
                    source_sequence=sequence,
                    protocol_ok=True,
                    metadata={"age_seconds": 0.0},
                )
            },
            assembled_monotonic=20.01 + sequence / 10.0,
        ).to_envelope(capture_uuid, {"温度": "C"})

    def test_remote_frames_replay_idempotently_and_reject_conflict_or_gap(self) -> None:
        mirror = RemoteAcquisitionMirror()
        capture_uuid = "capture-remote"
        first = {
            "capture_uuid": capture_uuid,
            "sequence": 0,
            "rows": [{"温度": 10.0}],
            "timestamps": [1000.0],
            "frames": [self.frame(capture_uuid, 0, 10.0)],
        }

        accepted = mirror.ingest(first)
        duplicate = mirror.ingest(first)
        conflict = dict(first)
        conflict["frames"] = [self.frame(capture_uuid, 0, 11.0)]
        rejected_conflict = mirror.ingest(conflict)
        rejected_gap = mirror.ingest(
            {
                "capture_uuid": capture_uuid,
                "sequence": 1,
                "rows": [{"温度": 12.0}],
                "timestamps": [1000.2],
                "frames": [self.frame(capture_uuid, 2, 12.0)],
            }
        )

        self.assertTrue(accepted["ok"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(rejected_conflict["error"], "frame_conflict")
        self.assertEqual(rejected_gap["error"], "frame_sequence_gap")
        self.assertEqual(rejected_gap["expected_frame_sequence"], 1)
        self.assertEqual(len(mirror.frame_envelopes()), 1)
        self.assertEqual(mirror.status()["fault_domain"], "remote_helper_transport")

    def test_negotiated_remote_frame_cannot_be_missing_or_disagree_with_legacy_projection(self) -> None:
        capture_uuid = "capture-strict"
        mirror = RemoteAcquisitionMirror()
        mirror.configure_contract(["温度"], {"温度": "C"})
        missing = mirror.ingest(
            {
                "capture_uuid": capture_uuid,
                "sequence": 0,
                "required_frame_contract": "unified_frame_v1",
                "rows": [{"温度": 1.0}],
                "timestamps": [1000.0],
            }
        )
        self.assertEqual(missing["error"], "unified_frames_required")

        frame = self.frame(capture_uuid, 0, 1.0)
        mismatch = mirror.ingest(
            {
                "capture_uuid": capture_uuid,
                "sequence": 0,
                "required_frame_contract": "unified_frame_v1",
                "rows": [{"温度": 999.0}],
                "timestamps": [frame["frame_time"]],
                "frames": [frame],
            }
        )
        self.assertEqual(mismatch["error"], "frame_projection_mismatch")

        invalid_time = mirror.ingest(
            {
                "capture_uuid": capture_uuid,
                "sequence": 0,
                "required_frame_contract": "unified_frame_v1",
                "rows": [{"温度": 1.0}],
                "timestamps": [float("nan")],
                "frames": [frame],
            }
        )
        self.assertEqual(invalid_time["error"], "sample_timestamp_invalid")

        epoch_frame = self.frame(capture_uuid, 0, 1.0)
        epoch_frame["frame_time"] = 1_700_000_000.0
        loose_epoch_time = mirror.ingest(
            {
                "capture_uuid": capture_uuid,
                "sequence": 0,
                "required_frame_contract": "unified_frame_v1",
                "rows": [{"温度": 1.0}],
                "timestamps": [1_700_000_001.0],
                "frames": [epoch_frame],
            }
        )
        self.assertEqual(loose_epoch_time["error"], "frame_projection_mismatch")

        empty_channels = dict(frame)
        empty_channels["channels"] = {}
        uncovered = mirror.ingest(
            {
                "capture_uuid": capture_uuid,
                "sequence": 0,
                "required_frame_contract": "unified_frame_v1",
                "rows": [{"温度": 999.0}],
                "timestamps": [frame["frame_time"]],
                "frames": [empty_channels],
            }
        )
        self.assertEqual(uncovered["error"], "frame_channel_contract_mismatch")

    def test_missing_channel_blank_projection_is_validated_without_mutation(self) -> None:
        capture_uuid = "capture-missing"
        mirror = RemoteAcquisitionMirror()
        mirror.configure_contract(["温度"], {"温度": "C"})
        frame = UnifiedFrame(
            capture_sequence=0,
            target_monotonic=20.0,
            target_wall_time=1000.0,
            channels={
                "温度": ChannelSample(
                    interface_id="temperature-device",
                    channel_name="温度",
                    value=None,
                    quality=ChannelQuality.MISSING,
                    received_wall_time=1000.0,
                    received_monotonic=20.0,
                    error="no-sample",
                )
            },
            assembled_monotonic=20.01,
        ).to_envelope(capture_uuid, {"温度": "C"})
        batch = {
            "capture_uuid": capture_uuid,
            "sequence": 0,
            "required_frame_contract": "unified_frame_v1",
            "rows": [{"温度": ""}],
            "timestamps": [1000.0],
            "frames": [frame],
        }

        validated = mirror.validate(batch)

        self.assertTrue(validated["ok"], validated)
        self.assertEqual(mirror.numeric_matrix(), ([], []))
        accepted = mirror.ingest(batch)
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(mirror.numeric_matrix(), ([{"温度": None}], [1000.0]))

    def test_transport_metrics_use_canonical_sample_rate_hz(self) -> None:
        class FakeManager:
            def status(self):
                return {"config": {"sample_rate_hz": 25.0}}

            def stream_counts(self):
                return {"total_count": 50}

        agent = LocalCaptureAgent(manager=FakeManager())
        metrics = agent.stream_metrics()

        self.assertEqual(metrics["sample_rate_hz"], 25.0)
        self.assertEqual(metrics["queue_age_seconds"], 2.0)

    def test_pending_start_contract_does_not_replace_active_capture_contract(self) -> None:
        registry = RemoteAcquisitionRegistry()
        session_id = "session-contract"
        old_capture = "capture-old"
        registry.configure_capture_contract(
            session_id, old_capture, ["温度"], {"温度": "C"}
        )
        first = {
            "capture_uuid": old_capture,
            "sequence": 0,
            "required_frame_contract": "unified_frame_v1",
            "rows": [{"温度": 1.0}],
            "timestamps": [1000.0],
            "frames": [self.frame(old_capture, 0, 1.0)],
        }
        self.assertTrue(registry.ingest(session_id, first)["ok"])

        registry.stage_contract(
            session_id, "request-new", ["压力"], {"压力": "N"}
        )
        following = {
            "capture_uuid": old_capture,
            "sequence": 1,
            "required_frame_contract": "unified_frame_v1",
            "rows": [{"温度": 2.0}],
            "timestamps": [1000.1],
            "frames": [self.frame(old_capture, 1, 2.0)],
        }

        accepted = registry.ingest(session_id, following)

        self.assertTrue(accepted["ok"], accepted)

    def test_server_binds_remote_mode_session_and_negotiated_capabilities(self) -> None:
        app_path = Path(__file__).parent / "app.py"
        module = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
        function = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "helper_real_capture_payload"
        )
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(app_path), "exec"), namespace)
        helper_real_capture_payload = namespace["helper_real_capture_payload"]
        forwarded = helper_real_capture_payload(
            {
                "acquisition_mode": "real",
                "real_acquisition_mode": "local_direct",
                "execution_host": "server",
                "helper_session_id": "attacker-value",
                "helper_capabilities": {"unified_frame_v1": False},
                "mysql_password": "must-not-forward",
            },
            helper_session_id="trusted-session",
            helper_capabilities={"unified_frame_v1": True},
        )

        self.assertEqual(forwarded["real_acquisition_mode"], "remote_helper")
        self.assertEqual(forwarded["execution_host"], "helper_local")
        self.assertEqual(forwarded["helper_session_id"], "trusted-session")
        self.assertEqual(forwarded["helper_capabilities"], {"unified_frame_v1": True})
        self.assertNotIn("mysql_password", forwarded)

    def test_frontend_always_emits_explicit_real_acquisition_mode(self) -> None:
        source = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            'real_acquisition_mode: simulation ? "" : '
            '(usesLocalCaptureHelper() ? "remote_helper" : "local_direct")',
            source,
        )


if __name__ == "__main__":
    unittest.main()
