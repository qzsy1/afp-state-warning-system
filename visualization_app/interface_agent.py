"""Local-first LangChain diagnosis for the original AFP acquisition UI."""
from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from copy import deepcopy
from typing import Any

try:
    from langchain_core.runnables import RunnableLambda
    from langchain_core.tools import tool
    from langsmith import tracing_context
except ImportError:  # pragma: no cover - used by the dependency-light EXE
    class RunnableLambda:
        def __init__(self, function):
            self.function = function

        def __or__(self, other):
            return RunnableLambda(lambda value: other.invoke(self.invoke(value)))

        def invoke(self, value, config=None):
            return self.function(value)

    def tool(function):
        def invoke(payload=None, **kwargs):
            values = payload if isinstance(payload, dict) else kwargs
            return function(**values)

        function.invoke = invoke
        return function

    @contextmanager
    def tracing_context(**_kwargs):
        yield


class AgentGateError(ValueError):
    """Raised when the UI gate is not satisfied."""


class SiliconFlowCallError(RuntimeError):
    """A sanitized provider failure that is safe to return to the local UI."""

    def __init__(self, public_message: str):
        super().__init__(public_message)
        self.public_message = public_message


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
    "sensor_states",
}

_HARDWARE_INTERFACE_FIELDS = {
    "id",
    "label",
    "role",
    "driver",
    "endpoint",
    "physical_interface_id",
    "physical_interface_kind",
    "protocol",
    "physical_verified",
    "physical_fallback",
    "physical_warning",
    "enabled",
    "expected_channels",
    "detected_channels",
    "missing_channels",
    "invalid_channels",
    "sample_counts",
    "invalid_sample_counts",
    "errors",
    "state",
    "message",
    "ok",
    "protocol_evidence",
    "raw_frame_summary",
}

_HARDWARE_SENSOR_FIELDS = {
    "name",
    "selected",
    "received_samples",
    "invalid_samples",
    "last_sample_age_seconds",
    "state",
    "message",
    "blocking",
    "ok",
}


def _clean_hardware_result(value: Any) -> dict[str, Any]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("硬件检查结果必须是对象")
    try:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("硬件检查结果不能序列化") from None
    if len(encoded) > 200_000:
        raise ValueError("硬件检查结果过大")
    interfaces = value.get("interfaces") or []
    sensors = value.get("sensors") or []
    if not isinstance(interfaces, list) or len(interfaces) > 64:
        raise ValueError("硬件接口检查结果无效")
    if not isinstance(sensors, list) or len(sensors) > 128:
        raise ValueError("硬件通道检查结果无效")
    clean_interfaces = []
    for item in interfaces:
        if not isinstance(item, dict):
            raise ValueError("硬件接口检查条目必须是对象")
        clean_interfaces.append(
            {key: deepcopy(item.get(key)) for key in _HARDWARE_INTERFACE_FIELDS if key in item}
        )
    clean_sensors = []
    for item in sensors:
        if not isinstance(item, dict):
            raise ValueError("硬件通道检查条目必须是对象")
        clean_sensors.append(
            {key: deepcopy(item.get(key)) for key in _HARDWARE_SENSOR_FIELDS if key in item}
        )
    return {
        "simulated": bool(value.get("simulated", False)),
        "ok": bool(value.get("ok", False)),
        "interfaces": clean_interfaces,
        "sensors": clean_sensors,
        "errors": [str(item)[:500] for item in (value.get("errors") or [])[:20]],
        "elapsed_seconds": value.get("elapsed_seconds"),
    }


