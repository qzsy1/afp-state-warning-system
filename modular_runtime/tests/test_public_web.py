from __future__ import annotations

import sys
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "app" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from public_web import PublicWebConfig, inspect_cloudflared_service, public_web_urls  # noqa: E402


class PublicWebConfigTests(unittest.TestCase):
    def test_defaults_keep_guest_public_and_local_admin_ports_separate(self):
        config = PublicWebConfig.from_mapping({"enabled": True, "hostname": "afp.example.com"})
        self.assertTrue(config.enabled)
        self.assertEqual(config.public_bind_host, "0.0.0.0")
        self.assertEqual(config.public_port, 8770)
        self.assertEqual(config.local_admin_bind_host, "127.0.0.1")
        self.assertEqual(config.local_admin_port, 8771)
        self.assertEqual(config.origin_url, "http://127.0.0.1:8770")

    def test_public_service_stays_alive_after_desktop_window_closes(self):
        config = PublicWebConfig.from_mapping({"enabled": True})
        self.assertTrue(
            config.keep_alive_after_window_close,
            "公网源站不能因桌面窗口关闭而被回收",
        )

    def test_rejects_public_admin_port_collision(self):
        with self.assertRaises(ValueError):
            PublicWebConfig.from_mapping({"public_port": 8771})

    def test_urls_are_explicitly_separated(self):
        self.assertEqual(
            public_web_urls(PublicWebConfig.from_mapping({"enabled": True, "hostname": "afp.example.com"}), ["192.168.1.8"]),
            {
                "public": ["https://afp.example.com/", "http://192.168.1.8:8770/"],
                "local_admin": "http://127.0.0.1:8771/",
            },
        )

    def test_hostname_rejects_scheme_path_and_ip(self):
        for value in ("https://afp.example.com", "afp.example.com/path", "192.168.1.2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PublicWebConfig.from_mapping({"enabled": True, "hostname": value})

    def test_cloudflared_inspection_returns_only_safe_fields(self):
        result = inspect_cloudflared_service("cloudflared")
        self.assertEqual(set(result), {"installed", "running", "service_name", "error_code"})


if __name__ == "__main__":
    unittest.main()
