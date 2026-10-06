"""SMRF CT08 USB-HID transport and temperature conversion.

The wire layout and commands mirror the vendor SMRF-T V1.61 program.  NIST
ITS-90 reference functions are used for cold-junction compensation.
"""
from __future__ import annotations

import ctypes
import math
import os
import queue
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any


SMRF_CHANNEL_TYPES = ("-", "K", "J", "T", "S", "N", "E", "R", "PT100", "1820")
DEFAULT_SMRF_TYPES = ("K",) * 8

# NIST SRD 60 forward reference functions: temperature (deg C) -> emf (mV).
# Coefficients are ordered from highest power to the constant term.
_NIST_TABLES: dict[str, tuple[tuple[float, float, tuple[float, ...], tuple[float, float, float] | None], ...]] = {
    "E": (
        (-270.0, 0.0, (-0.346578420130e-28, -0.558273287210e-25, -0.396736195160e-22, -0.164147763550e-19, -0.439794973910e-17, -0.803701236210e-15, -0.102876055340e-12, -0.932140586670e-11, -0.594525830570e-09, -0.258001608430e-07, -0.779980486860e-06, 0.454109771240e-04, 0.586655087080e-01, 0.0), None),
        (0.0, 1000.0, (0.359608994810e-27, -0.143880417820e-23, 0.214892175690e-20, -0.125366004970e-17, -0.191974955040e-15, 0.650244032700e-12, -0.330568966520e-09, 0.289084072120e-07, 0.450322755820e-04, 0.586655087100e-01, 0.0), None),
    ),
    "J": (
        (-210.0, 760.0, (0.156317256970e-22, -0.125383953360e-18, 0.209480906970e-15, -0.170529583370e-12, 0.132281952950e-09, -0.856810657200e-07, 0.304758369300e-04, 0.503811878150e-01, 0.0), None),
        (760.0, 1200.0, (-0.306913690560e-12, 0.157208190040e-08, -0.318476867010e-05, 0.317871039240e-02, -0.149761277860e01, 0.296456256810e03), None),
    ),
    "K": (
        (-270.0, 0.0, (-0.163226974860e-22, -0.198892668780e-19, -0.104516093650e-16, -0.310888728940e-14, -0.574103274280e-12, -0.675090591730e-10, -0.499048287770e-08, -0.328589067840e-06, 0.236223735980e-04, 0.394501280250e-01, 0.0), None),
        (0.0, 1372.0, (-0.121047212750e-25, 0.971511471520e-22, -0.320207200030e-18, 0.560750590590e-15, -0.560728448890e-12, 0.318409457190e-09, -0.994575928740e-07, 0.185587700320e-04, 0.389212049750e-01, -0.176004136860e-01), (0.1185976, -0.1183432e-3, 126.9686)),
    ),
    "N": (
        (-270.0, 0.0, (-0.934196678350e-19, -0.760893007910e-16, -0.226534380030e-13, -0.263033577160e-11, -0.464120397590e-10, -0.938411115540e-07, 0.109574842280e-04, 0.261591059620e-01, 0.0), None),
        (0.0, 1300.0, (-0.306821961510e-28, 0.208492293390e-24, -0.608632456070e-21, 0.997453389920e-18, -0.100634715190e-14, 0.643118193390e-12, -0.252611697940e-09, 0.438256272370e-07, 0.157101418800e-04, 0.259293946010e-01, 0.0), None),
    ),
    "R": (
        (-50.0, 1064.18, (-0.281038625251e-26, 0.157716482367e-22, -0.373105886191e-19, 0.500777441034e-16, -0.462347666298e-13, 0.356916001063e-10, -0.238855693017e-07, 0.139166589782e-04, 0.528961729765e-02, 0.0), None),
        (1064.18, 1664.5, (-0.293359668173e-15, 0.205305291024e-11, -0.764085947576e-08, 0.159564501865e-04, -0.252061251332e-02, 0.295157925316e01), None),
        (1664.5, 1768.1, (-0.934633971046e-14, -0.345895706453e-07, 0.171280280471e-03, -0.268819888545, 0.152232118209e03), None),
    ),
    "S": (
        (-50.0, 1064.18, (0.271443176145e-23, -0.125068871393e-19, 0.255744251786e-16, -0.331465196389e-13, 0.322028823036e-10, -0.232477968689e-07, 0.125934289740e-04, 0.540313308631e-02, 0.0), None),
        (1064.18, 1664.5, (0.129989605174e-13, -0.164856259209e-08, 0.654805192818e-05, 0.334509311344e-02, 0.132900444085e01), None),
        (1664.5, 1768.1, (-0.943223690612e-14, -0.330439046987e-07, 0.163693574641e-03, -0.258430516752, 0.146628232636e03), None),
    ),
    "T": (
        (-270.0, 0.0, (0.797951539270e-30, 0.139450270620e-26, 0.107955392700e-23, 0.487686622860e-21, 0.142515947790e-18, 0.282135219250e-16, 0.384939398830e-14, 0.360711542050e-12, 0.226511565930e-10, 0.901380195590e-09, 0.200329735540e-07, 0.118443231050e-06, 0.441944343470e-04, 0.387481063640e-01, 0.0), None),
        (0.0, 400.0, (-0.275129016730e-19, 0.454791352900e-16, -0.308157587720e-13, 0.109968809280e-10, -0.218822568460e-08, 0.206182434040e-06, 0.332922278800e-04, 0.387481063640e-01, 0.0), None),
    ),
}


