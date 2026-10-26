"""ABB 只读运动测量合同。设备时间未知，速度采用主机单调接收时间。"""

import json
import math
from urllib.parse import urlsplit
from acquisition_clock import ACQUISITION_CLOCK_DOMAIN

ABB_MEASUREMENT_CONTRACT = "fibercontrol_abb_v1"
ABB_TIME_BASIS = "host_receive_monotonic"


def requires_abb_measurement(config):
    return config.get("acquisition_mode", "real") == "real" and (
        config.get("driver") == "abb_robot" or any(
            item.get("enabled", True) and (item.get("driver") == "abb_robot" or item.get("role") == "robot")
            for item in config.get("interfaces", [])
        )
    )


def validate_abb_sample(name, value, record):
    """拒绝不完整或不能复算的 helper 运动来源。"""
    try:
        if not isinstance(record, dict) or record.get("measurement_profile") != ABB_MEASUREMENT_CONTRACT:
            raise ValueError()
        json.dumps(record, allow_nan=False)
        if set(record) - {
            "measurement_profile", "algorithm_version", "connection_generation", "source_sequence",
            "controller_endpoint", "resource_path", "mechanical_unit", "coordinate_system", "unit",
            "raw_xyz", "xyz", "request_started_monotonic", "request_started_wall_time", "host_monotonic",
            "host_wall_time", "response_seconds", "time_basis", "device_timestamp", "device_sequence",
            "speed_quality", "speed_basis", "clock_domain",
        }:
            raise ValueError()
        if record.get("clock_domain", ACQUISITION_CLOCK_DOMAIN) != ACQUISITION_CLOCK_DOMAIN:
            raise ValueError()
        endpoint = urlsplit(record["controller_endpoint"])
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
            raise ValueError()
        if record["algorithm_version"] != 1 or record["unit"] != "mm" or record["device_timestamp"] is not None or record["device_sequence"] is not None:
            raise ValueError()
        if record["speed_quality"] not in {"measured_new", "unverified_first_sample", "unverified_zero_delta", "unverified_after_expiry", "unverified_numeric"}:
            raise ValueError()
        if record["time_basis"] != ABB_TIME_BASIS or record["mechanical_unit"] != "ROB_1" or record["coordinate_system"] != "Base":
            raise ValueError()
        if record["resource_path"] != "/rw/motionsystem/mechunits/ROB_1/robtarget?coordinate=Base&json=1":
            raise ValueError()
        xyz = record["xyz"]
        if len(xyz) != 3 or not all(math.isfinite(float(v)) for v in xyz):
            raise ValueError()
        if [float(record["raw_xyz"][axis]) for axis in ("x", "y", "z")] != xyz:
            raise ValueError()
        if set(record["raw_xyz"]) != {"x", "y", "z"}:
            raise ValueError()
        for key in ("request_started_monotonic", "request_started_wall_time", "host_monotonic", "host_wall_time", "response_seconds"):
            if not math.isfinite(float(record[key])):
                raise ValueError()
        if record["source_sequence"] < 1 or record["connection_generation"] < 0:
            raise ValueError()
        if name in ("ABB_X", "ABB_Y", "ABB_Z") and value is not None and value != xyz[("ABB_X", "ABB_Y", "ABB_Z").index(name)]:
            raise ValueError()
        basis = record["speed_basis"]
        if (basis is not None) != (record["speed_quality"] == "measured_new"):
            raise ValueError()
        if basis is not None:
            if set(basis) != {"previous", "current", "distance_mm", "interval_seconds", "time_basis", "value_mm_s"} or basis["time_basis"] != ABB_TIME_BASIS:
                raise ValueError()
            previous, current = basis["previous"], basis["current"]
            for point in (previous, current):
                if set(point) != {"source_sequence", "connection_generation", "host_monotonic", "host_wall_time", "xyz"}:
                    raise ValueError()
                if len(point["xyz"]) != 3 or not all(math.isfinite(float(v)) for v in (*point["xyz"], point["host_monotonic"], point["host_wall_time"])):
                    raise ValueError()
            interval = current["host_monotonic"] - previous["host_monotonic"]
            if interval <= 0 or not math.isfinite(interval) or not math.isclose(interval, basis["interval_seconds"]):
                raise ValueError()
            if current != {key: record[key] for key in ("source_sequence", "connection_generation", "host_monotonic", "host_wall_time", "xyz")}:
                raise ValueError()
            if previous["connection_generation"] != current["connection_generation"] or previous["source_sequence"] >= current["source_sequence"]:
                raise ValueError()
            distance = math.dist(previous["xyz"], current["xyz"])
            if not math.isfinite(distance) or not math.isclose(distance, basis["distance_mm"]):
                raise ValueError()
            if basis["value_mm_s"] != round(distance / interval, 2):
                raise ValueError()
        if name == "线速度" and value is not None and (basis is None or value != basis["value_mm_s"]):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("abb_measurement_evidence_invalid: complete finite motion source required") from None
