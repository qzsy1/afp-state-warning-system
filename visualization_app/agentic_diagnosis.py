"""Evidence-driven diagnosis runners for the AFP acquisition interface.

The offline runner is a deterministic acceptance harness.  It exercises the
same read-only tool contract as the online model runner without pretending to
be a language model or a field-confirmed hardware diagnosis.
"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from diagnostic_tools import DiagnosticToolContext, execute_tool


MAX_AGENT_ROUNDS = 5
MAX_TOOL_CALLS = 8
OFFLINE_BOUNDARY = "离线测试诊断只验证软件流程，不等同于真实硬件故障确认"


_TOOL_SOURCE_LABELS = {
    "inspect_interface_mapping": "接口映射",
    "recheck_interface": "本次接口探测",
    "get_recent_channel_stats": "近期通道统计",
    "get_cross_interface_timeline": "跨接口状态比较",
    "check_network_path": "网络路径检查",
    "inspect_protocol_frame": "协议帧摘要",
    "lookup_device_knowledge": "内置设备知识",
    "compare_related_signals": "关联信号比较",
}


def _cache_key(name: str, arguments: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"


def _execute_cached(
    context: DiagnosticToolContext,
    cache: dict[str, dict[str, Any]],
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    key = _cache_key(name, arguments)
    if key in cache:
        return cache[key]
    if len(cache) >= MAX_TOOL_CALLS:
        raise RuntimeError(f"离线诊断工具调用超过 {MAX_TOOL_CALLS} 次限制")
    result = execute_tool(context, name, arguments)
    cache[key] = result
    return result


def _offline_tool_plan(context: DiagnosticToolContext) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    abnormal_ids = {
        str(event.get("interface_id"))
        for event in context.events
        if str(event.get("state")) not in {"ok", "healthy", "disabled", "video_only"}
    }
    shared_network_failure = {"plc_process", "abb_motion"}.issubset(abnormal_ids)
    plans: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for event in context.events:
        interface_id = str(event.get("interface_id"))
        state = str(event.get("state") or "")
        if interface_id in {"plc_process", "abb_motion"} and shared_network_failure:
            plans[interface_id] = [
                (
                    "get_cross_interface_timeline",
                    {
                        "interface_ids": ["plc_process", "abb_motion"],
                        "window_seconds": 60,
                    },
                ),
                ("check_network_path", {"interface_id": interface_id}),
            ]
        elif state in {"invalid_data", "invalid_protocol"}:
            plans[interface_id] = [
                ("inspect_interface_mapping", {"interface_id": interface_id}),
                ("inspect_protocol_frame", {"interface_id": interface_id}),
                (
                    "lookup_device_knowledge",
                    {"device_type": interface_id, "symptom": "invalid_protocol"},
                ),
            ]
        elif interface_id == "thermocouple_8ch":
            plans[interface_id] = [
                ("inspect_interface_mapping", {"interface_id": interface_id})
            ]
        else:
            plans[interface_id] = [
                ("inspect_interface_mapping", {"interface_id": interface_id}),
                (
                    "recheck_interface",
                    {"interface_id": interface_id, "duration_seconds": 1},
                ),
            ]
    return plans


def _evidence_fact(evidence: dict[str, Any]) -> str:
    tool = evidence.get("tool")
    if tool == "inspect_interface_mapping":
        if evidence.get("identity_verified"):
            return "目标接口身份和有效协议数据已经确认"
        if evidence.get("placeholder_binding"):
            return "当前物理接口只是占位或临时绑定，尚未确认目标设备身份"
        if evidence.get("candidate_detected"):
            return "检测到同类型物理接口，但尚未收到足以确认目标设备的有效数据"
        return "未检测到能够确认目标设备身份的物理接口"
    if tool == "recheck_interface":
        return str(evidence.get("message") or "本次硬件探测未返回有效数据")
    if tool == "get_cross_interface_timeline":
        if evidence.get("shared_physical_interface_id"):
            return "PLC与ABB在同一检查任务中异常，并绑定同一电脑网卡"
        return "PLC与ABB在同一检查任务中均处于异常状态"
    if tool == "check_network_path":
        if not evidence.get("adapter_detected"):
            return "未检测到配置所指向的电脑网卡"
        if not evidence.get("endpoint_reachable"):
            return f"目标端点 {evidence.get('configured_endpoint')} 当前不可达"
        return f"目标端点 {evidence.get('configured_endpoint')} 网络可达，但尚未确认协议数据"
    if tool == "inspect_protocol_frame":
        return str(evidence.get("boundary") or "当前没有可用协议帧证据")
    if tool == "lookup_device_knowledge":
        facts = evidence.get("confirmed_protocol_facts") or []
        return str(facts[0]) if facts else "内置知识没有提供可确认的协议事实"
    if tool == "get_recent_channel_stats":
        return "近期通道数据不足" if evidence.get("insufficient_data") else "已取得近期通道统计"
    if tool == "compare_related_signals":
        return "关联数据不足，不能比较" if evidence.get("insufficient_data") else "已完成关联信号比较"
    return "已取得只读诊断证据"


def _local_fallback_for(
    local_diagnoses: list[dict[str, Any]], interface_id: str
) -> dict[str, Any] | None:
    for item in local_diagnoses:
        if str(item.get("interface_id")) == interface_id:
            return deepcopy(item)
    return None


def _diagnosis_content(
    event: dict[str, Any],
    evidence: list[dict[str, Any]],
    local_fallback: dict[str, Any] | None,
) -> dict[str, Any]:
    interface_id = str(event.get("interface_id"))
    evidence_ids = [str(item["evidence_id"]) for item in evidence]
    state = str(event.get("state") or "unknown")
    cross_findings: list[str] = []
    if interface_id in {"plc_process", "abb_motion"}:
        cause = "PLC与ABB共用网络路径未建立或网络参数不匹配"
        actions = [
            ("核对电脑工控网卡是否启用以及IP和子网", "两个网络设备共用同一物理网卡"),
            ("检查网线、交换机和设备供电", "当前无法从软件区分物理链路和设备未上电"),
            ("网络恢复后分别验证Modbus TCP与ABB RWS", "网络可达不等于协议数据有效"),
        ]
        cross_findings = ["PLC与ABB同时异常时，应优先检查共用网卡和网络链路，而不是认定两台设备同时损坏"]
        unknowns = ["尚未确认是电脑网卡配置、网线、交换机还是设备未上电"]
        confidence = 0.8
        fault_type = "网络端点或共用链路未确认"
    elif interface_id == "thermocouple_8ch":
        cause = "未识别到目标SMRF USB HID设备或设备未发送有效温度帧"
        actions = [
            ("检查SMRF设备供电和USB连接", "当前没有目标身份和有效帧证据"),
            ("核对设备型号、VID/PID或序列号", "普通USB接口不能证明SMRF设备存在"),
        ]
        unknowns = ["尚未确认设备未连接、未上电还是驱动无法枚举"]
        confidence = 0.82
        fault_type = "目标USB HID设备身份未确认"
    elif interface_id == "uvc_temperature":
        cause = "未识别到能够输出BSV温度帧的目标UVC设备"
        actions = [
            ("检查BSV热像仪USB连接和设备管理器状态", "厂商DLL存在不等于设备已经连接"),
            ("设备出现后验证256×192温度帧和ROI换算", "普通摄像头画面不能替代温度矩阵"),
        ]
        unknowns = ["尚未确认设备未连接、被占用还是驱动未创建设备实例"]
        confidence = 0.82
        fault_type = "目标UVC温度设备身份未确认"
    elif interface_id == "m3232_pressure":
        cause = "当前串口未由有效M3232协议帧确认"
        actions = [
            ("核对M3232实际串口、设备供电和通信参数", "COM口存在不等于连接的是M3232"),
            ("连接设备后捕获一帧原始数据并核对矩阵包装", "没有真实帧不能确认解析协议"),
        ]
        unknowns = ["尚未确认设备未连接、串口选择错误还是当前没有发送数据"]
        confidence = 0.82
        fault_type = "目标串口设备身份或协议未确认"
    else:
        cause = "接口未返回能够确认目标传感器的有效数据"
        actions = [("检查接口映射并取得有效协议样本", "当前证据不足")]
        unknowns = ["当前无法进一步区分连接、驱动或协议问题"]
        confidence = 0.65
        fault_type = "接口状态未确认"

    return {
        "interface_id": interface_id,
        "interface_label": event.get("interface_label") or interface_id,
        "sensor_name": event.get("sensor_name"),
        "channels": list(event.get("channels") or []),
        "state": state,
        "fault_type": fault_type,
        "confirmed_ok": state in {"ok", "healthy"},
        "original_error": event.get("message"),
        "observed_facts": [
            {"text": _evidence_fact(item), "evidence_ids": [item["evidence_id"]]}
            for item in evidence
        ],
        "hypotheses": [
            {
                "cause": cause,
                "confidence": min(confidence, 0.85),
                "evidence_ids": evidence_ids,
            }
        ],
        "cross_interface_findings": cross_findings,
        "recommended_actions": [
            {
                "priority": index,
                "action": action,
                "reason": reason,
                "requires_shutdown": False,
            }
            for index, (action, reason) in enumerate(actions, start=1)
        ],
        "unknowns": unknowns,
        "evidence_sources": list(
            dict.fromkeys(_TOOL_SOURCE_LABELS.get(str(item.get("tool")), str(item.get("tool"))) for item in evidence)
        ),
        "evidence": evidence,
        "local_fallback": local_fallback,
        "evidence_boundary": OFFLINE_BOUNDARY,
    }


def _summary(context: DiagnosticToolContext) -> dict[str, int]:
    interfaces = [
        item for item in context.hardware_result.get("interfaces") or [] if isinstance(item, dict)
    ]
    sensors = [item for item in context.hardware_result.get("sensors") or [] if isinstance(item, dict)]
    return {
        "confirmed_interfaces": sum(
            bool(item.get("ok")) and bool(item.get("sample_counts")) for item in interfaces
        ),
        "total_interfaces": len(interfaces),
        "normal_channels": sum(str(item.get("state")) == "ok" for item in sensors),
        "total_channels": len(sensors),
    }


def run_offline_diagnosis(
    context: DiagnosticToolContext,
    local_diagnoses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Exercise phenomenon-specific tool selection without an external model."""

    plans = _offline_tool_plan(context)
    cache: dict[str, dict[str, Any]] = {}
    diagnoses: list[dict[str, Any]] = []
    for event in context.events:
        interface_id = str(event.get("interface_id"))
        if str(event.get("state")) in {"ok", "healthy", "disabled", "video_only"}:
            continue
        evidence = [
            _execute_cached(context, cache, tool_name, arguments)
            for tool_name, arguments in plans.get(interface_id, [])
        ]
        diagnoses.append(
            _diagnosis_content(
                event,
                evidence,
                _local_fallback_for(local_diagnoses, interface_id),
            )
        )
    return {
        "execution_mode": "offline_test",
        "model_status": "offline_success",
        "model_message": "未调用外部模型；已使用内置离线测试诊断器按现象选择只读取证工具",
        "model_name": "内置离线测试诊断器",
        "summary": _summary(context),
        "tool_call_count": len(cache),
        "diagnoses": diagnoses,
        "evidence_boundary": OFFLINE_BOUNDARY,
    }
