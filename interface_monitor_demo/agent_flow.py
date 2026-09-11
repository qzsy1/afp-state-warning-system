from __future__ import annotations

from copy import deepcopy
from typing import Any

from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool


class AgentGateError(ValueError):
    """Raised when the local demonstration gate is not satisfied."""


def _trace(context: dict[str, Any], step_id: str, title: str, detail: str) -> None:
    context["trace"].append(
        {"id": step_id, "title": title, "detail": detail, "status": "complete"}
    )


@tool
def get_interface_config(
    interface_id: str, catalog: list[dict[str, Any]]
) -> dict[str, Any]:
    """Return the sanitized configuration for one simulated AFP interface."""

    for interface in catalog:
        if interface.get("id") == interface_id:
            allowed = {
                "id",
                "sensor_name",
                "label",
                "role",
                "driver",
                "protocol",
                "endpoint",
                "baudrate",
                "channels",
                "processing",
            }
            return {key: deepcopy(value) for key, value in interface.items() if key in allowed}
    raise ValueError(f"诊断工具未找到接口：{interface_id}")


@tool
def inspect_latest_observation(event: dict[str, Any]) -> dict[str, Any]:
    """Extract whitelisted evidence from one normalized simulated fault event."""

    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    allowed = {
        "endpoint",
        "driver",
        "baudrate",
        "received_samples",
        "invalid_samples",
        "last_sample_age_ms",
        "missing_channels",
    }
    return {key: deepcopy(value) for key, value in evidence.items() if key in allowed}


_FAULT_RULES: dict[str, dict[str, Any]] = {
    "not_found": {
        "fault_type": "接口枚举异常",
        "causes": ["设备未连接或未上电", "驱动未正确加载", "接口标识与实际设备不一致"],
        "actions": ["检查供电与物理连接", "重新枚举本机接口", "核对设备驱动和接口标识"],
    },
    "open_failed": {
        "fault_type": "接口打开失败",
        "causes": ["接口被其他程序占用", "当前账号无访问权限", "连接参数与设备不匹配"],
        "actions": ["关闭可能占用接口的程序", "核对端点和通信参数", "重新打开接口并观察原始帧"],
    },
    "timeout": {
        "fault_type": "通信超时",
        "causes": ["设备未持续发送数据", "网络或串口链路中断", "采样周期超过监控阈值"],
        "actions": ["检查链路和设备发送状态", "确认采样周期配置", "观察连续原始帧而非仅检查接口存在"],
    },
    "stale": {
        "fault_type": "数据时效异常",
        "causes": ["采集线程停滞", "设备更新频率下降", "接口缓存仍保留旧值"],
        "actions": ["比较最近帧时间戳", "检查采集线程状态", "超过阈值后将旧值标为不可用"],
    },
    "invalid_data": {
        "fault_type": "数据解析异常",
        "causes": ["数据帧结构与解析器预期不一致", "通信参数不匹配", "设备返回非数值或不完整数据"],
        "actions": ["保存一帧原始数据用于协议核对", "核对通信参数和数据维度", "在解析通过前不把该值用于模型或控制"],
    },
    "partial_channels": {
        "fault_type": "通道缺失",
        "causes": ["部分测点未返回", "通道映射与设备输出不一致", "单个测点连接异常"],
        "actions": ["核对缺失通道清单", "检查接口到通道的唯一映射", "逐通道确认有效数值和更新时间"],
    },
}


@tool
def lookup_fault_rule(state: str, sensor_name: str) -> dict[str, Any]:
    """Match a normalized interface state to the local diagnostic rule set."""

    rule = deepcopy(_FAULT_RULES.get(state))
    if rule is None:
        raise ValueError(f"本地规则库不支持状态：{state}")
    if sensor_name == "M3232薄膜压力" and state == "invalid_data":
        rule["causes"] = [
            "M3232矩阵帧包装或行列尺寸与当前解析路径不一致",
            "串口端点或115200/8N1通信参数不匹配",
            "真实设备协议尚未通过原始帧完成最终核对",
        ]
        rule["actions"] = [
            "确认实际COM口以及115200/8N1参数",
            "捕获并保存一帧未经处理的真实串口数据",
            "核对矩阵尺寸、包装字段和有效像素规则后再接入模型",
        ]
    return rule


