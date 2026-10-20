from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
import subprocess
from unittest.mock import patch

import acquisition as a
from real_acquisition import ReadinessSnapshot


def config(root):
    return a.AcquisitionConfig(acquisition_mode="real", real_acquisition_mode="local_direct",
        processing_mode="capture_only", selected_sensors=["温度", "压力"], save_root=str(root),
        interfaces=[{"id": "one", "role": "custom", "driver": "serial_json", "endpoint": "COM91",
                     "physical_interface_id": "serial:COM91", "physical_verified": True},
                    {"id": "two", "role": "custom", "driver": "serial_json", "endpoint": "COM92",
                     "physical_interface_id": "serial:COM92", "physical_verified": True}],
        interface_channel_assignments={"one": ["温度"], "two": ["压力"]})


class StartLifecycleTests(unittest.TestCase):
    def test_cancelled_usb_start_invalidates_only_its_detection_evidence(self):
        from esr1001_usb import USB_DRIVER
        from real_acquisition import readiness_config_fingerprint

        entered = threading.Event()
        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            cfg.interfaces[0]["driver"] = USB_DRIVER
            manager._readiness_snapshot = ReadinessSnapshot.create(cfg, {"ok": True})
            manager._session_readiness_fingerprint = readiness_config_fingerprint(cfg)
            manager._interface_readiness_cache = {"one": {"ok": True}, "two": {"ok": True}}

            def cancelled_open(current):
                manager.config = current
                entered.set()
                manager._start_cancel.wait(2)
                raise RuntimeError("capture_start_cancelled")

            with patch.object(manager, "start", side_effect=cancelled_open):
                manager.start_async(cfg)
                self.assertTrue(entered.wait(1))
                manager.cancel_start()
                manager._start_thread.join(2)
            self.assertFalse(manager._start_thread.is_alive())
            self.assertEqual(manager.start_operation_status()["state"], "cancelled")
            self.assertEqual(manager.readiness_invalid_reason, "usb_start_transaction_cancelled")
            self.assertFalse(manager.session_readiness_matches(cfg))
            self.assertNotIn("one", manager._interface_readiness_cache)
            self.assertIn("two", manager._interface_readiness_cache)

    def test_stream_version_is_constant_within_one_status_tick_and_advances_on_changes(self):
        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            with patch("acquisition_start.time.monotonic", return_value=100.25):
                initial = manager.stream_version()
                self.assertEqual(manager.stream_version(), initial)

                manager.capture_phase = "starting"
                starting = manager.stream_version()
                self.assertNotEqual(starting, initial)

                manager.total_sample_count = 1
                sampled = manager.stream_version()
                self.assertNotEqual(sampled, starting)

                manager._curve_start_saved = True
                ready = manager.stream_version()
                self.assertNotEqual(ready, sampled)

                manager.record_start_event("source_observation", channel="温度")
                event_version = manager.stream_version()
                self.assertNotEqual(event_version, ready)

            with patch("acquisition_start.time.monotonic", return_value=100.50):
                active_tick = manager.stream_version()
                self.assertGreater(active_tick, event_version)

            manager.capture_phase = "completed"
            with patch("acquisition_start.time.monotonic", return_value=200.10):
                idle_tick = manager.stream_version()
            with patch("acquisition_start.time.monotonic", return_value=200.90):
                self.assertEqual(manager.stream_version(), idle_tick)

    def test_failed_start_before_config_install_preserves_request_identity(self):
        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            cfg.start_request_id = "early-failure"
            with patch.object(manager, "start", side_effect=RuntimeError("readiness_required")):
                accepted = manager.start_async(cfg)
                manager._start_thread.join(2)
            status = manager.status()
            self.assertEqual(status["capture_phase"], "start_failed")
            for key in ("start_request_id", "capture_generation"):
                self.assertEqual(status[key], accepted[key])
                self.assertEqual(status["capture_timeline"][key], accepted[key])

    def test_failed_async_start_preserves_persistence_blocker(self):
        for phase in ("database_pending_retry", "finalization_failed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                manager = a.AcquisitionManager(Path(root), recovery_scan=False)
                manager.capture_phase = phase
                manager.finalization_complete = False
                with patch.object(manager, "start", side_effect=RuntimeError("previous_capture_" + phase)):
                    for _ in range(2):
                        manager.start_async(config(root))
                        manager._start_thread.join(2)
                        self.assertFalse(manager._start_thread.is_alive())
                        self.assertEqual(manager.start_operation_status()["state"], "failed")
                        self.assertEqual(manager.capture_phase, phase)
                        self.assertFalse(manager.finalization_complete)

    def test_stop_waits_for_slow_usb_release_within_supplied_budget(self):
        from esr1001_usb import Esr1001UsbDriver
        from local_direct_acquisition import LocalDirectAdapter
        from real_acquisition import ChannelSampleCache

        class Driver(Esr1001UsbDriver):
            def close(self):
                time.sleep(.65)

        driver = Driver("COM91")
        worker = LocalDirectAdapter("usb", driver, ["CH1_ADC_signed"], ChannelSampleCache())
        multi = a.MultiInterfaceDriver([], ["CH1_ADC_signed"])
        multi.workers = [worker]
        t = time.monotonic()
        multi.close(timeout_seconds=1)
        self.assertGreaterEqual(time.monotonic()-t, .6)
        self.assertLess(time.monotonic()-t, 1.2)
        self.assertEqual(multi.workers, [])

    def test_status_and_cancel_during_slow_open(self):
        entered, release = threading.Event(), threading.Event()

        class Driver:
            def open(self):
                entered.set()
                release.wait(3)

            def cancel_open(self):
                release.set()

            def close(self):
                release.set()

            def read_sample(self):
                return {"温度": 20, "压力": 30}

        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            with patch("acquisition.build_driver", return_value=Driver()):
                try:
                    accepted = manager.start_async(config(root))
                    self.assertTrue(accepted["accepted"])
                    self.assertTrue(entered.wait(2))
                    t = time.monotonic()
                    self.assertEqual(manager.status()["start_operation"]["state"], "starting")
                    self.assertLess(time.monotonic()-t, .1)
                    with self.assertRaisesRegex(RuntimeError, "capture_start_in_progress"):
                        manager.start_async(config(root))
                    self.assertEqual(manager.stop()["capture_phase"], "cancelling_start")
                    manager._start_thread.join(3)
                    self.assertFalse(manager._start_thread.is_alive())
                    status = manager.status()
                    self.assertEqual(status["start_operation"]["state"], "cancelled")
                    self.assertFalse(status["running"])
                    self.assertEqual(status["sample_count"], 0)
                finally:
                    release.set()
                    manager._start_thread.join(4)

    def test_independent_probes_parallel_and_only_changed_interface_rechecked(self):
        barrier = threading.Barrier(2)
        calls, guard = [], threading.Lock()

        def probe(checker, cfg, timeout):
            item = cfg.interfaces[0]
            with guard:
                calls.append(item["id"])
                initial = len(calls) <= 2
            if initial:
                barrier.wait(2)
            return {"ok": True, "errors": [], "sensors": [
                {"name": n, "selected": n in cfg.selected_sensors, "received_samples": int(n in cfg.selected_sensors),
                 "invalid_samples": 0, "state": "ok", "ok": True} for n in cfg.schema_sensors],
                "interfaces": [{**item, "ok": True, "state": "ok", "enabled": True}]}

        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            with patch.object(a.AcquisitionManager, "_test_connection_once", probe):
                self.assertTrue(manager.test_connection(cfg)["ok"])
                self.assertCountEqual(calls, ["one", "two"])
                self.assertTrue(manager.test_connection(cfg)["ok"])
                self.assertEqual(len(calls), 2)
                cfg.interfaces[0]["endpoint"] = "COM93"
                self.assertTrue(manager.test_connection(cfg)["ok"])
                self.assertEqual(calls[-1], "one")
                self.assertEqual(len(calls), 3)
                manager.invalidate_readiness(["two"])
                self.assertFalse(manager.session_readiness_matches(cfg))
                manager.test_connection(cfg)
                self.assertEqual(calls[-1], "two")
                before = len(calls)
                manager.test_connection(cfg, force=True)
                self.assertEqual(len(calls), before+2)

    def test_imported_expired_evidence_is_not_a_session_authorization(self):
        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            snapshot = ReadinessSnapshot.create(cfg, {"ok": True}, now=time.time()-200)
            manager.import_readiness_snapshot(snapshot.to_dict())
            self.assertFalse(manager.session_readiness_matches(cfg))
            with self.assertRaisesRegex(ValueError, "expired"):
                manager._readiness_snapshot.validate(cfg)

    def test_frontend_session_check_and_pending_buttons(self):
        source = (Path(a.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")

        def function(name, next_name):
            begin = source.index("function " + name + "(")
            return source[begin:source.index("function " + next_name + "(", begin)]

        script = r'''
const state = {hardwareCheck:{ok:true,readiness_session_id:"session",readinessDeadline:1},
  hardwareCheckFingerprint:"same",acquisitionStatus:{running:false,start_operation:{state:"starting"}}};
const performance = {now:()=>1000000};
const controls = {autoHardwareCheck:{checked:true}};
let scheduled=0;
const window={clearTimeout(){},setTimeout(){scheduled++;}};
const hardwareConfigFingerprint=()=>"same";
const isPublicPrecomputedSimulationMode=()=>false;
const testSensorConnection=()=>{throw Error("重复检测");};
const nodes={startAcquisitionButton:{},stopAcquisitionButton:{}};
const $=key=>nodes[key];
''' + function("hardwareCheckIsCurrent", "rememberHardwareCheck") + function(
            "scheduleAutomaticHardwareCheck", "normalizeHardwareCheckResult") + function(
            "synchronizeAcquisitionButtons", "updateSimulationSettings") + r'''
if(!hardwareCheckIsCurrent("same") || hardwareCheckIsCurrent("changed")) throw Error("会话检测复用失败");
scheduleAutomaticHardwareCheck();
if(scheduled) throw Error("正常采集重复自动检查");
synchronizeAcquisitionButtons();
if(!nodes.startAcquisitionButton.disabled || nodes.stopAcquisitionButton.disabled) throw Error("启动期间不能取消");
if(nodes.stopAcquisitionButton.textContent!=="取消启动") throw Error("取消按钮标签错误");
'''
        completed = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_frontend_waits_for_all_channels_and_distinguishes_cancel(self):
        source = (Path(a.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        begin = source.index("async function waitForCaptureStart(")
        end = source.index("async function waitForHelperFlushComplete(", begin)
        script = r'''
const state={};const seen=[];
const renderAcquisitionStatus=s=>seen.push(s.start_operation.state);
const synchronizeAcquisitionButtons=()=>{};
const acquisitionConfig=()=>({});const hardwareCheckTimeoutMs=()=>20000;
const window={setTimeout:cb=>setImmediate(cb)};
let phase="starting",polls=0;
const requestLocalHelper=async()=>({running:polls++>0,start_operation:{id:"op",state:polls>1?phase:"starting"}});
''' + source[begin:end] + r'''
(async()=>{
  const accepted={accepted:true,start_operation:{id:"op",state:"starting"}};
  phase="running";
  const ready=await waitForCaptureStart(accepted,true);
  if(!ready.running || seen.join(",")!=="starting,starting,running")throw Error("首样本前提前报告成功");
  polls=0;phase="cancelled";
  try{await waitForCaptureStart(accepted,true);throw Error("缺少取消结果");}
  catch(error){if(error.code!=="capture_start_cancelled")throw error;}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        completed = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_helper_returns_pending_start_without_streaming_old_rows_and_can_cancel(self):
        from local_capture_agent import LocalCaptureAgent
        entered, release = threading.Event(), threading.Event()

        class Driver:
            def open(self):
                entered.set()
                release.wait(3)

            def cancel_open(self):
                release.set()

            def close(self):
                release.set()

        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            agent = LocalCaptureAgent(manager, simulation_cache_root=Path(root)/"cache")
            cfg = config(root)
            cfg.async_start = True
            with patch("acquisition.build_driver", return_value=Driver()):
                try:
                    accepted = agent.start_capture(cfg)
                    self.assertTrue(accepted["accepted"])
                    self.assertTrue(entered.wait(2))
                    self.assertFalse(agent.status()["running"])
                    self.assertEqual(agent.status()["sample_count"], 0)
                    self.assertIsNone(agent.next_sample_batch())
                    cancelled = agent.stop_capture()
                    self.assertEqual(cancelled["capture_phase"], "cancelling_start")
                    manager._start_thread.join(3)
                    self.assertEqual(agent.status()["start_operation"]["state"], "cancelled")
                finally:
                    release.set()
                    manager._start_thread.join(4)


if __name__ == "__main__":
    unittest.main()
