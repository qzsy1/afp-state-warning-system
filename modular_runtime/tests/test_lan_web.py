from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


CORE_DIR = Path(__file__).resolve().parents[1] / "app" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from lan_web import LanWebConfig, discover_lan_urls, safe_network_status  # noqa: E402


class LanWebConfigTests(unittest.TestCase):
    def test_defaults_use_fixed_lan_port(self):
        config = LanWebConfig.from_mapping({})
        self.assertTrue(config.enabled)
        self.assertEqual(config.bind_host, "0.0.0.0")
        self.assertEqual(config.port, 8770)
        self.assertTrue(config.open_desktop_window)

    def test_rejects_invalid_port(self):
        with self.assertRaises(ValueError):
            LanWebConfig.from_mapping({"port": 80})

    def test_filters_loopback_and_apipa(self):
        with patch(
            "lan_web._iter_ipv4_addresses",
            return_value=[
                "127.0.0.1",
                "169.254.20.3",
                "192.168.1.20",
                "10.0.0.8",
                "198.18.0.1",
            ],
        ):
            self.assertEqual(
                discover_lan_urls(8770),
                ["http://10.0.0.8:8770/", "http://192.168.1.20:8770/"],
            )

    def test_status_is_sanitized(self):
        status = safe_network_status(
            {"enabled": True, "bind_host": "0.0.0.0", "port": 8770},
            ["http://192.168.1.20:8770/"],
            started_at=10.0,
            now=12.5,
            error=None,
        )
        self.assertEqual(status["service_uptime_seconds"], 2.5)
        self.assertNotIn("api_key", status)


if __name__ == "__main__":
    unittest.main()
