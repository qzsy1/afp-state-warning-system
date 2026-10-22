import unittest
import sys
import json
import subprocess
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


class WebSocketLiveProtocolTests(unittest.TestCase):
    def test_plc_status_evidence_survives_websocket_compaction(self):
        from websocket_live import compact_live_payload
        record = {"words":[0,49184],"decoded_value":-2.5,"normalized_value":0,
                  "negative_pressure_clamped":True,"source_sequence":3,"host_wall_time":100,
                  "measurement_profile":"fibercontrol_plc_v1"}
        status = {"interfaces":[{"driver":"modbus_tcp","plc_evidence":{"channel_records":{"压力":record}}}]}
        payload = {"channels":[],"acquisition":status}
        packed = compact_live_payload(payload)
        self.assertEqual(packed["acquisition"], status)
        json.dumps(packed, allow_nan=False)

    def test_compact_live_payload_round_trips_all_evidence_in_browser(self):
        from websocket_live import compact_live_payload

        sources = [{"interface_id": "thermocouple", "source_sequence": index,
                    "capture_generation": "generation", "start_request_id": "request",
                    "quality": "measured_new", "after_start": True,
                    "received_monotonic": 100.0 + index} for index in range(48)]
        channels = [{"id": index, "name": f"温度{index + 1}",
                     "actual": list(range(48)), "source_observations": deepcopy(sources),
                     "startup_curve": {"source_observations": sources[:2]}}
                    for index in range(64)]
        channels[0]["source_observations"][0] = None
        channels[1]["source_observations"][0]["extra"] = {"reference": [1, 2]}
        channels[2]["source_observations"][24]["quality"] = "held_within_freshness"
        channels[3]["source_observations"][24]["capture_generation"] = "another-generation"
        payload = {"channels": channels, "selected_channel": channels[0],
                   "thermocouple64": {"other_channels": [channels[-1]], "raw_history": [[1, None]]},
                   "acquisition": {"running": True, "sample_count": 48}}
        original = deepcopy(payload)
        packed = compact_live_payload(payload)
        self.assertEqual(payload, original)
        self.assertLess(len(json.dumps(packed)), len(json.dumps(original)) * .25)
        source = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        decoder = source[source.index("function expandLivePayload("):source.index("function applyRealtimePayload(")]
        script = "const fs=require('fs'),assert=require('assert/strict');\n" + decoder + "\n" + r'''
const {original,packed}=JSON.parse(fs.readFileSync(0,'utf8'));
const restored=expandLivePayload(packed);
assert.deepEqual(restored,original);
assert.equal(restored.selected_channel,restored.channels[0]);
assert.equal(restored.thermocouple64.other_channels[0],restored.channels[63]);
assert.equal(expandLivePayload(restored),restored);
assert.equal(expandLivePayload(original),original);
'''
        result = subprocess.run(["node", "-e", script], input=json.dumps({"original": original, "packed": packed}),
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compaction_preserves_errors_and_payloads_without_channels(self):
        from websocket_live import compact_live_payload
        payload = {"type": "error", "error": "device offline"}
        self.assertIs(compact_live_payload(payload), payload)

    def test_compaction_round_trips_copied_and_distinct_secondary_channels(self):
        from websocket_live import compact_live_payload

        channels = [{"id": index, "name": f"channel{index}", "actual": list(range(48)),
                     "source_observations": [{"source_sequence": n, "quality": "measured_new"} for n in range(48)]}
                    for index in range(3)]
        distinct = deepcopy(channels[0])
        distinct["actual"][17] = -999
        payload = {"channels": channels, "selected_channel": deepcopy(channels[1]),
                   "thermocouple64": {"other_channels": [deepcopy(channels[2]), distinct]}}
        original = deepcopy(payload)
        packed = compact_live_payload(payload)
        self.assertEqual(packed["selected_channel"], {"live_channel_index": 1})
        self.assertEqual(packed["thermocouple64"]["other_channels"][0], {"live_channel_index": 2})
        self.assertNotIn("live_channel_index", packed["thermocouple64"]["other_channels"][1])
        source = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        decoder = source[source.index("function expandLivePayload("):source.index("function applyRealtimePayload(")]
        script = "const fs=require('fs'),assert=require('assert/strict');\n" + decoder + "\n" + r'''
const {original,packed}=JSON.parse(fs.readFileSync(0,'utf8'));
assert.deepEqual(expandLivePayload(packed),original);
packed.selected_channel.actual[0]=-1;
assert.notEqual(packed.thermocouple64.other_channels[1].actual[0],-1);
'''
        result = subprocess.run(["node", "-e", script], input=json.dumps({"original": original, "packed": packed}),
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload, original)

    def test_version_poll_interval_is_bounded_for_first_curve_delivery(self):
        from websocket_live import (
            LIVE_PAYLOAD_ERROR_RETRY_SECONDS,
            LIVE_PAYLOAD_VERSION_POLL_SECONDS,
        )

        self.assertGreater(LIVE_PAYLOAD_VERSION_POLL_SECONDS, 0)
        self.assertLessEqual(LIVE_PAYLOAD_VERSION_POLL_SECONDS, 0.01)
        self.assertGreaterEqual(LIVE_PAYLOAD_ERROR_RETRY_SECONDS, 0.05)

    def test_versioned_payload_cache_reuses_payload_until_stream_changes(self):
        from websocket_live import VersionedPayloadCache

        class Acquisition:
            version = 0

            def stream_version(self):
                return self.version

        acquisition = Acquisition()
        builds = []
        cache = VersionedPayloadCache()

        first = cache.payload_for(acquisition, lambda: builds.append(0) or {"value": 1})
        unchanged = cache.payload_for(acquisition, lambda: builds.append(1) or {"value": 2})
        acquisition.version = 1
        changed = cache.payload_for(acquisition, lambda: builds.append(2) or {"value": 3})

        self.assertEqual(first, {"value": 1})
        self.assertIsNone(unchanged)
        self.assertEqual(changed, {"value": 3})
        self.assertEqual(builds, [0, 2])

    def test_failed_payload_build_does_not_consume_the_stream_version(self):
        from websocket_live import VersionedPayloadCache

        class Acquisition:
            def stream_version(self):
                return 7

        cache = VersionedPayloadCache()
        with self.assertRaisesRegex(RuntimeError, "temporary build failure"):
            cache.payload_for(
                Acquisition(),
                lambda: (_ for _ in ()).throw(RuntimeError("temporary build failure")),
            )
        self.assertEqual(
            cache.payload_for(Acquisition(), lambda: {"recovered": True}),
            {"recovered": True},
        )

    def test_rfc6455_accept_value_matches_reference(self):
        from websocket_live import websocket_accept_value

        self.assertEqual(
            websocket_accept_value("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_server_text_frame_uses_unmasked_payload(self):
        from websocket_live import encode_server_frame

        self.assertEqual(encode_server_frame("hello"), b"\x81\x05hello")

    def test_server_frame_supports_payloads_over_125_bytes(self):
        from websocket_live import encode_server_frame

        payload = "x" * 126
        frame = encode_server_frame(payload)
        self.assertEqual(frame[:4], b"\x81\x7e\x00\x7e")
        self.assertEqual(frame[4:].decode("utf-8"), payload)

    def test_live_frame_replaces_nonfinite_prediction_values_like_http(self):
        from websocket_live import encode_json_frame

        frame = encode_json_frame({"type": "live", "prediction": {"score": float("nan"), "upper": float("inf"), "lower": float("-inf")}})
        self.assertEqual(frame[0], 0x81)
        size = frame[1] & 0x7F
        self.assertEqual(
            json.loads(frame[2:2 + size].decode("utf-8")),
            {"type": "live", "prediction": {"score": None, "upper": None, "lower": None}},
        )


if __name__ == "__main__":
    unittest.main()