def _polyval(coefficients: tuple[float, ...], value: float) -> float:
    result = 0.0
    for coefficient in coefficients:
        result = result * value + coefficient
    return result


def thermocouple_emf_mv(channel_type: str, temperature_c: float) -> float:
    channel_type = str(channel_type or "K").upper()
    pieces = _NIST_TABLES.get(channel_type)
    if not pieces:
        raise ValueError(f"不支持的热电偶类型：{channel_type}")
    for index, (minimum, maximum, coefficients, gaussian) in enumerate(pieces):
        if minimum <= temperature_c <= maximum or (index == len(pieces) - 1 and temperature_c == maximum):
            value = _polyval(coefficients, temperature_c)
            if gaussian:
                amplitude, exponent, center = gaussian
                value += amplitude * math.exp(exponent * (temperature_c - center) ** 2)
            return value
    raise ValueError(f"{channel_type}型热电偶温度超出NIST范围：{temperature_c:.3f} °C")


def thermocouple_temperature_c(channel_type: str, emf_mv: float) -> float:
    """Invert the monotonic NIST reference function by bisection."""
    pieces = _NIST_TABLES.get(str(channel_type or "K").upper())
    if not pieces:
        raise ValueError(f"不支持的热电偶类型：{channel_type}")
    low, high = pieces[0][0], pieces[-1][1]
    low_mv = thermocouple_emf_mv(channel_type, low)
    high_mv = thermocouple_emf_mv(channel_type, high)
    if not min(low_mv, high_mv) <= emf_mv <= max(low_mv, high_mv):
        raise ValueError(f"{channel_type}型热电势超出NIST范围：{emf_mv:.6f} mV")
    increasing = high_mv >= low_mv
    for _ in range(64):
        middle = (low + high) / 2.0
        middle_mv = thermocouple_emf_mv(channel_type, middle)
        if (middle_mv < emf_mv) == increasing:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def _signed_big_endian(data: bytes) -> int:
    return int.from_bytes(data, byteorder="big", signed=True)


def smrf_measurement_payload(
    payload: bytes,
    channel_types: tuple[str, ...] | list[str] = DEFAULT_SMRF_TYPES,
    calibration_offsets: tuple[float, ...] | list[float] | None = None,
) -> dict[str, float] | None:
    """Decode the vendor DeviceData buffer (HID report ID already removed)."""
    if len(payload) < 29:
        return None
    command = bytes(payload[2:8]).lower()
    if any(command.startswith(name) for name in (b"key", b"cal", b"cew", b"shtr", b"sspeed", b"tyw")):
        return None
    checksum = 0
    for value in payload[2:28]:
        checksum ^= value
    if checksum != payload[28]:
        return None
    types = [str(value or "K").upper() for value in channel_types]
    types.extend(["K"] * (8 - len(types)))
    offsets = list(calibration_offsets or ())
    offsets.extend([0.0] * (8 - len(offsets)))
    cold_c = _signed_big_endian(payload[26:28]) / 100.0
    result: dict[str, float] = {"COLD": cold_c}
    for index in range(8):
        start = 2 + index * 3
        measured_mv = _signed_big_endian(payload[start:start + 3]) / 10000.0
        channel_type = types[index]
        if channel_type in _NIST_TABLES:
            compensated_mv = measured_mv + thermocouple_emf_mv(channel_type, cold_c)
            temperature = thermocouple_temperature_c(channel_type, compensated_mv)
        elif channel_type == "-":
            continue
        else:
            # PT100/1820 use model-specific paths in the vendor program and are
            # not thermocouples.  Do not mislabel their raw values as degrees C.
            continue
        result[f"温度{index + 1}"] = temperature + float(offsets[index])
    return result


