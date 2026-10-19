"""使用生产驱动复现 PLC 与 ABB 的只读采集并保存逐通道证据。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import acquisition as a


CHANNELS = {"plc": ["温度", "压力", "张力"], "robot": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"]}
ROLE_SELECTION = {"both": ("plc", "robot"), "plc": ("plc",), "robot": ("robot",)}


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def summarize_samples(samples, channels, duration_seconds):
    result = {}
    for name in channels:
        times = [row["elapsed_seconds"] for row in samples if _finite(row["values"].get(name)) is not None]
        boundaries = [0.0, *times, duration_seconds]
        result[name] = {
            "finite_samples": len(times), "total_polls": len(samples),
            "finite_ratio": len(times) / len(samples) if samples else 0.0,
            "longest_no_valid_data_seconds": max(right - left for left, right in zip(boundaries, boundaries[1:])),
            "configured_unit": a.SENSOR_CHANNEL_METADATA[name]["unit"],
        }
    return result


def _probe_interface(item, duration_seconds, sample_rate_hz):
    role = item["role"]
    result = {
        "role": role, "endpoint": item["endpoint"],
        "physical_interface_id": item["physical_interface_id"],
        "read_only_probe_passed": False, "errors": [], "samples": [],
        "device_profile": item["device_profile"],
    }
    try:
        path = a.validate_network_path(item)
    except Exception as exc:
        result.update(status="network_path_invalid", errors=[str(exc)])
        return result
    result["network_path"] = path
    if not path["ok"]:
        result["status"] = "network_path_invalid"
        return result
    source = path["actual_source_address"]
    driver = (
        a.ModbusTcpDriver(item["endpoint"], register_map=item["register_map"], slave_id=item["slave_id"], source_address=source)
        if role == "plc" else a.AbbRobotDriver(item["endpoint"], source_address=source)
    )
    start = time.monotonic()
    try:
        driver.open()
        start = time.monotonic()
        deadline = start + duration_seconds
        next_poll = start
        while time.monotonic() < deadline:
            values = driver.read_sample() or {}
            result["samples"].append({
                "at": time.time(), "elapsed_seconds": time.monotonic() - start,
                "values": {name: _finite(values.get(name)) for name in CHANNELS[role]},
                "actual_source_address": driver.actual_source_address,
            })
            if driver.actual_source_address != source:
                raise RuntimeError("network_path_invalid: protocol connection source changed")
            next_poll += 1.0 / sample_rate_hz
            time.sleep(max(0.0, min(next_poll, deadline) - time.monotonic()))
    except Exception as exc:
        result["errors"].append(str(exc))
    finally:
        try:
            driver.close()
        except Exception as exc:
            result["errors"].append(str(exc))
    elapsed = time.monotonic() - start
    result["elapsed_seconds"] = elapsed
    result["channels"] = summarize_samples(result["samples"], CHANNELS[role], elapsed)
    result["observed_polls_per_second"] = len(result["samples"]) / elapsed if elapsed > 0 else 0.0
    result["read_only_probe_passed"] = bool(
        not result["errors"] and all(
            stats["finite_samples"] >= 2 and stats["finite_ratio"] >= 0.99
            and stats["longest_no_valid_data_seconds"] <= 2.0
            for stats in result["channels"].values()
        )
    )
    result["status"] = "data_observed" if result["read_only_probe_passed"] else "read_failed"
    return result


def probe_round(interface_id, *, duration_seconds=10.0, sample_rate_hz=10.0, roles=("plc", "robot")):
    if not math.isfinite(duration_seconds) or not 0 < duration_seconds <= 600:
        raise ValueError("duration_seconds must be finite and within (0, 600]")
    if not math.isfinite(sample_rate_hz) or not 0 < sample_rate_hz <= 10:
        raise ValueError("sample_rate_hz must be finite and within (0, 10]")
    if not roles or any(role not in CHANNELS for role in roles):
        raise ValueError("roles must select plc, robot, or both")
    items = [i for i in a.default_capture_interfaces() if i["role"] in roles]
    for item in items:
        item.update(physical_interface_id=interface_id, physical_interface_kind="ethernet")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {i["role"]: pool.submit(_probe_interface, i, duration_seconds, sample_rate_hz) for i in items}
        return {role: future.result() for role, future in futures.items()}


def run_probe(output_directory, *, interface_id="ethernet:以太网", duration_seconds=10.0, repeat=3, roles=("plc", "robot")):
    output = Path(output_directory).resolve()
    if output.drive and output.drive.upper() != "D:":
        raise ValueError("工业实物证据必须保存到 D 盘")
    writable = a.check_capture_save_root(output)
    if not writable["ok"]:
        raise ValueError(writable["message"])
    if not 1 <= repeat <= 10:
        raise ValueError("repeat must be within [1, 10]")
    result = {
        "schema_version": 1, "started_at": time.time(),
        "implementation_file": str(Path(a.__file__).resolve()),
        "implementation_sha256": hashlib.sha256(Path(a.__file__).read_bytes()).hexdigest(),
        "runtime_revision": os.environ.get("AFP_RUNTIME_REVISION", ""),
        "sample_rate_hz": 10.0, "duration_seconds_per_round": duration_seconds,
        "requested_roles": list(roles),
        "reference_semantics_verified": False, "field_acceptance_passed": False,
        "rounds": [],
    }
    run = output / ("industrial-readonly-" + datetime.now().strftime("%Y%m%dT%H%M%S%f"))
    run.mkdir()
    path = run / "samples.json"
    for _ in range(repeat):
        result["rounds"].append(probe_round(interface_id, duration_seconds=duration_seconds, roles=roles))
        result["read_only_probe_passed"] = all(
            item["read_only_probe_passed"] for row in result["rounds"] for item in row.values()
        )
        result["updated_at"] = time.time()
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return result, path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface-id", default="ethernet:以太网")
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--role", choices=ROLE_SELECTION, default="both")
    parser.add_argument("--output-directory", default="D:/AFP_Capture/diagnostics/fix-plc-abb-uvc-real-acquisition")
    args = parser.parse_args()
    result, path = run_probe(args.output_directory, interface_id=args.interface_id, duration_seconds=args.duration, repeat=args.repeat, roles=ROLE_SELECTION[args.role])
    print(json.dumps({"read_only_probe_passed": result["read_only_probe_passed"], "evidence_file": str(path)}, ensure_ascii=False))
    return 0 if result["read_only_probe_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
