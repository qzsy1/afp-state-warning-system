"""Local LangChain diagnosis for the original AFP acquisition UI.

The module deliberately accepts only a normalized hardware-check event.  It
does not receive credentials, instantiate a provider model, or perform network
I/O; LangChain Core is used only to make the local tool sequence auditable.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langsmith import tracing_context


class AgentGateError(ValueError):
    """Raised when the UI gate is not satisfied."""


_INTERFACE_LABELS = {
    "thermocouple_8ch": "SMRF 八通道热电偶",
    "plc_process": "松下 PLC",
    "uvc_temperature": "BSV UVC 热像仪",
    "abb_motion": "ABB 机器人",
    "m3232_pressure": "M3232 薄膜压力",
}
_EVENT_FIELDS = {
    "event_id",
    "interface_id",
    "interface_label",
    "role",
    "driver",
    "endpoint",
    "sensor_name",
    "channels",
    "state",
    "message",
    "evidence",
    "simulated",
}
_EVIDENCE_FIELDS = {
    "expected_channels",
    "detected_channels",
    "missing_channels",
    "invalid_channels",
    "sample_counts",
    "invalid_sample_counts",
    "received_samples",
    "invalid_samples",
    "last_sample_age_seconds",
}


def validate_agent_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the browser payload without accepting credential material."""

    allowed = {"api_key_present", "model_name", "event"}
    unexpected = set(payload or {}) - allowed
    if unexpected:
        raise ValueError("Agent 请求包含未允许字段；API Key、密码或令牌原文不得发送")
    event = (payload or {}).get("event")
    if not isinstance(event, dict):
        raise ValueError("Agent 请求缺少接口异常事件")
    unexpected_event = set(event) - _EVENT_FIELDS
    if unexpected_event:
        raise ValueError("Agent 事件包含未允许字段；不得携带凭据或其他秘密")
    evidence = event.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, dict):
            raise ValueError("Agent 事件证据必须是对象")
        unexpected_evidence = set(evidence) - _EVIDENCE_FIELDS
        if unexpected_evidence:
            raise ValueError("Agent 事件证据包含未允许字段")
    return event


def _trace(context: dict[str, Any], step_id: str, title: str, detail: str) -> None:
    context["trace"].append(
        {"id": step_id, "title": title, "detail": detail, "status": "complete"}
    )


def _clean_event(event: dict[str, Any]) -> dict[str, Any]:
    clean = {key: deepcopy(value) for key, value in event.items() if key in _EVENT_FIELDS}
    clean["channels"] = [str(item) for item in clean.get("channels") or []][:32]
    clean["evidence"] = {
        key: deepcopy(value)
        for key, value in (clean.get("evidence") or {}).items()
        if key in _EVIDENCE_FIELDS
    }
    return clean


def build_agent_event(hardware_result: dict[str, Any]) -> dict[str, Any] | None:
    """Convert the original ``/api/acquisition/test`` result into one event."""

    interfaces = [
        item
        for item in hardware_result.get("interfaces") or []
        if isinstance(item, dict) and item.get("enabled", True) and not item.get("ok")
    ]
    sensors = {
        str(item.get("name")): item
        for item in hardware_result.get("sensors") or []
        if isinstance(item, dict) and item.get("selected") and not item.get("ok")
    }
    if not interfaces and not sensors:
        return None

    sensor_names = set(sensors)
    def interface_match_score(item: dict[str, Any]) -> int:
        invalid = sensor_names.intersection({str(name) for name in item.get("invalid_channels") or []})
        missing = sensor_names.intersection({str(name) for name in item.get("missing_channels") or []})
        expected = sensor_names.intersection({str(name) for name in item.get("expected_channels") or []})
        return len(invalid) * 4 + len(missing) * 2 + len(expected)

    interface = max(interfaces, key=interface_match_score) if interfaces else {}
    interface_id = str(interface.get("id") or "")
    expected = [str(item) for item in interface.get("expected_channels") or []]
    candidates = [
        name
        for name in [
            *interface.get("invalid_channels", []),
            *interface.get("missing_channels", []),
            *expected,
        ]
        if str(name)
    ]
    sensor_name = candidates[0] if candidates else (next(iter(sensors), "接口"))
    sensor = sensors.get(sensor_name) or {}
    state = str(interface.get("state") or sensor.get("state") or "no_data")
    message = str(interface.get("message") or sensor.get("message") or "接口或通道未返回有效数据")
    channels = expected or ([sensor_name] if sensor_name != "接口" else [])
    evidence = {
        "expected_channels": expected,
        "detected_channels": list(interface.get("detected_channels") or []),
        "missing_channels": list(interface.get("missing_channels") or []),
        "invalid_channels": list(interface.get("invalid_channels") or []),
        "sample_counts": deepcopy(interface.get("sample_counts") or {}),
        "invalid_sample_counts": deepcopy(interface.get("invalid_sample_counts") or {}),
        "received_samples": sensor.get("received_samples"),
        "invalid_samples": sensor.get("invalid_samples"),
        "last_sample_age_seconds": sensor.get("last_sample_age_seconds"),
    }
    return _clean_event(
        {
            "interface_id": interface_id or "sensor_channel",
            "interface_label": _INTERFACE_LABELS.get(interface_id, interface_id or "传感器接口"),
            "role": interface.get("role", "custom"),
            "driver": interface.get("driver", ""),
            "endpoint": interface.get("endpoint", "未填写地址"),
            "sensor_name": sensor_name,
            "channels": channels,
            "state": state,
            "message": message,
            "evidence": evidence,
            "simulated": bool(hardware_result.get("simulated", False)),
        }
    )


