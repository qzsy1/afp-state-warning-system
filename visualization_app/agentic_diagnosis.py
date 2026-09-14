"""Evidence-driven diagnosis runners for the AFP acquisition interface.

The offline runner is a deterministic acceptance harness.  It exercises the
same read-only tool contract as the online model runner without pretending to
be a language model or a field-confirmed hardware diagnosis.
"""

from __future__ import annotations

from copy import deepcopy
import json
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from diagnostic_tools import (
    DiagnosticToolContext,
    DiagnosticToolError,
    execute_tool,
    tool_definitions,
)


MAX_AGENT_ROUNDS = 8
MAX_TOOL_CALLS = 24
OFFLINE_BOUNDARY = "离线测试诊断只验证软件流程，不等同于真实硬件故障确认"
SILICONFLOW_CHAT_COMPLETIONS_URL = "https://api.siliconflow.cn/v1/chat/completions"
ONLINE_BOUNDARY = "模型结论基于本次只读工具证据，仍不等同于已确认硬件损坏"


class AgentToolCallError(RuntimeError):
    """A sanitized failure safe to show in the local user interface."""

    def __init__(self, public_message: str):
        super().__init__(public_message)
        self.public_message = public_message


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
        network_evidence = next(
            (item for item in evidence if item.get("tool") == "check_network_path"),
            {},
        )
        if network_evidence.get("endpoint_reachable"):
            protocol_name = "Modbus TCP" if interface_id == "plc_process" else "ABB RWS"
            cause = (
                f"TCP端口可连接，但未收到有效{protocol_name}数据；"
                "可能目标设备身份、协议参数或访问权限不匹配"
            )
            actions = [
                ("确认该IP端点确实属于目标设备", "端口可连接不能确认设备身份，代理或其他服务也可能接受连接"),
                (
                    "核对Modbus站号、寄存器和字节序"
                    if interface_id == "plc_process"
                    else "核对ABB RWS服务、认证信息和robtarget路径",
                    f"当前缺少有效{protocol_name}响应",
                ),
                ("取得一份有效协议响应后再判定接口正常", "TCP握手不等于采集协议验证通过"),
            ]
            cross_findings = [
                "PLC与ABB的TCP端口均可连接但都没有有效采集数据时，共用网卡至少已具备基础连通性，应分别核对两个目标协议"
            ]
            unknowns = ["尚未确认端点设备身份、协议参数、访问权限或响应内容"]
            confidence = 0.72
            fault_type = "网络端口可达但目标协议数据未确认"
        else:
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


def _initial_case_payload(
    context: DiagnosticToolContext, *, include_event_evidence: bool = True
) -> dict[str, Any]:
    events = []
    for item in context.events:
        event = {
                key: deepcopy(item.get(key))
                for key in (
                    "interface_id",
                    "interface_label",
                    "role",
                    "driver",
                    "endpoint",
                    "physical_interface_id",
                    "physical_interface_kind",
                    "protocol",
                    "physical_fallback",
                    "physical_warning",
                    "sensor_name",
                    "channels",
                    "state",
                    "message",
                    "evidence",
                )
            }
        if not include_event_evidence:
            event.pop("evidence", None)
        events.append(event)
    return {
        "task": "根据现象自主选择只读取证工具；证据充分后输出结构化诊断",
        "events": events,
        "limits": {
            "max_rounds": MAX_AGENT_ROUNDS,
            "max_tool_calls": MAX_TOOL_CALLS,
            "read_only": True,
        },
    }


def _system_prompt() -> str:
    return (
        "你是AFP工业采集接口诊断Agent。先根据异常现象选择必要的只读工具，"
        "不要按固定顺序调用全部工具。你不能控制设备、修改配置或把推测写成事实。"
        "结论必须引用工具返回的evidence_id。证据不足时继续调用工具或明确写入unknowns。"
        "最终只输出JSON对象，包含diagnoses数组。每项必须包含interface_id、observed_facts、"
        "hypotheses、cross_interface_findings、recommended_actions和unknowns。"
        "observed_facts每项包含text和evidence_ids；hypotheses每项包含cause、confidence和"
        "evidence_ids；recommended_actions每项包含priority、action、reason和requires_shutdown。"
        "没有替换件、电气测量等独立证据时，不得宣称传感器损坏。"
    )


