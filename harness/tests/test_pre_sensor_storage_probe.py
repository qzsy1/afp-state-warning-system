from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from visualization_app.pre_sensor_preflight import probe_save_directory


class SaveDirectoryProbeTests(unittest.TestCase):
    def test_create_write_close_rename_and_cleanup_leave_no_probe_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "capture"
            outcome = probe_save_directory(
                {
                    "save_path": str(root),
                    "minimum_free_bytes": 1,
                    "maximum_write_seconds": 2,
                }
            )
            self.assertEqual(outcome.status, "passed")
            self.assertTrue(root.is_dir())
            self.assertEqual(list(root.glob(".afp-preflight-*")), [])
            self.assertGreater(outcome.details["bytes_written"], 0)

    def test_empty_unwritable_long_and_insufficient_paths_fail_by_stage(self) -> None:
        with self.assertRaisesRegex(ValueError, "save path is empty"):
            probe_save_directory({"save_path": ""})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "create"):
                probe_save_directory(
                    {
                        "save_path": str(root / "capture"),
                        "directory_factory": lambda _path: (_ for _ in ()).throw(
                            PermissionError("denied")
                        ),
                    }
                )
            with self.assertRaisesRegex(ValueError, "path length"):
                probe_save_directory(
                    {
                        "save_path": str(root / ("x" * 80)),
                        "maximum_path_length": 40,
                    }
                )
            with self.assertRaisesRegex(ValueError, "free space"):
                probe_save_directory(
                    {
                        "save_path": str(root / "space"),
                        "minimum_free_bytes": 1024,
                        "disk_usage": lambda _path: SimpleNamespace(free=100),
                    }
                )

    def test_slow_write_rename_and_cleanup_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "slow"

            def slow_writer(path: Path, content: bytes) -> None:
                time.sleep(0.03)
                path.write_bytes(content)

            with self.assertRaisesRegex(ValueError, "slow write"):
                probe_save_directory(
                    {
                        "save_path": str(root),
                        "minimum_free_bytes": 1,
                        "maximum_write_seconds": 0.01,
                        "writer": slow_writer,
                    }
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "rename"
            with self.assertRaisesRegex(ValueError, "rename"):
                probe_save_directory(
                    {
                        "save_path": str(root),
                        "minimum_free_bytes": 1,
                        "renamer": lambda _source, _target: (_ for _ in ()).throw(
                            OSError("rename denied")
                        ),
                    }
                )
            self.assertEqual(list(root.glob(".afp-preflight-*")), [])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cleanup"

            def cleanup_failure(path: Path) -> None:
                raise PermissionError(f"cannot remove {path.name}")

            with self.assertRaisesRegex(ValueError, "cleanup"):
                probe_save_directory(
                    {
                        "save_path": str(root),
                        "minimum_free_bytes": 1,
                        "cleanup": cleanup_failure,
                    }
                )
            self.assertTrue(list(root.glob(".afp-preflight-*")))


if __name__ == "__main__":
    unittest.main()