@dataclass(frozen=True)
class SmrfHidDevice:
    path: str
    product: str
    serial: str
    vendor_id: int
    product_id: int

    @property
    def label(self) -> str:
        return f"{self.product} (Serial={self.serial})" if self.serial else self.product


if os.name == "nt":
    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", _GUID), ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]

    class _HIDD_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Size", wintypes.ULONG), ("VendorID", wintypes.USHORT), ("ProductID", wintypes.USHORT), ("VersionNumber", wintypes.USHORT)]

    class _HIDP_CAPS(ctypes.Structure):
        _fields_ = [
            ("Usage", wintypes.USHORT), ("UsagePage", wintypes.USHORT),
            ("InputReportByteLength", wintypes.USHORT), ("OutputReportByteLength", wintypes.USHORT),
            ("FeatureReportByteLength", wintypes.USHORT), ("Reserved", wintypes.USHORT * 17),
            ("NumberLinkCollectionNodes", wintypes.USHORT), ("NumberInputButtonCaps", wintypes.USHORT),
            ("NumberInputValueCaps", wintypes.USHORT), ("NumberInputDataIndices", wintypes.USHORT),
            ("NumberOutputButtonCaps", wintypes.USHORT), ("NumberOutputValueCaps", wintypes.USHORT),
            ("NumberOutputDataIndices", wintypes.USHORT), ("NumberFeatureButtonCaps", wintypes.USHORT),
            ("NumberFeatureValueCaps", wintypes.USHORT), ("NumberFeatureDataIndices", wintypes.USHORT),
        ]


def _windows_libraries():
    if os.name != "nt":
        raise OSError("SMRF USB HID驱动仅支持Windows")
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID), wintypes.DWORD, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    if hasattr(kernel32, "CancelIoEx"):
        kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    return setupapi, hid, kernel32


def _hid_text(hid: Any, handle: int, function_name: str) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    function = getattr(hid, function_name)
    return buffer.value if function(handle, buffer, ctypes.sizeof(buffer)) else ""