@tool
def get_interface_context(event: dict[str, Any]) -> dict[str, Any]:
    """Return the endpoint and channel identity from the normalized event."""

    return {
        key: deepcopy(event.get(key))
        for key in (
            "interface_id",
            "interface_label",
            "role",
            "driver",
            "endpoint",
            "sensor_name",
            "channels",
        )
    }


@tool
def inspect_error_evidence(event: dict[str, Any]) -> dict[str, Any]:
    """Extract only whitelisted interface-check evidence."""

    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    return {
        "state": event.get("state"),
        "message": event.get("message"),
        **{
            key: deepcopy(value)
            for key, value in evidence.items()
            if key in _EVIDENCE_FIELDS
        },
    }


_RULES: dict[str, dict[str, Any]] = {
    "not_connected": {
        "fault_type": "接口无法打开或读取",
        "causes": ["接口被占用或设备未上电", "驱动未正确加载", "连接参数与设备不匹配"],
        "actions": ["检查端点是否正确", "关闭占用接口的程序", "核对驱动和通信参数"],
    },
    "no_data": {
        "fault_type": "接口未收到有效数据",
        "causes": ["设备未持续发送数据", "串口或网络链路中断", "接口与通道映射不一致"],
        "actions": ["确认设备供电和链路", "核对接口与通道映射", "保存一帧原始数据检查协议"],
    },
    "invalid_data": {
        "fault_type": "采集数据解析异常",
        "causes": ["返回帧结构与解析器预期不一致", "通信参数不匹配", "设备返回非数值或不完整数据"],
        "actions": ["保存一帧未经处理的原始数据", "核对通信参数和数据维度", "解析通过前不要用于模型或控制"],
    },
    "stale": {
        "fault_type": "接口数据已中断",
        "causes": ["采集线程停滞", "设备更新频率下降", "缓存仍保留旧值"],
        "actions": ["检查最近帧时间戳", "检查采集线程状态", "超过时延阈值后标记数据不可用"],
    },
    "waiting": {
        "fault_type": "接口等待首个数据",
        "causes": ["设备刚启动尚未发送数据", "接口打开但采样尚未开始"],
        "actions": ["等待首个有效帧", "若持续等待则检查端点和设备状态"],
    },
}


@tool
def lookup_local_rule(state: str, sensor_name: str) -> dict[str, Any]:
    """Match the original acquisition state to a local rule."""

    rule = deepcopy(_RULES.get(state) or _RULES["no_data"])
    if sensor_name == "薄膜压力" and state == "invalid_data":
        rule["causes"] = [
            "M3232 矩阵帧包装或行列尺寸与当前解析路径不一致",
            "COM 端点或 115200/8N1 参数不匹配",
            "M3232 原始协议尚未完成现场帧核对",
        ]
        rule["actions"] = [
            "确认实际 COM 口和 115200/8N1 参数",
            "捕获一帧未经处理的 M3232 串口数据",
            "核对矩阵尺寸、包装字段和有效像素规则",
        ]
    return rule


