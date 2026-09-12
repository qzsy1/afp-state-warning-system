from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import interface_agent
from interface_agent import (
    AgentGateError,
    build_agent_event,
    run_interface_diagnosis,
    validate_agent_payload,
)


ROOT = Path(__file__).resolve().parent


def m3232_result() -> dict:
    return {
        "simulated": False,
        "interfaces": [
            {
                "id": "m3232_pressure",
                "role": "pressure",
                "driver": "m3232_pressure",
                "endpoint": "COM8",
                "expected_channels": ["薄膜压力"],
                "detected_channels": [],
                "missing_channels": ["薄膜压力"],
                "invalid_channels": ["薄膜压力"],
                "sample_counts": {},
                "invalid_sample_counts": {"薄膜压力": 3},
                "state": "invalid_data",
                "message": "收到非数值数据：薄膜压力",
                "ok": False,
            }
        ],
        "sensors": [
            {
                "name": "薄膜压力",
                "selected": True,
                "received_samples": 0,
                "invalid_samples": 3,
                "state": "invalid_data",
                "message": "收到非数值数据",
                "ok": False,
            }
        ],
    }


def mixed_interface_result() -> dict:
    result = m3232_result()
    result["interfaces"].insert(0, {
        "id": "plc_process",
        "role": "process",
        "driver": "modbus_tcp",
        "endpoint": "192.168.1.10:502",
        "expected_channels": ["压力"],
        "detected_channels": [],
        "missing_channels": ["压力"],
        "invalid_channels": [],
        "state": "no_data",
        "message": "PLC 未返回数据",
        "ok": False,
    })
    result["sensors"].append({
        "name": "压力", "selected": True, "received_samples": 0,
        "invalid_samples": 0, "state": "no_data", "message": "没有采集数据", "ok": False,
    })
    return result