def _build_chat_payload(model_name: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": model_name,
        "messages": deepcopy(messages),
        "tools": tool_definitions(),
        "tool_choice": "auto",
        # SiliconFlow can take a long time to return the first complete JSON
        # response for tool-capable DeepSeek models.  Streaming returns headers
        # and partial deltas promptly, avoiding a false socket timeout while the
        # model is still producing tool arguments.
        "stream": True,
        "temperature": 0.1,
        "max_tokens": 1536,
    }


def _build_structured_payload(
    model_name: str, context: DiagnosticToolContext, offline_result: dict[str, Any]
) -> dict[str, Any]:
    """Build a no-tools compatibility request for models without function calling."""

    evidence = [
        item
        for diagnosis in offline_result.get("diagnoses") or []
        for item in diagnosis.get("evidence") or []
        if isinstance(item, dict)
    ]
    return {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是AFP工业采集接口诊断助手。当前模型不使用工具调用；只能根据给定的本地检查证据输出诊断。"
                    "只输出JSON对象，包含diagnoses数组，必须覆盖全部interface_id。每项包含interface_id、"
                    "observed_facts、hypotheses、cross_interface_findings、recommended_actions和unknowns。"
                    "observed_facts和hypotheses必须引用给定的evidence_id；没有证据不得写成已确认故障。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "case": _initial_case_payload(context, include_event_evidence=False),
                        "offline_evidence": evidence,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "response_format": {"type": "json_object"},
        "stream": True,
        "temperature": 0.1,
        "max_tokens": 4096,
    }