def enumerate_smrf_hid_devices() -> list[SmrfHidDevice]:
    if os.name != "nt":
        return []
    setupapi, hid, kernel32 = _windows_libraries()
    guid = _GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    info_set = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)
    invalid_handle = ctypes.c_void_p(-1).value
    if info_set == invalid_handle:
        return []
    devices: list[SmrfHidDevice] = []
    try:
        index = 0
        while True:
            interface = _SP_DEVICE_INTERFACE_DATA()
            interface.cbSize = ctypes.sizeof(interface)
            if not setupapi.SetupDiEnumDeviceInterfaces(info_set, None, ctypes.byref(guid), index, ctypes.byref(interface)):
                if ctypes.get_last_error() == 259:
                    break
                index += 1
                continue
            required = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(info_set, ctypes.byref(interface), None, 0, ctypes.byref(required), None)
            detail = ctypes.create_string_buffer(required.value)
            ctypes.cast(detail, ctypes.POINTER(wintypes.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if setupapi.SetupDiGetDeviceInterfaceDetailW(info_set, ctypes.byref(interface), detail, required, None, None):
                path = ctypes.wstring_at(ctypes.addressof(detail) + 4)
                handle = kernel32.CreateFileW(path, 0, 3, None, 3, 0, None)
                if handle != invalid_handle:
                    try:
                        product = _hid_text(hid, handle, "HidD_GetProductString")
                        serial = _hid_text(hid, handle, "HidD_GetSerialNumberString")
                        attributes = _HIDD_ATTRIBUTES()
                        attributes.Size = ctypes.sizeof(attributes)
                        hid.HidD_GetAttributes(handle, ctypes.byref(attributes))
                        if product.upper().startswith("SMRF"):
                            devices.append(SmrfHidDevice(path, product, serial, int(attributes.VendorID), int(attributes.ProductID)))
                    finally:
                        kernel32.CloseHandle(handle)
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info_set)
    return devices


class SmrfHidDriver:
    """One SMRF CT08 device, using the same commands and frame layout as V1.61."""

    def __init__(self, endpoint: str = "SMRFCT08B", channel_types: list[str] | tuple[str, ...] | None = None) -> None:
        self.endpoint = str(endpoint or "SMRF")
        self.channel_types = tuple(channel_types or DEFAULT_SMRF_TYPES)
        self.calibration_offsets = [0.0] * 8
        self.handle: int | None = None
        self.device: SmrfHidDevice | None = None
        self.input_report_length = 65
        self.output_report_length = 65
        self._kernel32 = None
        self._hid = None
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=32)
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._next_poll = 0.0

    def _select_device(self) -> SmrfHidDevice:
        devices = enumerate_smrf_hid_devices()
        match = self.endpoint.casefold().strip()
        for device in devices:
            fields = (device.label, device.product, device.serial, device.path)
            if not match or match == "smrf" or any(match in value.casefold() for value in fields if value):
                return device
        found = "、".join(device.label for device in devices) or "无"
        raise ValueError(f"未识别到SMRF八通道温度巡检仪（目标：{self.endpoint}；已发现：{found}）")

    def open(self) -> None:
        _, hid, kernel32 = _windows_libraries()
        device = self._select_device()
        handle = kernel32.CreateFileW(device.path, 0xC0000000, 3, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), f"SMRF HID接口无法打开：{device.label}")
        self.device, self.handle, self._kernel32, self._hid = device, handle, kernel32, hid
        preparsed = ctypes.c_void_p()
        try:
            if hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
                caps = _HIDP_CAPS()
                if hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) >= 0:
                    self.input_report_length = max(30, int(caps.InputReportByteLength))
                    self.output_report_length = max(10, int(caps.OutputReportByteLength))
        finally:
            if preparsed:
                hid.HidD_FreePreparsedData(preparsed)
        self._stop.clear()
        self._reader = threading.Thread(target=self._reader_loop, name="smrf-hid-reader", daemon=True)
        self._reader.start()
        # The vendor program issues HIDCAR after connection to obtain the
        # device/configuration response, then starts measurement with SSPEED.
        self._write_command(b"HIDCAR")
        time.sleep(0.02)
        self._write_command(b"SSPEED", bytes((10,)))
        self._next_poll = 0.0

    def _write_command(self, command: bytes, argument: bytes = b"") -> None:
        if self.handle is None or self._kernel32 is None:
            raise RuntimeError("SMRF HID接口尚未打开")
        report = bytearray(self.output_report_length)
        report[0] = 0
        report[1] = 0x16
        report[2:2 + len(command)] = command
        report[2 + len(command):2 + len(command) + len(argument)] = argument
        written = wintypes.DWORD()
        buffer = (ctypes.c_ubyte * len(report)).from_buffer_copy(report)
        if not self._kernel32.WriteFile(self.handle, buffer, len(report), ctypes.byref(written), None):
            raise OSError(ctypes.get_last_error(), f"SMRF HID命令发送失败：{command.decode('ascii', 'replace')}")

    def _reader_loop(self) -> None:
        assert self.handle is not None and self._kernel32 is not None
        while not self._stop.is_set():
            buffer = ctypes.create_string_buffer(self.input_report_length)
            count = wintypes.DWORD()
            ok = self._kernel32.ReadFile(self.handle, buffer, self.input_report_length, ctypes.byref(count), None)
            if not ok:
                if self._stop.is_set() or ctypes.get_last_error() in (6, 995):
                    break
                time.sleep(0.02)
                continue
            frame = bytes(buffer.raw[:count.value])
            try:
                self._frames.put_nowait(frame)
            except queue.Full:
                try:
                    self._frames.get_nowait()
                except queue.Empty:
                    pass
                self._frames.put_nowait(frame)

    def _consume_calibration(self, payload: bytes) -> bool:
        if len(payload) < 21 or payload[2:5].lower() != b"cal":
            return False
        self.calibration_offsets = [int.from_bytes(payload[5 + index:6 + index], "big", signed=True) / 10.0 for index in range(8)]
        received_types = []
        for value in payload[13:21]:
            received_types.append(SMRF_CHANNEL_TYPES[value] if 0 <= value < len(SMRF_CHANNEL_TYPES) else "-")
        if any(value != "-" for value in received_types):
            self.channel_types = tuple(received_types)
        return True

    def read_sample(self) -> dict[str, float] | None:
        now = time.monotonic()
        if now >= self._next_poll:
            self._write_command(b"HIDREAD")
            self._next_poll = now + 0.2
        latest = None
        while True:
            try:
                frame = self._frames.get_nowait()
            except queue.Empty:
                break
            payload = frame[1:] if frame and frame[0] == 0 else frame
            if self._consume_calibration(payload):
                continue
            decoded = smrf_measurement_payload(payload, self.channel_types, self.calibration_offsets)
            if decoded:
                latest = {name: value for name, value in decoded.items() if name.startswith("温度")}
        return latest

    def close(self) -> None:
        self._stop.set()
        if self.handle is not None and self._kernel32 is not None:
            try:
                self._kernel32.CancelIoEx(self.handle, None)
            except Exception:
                pass
        if self._reader is not None:
            self._reader.join(timeout=0.5)
            self._reader = None
        if self.handle is not None and self._kernel32 is not None:
            self._kernel32.CloseHandle(self.handle)
        self.handle = None
