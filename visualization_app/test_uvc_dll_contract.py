from __future__ import annotations

import math
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))

from acquisition import UvcThermalDriver  # noqa: E402


class NativeFunction:
    def __init__(self, result=None, callback=None) -> None:
        self.result = result
        self.callback = callback
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        if self.callback is not None:
            return self.callback(*args)
        return self.result


class FakeBsvDll:
    def __init__(
        self,
        *,
        set_range=1,
        open_result=1,
        start_result=1,
        has_frame=1,
        get_frame=1,
        last_error=-9,
    ) -> None:
        self.BsvSetTempRangeCode = NativeFunction(set_range)
        self.BsvOpen = NativeFunction(open_result)
        self.BsvStart = NativeFunction(start_result)
        self.BsvHasFrame = NativeFunction(has_frame)
        self.BsvGetFrame = NativeFunction(get_frame)
        self.BsvGetLastError = NativeFunction(last_error)
        self.BsvStop = NativeFunction(None)
        self.BsvClose = NativeFunction(None)


class UvcDllContractTests(unittest.TestCase):
    def test_helper_finds_native_api_beside_release_manifest(self) -> None:
        release_root = Path("D:/AFP_Runtime/.afp-staging/helper-fixture")
        expected = release_root / "native_dll" / "BsvUvcNative.dll"
        contract = SimpleNamespace(source_path=release_root / "RELEASE-MANIFEST.json")
        with patch("acquisition.RELEASE_CONTRACT", contract), patch.object(
            Path, "is_file", autospec=True,
            side_effect=lambda path: path == expected,
        ):
            self.assertEqual(UvcThermalDriver()._find_dll(), expected.resolve())

    def _driver(self, dll: FakeBsvDll, **kwargs) -> UvcThermalDriver:
        driver = UvcThermalDriver(call_timeout_seconds=0.05, **kwargs)
        self.finder = patch.object(driver, "_find_dll", return_value=Path("fixture.dll"))
        self.loader = patch("acquisition.ctypes.CDLL", return_value=dll)
        self.finder.start()
        self.loader.start()
        self.addCleanup(self.finder.stop)
        self.addCleanup(self.loader.stop)
        return driver

    def test_missing_or_unloadable_native_api_is_driver_incompatible(self) -> None:
        driver = UvcThermalDriver(call_timeout_seconds=0.05)
        with patch.object(driver, "_find_dll", return_value=Path("fixture.dll")), patch(
            "acquisition.ctypes.CDLL", side_effect=OSError("missing libusb")
        ):
            with self.assertRaisesRegex(Exception, "driver_incompatible"):
                driver.open()

    def test_open_and_start_failures_preserve_stage_and_native_codes(self) -> None:
        for stage, dll, expected_code in (
            ("open", FakeBsvDll(open_result=-2, last_error=-22), -2),
            ("start", FakeBsvDll(start_result=-3, last_error=-33), -3),
        ):
            with self.subTest(stage=stage):
                driver = self._driver(dll)
                with self.assertRaisesRegex(
                    Exception,
                    rf"device_open_failed.*stage={stage}.*code={expected_code}",
                ):
                    driver.open()

    def test_no_frame_and_get_frame_failure_are_no_valid_frame(self) -> None:
        for dll, expected in (
            (FakeBsvDll(has_frame=0, last_error=-40), "no_valid_frame"),
            (FakeBsvDll(get_frame=-4, last_error=-44), "code=-4"),
        ):
            with self.subTest(expected=expected):
                driver = self._driver(dll, poll_retries=2)
                driver.open()
                with self.assertRaisesRegex(Exception, expected):
                    driver.read_sample()

    def test_invalid_temperature_matrix_is_rejected(self) -> None:
        dll = FakeBsvDll()
        driver = self._driver(dll)
        driver.open()
        with patch.object(driver, "_roi_average_temperature", return_value=math.nan):
            with self.assertRaisesRegex(Exception, "invalid_temperature_matrix"):
                driver.read_sample()

    def test_successful_frame_and_close_call_every_native_stage(self) -> None:
        dll = FakeBsvDll()

        def fill_frame(_yuv, _yuv_size, temperature, _pixels):
            for index in (0, 1, 256, 257):
                temperature[index] = 6400
            return 1

        dll.BsvGetFrame.callback = fill_frame
        driver = self._driver(dll, roi=(0, 0, 2, 2))
        driver.open()

        sample = driver.read_sample()
        driver.close()

        self.assertEqual(sample, {"ROI平均温度": 50.0})
        self.assertEqual(len(dll.BsvSetTempRangeCode.calls), 1)
        self.assertEqual(len(dll.BsvOpen.calls), 1)
        self.assertEqual(len(dll.BsvStart.calls), 1)
        self.assertEqual(len(dll.BsvGetFrame.calls), 1)
        self.assertEqual(len(dll.BsvStop.calls), 1)
        self.assertEqual(len(dll.BsvClose.calls), 1)

    def test_blocking_open_and_stop_return_within_declared_timeout(self) -> None:
        never = threading.Event()
        dll = FakeBsvDll()
        dll.BsvOpen.callback = lambda: never.wait(60.0)
        driver = self._driver(dll)
        started = time.monotonic()
        with self.assertRaisesRegex(Exception, "native_call_timeout.*stage=open"):
            driver.open()
        self.assertLess(time.monotonic() - started, 0.25)

        dll = FakeBsvDll()
        dll.BsvStop.callback = lambda: never.wait(60.0)
        driver = self._driver(dll)
        driver.open()
        started = time.monotonic()
        with self.assertRaisesRegex(Exception, "native_call_timeout.*stage=stop"):
            driver.close()
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertEqual(len(dll.BsvClose.calls), 0)
        self.assertTrue(driver.native_call_pending)

    def test_native_timeout_prevents_reopen_and_concurrent_native_cleanup(self) -> None:
        release = threading.Event()
        dll = FakeBsvDll()
        dll.BsvOpen.callback = lambda: release.wait(1.0)
        driver = self._driver(dll)
        try:
            with self.assertRaisesRegex(Exception, "native_call_timeout.*stage=open"):
                driver.open()
            with self.assertRaisesRegex(Exception, "native_call_timeout"):
                driver.open()
            driver.close()
            self.assertEqual(len(dll.BsvOpen.calls), 1)
            self.assertEqual(len(dll.BsvStop.calls), 0)
            self.assertEqual(len(dll.BsvClose.calls), 0)
            self.assertTrue(driver.native_call_pending)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
