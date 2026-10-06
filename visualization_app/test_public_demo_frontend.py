from __future__ import annotations

from pathlib import Path
import subprocess
import unittest


APP_DIR = Path(__file__).resolve().parent
APP_JS = APP_DIR / "static" / "app.js"
PUBLIC_DEMO_JS = APP_DIR / "static" / "public_demo.js"


def run_node(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "-e", script],
        cwd=APP_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


class PublicDemoFrontendTests(unittest.TestCase):
    def test_public_demo_payload_is_complete_at_first_window_and_end(self):
        script = r'''
const fs = require("fs");
const path = require("path");
const demo = require("./static/public_demo.js");
const filename = fs.readdirSync("./static/demo").find((name) => name.startsWith("quick_240."));
const bundle = JSON.parse(fs.readFileSync(path.join("./static/demo", filename), "utf8"));
for (const index of [0, 23, 24, 239]) {
  const payload = demo.buildRealtimePayload(bundle, index, {
    selectedChannel: "temperature_1",
    history: 240,
    horizon: 24,
    threshold: 0.72,
    rho: 0.35,
  });
  if (payload.mode !== "public_precomputed_demo") process.exit(1);
  if (payload.progress.cursor !== index + 1) process.exit(2);
  if (payload.channels.length !== 17 || !payload.selected_channel) process.exit(3);
  if (payload.selected_channel.actual.length !== index + 1) process.exit(4);
  if (!payload.window || !payload.timeline || !payload.acquisition) process.exit(5);
  if (payload.acquisition.execution_host !== "browser_precomputed_demo") process.exit(6);
  if (payload.acquisition.sample_count !== index + 1) process.exit(7);
}
const finalPayload = demo.buildRealtimePayload(bundle, 239, {});
if (!finalPayload.progress.finished || finalPayload.acquisition.running) process.exit(8);
'''
        completed = run_node(script)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_public_demo_payload_has_stable_ranges_and_successful_logical_io(self):
        script = r'''
const fs = require("fs");
const path = require("path");
const demo = require("./static/public_demo.js");
const filename = fs.readdirSync("./static/demo").find((name) => name.startsWith("quick_240."));
const bundle = JSON.parse(fs.readFileSync(path.join("./static/demo", filename), "utf8"));
const first = demo.buildRealtimePayload(bundle, 0, {});
const later = demo.buildRealtimePayload(bundle, 180, {});
if (first.acquisition.interfaces.length !== 5) process.exit(1);
if (first.acquisition.sensors.length !== 17) process.exit(2);
if (!first.acquisition.interfaces.every((item) => item.ok && item.state === "precomputed_success")) process.exit(3);
if (!first.acquisition.sensors.every((item) => item.ok && item.state === "precomputed_success")) process.exit(4);
if (!first.diagnosis?.ok || first.diagnosis.state !== "precomputed_success") process.exit(5);
for (let index = 0; index < first.channels.length; index += 1) {
  const earlyRange = first.channels[index].display_range;
  const laterRange = later.channels[index].display_range;
  if (!earlyRange || !Number.isFinite(earlyRange.min) || !Number.isFinite(earlyRange.max)) process.exit(6);
  if (earlyRange.min >= earlyRange.max) process.exit(7);
  if (JSON.stringify(earlyRange) !== JSON.stringify(laterRange)) process.exit(8);
}
'''
        completed = run_node(script)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_public_start_and_stop_short_circuit_before_server_or_helper_paths(self):
        text = APP_JS.read_text(encoding="utf-8")
        start_fn = text[text.index("async function startAcquisition("):
                        text.index("async function waitForHelperFlushComplete(")]
        stop_fn = text[text.index("async function stopAcquisition("):
                       text.index("function buildSensorChecklist(")]
        self.assertIn("isPublicPrecomputedSimulationMode()", start_fn)
        self.assertIn("startPublicDemo()", start_fn)
        self.assertLess(start_fn.index("startPublicDemo()"), start_fn.index("postJson("))
        self.assertIn("isPublicPrecomputedSimulationMode()", stop_fn)
        self.assertIn("stopPublicDemo()", stop_fn)
        self.assertLess(stop_fn.index("stopPublicDemo()"), stop_fn.index("postJson("))
        stop_demo_fn = text[text.index("function stopPublicDemo("):
                            text.index("async function startAcquisition(")]
        self.assertIn("applyPublicDemoPayload(payload)", stop_demo_fn)
        self.assertNotIn("applyRealtimePayload(payload)", stop_demo_fn)

    def test_public_live_transport_is_disabled_after_bundle_is_ready(self):
        text = APP_JS.read_text(encoding="utf-8")
        for function_name, next_name in (
            ("function configureDataMode(", "function livePollIntervalMs("),
            ("function startLiveHttpFallback(", "function closeLiveWebSocket("),
            ("function openLiveWebSocket(", "function queryString("),
            ("async function loadRealtime(", "async function loadSimulationTemplate("),
        ):
            body = text[text.index(function_name):text.index(next_name, text.index(function_name))]
            self.assertIn("isPublicPrecomputedSimulationMode()", body, function_name)

    def test_public_demo_hides_external_side_effect_controls_but_keeps_io_status_visible(self):
        text = APP_JS.read_text(encoding="utf-8")
        self.assertIn("function setPublicDemoControlVisibility(publicDemo)", text)
        body = text[text.index("function setPublicDemoControlVisibility(publicDemo)"):
                    text.index("function updateSimulationSettings()")]
        for marker in (
            '$("browserLocalSavePanel")',
            'document.querySelector(".mysql-settings")',
            '$("acquisitionParameterPanel")',
            'controls.downloadSimulationPackage',
            'document.querySelectorAll(".prediction-setting")',
        ):
            self.assertIn(marker, body)
        self.assertNotIn('$("sensorSettingsSection")', body)
        self.assertNotIn('$("agentDiagnosisSection")', body)
        self.assertIn("renderPublicDemoSuccessState", text)
        update = text[text.index("function updateSimulationSettings()"):
                      text.index("function updateIntegrationSource()")]
        self.assertIn("setPublicDemoControlVisibility(publicDemo);", update)

    def test_public_demo_uses_stable_incremental_renderer(self):
        text = APP_JS.read_text(encoding="utf-8")
        render_demo = text[text.index("function renderPublicDemoIndex("):
                           text.index("function ensurePublicDemoPlayback(")]
        self.assertIn("applyPublicDemoPayload(payload)", render_demo)
        self.assertNotIn("applyRealtimePayload(payload)", render_demo)
        stable = text[text.index("function applyPublicDemoPayload("):
                      text.index("function renderPublicDemoIndex(")]
        self.assertIn("stablePublicDemo: true", stable)
        self.assertIn("previousPayload", stable)
        cards = text[text.index("function renderSensorCardsInto("):
                     text.index("const MODEL_LABELS", text.index("function renderSensorCardsInto("))]
        self.assertIn("reuse", cards)
        self.assertIn("dataset.channelId", cards)

    def test_simulation_discovery_cannot_overwrite_real_interface_snapshot(self):
        text = APP_JS.read_text(encoding="utf-8")
        discover = text[text.index("async function discoverInterfaces()"):
                        text.index("function rememberRealInterfaceSnapshot()")]
        simulation_guard = discover[:discover.index("if (usesLocalCaptureHelper())")]
        self.assertNotIn("rememberRealInterfaceSnapshot()", simulation_guard)
        self.assertIn("renderPublicDemoSuccessState", simulation_guard)

    def test_public_simulation_roles_route_to_browser_demo_only(self):
        text = APP_JS.read_text(encoding="utf-8")
        start = text.index("function simulationExecutionSelection(")
        end = text.index("function renderHelperStatus()", start)
        function = text[start:end]
        script = function + r'''
const readyHelper = {
  online: true,
  protocol_compatible: true,
  capabilities: {simulation_replay_v1: true},
};
const guest = simulationExecutionSelection("guest", "helper_local", readyHelper);
const authorized = simulationExecutionSelection("authorized", "helper_local", readyHelper);
const lan = simulationExecutionSelection("lan_operator", "helper_local", readyHelper);
const local = simulationExecutionSelection("local_admin", "helper_local", readyHelper);
if (guest.execution_host !== "browser_precomputed_demo" || guest.error) process.exit(1);
if (authorized.execution_host !== "browser_precomputed_demo" || authorized.error) process.exit(2);
if (lan.execution_host !== "helper_local" || lan.error) process.exit(3);
if (local.execution_host !== "server" || local.error) process.exit(4);
'''
        completed = run_node(script)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_playback_uses_elapsed_time_and_merges_to_latest_index(self):
        script = r'''
const {createPlaybackController} = require("./static/public_demo.js");
let now = 0;
let nextId = 1;
const timers = new Map();
const rendered = [];
const controller = createPlaybackController({
  now: () => now,
  setTimeoutFn: (callback) => { const id = nextId++; timers.set(id, callback); return id; },
  clearTimeoutFn: (id) => timers.delete(id),
  requestAnimationFrameFn: (callback) => { callback(now); return 0; },
  cancelAnimationFrameFn: () => {},
  render: (index) => rendered.push(index),
  renderIntervalMs: 250,
});
controller.load({points: 6000, sample_rate_hz: 10});
controller.start();
if (rendered.join(",") !== "0") process.exit(1);
const firstTimer = [...timers.values()][0];
now = 12345;
firstTimer();
if (rendered.at(-1) !== 123) process.exit(2);
const secondTimer = [...timers.values()][0];
now = 600000;
secondTimer();
if (rendered.at(-1) !== 5999) process.exit(3);
if (rendered.length !== 3) process.exit(4);
'''
        completed = run_node(script)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_stop_invalidates_late_callbacks_and_restart_has_one_clock(self):
        script = r'''
const {createPlaybackController} = require("./static/public_demo.js");
let now = 0;
let nextId = 1;
const timers = new Map();
const rendered = [];
const controller = createPlaybackController({
  now: () => now,
  setTimeoutFn: (callback) => { const id = nextId++; timers.set(id, callback); return id; },
  clearTimeoutFn: (id) => timers.delete(id),
  requestAnimationFrameFn: (callback) => { callback(now); return 0; },
  cancelAnimationFrameFn: () => {},
  render: (index) => rendered.push(index),
});
controller.load({points: 240, sample_rate_hz: 10});
controller.start();
const late = [...timers.values()][0];
controller.stop();
const frozen = controller.snapshot();
now = 5000;
late();
const afterLate = controller.snapshot();
if (afterLate.index !== frozen.index || rendered.length !== 1 || timers.size !== 0) process.exit(1);
controller.start();
if (controller.snapshot().index !== 0 || timers.size !== 1 || rendered.at(-1) !== 0) process.exit(2);
controller.stop();
controller.start();
if (timers.size !== 1 || controller.snapshot().index !== 0) process.exit(3);
'''
        completed = run_node(script)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_ready_start_and_stop_are_immediate_and_freeze_for_five_seconds(self):
        script = r'''
const {createPlaybackController} = require("./static/public_demo.js");
let now = 0;
let nextId = 1;
const timers = new Map();
const rendered = [];
const controller = createPlaybackController({
  now: () => now,
  setTimeoutFn: (callback) => { const id = nextId++; timers.set(id, callback); return id; },
  clearTimeoutFn: (id) => timers.delete(id),
  requestAnimationFrameFn: (callback) => { callback(now); return 0; },
  cancelAnimationFrameFn: () => {},
  render: (index) => rendered.push(index),
});
controller.load({points: 240, sample_rate_hz: 10});
const startAt = process.hrtime.bigint();
controller.start();
const startMs = Number(process.hrtime.bigint() - startAt) / 1e6;
if (startMs >= 200 || rendered.join(",") !== "0") process.exit(1);
now = 2400;
[...timers.values()][0]();
const stopAt = process.hrtime.bigint();
controller.stop();
const stopMs = Number(process.hrtime.bigint() - stopAt) / 1e6;
const frozen = controller.snapshot();
if (stopMs >= 100 || timers.size !== 0) process.exit(2);
now += 5000;
if (controller.snapshot().index !== frozen.index || rendered.at(-1) !== frozen.index) process.exit(3);
'''
        completed = run_node(script)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_1200ms_prepare_delay_does_not_affect_ready_playback_or_600s_clock(self):
        script = r'''
const {createPlaybackController} = require("./static/public_demo.js");
let now = 1200;
let nextId = 1;
let networkRequests = 0;
const timers = new Map();
const rendered = [];
// The 1200 ms preparation delay has already completed before load/start.
const controller = createPlaybackController({
  now: () => now,
  setTimeoutFn: (callback) => { const id = nextId++; timers.set(id, callback); return id; },
  clearTimeoutFn: (id) => timers.delete(id),
  requestAnimationFrameFn: (callback) => { callback(now); return 0; },
  cancelAnimationFrameFn: () => {},
  render: (index) => rendered.push(index),
});
controller.load({points: 6000, sample_rate_hz: 10});
controller.start();
if (controller.snapshot().index !== 0 || rendered.at(-1) !== 0) process.exit(1);
now += 600000;
[...timers.values()][0]();
const finalState = controller.snapshot();
const logicalSeconds = finalState.index / 10;
if (finalState.index !== 5999 || Math.abs(logicalSeconds - 599.9) > 0.5) process.exit(2);
if (networkRequests !== 0 || timers.size !== 0 || rendered.length !== 2) process.exit(3);
'''
        completed = run_node(script)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_background_pause_does_not_build_a_catch_up_queue(self):
        script = r'''
const {createPlaybackController} = require("./static/public_demo.js");
let now = 0;
let nextId = 1;
const timers = new Map();
const rendered = [];
const controller = createPlaybackController({
  now: () => now,
  setTimeoutFn: (callback) => { const id = nextId++; timers.set(id, callback); return id; },
  clearTimeoutFn: (id) => timers.delete(id),
  requestAnimationFrameFn: (callback) => { callback(now); return 0; },
  cancelAnimationFrameFn: () => {},
  render: (index) => rendered.push(index),
});
controller.load({points: 240, sample_rate_hz: 10});
controller.start();
now = 1000;
[...timers.values()][0]();
if (controller.snapshot().index !== 10) process.exit(1);
controller.setVisible(false);
if (timers.size !== 0) process.exit(2);
now = 11000;
controller.setVisible(true);
if (controller.snapshot().index !== 10 || timers.size !== 1) process.exit(3);
now = 12000;
[...timers.values()][0]();
if (controller.snapshot().index !== 20) process.exit(4);
'''
        completed = run_node(script)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_public_demo_script_is_loaded_before_application(self):
        html = (APP_DIR / "static" / "index.html").read_text(encoding="utf-8")
        public_demo = html.index("/public_demo.js")
        application = html.index("/app.js")
        self.assertLess(public_demo, application)
        self.assertTrue(PUBLIC_DEMO_JS.is_file())


if __name__ == "__main__":
    unittest.main()
