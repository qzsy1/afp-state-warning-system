from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from app import prepare_helper_simulation_payload
from acquisition import AcquisitionManager
from local_capture_agent import LocalCaptureAgent
from local_capture_helper_entry import HelperCommandDispatcher
from simulation_source_transfer import (
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
