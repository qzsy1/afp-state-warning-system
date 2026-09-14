from __future__ import annotations

import sys
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "app" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from public_web import PublicWebConfig, public_web_urls  # noqa: E402


class PublicWebConfigTests(unittest.TestCase):
    def test_defaults_keep_guest_public_and_local_admin_ports_separate(self):
        config = PublicWebConfig.from_mapping({})
        self.assertTrue(config.enabled)
        self.assertEqual(config.public_bind_host, "0.0.0.0")
        self.assertEqual(config.public_port, 8770)
        self.assertEqual(config.local_admin_bind_host, "127.0.0.1")
        self.assertEqual(config.local_admin_port, 8771)
        self.assertTrue(config.open_desktop_window)

    def test_rejects_public_admin_port_collision(self):
        with self.assertRaises(ValueError):
            PublicWebConfig.from_mapping({"public_port": 8771})

    def test_urls_are_explicitly_separated(self):
        self.assertEqual(
            public_web_urls(PublicWebConfig.from_mapping({}), ["192.168.1.8"]),
            {
                "public": ["http://192.168.1.8:8770/"],
                "local_admin": "http://127.0.0.1:8771/",
            },
        )


if __name__ == "__main__":
    unittest.main()