def validate_agent_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate one local request before any optional provider call."""

    allowed = {"api_key", "model_name", "events", "hardware_result"}
    unexpected = set(payload or {}) - allowed
    if unexpected:
        raise ValueError("Agent 请求包含未允许字段")
    api_key = str((payload or {}).get("api_key") or "").strip()
    model_name = str((payload or {}).get("model_name") or "").strip()
    if len(api_key) > 512:
        raise ValueError("API Key 长度无效")
    if len(model_name) > 240:
        raise ValueError("模型名称长度无效")
    events = (payload or {}).get("events")
    if not isinstance(events, list) or not events or len(events) > 64:
        raise ValueError("Agent 请求缺少有效的接口异常列表")
    clean_events: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Agent 异常条目必须是对象")
        unexpected_event = set(event) - _EVENT_FIELDS
        if unexpected_event:
            raise ValueError("Agent 异常条目包含未允许字段")
        evidence = event.get("evidence")
        if evidence is not None:
            if not isinstance(evidence, dict):
                raise ValueError("Agent 事件证据必须是对象")
            if set(evidence) - _EVIDENCE_FIELDS:
                raise ValueError("Agent 事件证据包含未允许字段")
        clean_events.append(_clean_event(event))
    return {
        "api_key": api_key,
        "model_name": model_name,
        "events": clean_events,
        "hardware_result": _clean_hardware_result((payload or {}).get("hardware_result")),
    }


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
    sensor_states = {
        name: {
            key: deepcopy(value)
            for key, value in item.items()
            if key in {
                "state",
                "message",
                "received_samples",
                "invalid_samples",
                "last_sample_age_seconds",
            }
        }
        for name, item in sensors.items()
    }
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
        "sensor_states": sensor_states,
    }
    return _clean_event(
        {
            "interface_id": interface_id or "sensor_channel",
            "interface_label": _INTERFACE_LABELS.get(interface_id, interface_id or "传感器接口"),
            "role": interface.get("role", "custom"),
            "driver": interface.get("driver", ""),
            "endpoint": interface.get("endpoint", "未填写地址"),
            "physical_interface_id": interface.get("physical_interface_id", ""),
            "physical_interface_kind": interface.get("physical_interface_kind", ""),
            "protocol": interface.get("protocol", interface.get("driver", "")),
            "physical_fallback": bool(interface.get("physical_fallback", False)),
            "physical_warning": interface.get("physical_warning", ""),
            "sensor_name": sensor_name,
            "channels": channels,
            "state": state,
            "message": message,
            "evidence": evidence,
            "simulated": bool(hardware_result.get("simulated", False)),
        }
    )


def build_agent_events(hardware_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert every abnormal interface or selected sensor into an event."""

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
    events: list[dict[str, Any]] = []
    covered_sensors: set[str] = set()

    for interface in interfaces:
        names = list(
            dict.fromkeys(
                str(name)
                for name in [
                    *(interface.get("invalid_channels") or []),
                    *(interface.get("missing_channels") or []),
                ]
                if str(name)
            )
        )
        if not names:
            names = [
                str(name)
                for name in interface.get("expected_channels") or []
                if str(name) in sensors
            ]
        if not names:
            event = build_agent_event(
                {"simulated": hardware_result.get("simulated", False), "interfaces": [interface], "sensors": []}
            )
            if event:
                events.append(event)
            continue
        # Keep one event per physical interface. A multi-channel device (for
        # example the eight-channel SMRF thermocouple) used to produce one
        # identical model request per channel, which obscured the real fault
        # and could exceed provider limits. Preserve every affected channel
        # and per-channel counters in the single event instead.
        selected_sensors = [sensors[name] for name in names if name in sensors]
        event = build_agent_event(
            {
                "simulated": hardware_result.get("simulated", False),
                "interfaces": [interface],
                "sensors": selected_sensors,
            }
        )
        if event:
            event["channels"] = names or list(event.get("channels") or [])
            events.append(_clean_event(event))
            covered_sensors.update(name for name in names if name in sensors)

    for name, sensor in sensors.items():
        if name in covered_sensors:
            continue
        event = build_agent_event(
            {"simulated": hardware_result.get("simulated", False), "interfaces": [], "sensors": [sensor]}
        )
        if event:
            events.append(event)

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        unique[(str(event.get("interface_id")), str(event.get("sensor_name")))] = event
    return list(unique.values())


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
            "physical_interface_id",
            "physical_interface_kind",
            "protocol",
            "physical_fallback",
            "physical_warning",
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


