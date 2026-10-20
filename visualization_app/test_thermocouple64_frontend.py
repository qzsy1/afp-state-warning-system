from __future__ import annotations

import sys
import subprocess
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from app import build_thermocouple64_live_payload  # noqa: E402
from thermocouple64 import (  # noqa: E402
    THERMOCOUPLE64_AGGREGATION_VERSION,
    THERMOCOUPLE64_DERIVED_CHANNELS,
)


class Thermocouple64FrontendContractsTests(unittest.TestCase):
    def _model_metadata(self) -> dict:
        return {
            "input_sensors": list(THERMOCOUPLE64_DERIVED_CHANNELS),
            "output_sensors": list(THERMOCOUPLE64_DERIVED_CHANNELS),
            "temperature_features": list(THERMOCOUPLE64_DERIVED_CHANNELS),
            "aggregation_version": THERMOCOUPLE64_AGGREGATION_VERSION,
            "imputation_scope": "causal_so_far",
            "temperature_feature_dimension": 8,
            "temperature_feature_scaling": {
                name: {"mean": 0.0, "scale": 1.0}
                for name in THERMOCOUPLE64_DERIVED_CHANNELS
            },
        }

    def _channels(self) -> list[dict]:
        channels = []
        for index in range(1, 65):
            channels.append(
                {
                    "id": index - 1,
                    "name": f"温度{index}",
                    "unit": "°C",
                    "x_observed": [-1.0, 0.0],
                    "actual": [float(index), float(index + 1)],
                    "actual_current": float(index + 1),
                }
            )
        channels.append(
            {
                "id": 64,
                "name": "压力",
                "unit": "N",
                "x_observed": [-1.0, 0.0],
                "actual": [1.0, 2.0],
            }
        )
        return channels

    def test_live_payload_separates_raw_derived_and_other_channels(self) -> None:
        payload = build_thermocouple64_live_payload(
            self._channels(),
            dataset_schema="new_collection_v13_0",
            model_input_sensors=list(THERMOCOUPLE64_DERIVED_CHANNELS),
            model_metadata=self._model_metadata(),
        )
        self.assertEqual(len(payload["raw_directory"]["channels"]), 64)
        self.assertEqual(len(payload["derived_directory"]["channels"]), 8)
        self.assertEqual(len(payload["raw_history"]), 2)
        self.assertEqual(len(payload["raw_history"][0]), 64)
        self.assertEqual(payload["derived_history"][0][0], 4.5)
        self.assertEqual(payload["derived_history"][1][7], 61.5)
        self.assertEqual([item["name"] for item in payload["other_channels"]], ["压力"])
        self.assertTrue(payload["model_support"])

    def test_model_support_requires_the_declared_aggregation_contract(self) -> None:
        payload = build_thermocouple64_live_payload(
            self._channels(),
            dataset_schema="new_collection_v13_0",
            model_input_sensors=list(THERMOCOUPLE64_DERIVED_CHANNELS),
        )
        self.assertFalse(payload["model_support"])

    def test_non_v13_mode_has_no_64_channel_panel_payload(self) -> None:
        self.assertIsNone(
            build_thermocouple64_live_payload(
                self._channels(), dataset_schema="new_collection_v12_0"
            )
        )

    def test_page_defines_eight_group_plots_four_normal_cards_and_shared_detail(self) -> None:
        html = (APP_DIR / "static" / "index.html").read_text(encoding="utf-8")
        script = (APP_DIR / "static" / "app.js").read_text(encoding="utf-8")
        styles = (APP_DIR / "static" / "styles.css").read_text(encoding="utf-8")
        for marker in ("sensorMonitorSplit", "sensorPanelResizer", "otherSensorsPanel",
                       "thermocouple64RawGroups", "thermocouple64AverageGroups", "sensorSharedDetail"):
            self.assertIn(f'id="{marker}"', html)
        self.assertEqual(html.count('id="thermocouple64GroupCanvas"'), 1)
        self.assertLess(html.index('id="sensorSharedDetail"'), html.index('id="thermocouple64GroupDetail"'))
        self.assertLess(html.index('id="thermocouple64GroupDetail"'), html.index('id="sensorMonitorSplit"'))
        self.assertIn('data-thermocouple-view="raw"', html)
        self.assertIn('data-thermocouple-view="average"', html)
        self.assertIn("createThermocouple64GroupCard", script)
        self.assertIn("thermocouple64-preview", script)
        self.assertIn("cache.rawSeries[group]", script)
        self.assertIn("[cache.derivedSeries[group]]", script)
        self.assertIn("8张组图 × 8条原始曲线", script)
        self.assertIn("8张组图 × 1条严格均值曲线", script)
        self.assertIn("THERMOCOUPLE64_HISTORY_LIMIT = 2048", script)
        self.assertIn("document.hidden", script)
        self.assertIn("thermocouple64VisibilityInitialized", script)
        self.assertIn("currentOtherSensorChannels()", script)
        self.assertIn("grid-template-columns: repeat(4, minmax(0,1fr))", styles)
        self.assertIn("height: 150px", styles)
        chart_rule = styles.split(".full-channel-chart, .sensor-card canvas", 1)[1].split("}", 1)[0]
        self.assertNotIn("44px", chart_rule)
        self.assertIn("grid-template-columns: repeat(8, minmax(0, 1fr))", styles)
        self.assertIn("SENSOR_PANEL_SPLIT_STORAGE_KEY", script)
        self.assertIn("const defaultRatio = 0.42", script)
        self.assertIn('handle.addEventListener("dblclick", resetRatio)', script)
        self.assertIn("/app.js?v=20261020-click-to-curve-5", html)
        self.assertIn("20261017-thermocouple64-v6", html)

    def test_group_drawing_has_eight_raw_lines_one_mean_and_preserves_gaps(self) -> None:
        script = (APP_DIR / "static" / "app.js").read_text(encoding="utf-8")
        functions = script[script.index("function thermocouple64DownsampleIndices"):
                           script.index("function thermocouple64UniformRange")]
        executable = r'''
const state={thermocouple64Cache:{timeAxis:[0,1,2,3,4]}};
const THERMOCOUPLE64_COLORS=Array(8).fill("blue");
let strokes=0,moves=0;
const ctx=new Proxy({}, {get(t,k){if(k==="stroke")return ()=>strokes++;if(k==="moveTo")return ()=>moves++;return ()=>{};},set(){return true;}});
function setupCanvas(){return {ctx,width:120,height:104};}
const canvas={isConnected:true,closest(){return null;},classList:{contains(){return true;}},dataset:{}};
''' + functions + r'''
const raw=Array.from({length:8},(_,i)=>[i+1,i+2,null,i+4,i+5]);
drawThermocouple64GroupDetail(canvas,raw,Array(8).fill(true));
if(strokes!==11||canvas.dataset.seriesCount!=="8"||moves!==19)throw Error("raw group or gaps incorrect");
strokes=0;
drawThermocouple64GroupDetail(canvas,[[4.5,5.5,null,7.5,8.5]],[true],{min:0,max:20});
if(strokes!==4||canvas.dataset.seriesCount!=="1"||canvas.dataset.rangeMax!=="20")throw Error("mean or uniform range incorrect");
strokes=0;
drawThermocouple64GroupDetail(canvas,raw,Array(8).fill(false));
if(strokes!==0||canvas.dataset.visibleSeriesCount!=="0")throw Error("legend visibility incorrect");
const sampled=thermocouple64DownsampleIndices([Array.from({length:6000},(_,i)=>i===3456?1e6:i)],100);
if(sampled.length>100||sampled[0]!==0||sampled.at(-1)!==5999||!sampled.includes(3456))throw Error("downsampling lost endpoints/extreme");
'''
        completed = subprocess.run(["node", "-e", executable], cwd=APP_DIR, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_view_switch_is_presentation_only_and_shows_the_active_mode(self) -> None:
        script = (APP_DIR / "static" / "app.js").read_text(encoding="utf-8")
        function = script[script.index("function initializeThermocouple64Controls()"):
                          script.index("initializeColumnResizers();", script.index("function initializeThermocouple64Controls()"))]
        executable = r'''
const state = {thermocouple64View: "raw", thermocouple64Cache: null, payload: null, acquisition: {selected: ["温度1"]}, saves: 0};
let drawCount = 0;
const nodes = Object.fromEntries([
  "thermocouple64RawView", "thermocouple64AverageGroups",
  "thermocouple64ViewStatus", "thermocouple64CurrentMode",
  "thermocouple64UniformScale", "thermocouple64AbnormalOnly", "thermocouple64OverviewViewport",
].map((id) => [id, {
  textContent: "", checked: false,
  classList: {toggle() {}}, addEventListener() {},
} ]));
let changeHandler;
const input = {value: "average", checked: true, addEventListener(_name, callback) { changeHandler = callback; }};
const document = {
  querySelectorAll(selector) { return selector.startsWith("input") ? [input] : []; },
  querySelector() { return {addEventListener() {}}; },
};
const window = {requestAnimationFrame(callback) { callback(); }};
const $ = (id) => nodes[id];
function scheduleThermocouple64Draw() { drawCount += 1; }
function updateThermocouple64ScrollHint() {}
''' + function + r'''
initializeThermocouple64Controls();
const before = JSON.stringify(state.acquisition);
changeHandler();
if (state.thermocouple64View !== "average") process.exit(1);
if (JSON.stringify(state.acquisition) !== before || state.saves !== 0) process.exit(2);
if (nodes.thermocouple64ViewStatus.textContent !== "当前模式：平均模式（8组均值）") process.exit(3);
if (nodes.thermocouple64CurrentMode.textContent !== "当前模式：平均模式（8组均值）") process.exit(4);
if (drawCount !== 1) process.exit(5);
'''
        completed = subprocess.run(
            ["node", "-e", executable], cwd=APP_DIR, capture_output=True, text=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_page_diagnostics_identify_required_data_route_and_tolerate_optional_403(self) -> None:
        script = (APP_DIR / "static" / "app.js").read_text(encoding="utf-8")
        live = script[script.index("async function loadRealtime()"):
                      script.index("async function loadSimulationTemplate()")]
        helper = script[script.index("async function loadHelperStatus("):
                        script.index("async function pairLocalHelper()")]
        mysql = script[script.index("async function loadMysqlDefaults()"):
                       script.index("const LAYER_EVIDENCE_VISIBILITY_KEY")]
        self.assertIn('state.accessRole === "guest" ? "/api/simulation/live" : "/api/live"', live)
        self.assertIn('页面数据请求 ${endpoint} 失败', live)
        self.assertIn('state.accessRole === "guest"', helper)
        self.assertIn("if (!response.ok) return", mysql)


if __name__ == "__main__":
    unittest.main()
