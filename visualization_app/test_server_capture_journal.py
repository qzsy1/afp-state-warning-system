from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from edge_capture import RemoteAcquisitionRegistry
from server_capture_journal import ServerCaptureJournal, TargetMySQLSaveCoordinator
from server_target_mysql import ServerTargetProfiles


def sample(sequence, values, *, final=False, count=None):
    return {
        "capture_uuid": "transport-capture-a", "sequence": sequence,
        "rows": [{"温度": value} for value in values],
        "timestamps": [float(value) for value in values],
        "status": {
            "running": not final, "finalization_complete": final,
            "sample_count": count if count is not None else sequence + len(values),
            "config": {"capture_uuid": "specimen-a", "specimen_id": "specimen-a",
                       "run_id": "specimen-a-L1", "layer": 0,
                       "schema_sensors": ["温度"], "process_columns": []},
        },
    }


def unified_frame(sequence, value, *, timestamp=None):
    frame_time = float(value if timestamp is None else timestamp)
    return {
        "contract_version": "unified_frame_v1",
        "capture_uuid": "transport-capture-a",
        "frame_sequence": sequence,
        "frame_time": frame_time,
        "target_monotonic": frame_time,
        "assembled_monotonic": frame_time,
        "deadline_missed": False,
        "channels": {
            "温度": {
                "value": value,
                "unit": "°C",
                "quality": "measured_new",
                "source_timestamp": frame_time,
                "received_timestamp": frame_time,
                "age_seconds": 0.0,
                "is_new": True,
                "interface_id": "temperature",
                "source_sequence": sequence,
                "protocol_ok": True,
                "error": "",
            }
        },
    }