def _build_evidence_synthesis_payload(
    model_name: str,
    context: DiagnosticToolContext,
    evidence_by_id: dict[str, dict[str, Any]],
    *,
    include_response_format: bool = True,
) -> dict[str, Any]:
    """Build a clean no-tools request from evidence gathered by the model."""

    required_interface_ids = [
        str(item.get("interface_id"))
        for item in context.events
        if str(item.get("state")) not in {"ok", "healthy", "disabled", "video_only"}
    ]
    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是AFP工业采集接口诊断Agent的最终结论生成器。工具检查已经结束，"
                    "本次请求不提供任何工具。只能根据tool_evidence中的evidence_id形成结论。"
                    "只输出一个JSON对象，顶层必须且只能包含diagnoses数组。diagnoses必须按"
                    "required_interface_ids逐项完整覆盖，不能输出下一步工具参数。每项必须包含"
                    "interface_id、observed_facts、hypotheses、cross_interface_findings、"
                    "recommended_actions和unknowns。observed_facts每项包含text和evidence_ids；"
                    "hypotheses每项包含cause、confidence和evidence_ids；recommended_actions每项"
                    "包含priority、action、reason和requires_shutdown。没有独立证据时不得宣称硬件损坏。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "required_interface_ids": required_interface_ids,
                        "case": _initial_case_payload(context, include_event_evidence=False),
                        "tool_evidence": list(evidence_by_id.values()),
                        "output_example": {
                            "diagnoses": [
                                {
                                    "interface_id": required_interface_ids[0]
                                    if required_interface_ids
                                    else "interface_id",
                                    "observed_facts": [
                                        {"text": "已观察事实", "evidence_ids": ["EV-001"]}
                                    ],
                                    "hypotheses": [
                                        {
                                            "cause": "待验证原因",
                                            "confidence": 0.5,
                                            "evidence_ids": ["EV-001"],
                                        }
                                    ],
                                    "cross_interface_findings": [],
                                    "recommended_actions": [
                                        {
                                            "priority": 1,
                                            "action": "下一步检查",
                                            "reason": "基于现有证据",
                                            "requires_shutdown": False,
                                        }
                                    ],
                                    "unknowns": ["仍需确认的信息"],
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "stream": True,
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    if include_response_format:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _build_tool_plan_payload(model_name: str, context: DiagnosticToolContext) -> dict[str, Any]:
    """Ask the model to choose a bounded read-only tool plan in plain JSON."""

    available_tools = [
        {
            "name": str(item.get("function", {}).get("name") or ""),
            "description": str(item.get("function", {}).get("description") or ""),
            "argument_names": list(
                (item.get("function", {}).get("parameters", {}).get("properties") or {}).keys()
            ),
        }
        for item in tool_definitions()
        if isinstance(item, dict)
    ]
    compact_events = [
        {
            "interface_id": str(item.get("interface_id") or ""),
            "interface_label": str(item.get("interface_label") or "")[:80],
            "role": str(item.get("role") or "")[:40],
            "driver": str(item.get("driver") or "")[:60],
            "endpoint": str(item.get("endpoint") or "")[:120],
            "physical_interface_id": str(item.get("physical_interface_id") or "")[:120],
            "protocol": str(item.get("protocol") or "")[:60],
            "channels": [str(value)[:80] for value in (item.get("channels") or [])[:17]],
            "state": str(item.get("state") or "")[:60],
            "message": str(item.get("message") or "")[:240],
        }
        for item in context.events
    ]
    return {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是AFP工业采集接口诊断Agent的工具规划器。根据异常现象自主选择必要的"
                    "只读取证工具，覆盖每个异常接口，但不要机械调用全部工具。只输出JSON对象："
                    '{"tool_calls":[{"name":"工具名","arguments":{}}]}。'
                    "不要解释和展开推理，立即输出工具计划。只能使用available_tools中的名称和参数；"
                    "最多8次调用，不得输出诊断结论。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "case": {
                            "task": "按异常现象选择只读取证工具",
                            "events": compact_events,
                            "limits": {"max_tool_calls": 8, "read_only": True},
                        },
                        "available_tools": available_tools,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "response_format": {"type": "json_object"},
        "stream": True,
        "temperature": 0.1,
        "max_tokens": 768,
    }


def _synthesize_from_evidence(
    caller: Any,
    model_name: str,
    context: DiagnosticToolContext,
    evidence_by_id: dict[str, dict[str, Any]],
    local_diagnoses: list[dict[str, Any]],
    tool_call_count: int,
) -> dict[str, Any]:
    last_error: AgentToolCallError | None = None
    for include_response_format in (True, False):
        try:
            response = caller(
                _build_evidence_synthesis_payload(
                    model_name,
                    context,
                    evidence_by_id,
                    include_response_format=include_response_format,
                )
            )
            message = _assistant_message(response)
            parsed = _parse_final_json(_final_message_content(message))
            return _validate_final_result(
                parsed,
                context,
                evidence_by_id,
                local_diagnoses,
                model_name,
                tool_call_count,
            )
        except AgentToolCallError as error:
            last_error = error
            public = error.public_message
            non_retryable = ("API Key", "权限", "额度", "超时", "网络", "服务暂不可用")
            if not include_response_format or any(item in public for item in non_retryable):
                raise
    raise last_error or AgentToolCallError("模型没有返回最终结构化诊断")


def run_siliconflow_planned_agent(
    api_key: str,
    model_name: str,
    context: DiagnosticToolContext,
    local_diagnoses: list[dict[str, Any]],
    *,
    transport: Any = None,
) -> dict[str, Any]:
    """Use a model-selected JSON tool plan when native tool calls are slow."""

    caller = transport or (lambda payload: _request_siliconflow(api_key, payload))
    plan_payload = _build_tool_plan_payload(model_name, context)
    initial_connection_retried = False
    for attempt in range(2):
        try:
            response = caller(plan_payload)
            break
        except AgentToolCallError as error:
            retryable = any(
                marker in error.public_message
                for marker in ("超时", "网络", "服务暂不可用")
            )
            if attempt == 0 and retryable:
                initial_connection_retried = True
                continue
            raise AgentToolCallError(f"模型工具规划阶段失败：{error.public_message}") from None
    message = _assistant_message(response)
    parsed = _parse_final_json(_final_message_content(message))
    calls = parsed.get("tool_calls")
    if not isinstance(calls, list):
        raise AgentToolCallError("模型没有返回可执行的诊断工具计划")
    allowed_tool_names = {
        str(item.get("function", {}).get("name") or "")
        for item in tool_definitions()
        if isinstance(item, dict)
    }
    evidence_by_id: dict[str, dict[str, Any]] = {}
    covered_interface_ids: set[str] = set()
    seen_calls: set[str] = set()
    tool_call_count = 0
    for raw_call in calls[:8]:
        if not isinstance(raw_call, dict):
            continue
        tool_name = str(raw_call.get("name") or "")
        if tool_name not in allowed_tool_names:
            continue
        arguments = raw_call.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                continue
        if not isinstance(arguments, dict):
            continue
        cache_key = _cache_key(tool_name, arguments)
        if cache_key in seen_calls:
            continue
        seen_calls.add(cache_key)
        try:
            evidence = execute_tool(context, tool_name, arguments)
        except DiagnosticToolError:
            continue
        evidence_by_id[str(evidence["evidence_id"])] = evidence
        tool_call_count += 1
        if arguments.get("interface_id"):
            covered_interface_ids.add(str(arguments["interface_id"]))
        covered_interface_ids.update(str(value) for value in arguments.get("interface_ids") or [])

    # A model plan may overlook one interface.  Add only a read-only mapping
    # snapshot for the missed interface so the final model cannot invent a
    # diagnosis without any evidence; fault classification remains model-led.
    for interface_id in sorted(context.interface_ids - covered_interface_ids):
        if tool_call_count >= MAX_TOOL_CALLS:
            break
        arguments = {"interface_id": interface_id}
        evidence = execute_tool(context, "inspect_interface_mapping", arguments)
        evidence_by_id[str(evidence["evidence_id"])] = evidence
        tool_call_count += 1
    if not evidence_by_id:
        raise AgentToolCallError("模型工具计划没有取得有效诊断证据")
    try:
        result = _synthesize_from_evidence(
            caller,
            model_name,
            context,
            evidence_by_id,
            local_diagnoses,
            tool_call_count,
        )
    except AgentToolCallError as error:
        raise AgentToolCallError(f"模型最终诊断阶段失败：{error.public_message}") from None
    result["agent_strategy"] = "model_planned_tools"
    result["initial_connection_retried"] = initial_connection_retried
    result["model_message"] = "硅基流动模型已自主规划并调用只读取证工具完成诊断"
    return result


def _parse_siliconflow_sse(lines: Any) -> dict[str, Any]:
    """Reassemble OpenAI-compatible SSE deltas into one assistant message."""

    message: dict[str, Any] = {"role": "assistant", "content": ""}
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason: Any = None
    received_choice = False
    for raw_line in lines:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8").strip()
        else:
            line = str(raw_line).strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") if isinstance(chunk, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        received_choice = True
        choice = choices[0]
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        if delta.get("role"):
            message["role"] = str(delta["role"])
        content = delta.get("content")
        if isinstance(content, str):
            message["content"] += content
        for fragment in delta.get("tool_calls") or []:
            if not isinstance(fragment, dict):
                continue
            try:
                index = int(fragment.get("index", 0))
            except (TypeError, ValueError):
                index = 0
            call = tool_calls.setdefault(
                index,
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if fragment.get("id"):
                call["id"] += str(fragment["id"])
            if fragment.get("type"):
                call["type"] = str(fragment["type"])
            function = fragment.get("function")
            if isinstance(function, dict):
                if function.get("name"):
                    call["function"]["name"] += str(function["name"])
                if function.get("arguments"):
                    call["function"]["arguments"] += str(function["arguments"])
        if choice.get("finish_reason") is not None:
            finish_reason = choice.get("finish_reason")
    if not received_choice:
        raise AgentToolCallError("硅基流动返回了空的流式响应")
    if tool_calls:
        message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


def _request_siliconflow(
    api_key: str, payload: dict[str, Any], *, timeout_seconds: float = 90.0
) -> dict[str, Any]:
    request = Request(
        SILICONFLOW_CHAT_COMPLETIONS_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in content_type:
                return _parse_siliconflow_sse(response)
            raw = response.read()
            if raw.lstrip().startswith(b"data:"):
                return _parse_siliconflow_sse(raw.splitlines())
            return json.loads(raw.decode("utf-8"))
    except HTTPError as error:
        if error.code in {401, 403}:
            raise AgentToolCallError("硅基流动API Key无效或没有调用权限") from None
        if error.code in {400, 404}:
            raise AgentToolCallError("当前硅基流动模型名称无效或不支持工具调用") from None
        if error.code == 429:
            raise AgentToolCallError("硅基流动调用受限或账户额度不足") from None
        raise AgentToolCallError(f"硅基流动服务暂不可用（HTTP {error.code}）") from None
    except (URLError, TimeoutError, socket.timeout):
        raise AgentToolCallError("连接硅基流动超时或网络不可用") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AgentToolCallError("硅基流动返回了无法解析的响应") from None


def _assistant_message(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") if isinstance(response, dict) else None
    if not isinstance(choices, list) or not choices:
        raise AgentToolCallError("模型没有返回诊断消息")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise AgentToolCallError("模型诊断消息格式无效")
    return deepcopy(message)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts).strip()
    return ""


def _final_message_content(message: dict[str, Any]) -> Any:
    """Use reasoning_content when a reasoning model leaves content empty."""

    content = message.get("content")
    if content not in (None, "", [], {}):
        return content
    return message.get("reasoning_content")


def _parse_final_json(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    text = _content_text(content)
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        parsed = None
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
                break
    if not isinstance(parsed, dict):
        raise AgentToolCallError("模型没有返回完整的结构化诊断")
    return parsed


def _string_list(value: Any, *, limit: int = 8, width: int = 500) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:width] for item in value if str(item).strip()][:limit]


def _evidence_ids(value: Any, known: dict[str, dict[str, Any]]) -> list[str]:
    values = _string_list(value, limit=MAX_TOOL_CALLS, width=40)
    unknown = [item for item in values if item not in known]
    if unknown:
        raise AgentToolCallError("模型引用了不存在的诊断证据")
    return list(dict.fromkeys(values))


def _validate_final_result(
    parsed: dict[str, Any],
    context: DiagnosticToolContext,
    evidence_by_id: dict[str, dict[str, Any]],
    local_diagnoses: list[dict[str, Any]],
    model_name: str,
    tool_call_count: int,
) -> dict[str, Any]:
    items = parsed.get("diagnoses")
    if not isinstance(items, list):
        raise AgentToolCallError("模型结果缺少diagnoses数组")
    expected_events = {
        str(item.get("interface_id")): item
        for item in context.events
        if str(item.get("state")) not in {"ok", "healthy", "disabled", "video_only"}
    }
    raw_by_id = {
        str(item.get("interface_id")): item
        for item in items
        if isinstance(item, dict) and str(item.get("interface_id")) in expected_events
    }
    if set(raw_by_id) != set(expected_events):
        raise AgentToolCallError("模型未覆盖当前全部异常接口")
    diagnoses = []
    for interface_id, event in expected_events.items():
        raw = raw_by_id[interface_id]
        facts = []
        referenced: list[str] = []
        for fact in raw.get("observed_facts") or []:
            if not isinstance(fact, dict):
                continue
            ids = _evidence_ids(fact.get("evidence_ids"), evidence_by_id)
            if not ids:
                continue
            referenced.extend(ids)
            facts.append({"text": str(fact.get("text") or "").strip()[:700], "evidence_ids": ids})
        hypotheses = []
        for hypothesis in raw.get("hypotheses") or []:
            if not isinstance(hypothesis, dict):
                continue
            ids = _evidence_ids(hypothesis.get("evidence_ids"), evidence_by_id)
            if not ids:
                continue
            referenced.extend(ids)
            try:
                confidence = float(hypothesis.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            hypotheses.append(
                {
                    "cause": str(hypothesis.get("cause") or "").strip()[:700],
                    "confidence": max(0.0, min(confidence, 1.0)),
                    "evidence_ids": ids,
                }
            )
        referenced = list(dict.fromkeys(referenced))
        if not facts or not hypotheses or not referenced:
            raise AgentToolCallError("模型结论缺少可验证的工具证据")
        evidence = [evidence_by_id[item] for item in referenced]
        actions = []
        for index, action in enumerate(raw.get("recommended_actions") or [], start=1):
            if not isinstance(action, dict):
                continue
            try:
                priority = int(action.get("priority", index))
            except (TypeError, ValueError):
                priority = index
            actions.append(
                {
                    "priority": max(1, priority),
                    "action": str(action.get("action") or "").strip()[:700],
                    "reason": str(action.get("reason") or "").strip()[:700],
                    "requires_shutdown": bool(action.get("requires_shutdown", False)),
                }
            )
        diagnoses.append(
            {
                "interface_id": interface_id,
                "interface_label": event.get("interface_label") or interface_id,
                "sensor_name": event.get("sensor_name"),
                "channels": list(event.get("channels") or []),
                "state": event.get("state"),
                "fault_type": str(raw.get("fault_type") or "模型基于工具证据的诊断")[:300],
                "confirmed_ok": False,
                "original_error": event.get("message"),
                "observed_facts": facts,
                "hypotheses": hypotheses,
                "cross_interface_findings": _string_list(raw.get("cross_interface_findings")),
                "recommended_actions": actions,
                "unknowns": _string_list(raw.get("unknowns")) or ["模型未说明仍需确认的事项"],
                "evidence_sources": list(
                    dict.fromkeys(
                        _TOOL_SOURCE_LABELS.get(str(item.get("tool")), str(item.get("tool")))
                        for item in evidence
                    )
                ),
                "evidence": evidence,
                "local_fallback": _local_fallback_for(local_diagnoses, interface_id),
                "evidence_boundary": ONLINE_BOUNDARY,
            }
        )
    return {
        "execution_mode": "siliconflow_agent",
        "model_status": "success",
        "model_message": "硅基流动模型已根据实际现象选择只读取证工具并完成诊断",
        "model_name": model_name,
        "summary": _summary(context),
        "tool_call_count": tool_call_count,
        "diagnoses": diagnoses,
        "evidence_boundary": ONLINE_BOUNDARY,
    }


def run_siliconflow_tool_agent(
    api_key: str,
    model_name: str,
    context: DiagnosticToolContext,
    local_diagnoses: list[dict[str, Any]],
    *,
    transport: Any = None,
) -> dict[str, Any]:
    """Run a bounded OpenAI-compatible Function Calling loop."""

    if not str(api_key).strip() or not str(model_name).strip():
        raise AgentToolCallError("未配置完整的API Key和工具调用模型")
    started = time.monotonic()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt()},
        {
            "role": "user",
            "content": json.dumps(_initial_case_payload(context), ensure_ascii=False, separators=(",", ":")),
        },
    ]
    evidence_by_id: dict[str, dict[str, Any]] = {}
    tool_call_count = 0
    allowed_tool_names = {
        str(item.get("function", {}).get("name") or "")
        for item in tool_definitions()
        if isinstance(item, dict)
    }
    caller = transport or (lambda payload: _request_siliconflow(api_key, payload))
    for _round_index in range(MAX_AGENT_ROUNDS - 1):
        if time.monotonic() - started > 90:
            raise AgentToolCallError("模型工具诊断超过90秒限制")
        response = caller(_build_chat_payload(model_name, messages))
        message = _assistant_message(response)
        calls = message.get("tool_calls") or []
        if not calls:
            parsed = _parse_final_json(_final_message_content(message))
            return _validate_final_result(
                parsed,
                context,
                evidence_by_id,
                local_diagnoses,
                model_name,
                tool_call_count,
            )
        if not isinstance(calls, list):
            raise AgentToolCallError("模型工具调用格式无效")
        messages.append(message)
        for call in calls:
            if tool_call_count >= MAX_TOOL_CALLS:
                raise AgentToolCallError(f"模型工具调用超过{MAX_TOOL_CALLS}次限制")
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                raise AgentToolCallError("模型工具调用缺少函数信息")
            try:
                arguments = json.loads(str(function.get("arguments") or "{}"))
            except json.JSONDecodeError:
                raise AgentToolCallError("模型工具参数不是有效JSON") from None
            tool_name = str(function.get("name") or "")
            if tool_name not in allowed_tool_names:
                raise AgentToolCallError(f"模型请求了未授权诊断工具：{tool_name}")
            try:
                evidence = execute_tool(context, tool_name, arguments)
            except DiagnosticToolError as error:
                tool_result = {
                    "ok": False,
                    "error": str(error)[:300],
                    "allowed_interface_ids": sorted(context.interface_ids),
                    "instruction": "请使用当前任务中的接口ID修正参数后重试",
                }
            else:
                evidence_by_id[evidence["evidence_id"]] = evidence
                tool_result = evidence
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or f"call_{tool_call_count + 1}"),
                    "content": json.dumps(tool_result, ensure_ascii=False, separators=(",", ":")),
                }
            )
            tool_call_count += 1
    last_error: AgentToolCallError | None = None
    for include_response_format in (True, False):
        try:
            response = caller(
                _build_evidence_synthesis_payload(
                    model_name,
                    context,
                    evidence_by_id,
                    include_response_format=include_response_format,
                )
            )
            message = _assistant_message(response)
            parsed = _parse_final_json(_final_message_content(message))
            return _validate_final_result(
                parsed,
                context,
                evidence_by_id,
                local_diagnoses,
                model_name,
                tool_call_count,
            )
        except AgentToolCallError as error:
            last_error = error
            public = error.public_message
            non_retryable = ("API Key", "权限", "额度", "超时", "网络", "服务暂不可用")
            if not include_response_format or any(item in public for item in non_retryable):
                raise
    raise last_error or AgentToolCallError("模型没有返回最终结构化诊断")


