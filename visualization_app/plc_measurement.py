"""设备测量来源的 helper 能力门禁与逐帧持久化快照。"""

from copy import deepcopy
from abb_measurement import ABB_MEASUREMENT_CONTRACT, requires_abb_measurement


PLC_MEASUREMENT_CONTRACT = "fibercontrol_plc_v1"


def requires_plc_measurement(config):
    return config.get("acquisition_mode", "real") == "real" and (
        config.get("driver") == "modbus_tcp" or any(
            item.get("enabled", True) and (item.get("driver") == "modbus_tcp" or item.get("role") == "plc")
            for item in config.get("interfaces", [])
        )
    )


def require_helper_measurement(config):
    if getattr(config, "real_acquisition_mode", "") != "remote_helper":
        return
    values = {"acquisition_mode": getattr(config, "acquisition_mode", "real"),
              "driver": getattr(config, "driver", ""), "interfaces": getattr(config, "interfaces", []) or []}
    capabilities = getattr(config, "helper_capabilities", None) or {}
    if requires_abb_measurement(values) and capabilities.get("abb_measurement_contract") != ABB_MEASUREMENT_CONTRACT:
        raise RuntimeError("abb_measurement_incompatible: fibercontrol_abb_v1 required")
    if requires_plc_measurement(values) and capabilities.get("plc_measurement_contract") != PLC_MEASUREMENT_CONTRACT:
        raise RuntimeError("plc_measurement_incompatible: fibercontrol_plc_v1 required")


def frame_source_records(frame):
    """Bind persisted evidence to the chosen frame, including held and invalid samples."""
    result = {}
    for name, sample in frame.channels.items():
        record = sample.metadata.get("latest_record")
        if not record:
            continue
        driver = sample.metadata.get("driver")
        if driver in {"modbus_tcp", "abb_robot"}:
            result[name] = dict(deepcopy(record), interface_id=sample.interface_id,
                                frame_quality=sample.quality.value,
                                age_seconds=sample.metadata.get("age_seconds"))
        elif driver == "esr1001_usb_ttl":
            result[name] = dict(record, calibration_snapshot=sample.metadata.get("calibration_snapshot"),
                                vendor_candidate_snapshot=sample.metadata.get("vendor_candidate_snapshot"))
        else:
            result[name] = {key: record.get(key) for key in (
                "connection_generation", "receive_sequence", "sha256", "byte_offset")}
    return result
