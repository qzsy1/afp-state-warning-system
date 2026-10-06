from __future__ import annotations

import gzip
from pathlib import Path
import tempfile
import unittest

from app import encode_static_file_response


class PublicDemoStaticDeliveryTests(unittest.TestCase):
    def test_content_addressed_demo_json_is_gzipped_and_immutable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            demo = root / "demo"
            demo.mkdir()
            path = demo / "quick_240.0123456789abcdef.json"
            original = ('{"values":[' + ','.join(str(index) for index in range(3000)) + ']}').encode()
            path.write_bytes(original)
            body, headers = encode_static_file_response(path, "gzip, deflate", root)
            self.assertEqual(gzip.decompress(body), original)
            self.assertEqual(headers["Content-Encoding"], "gzip")
            self.assertEqual(headers["Vary"], "Accept-Encoding")
            self.assertEqual(headers["Cache-Control"], "public, max-age=31536000, immutable")
            self.assertTrue(headers["ETag"].startswith('"'))

    def test_manifest_revalidates_and_ordinary_assets_keep_existing_semantics(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            demo = root / "demo"
            demo.mkdir()
            manifest = demo / "manifest-v1.json"
            manifest.write_text('{"packages":[]}', encoding="utf-8")
            body, headers = encode_static_file_response(manifest, "gzip", root)
            self.assertEqual(body, manifest.read_bytes())
            self.assertEqual(headers["Cache-Control"], "public, max-age=60, must-revalidate")
            ordinary = root / "app.js"
            ordinary.write_text("const value = 1;", encoding="utf-8")
            _body, ordinary_headers = encode_static_file_response(ordinary, "gzip", root)
            self.assertNotIn("Cache-Control", ordinary_headers)
            self.assertNotIn("Content-Encoding", ordinary_headers)


if __name__ == "__main__":
    unittest.main()