def _run_structured_fallback(
    api_key: str,
    model_name: str,
    context: DiagnosticToolContext,
    local_diagnoses: list[dict[str, Any]],
    caller: Any,
) -> dict[str, Any]:
    """Ask the model for a structured result when its tool API is unavailable."""

    offline_result = run_offline_diagnosis(context, local_diagnoses)
    evidence_by_id = {
        str(item.get("evidence_id")): item
        for diagnosis in offline_result.get("diagnoses") or []
        for item in diagnosis.get("evidence") or []
        if isinstance(item, dict) and item.get("evidence_id")
    }
    # Some OpenAI-compatible gateways accept the chat endpoint but reject
    # response_format=json_object (or silently ignore it).  Keep the first
    # request strict, then retry once without that optional parameter.  This
    # preserves a model-generated diagnosis while avoiding a false failure for
    # models that can return JSON in ordinary assistant content.
    base_payload = _build_structured_payload(model_name, context, offline_result)
    attempts = [base_payload, deepcopy(base_payload)]
    attempts[1].pop("response_format", None)
    last_error: AgentToolCallError | None = None
    for index, payload in enumerate(attempts):
        try:
            response = caller(payload)
            message = _assistant_message(response)
            parsed = _parse_final_json(_final_message_content(message))
            result = _validate_final_result(
                parsed,
                context,
                evidence_by_id,
                local_diagnoses,
                model_name,
                0,
            )
            result["execution_mode"] = "siliconflow_structured"
            result["model_status"] = "success_structured_fallback"
            result["model_message"] = (
                "当前模型不支持工具调用，已使用本地证据完成结构化模型诊断"
                if index == 0
                else "当前模型不支持工具调用，已关闭JSON模式并使用本地证据完成结构化模型诊断"
            )
            return result
        except AgentToolCallError as error:
            last_error = error
            # Credentials, quota and network failures will not be fixed by
            # changing response_format; fail fast so we do not add another
            # network timeout.  Compatibility errors continue to the second
            # (plain-content) request.
            public = error.public_message
            non_retryable = ("API Key", "权限", "额度", "超时", "网络", "服务暂不可用")
            if index == 1 or any(item in public for item in non_retryable):
                raise
    if last_error is not None:
        raise last_error
    raise AgentToolCallError("模型没有返回结构化诊断")


