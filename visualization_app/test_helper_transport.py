from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_capture_helper_entry import dispatch_command  # noqa: E402


class HelperTransportTests(unittest.TestCase):
    def test_dispatch_discover_returns_structured_result(self):
        agent = Mock()
        agent.discover.return_value = {"sensor_bindings": []}
        result = dispatch_command(
            agent,
            '{"type":"command","request_id":"r1","command":"discover","payload":{}}',
        )
        self.assertEqual(result["type"], "result")
        self.assertEqual(result["request_id"], "r1")
        self.assertEqual(result["payload"]["sensor_bindings"], [])

    def test_dispatch_rejects_unknown_command(self):
        with self.assertRaises(ValueError):
            dispatch_command(
                Mock(),
                '{"type":"command","request_id":"r1","command":"shell"}',
            )


if __name__ == "__main__":
    unittest.main()