@tool
def compose_diagnostic_prompt(
    event: dict[str, Any], context: dict[str, Any], evidence: dict[str, Any], rule: dict[str, Any]
) -> dict[str, Any]:
    """Compose a deterministic diagnostic message from local evidence."""

    return {
        "interface_id": event.get("interface_id"),
        "interface_label": context.get("interface_label"),
        "sensor_name": event.get("sensor_name"),
        "channels": list(event.get("channels") or []),
        "fault_type": rule["fault_type"],
        "summary": f"{context.get('interface_label')} / {event.get('sensor_name')}：{event.get('message')}",
        "error_message": event.get("message"),
        "evidence": evidence,
        "possible_causes": list(rule["causes"]),
        "recommended_actions": list(rule["actions"]),
        "evidence_boundary": "基于原系统接口检查结果的本地规则诊断，不等同于硬件故障确认",
        "simulated": bool(event.get("simulated", False)),
    }


def _receive_event(context: dict[str, Any]) -> dict[str, Any]:
    _trace(context, "event_received", "接收接口异常事件", f"收到 {context['event']['message']}")
    return context


def _check_gate(context: dict[str, Any]) -> dict[str, Any]:
    _trace(
        context,
        "gate_checked",
        "检查 Agent 启用条件",
        f"API Key 已填写；模型显示名为 {context['model_name']}；仅执行本地流程",
    )
    return context


def _call_context_tool(context: dict[str, Any]) -> dict[str, Any]:
    context["interface_context"] = get_interface_context.invoke({"event": context["event"]})
    _trace(
        context,
        "get_interface_context",
        "工具 1 · 定位接口与通道",
        f"定位到 {context['interface_context']['interface_label']} / {context['interface_context']['endpoint']}",
    )
    return context


def _call_evidence_tool(context: dict[str, Any]) -> dict[str, Any]:
    context["evidence"] = inspect_error_evidence.invoke({"event": context["event"]})
    _trace(context, "inspect_error_evidence", "工具 2 · 检查错误证据", "提取状态、通道、计数和时延信息")
    return context


def _call_rule_tool(context: dict[str, Any]) -> dict[str, Any]:
    context["rule"] = lookup_local_rule.invoke(
        {"state": context["event"]["state"], "sensor_name": context["event"]["sensor_name"]}
    )
    _trace(
        context,
        "lookup_local_rule",
        "工具 3 · 匹配本地规则",
        f"匹配为 {context['rule']['fault_type']}，未访问外部服务",
    )
    return context


def _call_report_tool(context: dict[str, Any]) -> dict[str, Any]:
    context["diagnosis"] = compose_diagnostic_prompt.invoke(
        {
            "event": context["event"],
            "context": context["interface_context"],
            "evidence": context["evidence"],
            "rule": context["rule"],
        }
    )
    _trace(context, "compose_diagnostic_prompt", "工具 4 ·形成诊断提示", "组合接口、传感器、错误、证据和建议")
    return context


def _complete(context: dict[str, Any]) -> dict[str, Any]:
    _trace(context, "complete", "诊断流程完成", "结果返回原系统界面；未修改采集配置或设备")
    return {
        "model_name": context["model_name"],
        "execution_mode": "langchain_local_runnable_simulation",
        "diagnosis": context["diagnosis"],
        "simulated": bool(context["event"].get("simulated", False)),
    }


_LOCAL_CHAIN = (
    RunnableLambda(_receive_event)
    | RunnableLambda(_check_gate)
    | RunnableLambda(_call_context_tool)
    | RunnableLambda(_call_evidence_tool)
    | RunnableLambda(_call_rule_tool)
    | RunnableLambda(_call_report_tool)
    | RunnableLambda(_complete)
)


def run_interface_diagnosis(
    event: dict[str, Any] | None,
    *,
    api_key_present: bool,
    model_name: str,
) -> dict[str, Any]:
    """Run the local chain after the original UI gate is satisfied."""

    if not api_key_present:
        raise AgentGateError("请先填写 API Key；其原文不会发送到后端")
    if not str(model_name).strip():
        raise AgentGateError("请先填写模型名称")
    if not event or str(event.get("state")) in {"ok", "healthy", "disabled", "video_only"}:
        raise AgentGateError("当前没有可诊断的接口异常")
    context = {"event": _clean_event(event), "model_name": str(model_name).strip(), "trace": []}
    # The integrated app is offline by design, even if the host enables tracing.
    with tracing_context(enabled=False):
        return _LOCAL_CHAIN.invoke(context, config={"callbacks": []})