SILICONFLOW_CHAT_COMPLETIONS_URL = "https://api.siliconflow.cn/v1/chat/completions"
DEFAULT_SILICONFLOW_MODEL = "deepseek-ai/DeepSeek-V3"


def _read_user_environment_setting(name: str) -> str:
    """Read the current Windows user environment even if the process is stale."""

    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _value_type = winreg.QueryValueEx(key, name)
        return str(value or "").strip()
    except (ImportError, OSError, TypeError, ValueError):
        return ""


def _environment_setting(name: str) -> str:
    return str(os.environ.get(name) or "").strip() or _read_user_environment_setting(name)


def get_agent_defaults() -> dict[str, Any]:
    """Return safe UI defaults without returning the local API key."""

    model_name = _environment_setting("AFP_SILICONFLOW_MODEL") or DEFAULT_SILICONFLOW_MODEL
    return {
        "model_name": model_name or DEFAULT_SILICONFLOW_MODEL,
        "default_key_available": bool(_environment_setting("AFP_SILICONFLOW_API_KEY")),
    }


def _resolve_agent_credentials(api_key: str, model_name: str) -> tuple[str, str]:
    """Use request values first, then optional machine-local environment defaults."""

    clean_key = str(api_key or "").strip() or _environment_setting("AFP_SILICONFLOW_API_KEY")
    clean_model = str(model_name or "").strip() or (
        _environment_setting("AFP_SILICONFLOW_MODEL") or DEFAULT_SILICONFLOW_MODEL
    )
    return clean_key, clean_model or DEFAULT_SILICONFLOW_MODEL


def _content_to_text(content: Any) -> str:
    """Flatten OpenAI-compatible text content into one string.

    SiliconFlow models normally return a string, but some compatible gateways
    return a list of typed content blocks.  Keeping this conversion local lets
    the rest of the parser handle both forms without weakening validation.
    """

    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        nested = content.get("content")
        if nested is not None:
            return _content_to_text(nested)
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif block.get("content") is not None:
                    parts.append(_content_to_text(block.get("content")))
        return "".join(parts)
    return ""


def _json_candidates(text: str) -> list[Any]:
    """Return JSON values found in fenced, prefixed, or plain model output."""

    source = str(text or "").replace("\ufeff", "").strip()
    if not source:
        return []
    candidates: list[str] = []
    if "```" in source:
        chunks = source.split("```")
        for index in range(1, len(chunks), 2):
            chunk = chunks[index].strip()
            if chunk.lower().startswith("json"):
                chunk = chunk[4:].lstrip(" :\t\r\n")
            if chunk:
                candidates.append(chunk)
    candidates.append(source)
    decoder = json.JSONDecoder()
    for start, char in enumerate(source):
        if char not in "[{":
            continue
        try:
            _, end = decoder.raw_decode(source[start:])
        except json.JSONDecodeError:
            continue
        candidates.append(source[start : start + end])

    parsed: list[Any] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed.append(json.loads(candidate))
        except (TypeError, json.JSONDecodeError):
            continue
    return parsed