def run_agentic_diagnoses(
    context: DiagnosticToolContext,
    local_diagnoses: list[dict[str, Any]],
    *,
    api_key: str,
    model_name: str,
    transport: Any = None,
) -> dict[str, Any]:
    """Select online tool calling when configured, otherwise run offline."""

    if not str(api_key).strip() or not str(model_name).strip():
        return run_offline_diagnosis(context, local_diagnoses)
    try:
        if str(model_name).strip().lower().endswith("/deepseek-v3"):
            return run_siliconflow_planned_agent(
                str(api_key).strip(),
                str(model_name).strip(),
                context,
                local_diagnoses,
                transport=transport,
            )
        return run_siliconflow_tool_agent(
            str(api_key).strip(),
            str(model_name).strip(),
            context,
            local_diagnoses,
            transport=transport,
        )
    except (AgentToolCallError, DiagnosticToolError) as error:
        message = error.public_message if isinstance(error, AgentToolCallError) else str(error)
    except Exception:
        message = "模型工具诊断失败，已使用内置离线测试诊断"
    caller = transport or (lambda payload: _request_siliconflow(api_key, payload))
    try:
        return _run_structured_fallback(
            str(api_key).strip(),
            str(model_name).strip(),
            context,
            local_diagnoses,
            caller,
        )
    except Exception:
        pass
    result = run_offline_diagnosis(context, local_diagnoses)
    result["model_status"] = "failed_offline_fallback"
    result["model_message"] = f"{message}；已使用内置离线测试诊断"
    result["model_name"] = str(model_name).strip()
    return result