class InterfaceAgentTests(unittest.TestCase):
    def test_interface_ui_exposes_physical_binding_and_shared_nic_rules(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("实际物理接口", script)
        self.assertIn("physical_interface_id", script)
        self.assertIn("physical_interfaces", script)
        self.assertIn("仅允许PLC与ABB共享同一网卡", script)
        self.assertIn("interface-physical", script)
        self.assertIn("未发现匹配的实际接口", script)
        self.assertIn("interface_kind", script)
        self.assertIn("autoAssignPhysicalInterfaces", script)
        self.assertIn("传感器设置与接口映射", html)

    def test_model_response_parser_accepts_fenced_json(self) -> None:
        parse = getattr(interface_agent, "parse_model_diagnoses")
        response = {
            "choices": [
                {
                    "message": {
                        "content": "```json\n{\"diagnoses\":[{\"event_index\":0,\"analysis\":\"链路异常\"}]}\n```"
                    }
                }
            ]
        }

        self.assertEqual(parse(response), [{"event_index": 0, "analysis": "链路异常"}])

    def test_model_response_parser_accepts_structured_content_blocks(self) -> None:
        parse = getattr(interface_agent, "parse_model_diagnoses")
        response = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "前置说明"},
                            {"type": "text", "text": "{\"diagnoses\":[{\"event_index\":1,\"analysis\":\"无数据\"}]}"},
                        ]
                    }
                }
            ]
        }

        self.assertEqual(parse(response), [{"event_index": 1, "analysis": "无数据"}])

    def test_model_response_parser_accepts_direct_object_content(self) -> None:
        parse = getattr(interface_agent, "parse_model_diagnoses")
        response = {
            "choices": [
                {"message": {"content": {"diagnoses": [{"event_index": 2, "analysis": "数据异常"}]}}}
            ]
        }

        self.assertEqual(parse(response), [{"event_index": 2, "analysis": "数据异常"}])

    def test_agent_defaults_use_deepseek_v3_without_exposing_key(self) -> None:
        with patch.dict(os.environ, {"AFP_SILICONFLOW_API_KEY": "sk-test-only"}, clear=False):
            defaults = interface_agent.get_agent_defaults()

        self.assertEqual(defaults["model_name"], "deepseek-ai/DeepSeek-V3")
        self.assertTrue(defaults["default_key_available"])
        self.assertNotIn("api_key", defaults)

    def test_empty_request_uses_local_environment_key(self) -> None:
        events = interface_agent.build_agent_events(mixed_interface_result())
        captured: list[tuple[str, str]] = []

        def successful_model(api_key, model_name, _events, local_diagnoses):
            captured.append((api_key, model_name))
            return [
                {"event_index": index, "analysis": "环境变量密钥调用成功"}
                for index in range(len(local_diagnoses))
            ]

        with patch.dict(
            os.environ,
            {"AFP_SILICONFLOW_API_KEY": "sk-test-only", "AFP_SILICONFLOW_MODEL": "deepseek-ai/DeepSeek-V3"},
            clear=False,
        ):
            result = interface_agent.run_interface_diagnoses(
                events, api_key="", model_name="", model_caller=successful_model
            )

        self.assertEqual(result["model_status"], "success")
        self.assertEqual(captured, [("sk-test-only", "deepseek-ai/DeepSeek-V3")])

    def test_model_alias_fields_are_normalized_into_enhancement(self) -> None:
        result = interface_agent.run_interface_diagnoses(
            interface_agent.build_agent_events(mixed_interface_result()),
            api_key="sk-test-only",
            model_name="deepseek-ai/DeepSeek-V4-Flash",
            model_caller=lambda *_args: [
                {"event_index": 0, "summary": "PLC 链路分析", "causes": ["网络不通"], "actions": ["检查网线"]},
                {"event_index": 1, "diagnosis": "M3232 帧分析", "causes": ["帧格式"], "actions": ["抓取原始帧"]},
            ],
        )

        self.assertEqual(result["model_status"], "success")
        self.assertEqual(result["diagnoses"][0]["model_enhancement"]["analysis"], "PLC 链路分析")
        self.assertEqual(result["diagnoses"][1]["model_enhancement"]["recommended_actions"], ["抓取原始帧"])

    def test_siliconflow_retries_without_json_mode_when_provider_rejects_it(self) -> None:
        events = interface_agent.build_agent_events(mixed_interface_result())
        local_diagnoses = [
            {"interface_label": "PLC", "sensor_name": "压力", "channels": ["压力"], "error_message": "无数据", "evidence": {}, "fault_type": "接口未收到有效数据", "possible_causes": [], "recommended_actions": []},
            {"interface_label": "M3232", "sensor_name": "薄膜压力", "channels": ["薄膜压力"], "error_message": "非数值", "evidence": {}, "fault_type": "采集数据解析异常", "possible_causes": [], "recommended_actions": []},
        ]
        bodies: list[dict] = []

        class FakeResponse:
            def __init__(self, body: dict):
                self.body = json.dumps(body, ensure_ascii=False).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.body

        success = {
            "choices": [{"message": {"content": "{\"diagnoses\":[{\"event_index\":0,\"analysis\":\"a\"},{\"event_index\":1,\"analysis\":\"b\"}]}"}}]
        }

        def fake_urlopen(request, timeout):
            body = json.loads(request.data.decode("utf-8"))
            bodies.append(body)
            if len(bodies) == 1:
                raise HTTPError(request.full_url, 400, "response_format unsupported", {}, None)
            return FakeResponse(success)

        with patch.object(interface_agent, "urlopen", side_effect=fake_urlopen):
            result = interface_agent.call_siliconflow_model(
                "sk-test-only", "deepseek-ai/DeepSeek-V4-Flash", events, local_diagnoses
            )

        self.assertEqual(len(result), 2)
        self.assertIn("response_format", bodies[0])
        self.assertNotIn("response_format", bodies[1])

    def test_build_events_keeps_every_abnormal_interface(self) -> None:
        build_all = getattr(interface_agent, "build_agent_events", lambda _result: [])

        events = build_all(mixed_interface_result())

        self.assertEqual(len(events), 2)
        self.assertEqual(
            {(item["interface_id"], item["sensor_name"]) for item in events},
            {("plc_process", "压力"), ("m3232_pressure", "薄膜压力")},
        )

    def test_missing_model_configuration_returns_all_local_diagnoses(self) -> None:
        run_all = getattr(
            interface_agent,
            "run_interface_diagnoses",
            lambda *_args, **_kwargs: {
                "execution_mode": "missing",
                "model_status": "missing",
                "diagnoses": [],
            },
        )

        result = run_all(
            interface_agent.build_agent_events(mixed_interface_result()),
            api_key="",
            model_name="",
        )

        self.assertEqual(result["execution_mode"], "local_rules")
        self.assertEqual(result["model_status"], "not_configured")
        self.assertEqual(len(result["diagnoses"]), 2)
        self.assertTrue(all("model_enhancement" not in item for item in result["diagnoses"]))

    def test_valid_model_configuration_adds_enhancement_to_every_diagnosis(self) -> None:
        def successful_model(_api_key, _model_name, _events, _local_diagnoses):
            return [
                {
                    "event_index": 0,
                    "analysis": "PLC 通信链路需要进一步核对",
                    "possible_causes": ["模型补充原因 A"],
                    "recommended_actions": ["模型补充建议 A"],
                },
                {
                    "event_index": 1,
                    "analysis": "M3232 原始帧需要进一步核对",
                    "possible_causes": ["模型补充原因 B"],
                    "recommended_actions": ["模型补充建议 B"],
                },
            ]

        result = interface_agent.run_interface_diagnoses(
            interface_agent.build_agent_events(mixed_interface_result()),
            api_key="sk-test-only",
            model_name="deepseek-ai/DeepSeek-V4-Flash",
            model_caller=successful_model,
        )

        self.assertEqual(result["execution_mode"], "siliconflow_enhanced")
        self.assertEqual(result["model_status"], "success")
        self.assertEqual(
            [item["model_enhancement"]["analysis"] for item in result["diagnoses"]],
            ["PLC 通信链路需要进一步核对", "M3232 原始帧需要进一步核对"],
        )

    def test_invalid_model_configuration_falls_back_without_leaking_key(self) -> None:
        def failed_model(_api_key, _model_name, _events, _local_diagnoses):
            raise RuntimeError("401 invalid sk-test-only")

        result = interface_agent.run_interface_diagnoses(
            interface_agent.build_agent_events(mixed_interface_result()),
            api_key="sk-test-only",
            model_name="missing/model",
            model_caller=failed_model,
        )

        self.assertEqual(result["execution_mode"], "local_rules")
        self.assertEqual(result["model_status"], "failed")
        self.assertEqual(len(result["diagnoses"]), 2)
        self.assertNotIn("sk-test-only", json.dumps(result, ensure_ascii=False))

    def test_build_event_keeps_m3232_thin_film_pressure_identity(self) -> None:
        event = build_agent_event(m3232_result())

        self.assertIsNotNone(event)
        self.assertEqual(event["interface_id"], "m3232_pressure")
        self.assertEqual(event["endpoint"], "COM8")
        self.assertEqual(event["sensor_name"], "薄膜压力")
        self.assertEqual(event["channels"], ["薄膜压力"])
        self.assertEqual(event["state"], "invalid_data")

    def test_build_event_matches_sensor_to_its_interface(self) -> None:
        event = build_agent_event(mixed_interface_result())

        self.assertEqual(event["interface_id"], "m3232_pressure")
        self.assertEqual(event["endpoint"], "COM8")
        self.assertEqual(event["sensor_name"], "薄膜压力")

    def test_run_diagnosis_returns_diagnosis_without_call_trace(self) -> None:
        result = run_interface_diagnosis(
            build_agent_event(m3232_result()),
            api_key_present=True,
            model_name="local-demo-model",
        )

        self.assertNotIn("trace", result)
        self.assertEqual(result["diagnosis"]["interface_id"], "m3232_pressure")
        self.assertEqual(result["diagnosis"]["sensor_name"], "薄膜压力")
        self.assertNotIn("PLC压力", result["diagnosis"]["summary"])
        self.assertEqual(result["execution_mode"], "langchain_local_runnable_simulation")

    def test_gate_blocks_when_key_or_model_is_missing(self) -> None:
        event = build_agent_event(m3232_result())
        with self.assertRaisesRegex(AgentGateError, "API Key"):
            run_interface_diagnosis(event, api_key_present=False, model_name="local-demo-model")
        with self.assertRaisesRegex(AgentGateError, "模型名称"):
            run_interface_diagnosis(event, api_key_present=True, model_name=" ")

    def test_result_never_contains_secret_material(self) -> None:
        result = run_interface_diagnosis(
            build_agent_event(m3232_result()),
            api_key_present=True,
            model_name="local-demo-model",
        )

        serialized = json.dumps(result, ensure_ascii=False).lower()
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("authorization", serialized)

    def test_global_langsmith_tracing_is_disabled_for_local_flow(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "LANGSMITH_TRACING": "true",
                    "LANGCHAIN_TRACING_V2": "true",
                    "LANGSMITH_API_KEY": "must-not-be-used",
                },
            ),
            patch(
                "langchain_core.tracers.langchain.LangChainTracer",
                side_effect=AssertionError("external tracer must not be created"),
            ) as tracer,
        ):
            run_interface_diagnosis(
                build_agent_event(m3232_result()),
                api_key_present=True,
                model_name="local-demo-model",
            )
        tracer.assert_not_called()

    def test_original_frontend_contains_agent_additions(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        for element_id in (
            "agentApiKeyInput",
            "agentModelNameInput",
            "agentGateStatus",
            "agentDiagnoseButton",
            "hardwareCheckStatus",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertNotIn('id="agentDiagnosisPanel"', html)
        self.assertIn("/api/agent/diagnose", script)
        self.assertIn("api_key: agentApiKeyInput.value.trim()", script)
        self.assertNotIn("api_key_present", script)
        self.assertIn("buildAgentEvents", script)
        self.assertIn("result.diagnoses", script)
        self.assertIn("薄膜压力", html)
        self.assertIn("LangChain", html)
        self.assertIn("硅基流动", html)
        self.assertIn("本地规则", html)
        self.assertIn("deepseek-ai/DeepSeek-V3", html)
        self.assertIn("/api/agent/defaults", script)
        self.assertNotIn("工具调用轨迹", html)
        self.assertNotIn("agent-trace", script)
        self.assertIn("/api/agent/diagnose", app_source)
        self.assertIn("/api/agent/defaults", app_source)
        self.assertIn("run_interface_diagnoses", app_source)
        self.assertIn("langchain-core==1.6.2", requirements)

    def test_original_frontend_groups_functional_modules_as_collapsible_sections(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

        for module_class in (
            "chart-panel",
            "channel-panel",
            "collapsible-rail-block",
            "layer-panel",
            "timeline-panel",
            "sensor-settings-section",
            "agent-diagnosis-section",
            "acquisition-parameter-panel",
        ):
            self.assertIn(f"{module_class}", html)
        self.assertGreaterEqual(html.count('class="control-section'), 5)
        self.assertIn("collapsible-panel-summary", html)
        self.assertIn("collapsible-panel-content", html)
        self.assertNotIn("collapsible-status-card", html)
        self.assertIn('<section class="panel channel-panel">', html)
        self.assertNotIn('class="panel channel-panel collapsible-panel"', html)
        self.assertIn("grid-template-rows: auto minmax(0, 1fr)", styles)
        self.assertRegex(styles, r"\.channel-panel\s*\{[^}]*height:\s*100%")
        self.assertGreaterEqual(html.count('class="status-card'), 4)
        self.assertIn("sensorCardsViewport", html)
        self.assertNotIn("sensorCardsResizer", html)
        self.assertIn("verticalPanelResizer", html)
        self.assertNotIn("系统功能与判定边界说明", html)
        self.assertIn("grid-template-rows: minmax(300px, var(--upper-row-height)) 4px", styles)
        self.assertIn("row-gap: 0", styles)
        self.assertIn(".evidence-dock { margin-top: -5px", styles)
        self.assertIn("grid-template-columns: subgrid", styles)
        self.assertIn("#layerEvidencePanel { grid-column: 1;", styles)
        self.assertIn("#timelineEvidencePanel { grid-column: 3;", styles)
        self.assertIn("border-bottom-left-radius: 0", styles)
        self.assertIn("border-top-left-radius: 0", styles)
        self.assertRegex(styles, r"\.main-content\s*\{[^}]*padding-right:\s*0")
        self.assertRegex(styles, r"\.channel-panel\s*\{[^}]*display:\s*grid[^}]*grid-template-rows:\s*auto minmax\(0,\s*1fr\)")
        self.assertRegex(styles, r"\.channel-panel-content\s*\{[^}]*height:\s*100%")
        self.assertRegex(styles, r"\.sensor-cards-viewport\s*\{[^}]*height:\s*100%[^}]*max-height:\s*none")
        self.assertNotIn("SENSOR_CARDS_HEIGHT_STORAGE_KEY", script)
        self.assertIn("vertical-panel-resizer", styles)
        self.assertIn('.collapsible-subsection:not([open]) > summary::after', styles)
        self.assertIn('content: "+"', styles)

    def test_hardware_check_does_not_repeat_or_replace_agent_result(self) -> None:
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("scheduleAutomaticHardwareCheck(800)", script)
        self.assertNotIn("autoCheckInterval", script)
        self.assertNotIn("scheduleAutomaticHardwareCheck(0), 30000", script)
        self.assertNotIn("updateAgentFromHardwareResult(status, {automatic: true});", script)
        self.assertNotIn("updateAgentFromHardwareResult(result, {automatic: true});", script)

    def test_app_payload_accepts_key_and_all_events_for_local_backend_only(self) -> None:
        events = interface_agent.build_agent_events(mixed_interface_result())
        payload = {
            "api_key": "sk-test-only",
            "model_name": "deepseek-ai/DeepSeek-V4-Flash",
            "events": events,
        }
        try:
            validated = validate_agent_payload(payload)
        except ValueError:
            validated = {"api_key": "", "model_name": "", "events": []}
        self.assertEqual(validated["api_key"], "sk-test-only")
        self.assertEqual(validated["model_name"], "deepseek-ai/DeepSeek-V4-Flash")
        self.assertEqual(len(validated["events"]), 2)

    def test_app_payload_rejects_unknown_fields(self) -> None:
        payload = {
            "api_key": "sk-test-only",
            "model_name": "deepseek-ai/DeepSeek-V4-Flash",
            "events": interface_agent.build_agent_events(mixed_interface_result()),
            "unexpected": "value",
        }
        with self.assertRaisesRegex(ValueError, "未允许字段"):
            validate_agent_payload(payload)


if __name__ == "__main__":
    unittest.main()
