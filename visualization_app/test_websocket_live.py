import unittest
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


class WebSocketLiveProtocolTests(unittest.TestCase):
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
