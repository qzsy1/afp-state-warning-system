"""独立物理资源的并行检测与当前软件会话内的检测证据缓存。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import time

from real_acquisition import ReadinessSnapshot, readiness_config_fingerprint


def check_interfaces(manager, config, timeout_seconds):
    configs = list(config.interfaces or [])
    resources = []
    for item in configs:
        if item.get("enabled", True):
            endpoint = str(item.get("endpoint") or "").casefold()
            resources.append((endpoint, "serial" if endpoint.startswith("com") else str(item.get("driver") or "")))
    if len(resources) != len(set(resources)) or getattr(config, "configuration_issues", []):
        return manager._test_connection_once(config, timeout_seconds)
    started = time.time()

    def probe(item):
        isolated = deepcopy(config)
        identity = str(item.get("id") or "")
        isolated.interfaces = [deepcopy(item)]
        isolated.selected_sensors = list(config.interface_channel_assignments.get(identity, []))
        isolated.interface_channel_assignments = {identity: isolated.selected_sensors}
        fingerprint = readiness_config_fingerprint(isolated)
        with manager.lock:
            cached = manager._interface_readiness_cache.get(identity)
        if cached and cached[0] == fingerprint:
            return deepcopy(cached[1])
        checker = type(manager)(manager.capture_root, recovery_scan=False)
        result = checker._test_connection_once(isolated, timeout_seconds)
        if all(row.get("ok") for row in result["interfaces"]):
            with manager.lock:
                manager._interface_readiness_cache[identity] = (fingerprint, deepcopy(result))
        return result

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(configs)))) as pool:
        parts = list(pool.map(probe, configs))
    interfaces = [row for part in parts for row in part["interfaces"]]
    errors = list(dict.fromkeys(error for part in parts for error in part["errors"]))
    sensors = []
    selected = set(config.selected_sensors or [])
    for name in config.schema_sensors:
        rows = [row for part in parts for row in part["sensors"] if row["name"] == name and row["selected"]]
        received = sum(row["received_samples"] for row in rows)
        invalid = sum(row["invalid_samples"] for row in rows)
        state = "not_selected" if name not in selected else "ok" if received else "invalid_data" if invalid else "no_data"
        sensors.append({"name": name, "selected": name in selected, "received_samples": received,
                        "invalid_samples": invalid, "state": state, "blocking": False,
                        "message": {"not_selected": "未选择采集", "ok": "收到有效数据", "invalid_data": "收到非数值数据", "no_data": "未检测到数据"}[state],
                        "ok": state in {"not_selected", "ok"}})
    result = {"ok": bool(selected) and not errors and all(row["ok"] for row in interfaces if row.get("enabled", True)),
              "driver": config.driver, "endpoint": config.endpoint, "elapsed_seconds": time.time()-started,
              "checked_at": time.time(), "errors": errors, "sensors": sensors, "interfaces": interfaces,
              "configuration_issues": getattr(config, "configuration_issues", [])}
    with manager.lock:
        manager._readiness_snapshot = ReadinessSnapshot.create(config, result, capabilities=config.helper_capabilities)
        manager._session_readiness_fingerprint = readiness_config_fingerprint(config) if result["ok"] else ""
        manager.readiness_version += 1
        manager.readiness_invalid_reason = "" if result["ok"] else "hardware_check_failed"
        result["readiness_snapshot"] = manager._readiness_snapshot.to_dict()
        result["readiness_session_id"] = manager.readiness_session_id
        result["readiness_version"] = manager.readiness_version
        manager._latest_check_result = deepcopy(result)
    return result
