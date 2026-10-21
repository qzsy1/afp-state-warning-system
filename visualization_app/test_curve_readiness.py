from __future__ import annotations
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import acquisition as acquisition_module

from acquisition import AcquisitionManager
from test_acquisition_start import config
from real_acquisition import ChannelQuality, ChannelSample, UnifiedFrame


class CurveReadinessTests(unittest.TestCase):
    def test_main_matrix_renders_zero_and_central_pixel_without_aggregation_loss(self):
        source = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        function = source[source.index("function renderM3232Matrix(status)"):source.index("function renderAcquisitionStatus(status)")]
        script = r'''
const assert = require('assert');
const paints=[], callbacks=[];
const nodes=Object.fromEntries(['m3232MatrixPanel','m3232MatrixCanvas','m3232MatrixStatus','m3232MatrixValues','m3232MatrixData'].map(id=>[id,{textContent:'',dataset:{},classList:{toggle(){}},getContext(){return {clearRect(){paints.length=0},fillRect(x,y,w,h){paints.push({x,y,color:this.fillStyle})}}}}]));
const document={getElementById:id=>nodes[id]};
const window={afpCurveTrace:{accepts:s=>s.capture_generation==='current'}};
const requestAnimationFrame=f=>callbacks.push(f);
const originalCurveCanvasVisible=()=>true;
const performance={now:()=>120};let saved=0;
const persistCurveRenderEvidence=()=>saved++;
''' + function + r'''
const evidence={protocol_verified:true,connection_generation:'connection',frame_age_seconds:.01,valid_frame_rate_hz:8,matrix_shape:[32,32],latest_matrix:{connection_generation:'connection',receive_sequence:1,raw_total:0,matrix:Array.from({length:32},()=>Array(32).fill(0))}};
const status={running:true,capture_generation:'current',start_request_id:'click',interfaces:[{enabled:true,driver:'m3232_pressure',m3232_evidence:evidence}]};
renderM3232Matrix(status);assert.equal(paints.length,1024);assert.match(nodes.m3232MatrixValues.textContent,/完整全零帧/);
while(callbacks.length)callbacks.shift()();assert.equal(saved,1);
evidence.latest_matrix.matrix[15][16]=27;evidence.latest_matrix.receive_sequence=2;evidence.latest_matrix.raw_total=27;
renderM3232Matrix(status);assert.equal(paints.length,2048);
assert.notEqual(paints[1024+15*32+16].color,paints[1024].color);
assert.match(nodes.m3232MatrixValues.textContent,/0 \/ 27 \/ 0 \/ 0/);assert.match(nodes.m3232MatrixData.textContent,/27/);
while(callbacks.length)callbacks.shift()();assert.equal(window.afpM3232MatrixEvidence.source_sequence,2);
evidence.frame_age_seconds=3;renderM3232Matrix(status);assert.equal(paints.length,0);assert.equal(window.afpM3232MatrixEvidence,null);
evidence.frame_age_seconds=.01;status.capture_generation='old';renderM3232Matrix(status);assert.equal(paints.length,0);
status.capture_generation='current';evidence.latest_matrix.matrix[0]=[NaN];renderM3232Matrix(status);assert.equal(paints.length,0);
'''
        result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_status_update_refreshes_current_m3232_matrix(self):
        source = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        begin = source.index("function renderAcquisitionStatus(status)")
        end = source.index("  if (status?.save_status)", begin)
        self.assertIn("renderM3232Matrix(status)",source[begin:end])

    def test_render_evidence_requires_current_real_source_and_keeps_three_second_failure(self):
        from render_evidence import save_render_evidence
        from copy import deepcopy
        with tempfile.TemporaryDirectory() as root:
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            cfg.selected_sensors = ["压力"]
            cfg.start_request_id = "trusted-click"
            manager.config = cfg
            manager._begin_capture_timeline(cfg)
            manager.capture_record_dir = Path(root)
            points = [dict(interface_id="one", source_sequence=i, received_monotonic=100+i,
                           capture_generation=cfg.capture_generation, start_request_id=cfg.start_request_id,
                           quality="measured_new", after_start=True, protocol_ok=True) for i in (1,2)]
            manager._curve_start_observations = {"压力":points}
            trace = dict(start_request_id=cfg.start_request_id, capture_generation=cfg.capture_generation,
                         complete=True, input=dict(valid=True,trusted=True,timestamp=100),
                         selected_channels=["压力"], displayed={"压力":3100.1}, observations={"压力":points})
            payload = {"trace":trace}
            result = save_render_evidence(manager,payload)
            self.assertFalse(result["pass"])
            self.assertAlmostEqual(result["elapsed_ms"],3000.1)
            self.assertTrue(Path(result["path"]).is_file())
            for field,value in [("capture_generation","old"),("input",dict(valid=False,trusted=False,timestamp=100)),
                                ("observations",{"压力":[dict(p,source_sequence=99) for p in points]})]:
                invalid = deepcopy(payload)
                invalid["trace"][field] = value
                with self.assertRaises(ValueError):save_render_evidence(manager,invalid)

    def test_usb_profile_requires_current_session_stability_and_matching_handshakes(self):
        from real_acquisition import ReadinessSnapshot, readiness_config_fingerprint
        from esr1001_usb import USB_DRIVER, BOUNDED_START_PARAMETER_HEX
        with tempfile.TemporaryDirectory() as root:
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            cfg.interfaces[0]["driver"] = USB_DRIVER
            row = {"id": "one", "driver": USB_DRIVER, "ok": True, "state": "data_ready",
                   "usb_evidence": {"parameters": {"raw_hex": BOUNDED_START_PARAMETER_HEX},
                                    "current_stream_stable": True,
                                    "commands": [
                                        {"command_hex": "23a8000000ff", "matched_response_hex": BOUNDED_START_PARAMETER_HEX},
                                        {"command_hex": "23a1010000ff", "matched_response_hex": "25a10100000000000000000000ff"}]}}
            result = {"ok": True, "interfaces": [row]}
            manager._latest_check_result = result
            manager._readiness_snapshot = ReadinessSnapshot.create(cfg, result)
            self.assertEqual(manager.usb_startup_profiles(cfg), {})
            manager._session_readiness_fingerprint = readiness_config_fingerprint(cfg)
            self.assertTrue(manager.session_readiness_matches(cfg))
            self.assertEqual(manager.usb_startup_profiles(cfg), {"one": "bounded_response"})
            row["usb_evidence"]["current_stream_stable"] = False
            self.assertEqual(manager.usb_startup_profiles(cfg), {})
            row["usb_evidence"]["current_stream_stable"] = True
            row["usb_evidence"]["commands"][1]["matched_response_hex"] = ""
            self.assertEqual(manager.usb_startup_profiles(cfg), {})
            row["usb_evidence"]["commands"][1]["matched_response_hex"] = "25a10100000000000000000000ff"
            row["usb_evidence"]["stop_error"] = "usb_stop_timeout"
            self.assertEqual(manager.usb_startup_profiles(cfg), {})
            row["usb_evidence"]["stop_error"] = ""
            row["usb_evidence"]["parameters"]["raw_hex"] = "different"
            self.assertEqual(manager.usb_startup_profiles(cfg), {})
            row["usb_evidence"]["parameters"]["raw_hex"] = BOUNDED_START_PARAMETER_HEX
            manager.invalidate_readiness(["one"])
            self.assertEqual(manager.usb_startup_profiles(cfg), {})

    def test_two_fresh_source_observations_survive_unified_frame_holding(self):
        with tempfile.TemporaryDirectory() as root:
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            cfg.dataset_schema = "new_collection_v11_3"
            cfg.selected_sensors = ["温度", "压力"]
            cfg.start_request_id = "new-click"
            cfg.interface_channel_assignments = {"one": ["温度"], "two": ["压力"]}
            manager.config = cfg
            manager._begin_capture_timeline(cfg)
            started = manager._capture_start_monotonic
            manager.driver = acquisition_module.MultiInterfaceDriver([], cfg.selected_sensors)
            for name in cfg.selected_sensors:
                for sequence in (1, 2):
                    manager.driver.cache.publish(ChannelSample("one", name, 42.0, ChannelQuality.MEASURED_NEW,
                        received_monotonic=started + sequence * .01, source_sequence=sequence, protocol_ok=True))
            held = UnifiedFrame(0, started+.1, 1, {"温度": ChannelSample("one", "温度", 42,
                ChannelQuality.HELD_WITHIN_FRESHNESS, source_sequence=2, received_monotonic=started+.02)}, False, started+.1)
            with patch("acquisition_start.time.monotonic", return_value=started+.1):
                manager._record_start_frame()
            self.assertTrue(manager._curve_start_saved)
            self.assertEqual(manager.total_sample_count, 0)
            self.assertTrue(all(point["capture_generation"] == cfg.capture_generation
                                and point["start_request_id"] == "new-click"
                                for points in manager._curve_start_observations.values() for point in points))
            self.assertEqual([point["source_sequence"] for point in manager._curve_start_observations["温度"]], [1, 2])
            with patch("acquisition_start.time.monotonic", return_value=started+2):
                manager._begin_capture_timeline(cfg)
                manager._record_start_frame(held)
            self.assertFalse(manager._curve_start_saved)
            self.assertEqual(manager._source_observations, {})

    def test_same_click_is_idempotent_and_rejects_changed_configuration(self):
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as root:
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            cfg.start_request_id = "same-click"
            def start(_):
                entered.set()
                release.wait(2)
            with patch.object(manager, "start", side_effect=start) as start_call:
                try:
                    first = manager.start_async(cfg)
                    self.assertTrue(entered.wait(1))
                    second = manager.start_async(cfg)
                    self.assertEqual(first["start_operation"]["id"], second["start_operation"]["id"])
                    cfg.sample_rate_hz += 1
                    with self.assertRaisesRegex(RuntimeError, "start_request_config_conflict"):
                        manager.start_async(cfg)
                    self.assertEqual(start_call.call_count, 1)
                finally:
                    release.set()
                    manager._start_thread.join(3)

    def test_delayed_status_cannot_invalidate_newer_readiness_evidence(self):
        source = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        start = source.index("function renderAcquisitionStatus(status)")
        end = source.index("  if (status?.save_status)", start)
        script = r'''
const assert=require('node:assert/strict');const window={};
const controls={acquisitionMode:{value:'real'}};
const renderEsrNumeric=()=>{};
const state={hardwareCheck:{ok:true,readiness_session_id:'backend',readiness_version:3},hardwareCheckFingerprint:'same'};
''' + source[start:end] + "}\n" + r'''
renderAcquisitionStatus({readiness_session_id:'backend',readiness_version:2,readiness_session_valid:false});
assert.equal(state.hardwareCheck.ok,true);
renderAcquisitionStatus({readiness_session_id:'backend',readiness_version:3,readiness_session_valid:true});
assert.equal(state.hardwareCheck.ok,true);
renderAcquisitionStatus({readiness_session_id:'backend',readiness_version:4,readiness_session_valid:false});
assert.equal(state.hardwareCheck,null);
'''
        result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_windows_enumeration_cache_requires_unchanged_native_bindings(self):
        with patch.object(acquisition_module, "_WINDOWS_ADAPTER_CACHE", None), \
             patch.object(acquisition_module, "_windows_network_signature", side_effect=["same", "same", "changed", None, None]), \
             patch.object(acquisition_module, "_query_windows_network_adapters", return_value=[{"id": "ethernet:industrial"}]) as query:
            for expected in (1, 1, 2, 3, 4):
                self.assertTrue(acquisition_module._windows_network_adapter_snapshot())
                self.assertEqual(query.call_count, expected)

    def test_generation_changes_for_same_specimen_and_only_new_source_counts(self):
        with tempfile.TemporaryDirectory() as root:
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            cfg.start_request_id = "click-one"
            cfg.selected_sensors = ["温度"]
            manager._begin_capture_timeline(cfg)
            manager.config = cfg
            generation = cfg.capture_generation
            start = manager._capture_start_monotonic
            def frame(sequence, when, quality=ChannelQuality.MEASURED_NEW):
                return UnifiedFrame(0, when, 1, {"温度": ChannelSample(
                    "tc64", "温度", 20, quality, received_monotonic=when,
                    received_wall_time=1, source_sequence=sequence, protocol_ok=True)}, False, when)
            manager._record_start_frame(frame(1, start - 1))
            manager._record_start_frame(frame(2, start + .01))
            manager._record_start_frame(frame(2, start + .01, ChannelQuality.HELD_WITHIN_FRESHNESS))
            self.assertEqual(len(manager._source_observations["温度"]), 1)
            manager._record_start_frame(frame(3, start + .02))
            self.assertEqual(len(manager._source_observations["温度"]), 2)
            cfg.start_request_id = "click-two"
            manager._begin_capture_timeline(cfg)
            self.assertNotEqual(generation, cfg.capture_generation)
            self.assertEqual(manager._source_observations, {})

    def test_client_queue_clock_freshness_generation_and_actual_drawing(self):
        module = Path(__file__).parent / "static" / "curve_readiness.js"
        script = "const {CurveTrace,inputTime}=require(" + json.dumps(str(module)) + ");\n" + r'''
const assert=require('node:assert/strict');
let now=1800;const clock={now:()=>now,timeOrigin:1700000000000};
const cfg={selected_sensors:['A','B'],interfaces:[{id:'dev',enabled:true}],
  interface_channel_assignments:{dev:['A','B']}};
const trace=new CurveTrace({isTrusted:true,timeStamp:100},cfg,{clock,id:'click'});
cfg.selected_sensors.pop();assert.equal(trace.channels.length,2);
assert.equal(trace.snapshot().events[0].input_queue_ms,1700);
assert.equal(inputTime({isTrusted:true,timeStamp:clock.timeOrigin+100},clock).timestamp,100);
assert.equal(inputTime({isTrusted:false,timeStamp:100},clock).valid,false);
assert.equal(trace.accepts({start_request_id:'previous',capture_generation:'old'}),false);
assert.equal(trace.accepts({start_request_id:'click',capture_generation:'new'}),true);
assert.equal(trace.accepts({start_request_id:'click',capture_generation:'old'}),false);
const canvas={isConnected:true,clientWidth:300,clientHeight:200};
const source=(sequence,quality='measured_new')=>({source_sequence:sequence,interface_id:'dev',
 capture_generation:'new',start_request_id:'click',
 received_monotonic:500+sequence,after_start:true,protocol_ok:true,quality});
const channel={name:'display A',source_channel_name:'A',actual:[20,20],x_observed:[-1,0],source_observations:[source(1),source(1,'held_within_freshness')]};
assert.equal(trace.drawnChannel(channel,canvas,true),false);
channel.source_observations[1]=source(2);
assert.equal(trace.drawnChannel(channel,{...canvas,isConnected:false},true),false);
assert.equal(trace.drawnChannel(channel,canvas,false),false);
assert.equal(trace.drawnChannel({...channel,actual:[]},canvas,true),false);
assert.equal(trace.drawnChannel(channel,canvas,true),true);
assert.equal(trace.drawnChannel({...channel,source_observations:[{...source(1),capture_generation:'old'},source(2)]},canvas,true),false);
trace.confirmDisplayed(['A'],true,{surface:'sensor_card',canvas_width:300,canvas_height:200});assert.equal(trace.complete,false);
assert.equal(trace.drawnChannel({...channel,name:'display B',source_channel_name:'B'},canvas,true),true);
trace.confirmDisplayed(['B'],false);assert.equal(trace.complete,false);
now=3200;trace.confirmDisplayed(['B'],true,{surface:'thermocouple64_group',canvas_width:300,canvas_height:200});
assert.equal(trace.snapshot().presentations.A.surface,'sensor_card');
assert.equal(trace.snapshot().presentations.B.surface,'thermocouple64_group');
assert.equal(trace.snapshot().elapsed_ms,3100);assert.equal(trace.snapshot().pass,false);
assert.equal(trace.snapshot().target_pass,false);
now=3100;trace.drawn.set('B',now);assert.equal(trace.snapshot().pass,true);
now=3100.1;trace.drawn.set('B',now);assert.equal(trace.snapshot().pass,false);
const synthetic=new CurveTrace({isTrusted:false,timeStamp:100},cfg,{clock,id:'script'});
synthetic.accepts({start_request_id:'script',capture_generation:'gen'});
synthetic.drawnChannel(channel,canvas,true);synthetic.confirmDisplayed(['A'],true);
assert.equal(synthetic.complete,false);
'''
        result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_original_sensor_card_draws_in_outer_frame_then_confirms_after_paint(self):
        app_source = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        canvas_functions = app_source[
            app_source.index("function setupCanvas"):
            app_source.index("function renderTimeline")
        ]
        card_functions = app_source[
            app_source.index("function renderSensorCardsInto"):
            app_source.index("function renderSensorCards(")
        ]
        readiness = Path(__file__).parent / "static" / "curve_readiness.js"
        script = "globalThis.window={};const {CurveTrace}=require(" + json.dumps(str(readiness)) + ");\n" + r'''
const assert=require('node:assert/strict');
let now=100;const clock={now:()=>now,timeOrigin:0};
const trace=new CurveTrace({isTrusted:true,timeStamp:10},{selected_sensors:['压力'],
  interfaces:[{id:'plc',enabled:true}],interface_channel_assignments:{plc:['压力']}},{clock,id:'click'});
trace.accepts({start_request_id:'click',capture_generation:'generation'});
const frames=[];
Object.assign(globalThis.window,{afpCurveTrace:trace,requestAnimationFrame:(callback)=>{frames.push(callback);return frames.length;}});
const document={visibilityState:'visible',hidden:false};
const controls={acquisitionMode:{value:'real'}};
const state={showResidual:false};
const context=new Proxy({setTransform(){},setLineDash(){}},{get:(target,key)=>target[key]||(()=>{})});
const canvas={isConnected:true,clientWidth:320,clientHeight:150,width:0,height:0,dataset:{},
  getContext:()=>context,closest:()=>null,checkVisibility:()=>true};
const textNode=()=>({textContent:''});
const card={dataset:{channelId:'7'},className:'',querySelector:(selector)=>selector==='canvas'?canvas:textNode()};
const target={querySelectorAll:()=>[card]};
const source=(sequence)=>({source_sequence:sequence,interface_id:'plc',capture_generation:'generation',
  start_request_id:'click',received_monotonic:100+sequence,after_start:true,protocol_ok:true,stable:true,
  quality:'measured_new'});
const channel={id:7,name:'压实力',source_channel_name:'压力',unit:'N',prediction_enabled:false,
  actual:[20,20],x_observed:[-0.1,0],source_observations:[source(1),{...source(1),quality:'held_within_freshness'}],
  startup_curve:{actual:[20,21],x_observed:[-0.08,-0.01],x_domain:'seconds_before_payload',
    source_observations:[source(1),source(2)]},
  prediction_observed:[],x_future:[],prediction_future:[],actual_current:21};
function fmt(value){return String(value)}
''' + canvas_functions + "\n" + card_functions + r'''
window.requestAnimationFrame(()=>renderSensorCardsInto(target,[channel],7,{reuse:true}));
assert.equal(trace.snapshot().complete,false);
assert.equal(frames.length,1);
let batch=frames.splice(0);now+=16;batch.forEach(callback=>callback(now));
assert.ok(canvas.width>0);
assert.equal(trace.snapshot().complete,false);
assert.equal(frames.length,1);
batch=frames.splice(0);now+=16;batch.forEach(callback=>callback(now));
const snapshot=trace.snapshot();
assert.equal(snapshot.complete,true);
assert.equal(frames.length,0);
assert.deepEqual(snapshot.selected_channels,['压力']);
assert.deepEqual(Object.keys(snapshot.displayed),['压力']);
assert.equal(snapshot.presentations['压力'].surface,'sensor_card');
assert.deepEqual(snapshot.presentations['压力'].channels,['压力']);
assert.deepEqual(snapshot.observations['压力'].map(point=>point.source_sequence),[1,2]);
assert.deepEqual(channel.actual,[20,20]);
assert.ok(snapshot.events.some(event=>event.stage==='C3_all'));
'''
        result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_original_sensor_card_draws_after_mount_in_outer_frame_then_confirms(self):
        app_source = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        canvas_functions = app_source[
            app_source.index("function setupCanvas"):
            app_source.index("function renderTimeline")
        ]
        card_functions = app_source[
            app_source.index("function renderSensorCardsInto"):
            app_source.index("function renderSensorCards(")
        ]
        readiness = Path(__file__).parent / "static" / "curve_readiness.js"
        script = "globalThis.window={};const {CurveTrace}=require(" + json.dumps(str(readiness)) + ");\n" + r'''
const assert=require('node:assert/strict');
let now=100;const clock={now:()=>now,timeOrigin:0};
const trace=new CurveTrace({isTrusted:true,timeStamp:10},{selected_sensors:['ABB扭矩'],
  interfaces:[{id:'abb',enabled:true}],interface_channel_assignments:{abb:['ABB扭矩']}},{clock,id:'click'});
trace.accepts({start_request_id:'click',capture_generation:'generation'});
const frames=[];
Object.assign(globalThis.window,{afpCurveTrace:trace,requestAnimationFrame:(callback)=>{frames.push(callback);return frames.length;}});
const controls={acquisitionMode:{value:'real'},sensor:{value:''}};
const state={showResidual:false};
const context=new Proxy({setTransform(){},setLineDash(){}},{get:(target,key)=>target[key]||(()=>{})});
const makeCanvas=()=>({isConnected:false,clientWidth:320,clientHeight:150,width:0,height:0,dataset:{},
  getContext:()=>context,closest:()=>null,checkVisibility:()=>true});
class Card {
  constructor(){this.dataset={};this.canvas=makeCanvas();this.nodes=new Map();}
  set innerHTML(value){this.html=value;}
  querySelector(selector){
    if(selector==='canvas')return this.canvas;
    if(!this.nodes.has(selector))this.nodes.set(selector,{textContent:''});
    return this.nodes.get(selector);
  }
  addEventListener(){}
  setAttribute(){}
}
const document={visibilityState:'visible',hidden:false,createElement:()=>new Card()};
const target={cards:[],querySelectorAll:()=>[],replaceChildren(...cards){this.cards=cards;cards.forEach(card=>{card.canvas.isConnected=true;});}};
const source=(sequence)=>({source_sequence:sequence,interface_id:'abb',capture_generation:'generation',
  start_request_id:'click',received_monotonic:100+sequence,after_start:true,protocol_ok:true,stable:true,
  quality:'measured_new'});
const channel={id:9,name:'扭矩',source_channel_name:'ABB扭矩',unit:'Nm',prediction_enabled:false,
  actual:[20,21],x_observed:[-0.1,0],source_observations:[source(1),source(2)],
  prediction_observed:[],x_future:[],prediction_future:[],actual_current:21};
function fmt(value){return String(value)}
function showSensorSharedDetail(){}
function loadRealtime(){}
''' + canvas_functions + "\n" + card_functions + r'''
window.requestAnimationFrame(()=>renderSensorCardsInto(target,[channel],9));
assert.equal(target.cards.length,0);
assert.equal(frames.length,1);
now+=16;frames.shift()(now);
assert.equal(target.cards.length,1);
assert.ok(target.cards[0].canvas.width>0);
assert.equal(trace.snapshot().complete,false);
assert.equal(frames.length,1);
now+=16;frames.shift()(now);
assert.equal(trace.snapshot().complete,true);
assert.equal(frames.length,0);
'''
        result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_original_tc64_group_canvases_draw_now_and_complete_after_paint(self):
        app_source = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        canvas_functions = app_source[
            app_source.index("function setupCanvas"):
            app_source.index("function renderTimeline")
        ]
        thermocouple_functions = app_source[
            app_source.index("const THERMOCOUPLE64_COLORS"):
            app_source.index("function thermocouple64GroupLabel")
        ]
        readiness = Path(__file__).parent / "static" / "curve_readiness.js"
        script = "globalThis.window={};const {CurveTrace}=require(" + json.dumps(str(readiness)) + ");\n" + r'''
const assert=require('node:assert/strict');
let now=100;const clock={now:()=>now,timeOrigin:0};
const names=Array.from({length:64},(_,index)=>`温度${index+1}`);
const trace=new CurveTrace({isTrusted:true,timeStamp:10},{selected_sensors:names,
  interfaces:[{id:'tc64',enabled:true}],interface_channel_assignments:{tc64:names}},{clock,id:'click'});
trace.accepts({start_request_id:'click',capture_generation:'generation'});
const frames=[];
Object.assign(globalThis.window,{afpCurveTrace:trace,requestAnimationFrame:(callback)=>{frames.push(callback);return frames.length;}});
const document={visibilityState:'visible',hidden:false};
const controls={acquisitionMode:{value:'real'}};
const contexts=Array.from({length:8},()=>new Proxy({setTransform(){},setLineDash(){}},{get:(target,key)=>target[key]||(()=>{})}));
const canvases=contexts.map((context)=>({isConnected:true,clientWidth:320,clientHeight:104,width:0,height:0,dataset:{},
  getContext:()=>context,closest:()=>null,checkVisibility:()=>true,classList:{contains:()=>true}}));
const target={querySelectorAll:()=>canvases};
const source=(sequence)=>({source_sequence:sequence,interface_id:'tc64',capture_generation:'generation',
  start_request_id:'click',received_monotonic:100+sequence,after_start:true,protocol_ok:true,stable:true,
  quality:'measured_new'});
const channels=names.map((name,index)=>({id:index,name,source_channel_name:name,actual:[20+index,21+index],
  x_observed:[-0.1,0],source_observations:[source(index*2+1),source(index*2+2)]}));
const state={showResidual:false,thermocouple64Raf:0,thermocouple64View:'raw',
  thermocouple64Visible:new Set(Array.from({length:64},(_,index)=>index)),thermocouple64MeanVisible:new Set(),
  thermocouple64DetailActive:false,payload:{channels},thermocouple64Cache:{
    rawSeries:Array.from({length:8},(_,group)=>Array.from({length:8},(_,offset)=>channels[group*8+offset].actual)),
    derivedSeries:Array.from({length:8},()=>[20,21]),qualities:Array(64).fill('measured_new'),timeAxis:[-0.1,0]}};
function $(id){if(id==='thermocouple64RawGroups'||id==='thermocouple64AverageGroups')return target;
  if(id==='thermocouple64UniformScale'||id==='thermocouple64AbnormalOnly')return {checked:false};
  if(id==='sensorSharedDetail')return {open:false};return null;}
''' + canvas_functions + "\n" + thermocouple_functions + r'''
scheduleThermocouple64Draw();
assert.equal(trace.snapshot().complete,false);
assert.equal(frames.length,1);
let batch=frames.splice(0);now+=16;batch.forEach(callback=>callback(now));
assert.equal(trace.snapshot().complete,false);
assert.equal(frames.length,8);
batch=frames.splice(0);now+=16;batch.forEach(callback=>callback(now));
const snapshot=trace.snapshot();
assert.equal(snapshot.complete,true);
assert.equal(frames.length,0);
assert.equal(snapshot.selected_channels.length,64);
assert.equal(Object.keys(snapshot.displayed).length,64);
assert.equal(snapshot.presentations['温度1'].surface,'thermocouple64_group');
assert.deepEqual(snapshot.presentations['温度1'].channels,names.slice(0,8));
assert.ok(snapshot.events.some(event=>event.stage==='C3_all'));
'''
        result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_complete_startup_websocket_payload_draws_from_outer_frame_and_coalesces(self):
        app_source = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        websocket_functions = app_source[
            app_source.index("function startupCurvePayloadHasAllChannels"):
            app_source.index("function queryString()")
        ]
        script = r'''
const assert=require('node:assert/strict');
const applied=[];const frames=[];const sockets=[];
class FakeWebSocket {
  static OPEN=1;static CONNECTING=0;
  constructor(url){this.url=url;this.readyState=FakeWebSocket.OPEN;sockets.push(this);}
  close(){}
}
globalThis.WebSocket=FakeWebSocket;
const trace={id:'click',generation:'generation',channels:['A','B'],complete:false,
  accepts(status){return status?.start_request_id===this.id&&status?.capture_generation===this.generation;}};
const window={WebSocket:FakeWebSocket,afpCurveTrace:trace,location:{protocol:'http:',host:'localhost'},
  requestAnimationFrame(callback){frames.push(callback);return frames.length;},setTimeout(){return 1;}};
const controls={dataMode:{value:'live'},acquisitionMode:{value:'real'}};
const state={accessRole:'local_admin',liveSocket:null,liveSocketQuery:'',liveSocketReconnectAttempt:0};
const document={querySelector:()=>({classList:{add(){}}})};
const $=()=>({textContent:''});
const isPublicPrecomputedSimulationMode=()=>false;
const supportsLiveWebSocket=()=>true;
const queryString=()=>'';
const stopLiveHttpFallback=()=>{};
const startLiveHttpFallback=()=>{};
const applyRealtimePayload=(payload)=>applied.push(payload.sequence);
const source=(sequence)=>({quality:'measured_new',after_start:true,protocol_ok:true,stable:true,
  capture_generation:'generation',start_request_id:'click',interface_id:'device',source_sequence:sequence,
  received_monotonic:100+sequence,value:sequence});
const payload=(sequence)=>({sequence,mode:'capture_only',acquisition:{start_request_id:'click',capture_generation:'generation'},
  channels:['A','B'].map(name=>({name,source_channel_name:name,startup_curve:{source_observations:[source(1),source(2)]}}))});
''' + websocket_functions + r'''
openLiveWebSocket();
const socket=sockets[0];
socket.onmessage({data:JSON.stringify(payload(1))});
assert.deepEqual(applied,[]);
assert.equal(frames.length,1);
socket.onmessage({data:JSON.stringify(payload(2))});
socket.onmessage({data:JSON.stringify(payload(3))});
assert.deepEqual(applied,[]);
assert.equal(frames.length,1);
frames.shift()();
assert.deepEqual(applied,[3]);
assert.equal(state.browserCoalescedPayloads,2);
const partial=payload(4);partial.channels[1].startup_curve.source_observations=[source(1)];
socket.onmessage({data:JSON.stringify(partial)});
assert.deepEqual(applied,[3]);
assert.equal(frames.length,1);
frames.shift()();
assert.deepEqual(applied,[3,4]);
socket.onmessage({data:JSON.stringify({...payload(5),acquisition:{start_request_id:'other',capture_generation:'generation'}})});
assert.deepEqual(applied,[3,4]);
'''
        result = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
