from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


CORE_DIR = Path(__file__).resolve().parents[1] / "app" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from lan_web import (  # noqa: E402
    LanWebConfig,
    discover_lan_endpoints,
    discover_lan_urls,
    safe_network_status,
)


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
            "lan_web._iter_ipv4_candidates",
            return_value=[
                {"address": "127.0.0.1"},
                {"address": "169.254.20.3"},
                {"address": "192.168.1.20"},
                {"address": "10.0.0.8"},
                {"address": "198.18.0.1"},
            ],
        ):
            self.assertEqual(
                discover_lan_urls(8770),
                ["http://10.0.0.8:8770/", "http://192.168.1.20:8770/"],
            )

    def test_default_gateway_physical_adapter_is_recommended_before_hyper_v(self):
        endpoints = discover_lan_endpoints(
            8770,
            candidates=[
                {
                    "address": "172.29.32.1",
                    "interface_name": "vEthernet (Default Switch)",
                    "interface_type": "virtual",
                    "is_up": True,
                    "has_default_gateway": False,
                },
                {
                    "address": "192.168.101.31",
                    "interface_name": "以太网 3",
                    "interface_type": "ethernet",
                    "is_up": True,
                    "has_default_gateway": True,
                },
            ],
        )

        self.assertEqual(
            endpoints["recommended_url"],
            "http://192.168.101.31:8770/",
        )
        self.assertEqual(endpoints["urls"][0], endpoints["recommended_url"])
        self.assertTrue(endpoints["candidates"][0]["recommended"])
        self.assertFalse(endpoints["candidates"][1]["recommended"])

    def test_virtual_or_tunnel_adapter_is_never_the_recommended_url(self):
        endpoints = discover_lan_endpoints(
            8770,
            candidates=[
                {
                    "address": "100.80.20.3",
                    "interface_name": "Tailscale",
                    "interface_type": "tunnel",
                    "is_up": True,
                    "has_default_gateway": True,
                },
                {
                    "address": "192.168.50.20",
                    "interface_name": "Wi-Fi",
                    "interface_type": "wifi",
                    "is_up": True,
                    "has_default_gateway": False,
                },
            ],
        )

        self.assertEqual(
            endpoints["recommended_url"],
            "http://192.168.50.20:8770/",
        )
        self.assertEqual(endpoints["candidates"][0]["interface_name"], "Wi-Fi")

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

    def test_status_keeps_legacy_urls_and_exposes_recommended_candidate(self):
        endpoints = {
            "recommended_url": "http://192.168.101.31:8770/",
            "urls": [
                "http://192.168.101.31:8770/",
                "http://172.29.32.1:8770/",
            ],
            "candidates": [
                {
                    "address": "192.168.101.31",
                    "url": "http://192.168.101.31:8770/",
                    "recommended": True,
                }
            ],
        }
        status = safe_network_status(
            {"enabled": True, "bind_host": "0.0.0.0", "port": 8770},
            endpoints,
            started_at=10.0,
            now=12.5,
        )

        self.assertEqual(status["urls"], endpoints["urls"])
        self.assertEqual(status["recommended_url"], endpoints["recommended_url"])
        self.assertEqual(status["candidates"], endpoints["candidates"])


if __name__ == "__main__":
    unittest.main()