@tool
def compose_diagnostic_report(
    event: dict[str, Any],
    interface: dict[str, Any],
    observation: dict[str, Any],
    rule: dict[str, Any],
) -> dict[str, Any]:
    """Compose a structured local diagnostic report from tool evidence."""

    evidence_count = sum(
        value not in (None, "", [], 0)
        for value in observation.values()
    )
    confidence_label = "规则高度匹配" if evidence_count >= 2 else "规则初步匹配"
    return {
        "interface_id": event["interface_id"],
        "interface_label": interface["label"],
        "sensor_name": event["sensor_name"],
        "channels": list(event["channels"]),
        "fault_type": rule["fault_type"],
        "summary": f"{event['sensor_name']}：{event['summary']}",
        "evidence": observation,
        "possible_causes": list(rule["causes"]),
        "recommended_actions": list(rule["actions"]),
        "confidence_label": confidence_label,
        "evidence_boundary": "本地规则与模拟数据结果，不代表真实硬件故障概率",
        "simulated": True,
    }


def _receive_event(context: dict[str, Any]) -> dict[str, Any]:
    event = context["event"]
    _trace(
        context,
        "event_received",
        "接收异常事件",
        f"收到 {event['sensor_name']} 的 {event['state']} 标准事件",
    )
    return context


def _check_gate(context: dict[str, Any]) -> dict[str, Any]:
    _trace(
        context,
        "gate_checked",
        "检查 Agent 启用条件",
        f"API Key 已填写；模型显示名为 {context['model_name']}；仅执行本地模拟",
    )
    return context


def _call_config_tool(context: dict[str, Any]) -> dict[str, Any]:
    context["interface"] = get_interface_config.invoke(
        {"interface_id": context["event"]["interface_id"], "catalog": context["catalog"]}
    )
    _trace(
        context,
        "get_interface_config",
        "工具 1 · 查询接口配置",
        f"定位到 {context['interface']['label']} / {context['interface']['endpoint']}",
    )
    return context


def _call_observation_tool(context: dict[str, Any]) -> dict[str, Any]:
    context["observation"] = inspect_latest_observation.invoke(
        {"event": context["event"]}
    )
    _trace(
        context,
        "inspect_latest_observation",
        "工具 2 · 检查最近观测",
        "提取采样计数、时延、缺失通道和解析状态",
    )
    return context


def _call_rule_tool(context: dict[str, Any]) -> dict[str, Any]:
    context["rule"] = lookup_fault_rule.invoke(
        {
            "state": context["event"]["state"],
            "sensor_name": context["event"]["sensor_name"],
        }
    )
    _trace(
        context,
        "lookup_fault_rule",
        "工具 3 · 匹配本地规则",
        f"匹配为 {context['rule']['fault_type']}，未访问外部知识服务",
    )
    return context


def _call_report_tool(context: dict[str, Any]) -> dict[str, Any]:
    context["diagnosis"] = compose_diagnostic_report.invoke(
        {
            "event": context["event"],
            "interface": context["interface"],
            "observation": context["observation"],
            "rule": context["rule"],
        }
    )
    _trace(
        context,
        "compose_diagnostic_report",
        "工具 4 · 形成诊断报告",
        "将问题接口、通道、证据、可能原因和建议组合为结构化结果",
    )
    return context


def _complete(context: dict[str, Any]) -> dict[str, Any]:
    _trace(
        context,
        "complete",
        "诊断流程完成",
        "结果已返回前端；设备配置和控制参数均未修改",
    )
    return {
        "model_name": context["model_name"],
        "execution_mode": "langchain_local_runnable_simulation",
        "trace": context["trace"],
        "diagnosis": context["diagnosis"],
        "simulated": True,
    }


_LOCAL_DIAGNOSIS_CHAIN = (
    RunnableLambda(_receive_event)
    | RunnableLambda(_check_gate)
    | RunnableLambda(_call_config_tool)
    | RunnableLambda(_call_observation_tool)
    | RunnableLambda(_call_rule_tool)
    | RunnableLambda(_call_report_tool)
    | RunnableLambda(_complete)
)


def run_diagnosis(
    event: dict[str, object] | None,
    catalog: list[dict[str, object]],
    *,
    api_key_present: bool,
    model_name: str,
) -> dict[str, object]:
    """Run the local visual diagnostic flow after checking browser-side gates."""

    if not api_key_present:
        raise AgentGateError("请先填写 API Key；其原文不会发送到后端")
    if not model_name.strip():
        raise AgentGateError("请先填写模型名称")
    if event is None or event.get("state") == "healthy":
        raise AgentGateError("当前没有可诊断的异常事件")
    context: dict[str, Any] = {
        "event": deepcopy(event),
        "catalog": deepcopy(catalog),
        "model_name": model_name.strip(),
        "trace": [],
    }
    return _LOCAL_DIAGNOSIS_CHAIN.invoke(context)