class ServerCaptureJournalTests(unittest.TestCase):
    def test_abb_motion_source_survives_durable_helper_journal_and_target_mysql(self):
        import json
        from real_acquisition import ChannelSampleCache, FrameAssembler
        from local_direct_acquisition import LocalDirectAdapter
        from test_mysql_identity import _FakeConnection
        from mysql_storage import MySQLCaptureStore, MySQLSettings
        from acquisition import AcquisitionConfig, AbbRobotDriver
        from unittest.mock import patch
        now=[10.0]
        positions=iter([(0,0,0),(3,4,0)])
        driver=AbbRobotDriver("http://fixture",clock=lambda:now[0],wall_clock=lambda:1000+now[0])
        driver._fetch_position=lambda:next(positions)
        driver.open()
        cache=ChannelSampleCache()
        names=["ABB_X","ABB_Y","ABB_Z","线速度"]
        adapter=LocalDirectAdapter("abb",driver,names,cache)
        for values,record in driver.read_samples(): adapter._publish(values,record)
        now[0]=10.1
        for values,record in driver.read_samples(): adapter._publish(values,record)
        frame=FrameAssembler(names,10,dict.fromkeys(names,1),start_monotonic=10,start_wall_time=1000).assemble(cache,10.2)
        envelope=frame.to_envelope("abb-remote",dict.fromkeys(names[:3],"mm") | {"线速度":"mm/s"})
        self.journal.disarm("session-a")
        self.journal.arm("session-a","selection-a",config={"selected_sensors":names})
        self.mirrors.configure_capture_contract("session-a","abb-remote",names,{})
        batch={"capture_uuid":"abb-remote","sequence":0,"rows":[frame.values()],"timestamps":[1000.2],
               "frames":[envelope],"required_frame_contract":"unified_frame_v1",
               "status":{"running":False,"finalization_complete":True,"sample_count":1}}
        ingested=self.journal.ingest("session-a",batch,self.mirrors)
        self.assertTrue(ingested["ok"],ingested)
        capture=self.journal.ready_capture("session-a","abb-remote")
        # 较新驱动状态不能改写已持久化帧的证据。
        driver.latest_record["xyz"][0]=999
        row=list(capture.rows)[0]
        record=row["_channel_source_records"]["线速度"]
        self.assertEqual(record["speed_basis"]["value_mm_s"],50)
        self.assertEqual(record["xyz"],[3,4,0])
        connection=_FakeConnection()
        store=MySQLCaptureStore(MySQLSettings(enabled=True,database="afp_fixture"))
        with patch.object(store,"_connect",return_value=("fake",connection)):
            result=store.save_layer(AcquisitionConfig(dataset_schema="new_collection_v11_3",selected_sensors=names),rows=capture.rows,layer_file=None,
                full_specimen_file=None,timestamp_file=None,folder_path=None,summary={},source_records=capture.rows.source_records())
        self.assertTrue(result["ok"],result)
        evidence=[json.loads(value[4]) for sql,values in connection.cursor_instance.executed
                  if "INSERT INTO afp_source_evidence" in sql for value in values if value[2]=="abb_frame"]
        self.assertEqual(evidence[0]["channels"],row["_channel_source_records"])

    def test_plc_source_survives_durable_helper_journal_and_target_mysql(self):
        import json
        from real_acquisition import ChannelSampleCache, FrameAssembler
        from local_direct_acquisition import LocalDirectAdapter
        from test_plc_modbus_contract import PlcModbusContractTests
        from test_mysql_identity import _FakeConnection
        from mysql_storage import MySQLCaptureStore, MySQLSettings
        from acquisition import AcquisitionConfig
        from unittest.mock import patch
        driver = PlcModbusContractTests().make_driver({"压力": -2.5}, clock=lambda:10.0, wall_clock=lambda:1000.0)
        cache = ChannelSampleCache()
        adapter = LocalDirectAdapter("plc", driver, ["压力"], cache)
        for values, record in driver.read_samples():
            adapter._publish(values, record)
        frame = FrameAssembler(["压力"], 10, {"压力":.5}, start_monotonic=10, start_wall_time=1000).assemble(cache,10.1)
        envelope = frame.to_envelope("plc-remote", {"压力":"N"})
        self.journal.disarm("session-a")
        self.journal.arm("session-a", "selection-a", config={"selected_sensors":["压力"]})
        self.mirrors.configure_capture_contract("session-a", "plc-remote", ["压力"], {"压力":"N"})
        batch = {"capture_uuid":"plc-remote", "sequence":0, "rows":[{"压力":0.0}],
                 "timestamps":[1000.1], "frames":[envelope], "required_frame_contract":"unified_frame_v1",
                 "status":{"running":False,"finalization_complete":True,"sample_count":1}}
        self.assertTrue(self.journal.ingest("session-a", batch, self.mirrors)["ok"])
        capture = self.journal.ready_capture("session-a", "plc-remote")
        row = list(capture.rows)[0]
        self.assertEqual(row["_channel_source_records"]["压力"]["decoded_value"], -2.5)
        connection = _FakeConnection()
        store = MySQLCaptureStore(MySQLSettings(enabled=True, database="afp_fixture"))
        with patch.object(store, "_connect", return_value=("fake",connection)):
            result = store.save_layer(AcquisitionConfig(selected_sensors=["压力"]), rows=capture.rows,
                                      layer_file=None,full_specimen_file=None,timestamp_file=None,
                                      folder_path=None,summary={},source_records=capture.rows.source_records())
        self.assertTrue(result["ok"], result)
        evidence = [json.loads(value[4]) for sql, values in connection.cursor_instance.executed
                    if "INSERT INTO afp_source_evidence" in sql for value in values]
        self.assertEqual(evidence[0]["channels"], row["_channel_source_records"])

    def test_async_start_binds_target_before_first_sample(self):
        self.journal.disarm("session-a")
        self.journal.arm_start("session-a", "request-1", "selection-a", config={"selected_sensors": ["温度"]})
        self.assertTrue(self.journal.bind_start_result("session-a", "request-1", {
            "accepted": True, "running": False, "capture_uuid": "capture-1",
            "start_operation": {"id": "operation-1", "state": "starting"},
        }))
        self.assertTrue(self.journal.manages("session-a", "capture-1"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = ServerCaptureJournal(Path(self.temp.name) / "journal.sqlite3", max_capture_bytes=300000)
        self.mirrors = RemoteAcquisitionRegistry(max_rows=24)
        self.journal.arm(
            "session-a", "selection-a", config={"selected_sensors": ["温度"]}
        )

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def test_ack_is_idempotent_and_final_status_waits_for_all_rows(self):
        first = sample(0, [0, 1])
        self.assertTrue(self.journal.ingest("session-a", first, self.mirrors)["ok"])
        duplicate = self.journal.ingest("session-a", first, self.mirrors)
        self.assertTrue(duplicate["duplicate"])
        self.assertFalse(self.journal.ingest("session-a", sample(2, [2]), self.mirrors)["ok"])
        self.assertFalse(self.journal.ingest("session-b", sample(0, [7]), self.mirrors)["ok"])
        self.assertTrue(self.journal.ingest("session-a", sample(1, [2], final=True, count=3), self.mirrors)["ok"])
        record = self.journal.ready_capture("session-a", "transport-capture-a")
        self.assertEqual(record.row_count, 3)
        self.assertEqual([row["温度"] for row in record.rows], [0, 1, 2])
        self.assertEqual(record.target_config_id, "selection-a")

    def test_unified_frame_rejection_never_commits_or_becomes_duplicate_ack(self):
        invalid = sample(0, [1], final=True, count=1)
        invalid["required_frame_contract"] = "unified_frame_v1"
        invalid["frames"] = [unified_frame(0, 999, timestamp=1)]

        first = self.journal.ingest("session-a", invalid, self.mirrors)
        repeated = self.journal.ingest("session-a", invalid, self.mirrors)

        self.assertFalse(first["ok"])
        self.assertEqual(first["error"], "frame_projection_mismatch")
        self.assertFalse(repeated["ok"])
        self.assertNotIn("duplicate", repeated)
        self.assertIsNone(
            self.journal.ready_capture("session-a", "transport-capture-a")
        )

        corrected = sample(0, [1], final=True, count=1)
        corrected["required_frame_contract"] = "unified_frame_v1"
        corrected["frames"] = [unified_frame(0, 1)]
        accepted = self.journal.ingest("session-a", corrected, self.mirrors)

        self.assertTrue(accepted["ok"], accepted)
        record = self.journal.ready_capture("session-a", "transport-capture-a")
        self.assertIsNotNone(record)
        self.assertEqual([row["温度"] for row in record.rows], [1])

    def test_esr_source_evidence_is_durable_and_part_of_duplicate_identity(self):
        from esr1001_usb import RAW_CHANNELS, measurement_record
        from esr1001_fixtures import USB_DATA, USB_EXPECTED_ADC
        row = dict(zip(RAW_CHANNELS, USB_EXPECTED_ADC), raw_hex=USB_DATA.hex(), receive_sequence=1,
                   calibration_digest="", connection_generation=1)
        row.update(measurement_record(row, None))
        batch = sample(0, [1], final=True, count=1)
        batch["esr_source_records"] = [{"interface_id": "esr", "record": row, "calibration_snapshot": None}]
        self.assertTrue(self.journal.ingest("session-a", batch, self.mirrors)["ok"])
        record = self.journal.ready_capture("session-a", "transport-capture-a")
        self.assertEqual(list(record.rows.source_records()), batch["esr_source_records"])
        self.assertTrue(self.journal.ingest("session-a", batch, self.mirrors)["duplicate"])
        changed = dict(batch, esr_source_records=[])
        self.assertEqual(self.journal.ingest("session-a", changed, self.mirrors)["error"], "sample_sequence_conflict")

    def test_restart_restores_mirror_before_duplicate_and_next_batch(self):
        for sequence, value in enumerate((1, 2)):
            current = sample(sequence, [value])
            current["required_frame_contract"] = "unified_frame_v1"
            current["frames"] = [unified_frame(sequence, value)]
            self.assertTrue(
                self.journal.ingest("session-a", current, self.mirrors)["ok"]
            )

        restarted_mirrors = RemoteAcquisitionRegistry(max_rows=24)
        duplicate = sample(1, [2])
        duplicate["required_frame_contract"] = "unified_frame_v1"
        duplicate["frames"] = [unified_frame(1, 2)]
        replay = self.journal.ingest("session-a", duplicate, restarted_mirrors)

        self.assertTrue(replay["ok"], replay)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(
            restarted_mirrors.for_session("session-a").status()[
                "remote_expected_sequence"
            ],
            2,
        )

        following = sample(2, [3], final=True, count=3)
        following["required_frame_contract"] = "unified_frame_v1"
        following["frames"] = [unified_frame(2, 3)]
        accepted = self.journal.ingest(
            "session-a", following, restarted_mirrors
        )
        self.assertTrue(accepted["ok"], accepted)
        record = self.journal.ready_capture("session-a", "transport-capture-a")
        self.assertEqual([row["温度"] for row in record.rows], [1, 2, 3])

    def test_target_journal_enforces_authoritative_channel_unit(self):
        wrong_unit = sample(0, [1], final=True, count=1)
        wrong_unit["required_frame_contract"] = "unified_frame_v1"
        frame = unified_frame(0, 1)
        frame["channels"]["温度"]["unit"] = "K"
        wrong_unit["frames"] = [frame]

        rejected = self.journal.ingest("session-a", wrong_unit, self.mirrors)

        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"], "frame_unit_contract_mismatch")
        self.assertIsNone(
            self.journal.ready_capture("session-a", "transport-capture-a")
        )

    def test_more_than_mirror_capacity_remains_in_full_journal(self):
        for index in range(55):
            ack = self.journal.ingest(
                "session-a", sample(index, [index], final=index == 54, count=index + 1), self.mirrors
            )
            self.assertTrue(ack["ok"], ack)
        record = self.journal.ready_capture("session-a", "transport-capture-a")
        self.assertEqual(record.row_count, 55)
        self.assertEqual(len(self.mirrors.for_session("session-a").numeric_matrix()[0]), 24)
        self.assertEqual([row["温度"] for row in record.rows], list(range(55)))

    def test_fifty_thousand_and_one_rows_are_retained_beyond_live_mirror_limit(self):
        journal = ServerCaptureJournal(Path(self.temp.name) / "large-journal.sqlite3", max_capture_bytes=32 * 1024 * 1024)
        mirrors = RemoteAcquisitionRegistry(max_rows=50_000)
        journal.arm("large-session", "selection-a")
        total = 50_001
        batch_size = 1_000
        for sequence, start in enumerate(range(0, total, batch_size)):
            values = list(range(start, min(start + batch_size, total)))
            ack = journal.ingest(
                "large-session",
                sample(sequence, values, final=start + len(values) == total, count=start + len(values)),
                mirrors,
            )
            self.assertTrue(ack["ok"], ack)
        record = journal.ready_capture("large-session", "transport-capture-a")
        self.assertEqual(record.row_count, total)
        self.assertEqual(len(mirrors.for_session("large-session").numeric_matrix()[0]), 50_000)
        recovered = iter(record.rows)
        self.assertEqual(next(recovered)["温度"], 0)
        for last in recovered:
            pass
        self.assertEqual(last["温度"], 50_000)

    def test_quota_refuses_batch_without_ack_or_mirror_change(self):
        self.journal.max_capture_bytes = 20
        rejected = self.journal.ingest("session-a", sample(0, [1]), self.mirrors)
        self.assertFalse(rejected["ok"])
        self.assertEqual(self.mirrors.for_session("session-a").numeric_matrix()[0], [])

    def test_incomplete_final_count_does_not_report_ready(self):
        self.journal.ingest("session-a", sample(0, [1], final=True, count=2), self.mirrors)
        self.assertIsNone(self.journal.ready_capture("session-a", "transport-capture-a"))

    def test_capture_keeps_safe_start_config_and_reconstructs_timestamps(self):
        self.journal.disarm("session-a")
        self.journal.arm(
            "session-a", "selection-a",
            config={"specimen_id": "specimen-a", "selected_sensors": ["温度"],
                    "mysql_password": "must-not-persist"},
            target={"scope": "server_target", "host": "db.example", "database": "afp"},
        )
        self.assertTrue(self.journal.ingest(
            "session-a", sample(0, [12], final=True, count=1), self.mirrors
        )["ok"])
        record = self.journal.ready_capture("session-a", "transport-capture-a")
        self.assertEqual(record.config["specimen_id"], "specimen-a")
        self.assertNotIn("mysql_password", str(record.config))
        self.assertEqual(list(record.rows)[0]["timestamp_unix"], 12.0)
        status = self.journal.capture_status("session-a", "transport-capture-a")
        self.assertEqual(status["state"], "ready")
        self.assertNotIn("must-not-persist", str(status))

    def test_start_intent_binds_only_the_helper_returned_capture_uuid(self):
        self.journal.disarm("session-a")
        self.journal.arm_start(
            "session-a", "request-a", "selection-a", config={"specimen_id": "specimen-a"}
        )
        self.assertFalse(self.journal.ingest("session-a", sample(0, [1]), self.mirrors)["ok"])
        self.assertTrue(self.journal.bind_start_result(
            "session-a", "request-a", {"running": True, "capture_uuid": "transport-capture-a"}
        ))
        self.assertTrue(self.journal.ingest("session-a", sample(0, [1]), self.mirrors)["ok"])
        wrong = sample(0, [2])
        wrong["capture_uuid"] = "transport-capture-b"
        self.assertFalse(self.journal.ingest("session-a", wrong, self.mirrors)["ok"])

    def test_server_target_save_uses_bound_profile_and_keeps_failed_capture_retryable(self):
        profiles = ServerTargetProfiles(lambda: {"target": {
            "host": "db.example", "port": 3306, "user": "afp_app",
            "database": "afp", "password": "server-only-secret",
        }})
        selection = profiles.resolve("session-a", {
            "mysql_enabled": True, "mysql_host": "db.example", "mysql_port": 3306,
            "mysql_user": "afp_app", "mysql_database": "afp", "mysql_password": "",
        })
        self.journal.disarm("session-a")
        self.journal.arm(
            "session-a", selection.config_id,
            config={"dataset_schema": "new_collection_v11_3", "driver": "simulator",
                    "processing_mode": "capture_only", "selected_sensors": ["温度"],
                    "specimen_id": "specimen-a"},
            target=selection.public(),
        )
        self.assertTrue(self.journal.ingest(
            "session-a", sample(0, [1], final=True, count=1), self.mirrors
        )["ok"])
        calls = []

        class Store:
            def __init__(self, settings):
                self.settings = settings

            def save_layer(self, config, **kwargs):
                calls.append((self.settings, config, list(kwargs["rows"])))
                return {"ok": len(calls) > 1, "saved_rows": 1, "error": "temporary"}

        coordinator = TargetMySQLSaveCoordinator(self.journal, profiles, Store)
        first = coordinator.save_now("session-a", "transport-capture-a")
        self.assertFalse(first["ok"])
        self.assertEqual(self.journal.capture_status("session-a", "transport-capture-a")["state"], "failed")
        second = coordinator.save_now("session-a", "transport-capture-a")
        self.assertTrue(second["ok"])
        state = self.journal.capture_status("session-a", "transport-capture-a")
        self.assertEqual(state["state"], "saved")
        self.assertEqual(calls[0][0].password, "server-only-secret")
        self.assertEqual(calls[0][1].capture_uuid, "transport-capture-a")
        self.assertEqual(calls[0][2][0]["timestamp_unix"], 1.0)
        self.assertNotIn("server-only-secret", str(state))

    def test_target_write_1045_is_exposed_as_safe_actionable_diagnostic(self):
        profiles = ServerTargetProfiles(lambda: {"target": {
            "host": "db.example", "port": 3306, "user": "afp_app",
            "database": "afp", "password": "server-only-secret",
        }})
        selection = profiles.resolve("session-a", {
            "mysql_enabled": True, "mysql_host": "db.example", "mysql_port": 3306,
            "mysql_user": "afp_app", "mysql_database": "afp", "mysql_password": "",
        })
        self.journal.disarm("session-a")
        self.journal.arm(
            "session-a", selection.config_id,
            config={"dataset_schema": "new_collection_v11_3", "driver": "simulator",
                    "processing_mode": "capture_only", "selected_sensors": ["温度"],
                    "specimen_id": "specimen-a"},
            target=selection.public(),
        )
        self.assertTrue(self.journal.ingest(
            "session-a", sample(0, [1], final=True, count=1), self.mirrors
        )["ok"])

        class Store:
            def __init__(self, _settings):
                pass

            def save_layer(self, *_args, **_kwargs):
                return {"ok": False, "saved_rows": 0,
                        "error": "[1045] Access denied for user 'afp_app'@'DESKTOP-410SFVI'"}

        result = TargetMySQLSaveCoordinator(self.journal, profiles, Store).save_now(
            "session-a", "transport-capture-a"
        )
        self.assertEqual(result["error_detail"]["category"], "authentication")
        status = self.journal.capture_status("session-a", "transport-capture-a")
        self.assertEqual(status["error_detail"]["code"], "1045")
        self.assertIn("用户@来源主机", status["error_detail"]["message"])
        self.assertNotIn("server-only-secret", str(status))


if __name__ == "__main__":
    unittest.main()
