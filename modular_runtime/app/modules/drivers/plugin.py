from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from contracts import ModuleRegistration


class DriverService:
    def __init__(self, context: Any) -> None:
        self.context = context
        self.acquisition = importlib.import_module("acquisition")
        self.smrf_hid = importlib.import_module("smrf_hid")

    def available(self) -> list[dict[str, str]]:
        return list(self.acquisition.AcquisitionManager.available_drivers())

    def enumerate_smrf(self) -> list[Any]:
        return list(self.smrf_hid.enumerate_smrf_hid_devices())

    def build(self, config: Any) -> Any:
        return self.acquisition.build_driver(config)

    def health_check(self) -> dict[str, Any]:
        dll_dir = self.context.paths.native_dll_dir
        bsv = (dll_dir / "BsvUvcNative.dll").exists()
        libusb = (dll_dir / "libusb-1.0.dll").exists()
        return {
            "ok": hasattr(self.acquisition, "MultiInterfaceDriver") and bsv and libusb,
            "driver_count": len(self.available()),
            "native_dll_dir": str(dll_dir),
            "bsv_uvc_dll": bsv,
            "libusb_dll": libusb,
        }


def register(context: Any) -> ModuleRegistration:
    service = DriverService(context)
    return ModuleRegistration("drivers", "2.0.0", "2.0", tuple(item["id"] for item in service.available()), service)
