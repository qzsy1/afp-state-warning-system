from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_capture_agent import HelperTransport  # noqa: E402
from local_capture_helper_entry import load_saved_runtime_config, save_runtime_config  # noqa: E402
from websocket_live import decode_client_frame  # noqa: E402


class HelperWebSocketTransportTests(unittest.TestCase):
    def test_helper_websocket_contract_carries_sample_batches_and_acknowledgements(self):
        server = Path(__file__).with_name("app.py").read_text(encoding="utf-8")
        helper = Path(__file__).with_name("local_capture_helper_entry.py").read_text(encoding="utf-8")

        self.assertIn('message_type == "sample_batch"', server)
        self.assertIn('"type": "sample_ack"', server)
        self.assertIn('message.get("type") == "sample_ack"', helper)
        self.assertIn("next_sample_batch", helper)

    def test_live_routes_select_session_remote_acquisition(self):
        server = Path(__file__).with_name("app.py").read_text(encoding="utf-8")
        self.assertIn("def _request_acquisition", server)
        self.assertIn("acquisition = acquisition or self._request_acquisition()", server)
        self.assertIn('parsed.path == "/api/acquisition/status"', server)
        self.assertIn("self._request_acquisition().status()", server)

    def test_websocket_url_adds_helper_endpoint_and_device_id(self):
        transport = HelperTransport(
            "https://afp.example.test/base",
            "pair-token",
            device_id="pc-01",
        )

        with patch("websocket.create_connection", return_value=object()) as create:
            transport.connect_once()

        url = create.call_args.args[0]
        parsed = urlsplit(url)
        self.assertEqual(parsed.scheme, "wss")
        self.assertEqual(parsed.path, "/api/helper/ws")
        self.assertEqual(parse_qs(parsed.query)["device_id"], ["pc-01"])
        self.assertIn("Authorization: Bearer pair-token", create.call_args.kwargs["header"])

    def test_client_frame_decoder_accepts_masked_json_text(self):
        payload = json.dumps({"type": "heartbeat"}, separators=(",", ":")).encode()
        mask = b"\x01\x02\x03\x04"
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        frame = bytes([0x81, 0x80 | len(payload)]) + mask + masked

        opcode, decoded = decode_client_frame(frame)

        self.assertEqual(opcode, 0x1)
        self.assertEqual(json.loads(decoded), {"type": "heartbeat"})

    def test_saved_https_transport_migrates_to_auto_with_http_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_runtime_config(
                "https://afp.example.test",
                "pair-token",
                transport="https",
                path=path,
            )
            saved = load_saved_runtime_config(path=path)

        self.assertIsNotNone(saved)
        self.assertEqual(saved["transport"], "auto")


if __name__ == "__main__":
    unittest.main()