def parse_model_diagnoses(response_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the diagnoses array from common SiliconFlow response shapes."""

    try:
        message = response_data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise SiliconFlowCallError("硅基流动响应缺少 choices/message 字段") from None
    if not isinstance(message, dict):
        raise SiliconFlowCallError("硅基流动响应的 message 格式无效") from None

    content = message.get("content")
    if content is None or content == "":
        # A few reasoning-capable models put their final text in this field.
        content = message.get("reasoning_content")
    if isinstance(content, dict):
        parsed_values: list[Any] = [content]
    else:
        parsed_values = _json_candidates(_content_to_text(content))

    for parsed in parsed_values:
        if isinstance(parsed, dict):
            diagnoses = parsed.get("diagnoses")
            if isinstance(diagnoses, list):
                return [item for item in diagnoses if isinstance(item, dict)]
            # Accept a one-item object returned without the wrapper; coverage
            # validation still happens in _clean_model_enhancements.
            if parsed.get("event_index") is not None:
                return [parsed]
        elif isinstance(parsed, list):
            items = [item for item in parsed if isinstance(item, dict)]
            if items:
                return items
    raise SiliconFlowCallError("硅基流动模型响应格式无法解析，已保留完整本地诊断") from None


def _model_prompt_data(
    events: list[dict[str, Any]],
    local_diagnoses: list[dict[str, Any]],
    *,
    compact: bool = False,
) -> list[dict[str, Any]]:
    """Build a bounded prompt so large event batches do not truncate JSON."""

    result: list[dict[str, Any]] = []
    for index, item in enumerate(local_diagnoses):
        base = {
            "event_index": index,
            "interface": item.get("interface_label"),
            "endpoint": events[index].get("endpoint"),
            "physical_interface": events[index].get("physical_interface_id"),
            "physical_kind": events[index].get("physical_interface_kind"),
            "physical_warning": events[index].get("physical_warning"),
            "sensor": item.get("sensor_name"),
            "channels": item.get("channels"),
            "state": events[index].get("state"),
            "original_error": item.get("error_message"),
        }
        if not compact:
            base.update(
                {
                    "evidence": item.get("evidence"),
                    "local_fault_type": item.get("fault_type"),
                    "local_causes": item.get("possible_causes"),
                    "local_actions": item.get("recommended_actions"),
                }
            )
        result.append(base)
    return result


def _request_siliconflow(
    api_key: str,
    model_name: str,
    prompt_data: list[dict[str, Any]],
    *,
    max_tokens: int,
    include_response_format: bool = True,
) -> dict[str, Any]:
    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是工业传感器接口诊断助手。只能基于给定证据分析，不能把推测写成已确认硬件故障。"
                    "只输出一个 JSON 对象，不要 Markdown、不要解释文字。对象必须只有 diagnoses 数组；"
                    "数组每项包含 event_index、analysis、possible_causes、recommended_actions，且覆盖所有 event_index。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt_data, ensure_ascii=False, separators=(",", ":")),
            },
        ],
        "stream": False,
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    if include_response_format:
        payload["response_format"] = {"type": "json_object"}
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
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code in {401, 403}:
            raise SiliconFlowCallError("硅基流动 API Key 无效或没有调用权限") from None
        if error.code in {400, 404}:
            raise SiliconFlowCallError("硅基流动模型名称无效或请求不受支持") from None
        if error.code == 429:
            raise SiliconFlowCallError("硅基流动调用受限或账户额度不足") from None
        raise SiliconFlowCallError(f"硅基流动服务暂不可用（HTTP {error.code}）") from None
    except (URLError, TimeoutError, socket.timeout):
        raise SiliconFlowCallError("连接硅基流动超时或网络不可用") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SiliconFlowCallError("硅基流动返回了无法解析的响应") from None


def call_siliconflow_model(
    api_key: str,
    model_name: str,
    events: list[dict[str, Any]],
    local_diagnoses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Request structured enhancements, retrying once with a compact prompt."""

    if not events or not local_diagnoses:
        raise SiliconFlowCallError("没有可发送给硅基流动的诊断数据")
    expected_count = len(local_diagnoses)
    attempts = (
        (_model_prompt_data(events, local_diagnoses), max(4096, expected_count * 240), True),
        (_model_prompt_data(events, local_diagnoses, compact=True), max(4096, expected_count * 180), False),
    )
    last_error: SiliconFlowCallError | None = None
    for prompt_data, max_tokens, include_response_format in attempts:
        try:
            response_data = _request_siliconflow(
                api_key,
                model_name,
                prompt_data,
                max_tokens=min(max_tokens, 8192),
                include_response_format=include_response_format,
            )
            diagnoses = parse_model_diagnoses(response_data)
            # If the provider returned only part of the batch, the compact retry
            # gets a chance to recover instead of silently accepting a partial result.
            indexes = {
                int(item.get("event_index"))
                for item in diagnoses
                if isinstance(item, dict)
                and str(item.get("event_index", "")).strip().lstrip("-").isdigit()
            }
            if len(indexes) == expected_count and indexes == set(range(expected_count)):
                return diagnoses
            last_error = SiliconFlowCallError("硅基流动模型未覆盖全部异常，已自动重试")
        except SiliconFlowCallError as error:
            last_error = error
    raise last_error or SiliconFlowCallError("硅基流动模型调用失败，已使用本地规则")


def _call_model_runnable(context: dict[str, Any]) -> list[dict[str, Any]]:
    return call_siliconflow_model(
        context["api_key"],
        context["model_name"],
        context["events"],
        context["local_diagnoses"],
    )


_MODEL_ENHANCEMENT_CHAIN = RunnableLambda(_call_model_runnable)


def _clean_model_enhancements(
    items: list[dict[str, Any]], expected_count: int
) -> dict[int, dict[str, Any]]:
    cleaned: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("event_index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= expected_count:
            continue
        analysis = str(
            item.get("analysis") or item.get("summary") or item.get("diagnosis") or item.get("reason") or ""
        ).strip()[:1200]
        raw_causes = item.get("possible_causes") or item.get("causes") or []
        raw_actions = item.get("recommended_actions") or item.get("actions") or []
        if isinstance(raw_causes, str):
            raw_causes = [raw_causes]
        if isinstance(raw_actions, str):
            raw_actions = [raw_actions]
        causes = [str(value).strip()[:300] for value in raw_causes if str(value).strip()][:6]
        actions = [str(value).strip()[:300] for value in raw_actions if str(value).strip()][:6]
        if analysis or causes or actions:
            cleaned[index] = {
                "analysis": analysis,
                "possible_causes": causes,
                "recommended_actions": actions,
            }
    if len(cleaned) != expected_count:
        raise SiliconFlowCallError("硅基流动模型未覆盖全部异常，已保留完整本地诊断")
    return cleaned


def run_interface_diagnoses(
    events: list[dict[str, Any]] | None,
    *,
    api_key: str,
    model_name: str,
    model_caller: Any = None,
    hardware_result: dict[str, Any] | None = None,
    discovery: dict[str, Any] | None = None,
    acquisition_status: dict[str, Any] | None = None,
    transport: Any = None,
) -> dict[str, Any]:
    """Run local fallback rules, then select offline or tool-calling diagnosis."""

    clean_events = [
        _clean_event(event)
        for event in events or []
        if isinstance(event, dict)
        and str(event.get("state")) not in {"ok", "healthy", "disabled", "video_only"}
    ]
    if not clean_events:
        raise AgentGateError("当前没有可诊断的接口异常")

    diagnoses: list[dict[str, Any]] = []
    _, display_model = _resolve_agent_credentials(api_key, model_name)
    with tracing_context(enabled=False):
        for event in clean_events:
            local_result = _LOCAL_CHAIN.invoke(
                {"event": event, "model_name": display_model, "trace": []},
                config={"callbacks": []},
            )
            diagnoses.append(local_result["diagnosis"])

    clean_key, clean_model = _resolve_agent_credentials(api_key, model_name)
    if model_caller is None:
        from agentic_diagnosis import run_agentic_diagnoses
        from diagnostic_tools import DiagnosticToolContext

        source_result = deepcopy(hardware_result) if isinstance(hardware_result, dict) else {}
        if not source_result:
            interfaces = []
            sensors = []
            for event in clean_events:
                evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
                channels = list(event.get("channels") or [])
                interfaces.append(
                    {
                        "id": event.get("interface_id"),
                        "role": event.get("role"),
                        "driver": event.get("driver"),
                        "endpoint": event.get("endpoint"),
                        "physical_interface_id": event.get("physical_interface_id"),
                        "physical_interface_kind": event.get("physical_interface_kind"),
                        "protocol": event.get("protocol"),
                        "physical_fallback": event.get("physical_fallback", False),
                        "physical_warning": event.get("physical_warning", ""),
                        "enabled": True,
                        "expected_channels": channels,
                        "detected_channels": list(evidence.get("detected_channels") or []),
                        "missing_channels": list(evidence.get("missing_channels") or channels),
                        "invalid_channels": list(evidence.get("invalid_channels") or []),
                        "sample_counts": deepcopy(evidence.get("sample_counts") or {}),
                        "invalid_sample_counts": deepcopy(evidence.get("invalid_sample_counts") or {}),
                        "errors": [],
                        "state": event.get("state"),
                        "message": event.get("message"),
                        "ok": False,
                    }
                )
                sensor_states = evidence.get("sensor_states") if isinstance(evidence.get("sensor_states"), dict) else {}
                for name in channels:
                    state = sensor_states.get(name) if isinstance(sensor_states.get(name), dict) else {}
                    sensors.append(
                        {
                            "name": name,
                            "selected": True,
                            "received_samples": int(state.get("received_samples") or 0),
                            "invalid_samples": int(state.get("invalid_samples") or 0),
                            "state": state.get("state") or "no_data",
                            "message": state.get("message") or "未检测到数据",
                            "ok": str(state.get("state")) == "ok",
                        }
                    )
            source_result = {
                "simulated": any(bool(item.get("simulated")) for item in clean_events),
                "ok": False,
                "interfaces": interfaces,
                "sensors": sensors,
            }
        context = DiagnosticToolContext(
            events=tuple(clean_events),
            hardware_result=source_result,
            discovery=discovery or {},
            acquisition_status=acquisition_status or {"running": False, "sensors": source_result.get("sensors", [])},
        )
        return run_agentic_diagnoses(
            context,
            diagnoses,
            api_key=clean_key,
            model_name=clean_model,
            transport=transport,
        )

    if not clean_key or not clean_model:
        return {
            "execution_mode": "local_rules",
            "model_status": "not_configured",
            "model_message": "未配置完整的 API Key 和模型名称，已使用本地规则",
            "model_name": clean_model,
            "diagnoses": diagnoses,
        }

    caller = model_caller or call_siliconflow_model
    try:
        if model_caller is None:
            with tracing_context(enabled=False):
                raw_enhancements = _MODEL_ENHANCEMENT_CHAIN.invoke(
                    {
                        "api_key": clean_key,
                        "model_name": clean_model,
                        "events": clean_events,
                        "local_diagnoses": diagnoses,
                    },
                    config={"callbacks": []},
                )
        else:
            raw_enhancements = caller(clean_key, clean_model, clean_events, diagnoses)
        enhancements = _clean_model_enhancements(
            raw_enhancements, len(diagnoses)
        )
        for index, diagnosis in enumerate(diagnoses):
            diagnosis["model_enhancement"] = enhancements[index]
        return {
            "execution_mode": "siliconflow_enhanced",
            "model_status": "success",
            "model_message": "硅基流动模型增强完成",
            "model_name": clean_model,
            "diagnoses": diagnoses,
        }
    except SiliconFlowCallError as error:
        message = error.public_message
    except Exception:
        message = "硅基流动模型调用失败，已使用本地规则"
    return {
        "execution_mode": "local_rules",
        "model_status": "failed",
        "model_message": message,
        "model_name": clean_model,
        "diagnoses": diagnoses,
    }


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
