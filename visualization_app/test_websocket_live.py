import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


class WebSocketLiveProtocolTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
