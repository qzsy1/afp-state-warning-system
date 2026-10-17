from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from visualization_app.pre_sensor_preflight import (
    detect_runtime_driver_statuses,
    probe_candidate_runtime,
)


def _pe(machine: int = 0x8664) -> bytes:
    payload = bytearray(256)
    payload[0:2] = b"MZ"
    payload[0x3C:0x40] = (0x80).to_bytes(4, "little")
    payload[0x80:0x84] = b"PE\0\0"
    payload[0x84:0x86] = machine.to_bytes(2, "little")
    return bytes(payload)


class CandidateRuntimeProbeTests(unittest.TestCase):
    def test_windows_hid_uses_the_built_in_driver_dll_not_a_python_hid_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "native_dll").mkdir()
            (root / "native_dll" / "BsvUvcNative.dll").write_bytes(b"dll")
            loaded: list[str] = []
            statuses = detect_runtime_driver_statuses(
                root,
                platform_name="nt",
                hid_loader=lambda name: loaded.append(name) or object(),
            )
        self.assertTrue(statuses["smrf_hid"])
        self.assertEqual(loaded, ["hid.dll"])
        self.assertTrue(statuses["serial_port"])
        self.assertTrue(statuses["modbus_rtu_esr1001"])
        self.assertTrue(statuses["uvc"])
    def _candidate(self, root: Path, *, native: bytes | None = None) -> dict:
        runtime = root / "_internal" / "python311.dll"
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes(_pe())
        native_path = root / "native_dll" / "vendor.dll"
        if native is not None:
            native_path.parent.mkdir()
            native_path.write_bytes(native)
        helper_path = root / "local_helper" / "AFP_Local_Capture_Helper.exe"
        helper_path.parent.mkdir()
        helper_path.write_bytes(b"helper")
        manifest = {
            "runtime_requirements": {
                "python_runtime": "bundled",
                "required_modules": ["json", "required_vendor_module"],
                "required_executables": [
                    {
                        "id": "local_capture_helper",
                        "path": "local_helper/AFP_Local_Capture_Helper.exe",
                        "sha256": hashlib.sha256(b"helper").hexdigest(),
                    }
                ],
                "native_components": [
                    {
                        "id": "vendor",
                        "path": "native_dll/vendor.dll",
                        "sha256": hashlib.sha256(native or b"").hexdigest(),
                    }
                ],
                "vendor_sdks": [
                    {
                        "id": "vendor_sdk",
                        "required_for": "camera",
                        "detection": "native_dll/vendor.dll",
                    }
                ],
                "drivers": [
                    {"id": "serial_port", "required_for": "pressure"},
                    {"id": "uvc", "required_for": "camera"},
                ],
            }
        }
        (root / "RELEASE-MANIFEST.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return {
            "root": str(root),
            "module_checker": lambda name: name in {"json", "required_vendor_module"},
            "driver_status": {"serial_port": True, "uvc": True},
        }

    def test_bundled_runtime_modules_native_sdk_and_drivers_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = self._candidate(Path(temporary), native=_pe())
            outcome = probe_candidate_runtime(context)
        self.assertEqual(outcome.status, "passed")
        self.assertEqual(outcome.details["required_modules"], 2)
        self.assertEqual(outcome.details["required_executables"], 1)
        self.assertEqual(outcome.details["native_components"], 1)
        self.assertEqual(outcome.details["drivers"], 2)

    def test_missing_module_native_sdk_or_driver_fails_with_component_id(self) -> None:
        mutations = (
            ("module", "required_vendor_module"),
            ("native", "vendor"),
            ("driver", "uvc"),
        )
        for mutation, expected in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = self._candidate(root, native=_pe())
                if mutation == "module":
                    context["module_checker"] = lambda name: name == "json"
                elif mutation == "native":
                    (root / "native_dll" / "vendor.dll").unlink()
                else:
                    context["driver_status"]["uvc"] = False
                with self.assertRaisesRegex(ValueError, expected):
                    probe_candidate_runtime(context)

    def test_incompatible_or_unreadable_native_component_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = self._candidate(Path(temporary), native=b"not-a-pe")
            with self.assertRaisesRegex(ValueError, "vendor.*compatible"):
                probe_candidate_runtime(context)

    def test_probe_never_accepts_source_worktree_as_bundled_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self._candidate(root, native=_pe())
            (root / "_internal" / "python311.dll").unlink()
            context["source_worktree"] = str(Path(__file__).resolve().parents[2])
            with self.assertRaisesRegex(ValueError, "bundled Python runtime"):
                probe_candidate_runtime(context)


if __name__ == "__main__":
    unittest.main()
