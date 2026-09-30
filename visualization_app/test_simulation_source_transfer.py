from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import bind_ready_helper_simulation_payload, prepare_helper_simulation_payload
from acquisition import AcquisitionManager, FolderCsvSimulatorDriver, SimulatorDriver
from local_capture_agent import LocalCaptureAgent
from local_capture_helper_entry import HelperCommandDispatcher
from simulation_source_transfer import (
    MAX_TRANSFER_CHUNK_BYTES,
    SimulationSourceCache,
    SimulationSourceTicketStore,
    SimulationSourceTransferError,
    build_source_manifest,
)


class SimulationSourceTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "uploaded"
        self.source.mkdir()
        (self.source / "a.csv").write_bytes(b"temperature,pressure\n1,2\n")
        (self.source / "nested").mkdir()
        (self.source / "nested" / "b.csv").write_bytes(b"x,y\n3,4\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manifest_contains_only_safe_relative_paths_and_hashes(self):
        manifest, source_root = build_source_manifest(
            "source-a", "folder_csv", self.source
        )

        self.assertEqual(source_root, self.source.resolve())
        self.assertEqual(
            [item["relative_path"] for item in manifest["files"]],
            ["a.csv", "nested/b.csv"],
        )
        self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest["files"]))
        self.assertNotIn(str(self.root), str(manifest))

    def test_manifest_rejects_zero_byte_csv_before_helper_download(self):
        empty = self.source / "empty.csv"
        empty.write_bytes(b"")

        with self.assertRaisesRegex(SimulationSourceTransferError, "空|0 字节"):
            build_source_manifest("source-empty", "single_csv", empty)

    def test_manifest_rejects_csv_without_a_real_header(self):
        no_header = self.source / "no-header.csv"
        no_header.write_text("1,2\n3,4\n", encoding="utf-8")

        with self.assertRaisesRegex(SimulationSourceTransferError, "表头"):
            build_source_manifest("source-no-header", "single_csv", no_header)

    def test_manifest_rejects_csv_without_valid_numeric_rows(self):
        invalid = self.source / "invalid.csv"
        invalid.write_text("temperature,pressure\nbad,text\n", encoding="utf-8")

        with self.assertRaisesRegex(SimulationSourceTransferError, "有效数值"):
            build_source_manifest("source-invalid", "single_csv", invalid)

    def test_ticket_is_bound_to_helper_and_web_session(self):
        manifest, source_root = build_source_manifest(
            "source-a", "folder_csv", self.source
        )
        store = SimulationSourceTicketStore()
        transfer = store.issue(
            web_session_id="web-a",
            helper_session_id="helper-a",
            manifest=manifest,
            source_root=source_root,
        )

        with self.assertRaisesRegex(SimulationSourceTransferError, "会话"):
            store.read_chunk(
                transfer["ticket"], "helper-b", file_index=0, offset=0, limit=8
            )
        chunk = store.read_chunk(
            transfer["ticket"], "helper-a", file_index=0, offset=0, limit=8
        )
        self.assertEqual(chunk["relative_path"], "a.csv")
        self.assertLessEqual(len(chunk["data"]), 16)

    def test_cache_downloads_in_chunks_and_reuses_verified_content(self):
        manifest, source_root = build_source_manifest(
            "source-a", "folder_csv", self.source
        )
        store = SimulationSourceTicketStore()
        transfer = store.issue(
            web_session_id="web-a",
            helper_session_id="helper-a",
            manifest=manifest,
            source_root=source_root,
        )
        cache = SimulationSourceCache(self.root / "cache", chunk_bytes=7)
        calls = []

        def fetch(ticket, file_index, offset, limit):
            calls.append((file_index, offset, limit))
            return store.read_chunk(
                ticket, "helper-a", file_index=file_index, offset=offset, limit=limit
            )

        installed = cache.materialize(transfer, fetch)
        first_call_count = len(calls)
        reused = cache.materialize(transfer, fetch)

        self.assertEqual(installed, reused)
        self.assertEqual((installed / "a.csv").read_bytes(), (self.source / "a.csv").read_bytes())
        self.assertEqual(
            (installed / "nested" / "b.csv").read_bytes(),
            (self.source / "nested" / "b.csv").read_bytes(),
        )
        self.assertGreater(first_call_count, 2)
        self.assertEqual(len(calls), first_call_count)

    def test_default_cache_requests_the_largest_bounded_chunk(self):
        manifest, source_root = build_source_manifest(
            "source-a", "single_csv", self.source / "a.csv"
        )
        store = SimulationSourceTicketStore()
        transfer = store.issue(
            web_session_id="web-a",
            helper_session_id="helper-a",
            manifest=manifest,
            source_root=source_root,
        )
        cache = SimulationSourceCache(self.root / "cache")
        limits = []

        def fetch(ticket, file_index, offset, limit):
            limits.append(limit)
            return store.read_chunk(
                ticket, "helper-a", file_index=file_index, offset=offset, limit=limit
            )

        cache.materialize(transfer, fetch)

        self.assertEqual(limits, [MAX_TRANSFER_CHUNK_BYTES])

    def test_agent_reports_cache_hit_and_does_not_refetch_verified_bytes(self):
        manifest, source_root = build_source_manifest(
            "source-a", "single_csv", self.source / "a.csv"
        )
        store = SimulationSourceTicketStore()
        transfer = store.issue(
            web_session_id="web-a",
            helper_session_id="helper-a",
            manifest=manifest,
            source_root=source_root,
        )
        agent = LocalCaptureAgent(simulation_cache_root=self.root / "helper-cache")

        first = agent.prepare_simulation_source(
            transfer,
            lambda ticket, file_index, offset, limit: store.read_chunk(
                ticket,
                "helper-a",
                file_index=file_index,
                offset=offset,
                limit=limit,
            ),
        )
        second = agent.prepare_simulation_source(
            transfer,
            lambda *_args: self.fail("cache hit must not download bytes again"),
        )

        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])

    def test_single_csv_replay_streams_and_loops_without_pandas_materialization(self):
        source = self.source / "stream.csv"
        source.write_text("温度,压力\n1,2\n3,4\n", encoding="utf-8")
        driver = SimulatorDriver(source, ["温度", "压力"])

        with patch("acquisition.pd.read_csv", side_effect=AssertionError("must stream")):
            driver.open()
            samples = [driver.read_sample() for _ in range(3)]
            driver.close()

        self.assertEqual(samples[0], {"温度": 1.0, "压力": 2.0})
        self.assertEqual(samples[1], {"温度": 3.0, "压力": 4.0})
        self.assertEqual(samples[2], samples[0])
        self.assertFalse(hasattr(driver, "records"))

    def test_folder_csv_replay_streams_files_in_order_and_loops(self):
        folder = self.root / "stream-folder"
        folder.mkdir()
        (folder / "a.csv").write_text("温度\n1\n", encoding="utf-8")
        (folder / "b.csv").write_text("温度\n2\n", encoding="utf-8")
        driver = FolderCsvSimulatorDriver(folder, ["温度"])

        with patch("acquisition.pd.read_csv", side_effect=AssertionError("must stream")):
            driver.open()
            values = [driver.read_sample()["温度"] for _ in range(3)]
            driver.close()

        self.assertEqual(values, [1.0, 2.0, 1.0])

    def test_helper_rejects_verified_bytes_when_selected_channels_are_missing(self):
        manifest, source_root = build_source_manifest(
            "source-a", "single_csv", self.source / "a.csv"
        )
        store = SimulationSourceTicketStore()
        transfer = store.issue(
            web_session_id="web-a",
            helper_session_id="helper-a",
            manifest=manifest,
            source_root=source_root,
        )
        agent = LocalCaptureAgent(
            simulation_cache_root=self.root / "helper-cache",
        )

        with self.assertRaisesRegex(ValueError, "缺少.*通道|通道.*缺少"):
            agent.prepare_simulation_source(
                transfer,
                lambda ticket, file_index, offset, limit: store.read_chunk(
                    ticket,
                    "helper-a",
                    file_index=file_index,
                    offset=offset,
                    limit=limit,
                ),
                required_channels=["温度1"],
            )

    def test_content_cache_reuses_same_bytes_after_browser_source_id_changes(self):
        manifest, source_root = build_source_manifest(
            "source-a", "single_csv", self.source / "a.csv"
        )
        store = SimulationSourceTicketStore()
        first = store.issue(
            web_session_id="web-a",
            helper_session_id="helper-a",
            manifest=manifest,
            source_root=source_root,
        )
        cache = SimulationSourceCache(self.root / "cache", chunk_bytes=8)

        def fetch(ticket, file_index, offset, limit):
            return store.read_chunk(
                ticket, "helper-a", file_index=file_index, offset=offset, limit=limit
            )

        cache.materialize(first, fetch)
        second_manifest = dict(manifest)
        second_manifest["source_id"] = "source-b"
        second = store.issue(
            web_session_id="web-a",
            helper_session_id="helper-a",
            manifest=second_manifest,
            source_root=source_root,
        )

        cache.materialize(
            second,
            lambda *_args: self.fail("verified content cache should not download again"),
        )

    def test_cache_rejects_hash_mismatch_without_publishing_partial_content(self):
        manifest, source_root = build_source_manifest(
            "source-a", "single_csv", self.source / "a.csv"
        )
        store = SimulationSourceTicketStore()
        transfer = store.issue(
            web_session_id="web-a",
            helper_session_id="helper-a",
            manifest=manifest,
            source_root=source_root,
        )
        transfer["manifest"]["files"][0]["sha256"] = "0" * 64
        transfer["manifest"]["content_sha256"] = "1" * 64
        cache = SimulationSourceCache(self.root / "cache", chunk_bytes=8)

        with self.assertRaisesRegex(SimulationSourceTransferError, "SHA-256"):
            cache.materialize(
                transfer,
                lambda ticket, file_index, offset, limit: store.read_chunk(
                    ticket,
                    "helper-a",
                    file_index=file_index,
                    offset=offset,
                    limit=limit,
                ),
            )

        self.assertFalse((self.root / "cache" / ("1" * 64)).exists())

    def test_server_prepares_session_bound_helper_payload_without_target_secret(self):
        manifest, source_root = build_source_manifest(
            "source-a", "single_csv", self.source / "a.csv"
        )
        manager = SimpleNamespace(
            source_transfer_manifest=lambda session_id, source_id: (
                manifest,
                source_root,
            )
        )
        store = SimulationSourceTicketStore()

        payload = prepare_helper_simulation_payload(
            manager,
            store,
            SimpleNamespace(guest_id="web-a"),
            "helper-a",
            {
                "acquisition_mode": "simulation",
                "simulation_source_id": "source-a",
                "mysql_enabled": True,
                "mysql_password": "server-secret",
                "mysql_local_enabled": True,
            },
        )

        self.assertEqual(payload["execution_host"], "helper_local")
        self.assertIn("simulation_source_transfer", payload)
        self.assertNotIn("mysql_password", payload)
        self.assertFalse(payload["mysql_enabled"])
        self.assertTrue(payload["mysql_local_enabled"])
        self.assertNotIn(str(self.root), str(payload["simulation_source_transfer"]))

    def test_server_binds_start_only_to_current_session_ready_hash(self):
        manifest, source_root = build_source_manifest(
            "source-a", "single_csv", self.source / "a.csv"
        )
        manager = SimpleNamespace(
            source_transfer_manifest=lambda session_id, source_id: (
                manifest,
                source_root,
            )
        )
        identity = SimpleNamespace(guest_id="web-a")
        ready = {
            "source_id": "source-a",
            "source_type": "single_csv",
            "content_sha256": manifest["content_sha256"],
            "relative_paths": ["a.csv"],
        }

        payload = bind_ready_helper_simulation_payload(
            manager,
            identity,
            {"simulation_source_id": "source-a", "simulation_source_ready": ready},
        )

        self.assertEqual(payload["simulation_source_ready"]["content_sha256"], manifest["content_sha256"])
        self.assertNotIn("path", payload["simulation_source_ready"])
        with self.assertRaisesRegex(Exception, "就绪|重新"):
            bind_ready_helper_simulation_payload(
                manager,
                identity,
                {
                    "simulation_source_id": "source-a",
                    "simulation_source_ready": {**ready, "content_sha256": "0" * 64},
                },
            )

    def test_downloaded_source_runs_through_real_helper_capture_and_local_csv(self):
        source = self.source / "replay.csv"
        source.write_text(
            "温度1,压力\n"
            + "\n".join(f"{350 + index},{400 + index}" for index in range(20))
            + "\n",
            encoding="utf-8",
        )
        manifest, source_root = build_source_manifest(
            "source-replay", "single_csv", source
        )
        store = SimulationSourceTicketStore()
        transfer = store.issue(
            web_session_id="web-a",
            helper_session_id="helper-a",
            manifest=manifest,
            source_root=source_root,
        )
        save_root = self.root / "capture-output"
        save_root.mkdir()
        manager = AcquisitionManager(capture_root=save_root)
        agent = LocalCaptureAgent(
            manager=manager,
            simulation_cache_root=self.root / "helper-cache",
        )
        dispatcher = HelperCommandDispatcher(
            agent,
            source_fetcher=lambda ticket, file_index, offset, limit: store.read_chunk(
                ticket,
                "helper-a",
                file_index=file_index,
                offset=offset,
                limit=limit,
            ),
        )
        responses = []
        try:
            dispatcher.submit(
                {
                    "type": "command",
                    "request_id": "start-replay",
                    "command": "start_capture",
                    "payload": {
                        "acquisition_mode": "simulation",
                        "driver": "simulator",
                        "processing_mode": "capture_only",
                        "dataset_schema": "legacy_original",
                        "sample_rate_hz": 50.0,
                        "selected_sensors": ["温度1", "压力"],
                        "prediction_sensors": [],
                        "model_input_sensors": [],
                        "model_output_sensors": [],
                        "save_root": str(save_root),
                        "specimen_id": "HELPER_SIM_E2E",
                        "simulation_source_transfer": transfer,
                    },
                },
                responses.append,
            )
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not responses:
                time.sleep(0.01)
            self.assertTrue(responses, "helper simulation start did not return")
            self.assertTrue(responses[0]["payload"].get("running"), responses[0])
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and manager.status()["sample_count"] < 5:
                time.sleep(0.02)
            stopped = agent.stop_capture()

            self.assertGreaterEqual(stopped["sample_count"], 5)
            self.assertTrue(stopped["capture_saved"], stopped)
            self.assertEqual(stopped["execution_host"], "helper_local")
            self.assertEqual(stopped["source_transfer"]["state"], "ready")
            self.assertTrue(Path(stopped["layer_file"]).is_file())
        finally:
            dispatcher.close()
            if manager.status()["running"]:
                manager.stop()


if __name__ == "__main__":
    unittest.main()
