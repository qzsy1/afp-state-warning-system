from __future__ import annotations

import threading
import unittest
import urllib.request
from html.parser import HTMLParser

from interface_monitor_demo.server import create_server


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.text: list[str] = []
        self.password_inputs = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(str(values["id"]))
        if tag == "input" and values.get("type") == "password":
            self.password_inputs += 1

    def handle_data(self, data: str) -> None:
        normalized = data.strip()
        if normalized:
            self.text.append(normalized)


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server(port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    @classmethod
    def fetch(cls, path: str) -> tuple[str, str]:
        with urllib.request.urlopen(cls.base_url + path, timeout=2) as response:
            return response.headers.get_content_type(), response.read().decode("utf-8")

    def test_page_exposes_visual_dashboard_and_five_flow_nodes(self) -> None:
        content_type, html = self.fetch("/")
        parser = _PageParser()
        parser.feed(html)
        required_ids = {
            "simulationBanner",
            "interfaceGrid",
            "apiKeyInput",
            "modelNameInput",
            "agentEnabledState",
            "diagnoseButton",
            "scenarioInterface",
            "scenarioType",
            "injectScenarioButton",
            "resetButton",
            "thoughtFlow",
            "toolTrace",
            "diagnosisPanel",
            "flow-monitor",
            "flow-event",
            "flow-gate",
            "flow-langchain",
            "flow-report",
        }

        self.assertEqual(content_type, "text/html")
        self.assertEqual(required_ids - parser.ids, set())
        self.assertEqual(parser.password_inputs, 1)

    def test_page_explains_separate_pressure_channels_and_local_simulation(self) -> None:
        _, html = self.fetch("/")
        parser = _PageParser()
        parser.feed(html)
        text = " ".join(parser.text)

        self.assertIn("压力", text)
        self.assertIn("薄膜压力", text)
        self.assertIn("LangChain 诊断思路", text)
        self.assertIn("不会访问外部模型", text)

    def test_javascript_uses_presence_flag_without_key_payload(self) -> None:
        content_type, javascript = self.fetch("/app.js")

        self.assertIn(content_type, {"application/javascript", "text/javascript"})
        self.assertIn("api_key_present", javascript)
        self.assertNotIn("api_key: apiKeyInput.value", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("sessionStorage", javascript)

    def test_stylesheet_defines_flow_and_responsive_layout(self) -> None:
        content_type, stylesheet = self.fetch("/styles.css")

        self.assertEqual(content_type, "text/css")
        self.assertIn(".thought-flow", stylesheet)
        self.assertIn(".flow-node.active", stylesheet)
        self.assertIn("@media", stylesheet)


if __name__ == "__main__":
    unittest.main()
