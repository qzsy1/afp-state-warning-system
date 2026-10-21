"""ESR数值语义、来源和历史复算合同。"""
import copy
import csv
import json
import sys
import tempfile
import time
import unittest
import subprocess
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from esr1001_usb import (RAW_CHANNELS, Esr1001UsbDriver, RawCaptureWriter, UsbProtocolError,
    calibrated_values, content_digest, measurement_record, read_vendor_candidate,
    confirm_vendor_candidate, load_calibration, normalize_vendor_candidate, replay_source_capture,
    vendor_display, verify_source_evidence)
from esr1001_fixtures import USB_DATA, USB_EXPECTED_ADC


def fixture_profile():
    return {"version": 1, "unit": "με", "source": "approved-test-input",
        "device_binding": {"location": "fixture"}, "bridge": "quarter", "gain_code": 6,
        "channels": [{"K": .007456, "B": 420.95}] * 4}


def fixture_vendor_candidate():
    return {"kind": "esr1001_vendor_candidate_v1", "confirmed": False,
        "source": r"C:\Program Files (x86)\ESR1001四通道应变仪采集软件\Config.xml",
        "source_sha256": "d82372383067befd70f00e6823e824826817009ba3798f4a9e78527a4aef2822",
        "unit": "με", "display_format": "0.0",
        "channels": [{"K": .007456, "B": 420.95}] * 4,
        "vendor_settings": {"PGA": "6", "Speed": "6", "SendNum": "1", "FilterNum": "0"},
        "applicability": "待逐路确认设备、接线、桥路、增益和标定依据；示例系数不能证明适用"}


def source_record(sequence=1):
    return dict(zip(RAW_CHANNELS, USB_EXPECTED_ADC), receive_sequence=sequence,
        host_wall_time=time.time(), host_monotonic=time.monotonic(), raw_hex=USB_DATA.hex(),
        unit="adc_counts", connection_generation=1, calibration_digest=content_digest(fixture_profile()))


class NumericTests(unittest.TestCase):
    def test_main_csv_saves_display_strain_and_keeps_adc_source_evidence(self):
        from acquisition import AcquisitionConfig, AcquisitionManager
        from test_esr1001_usb import StreamingSerial, UsbIntegrationTests
        candidate = fixture_vendor_candidate()
        interface = dict(UsbIntegrationTests.config().interfaces[0],
                         channel_settings={"esr_vendor_candidate": candidate})
        with tempfile.TemporaryDirectory() as root, \
                patch("serial.Serial", side_effect=StreamingSerial), \
                patch("serial.tools.list_ports.comports", return_value=[SimpleNamespace(device="COM91", location="test")]):
            config = AcquisitionConfig(acquisition_mode="real", dataset_schema="new_collection_v13_0",
                interfaces=[interface], processing_mode="capture_only", save_root=root)
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            self.assertTrue(manager.test_connection(config)["ok"])
            manager.start(config)
            try:
                deadline = time.monotonic() + 2
                while manager.total_sample_count < 3 and time.monotonic() < deadline:
                    time.sleep(.01)
            finally:
                result = manager.stop()
            self.assertTrue(result["finalization_complete"], result)
            with manager.raw_path.open(encoding="gb18030", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertGreaterEqual(len(rows), 3)
            expected = {f"应变{i}": vendor_display(adc * .007456 + 420.95)
                        for i, adc in enumerate(USB_EXPECTED_ADC, 1)}
            for row in rows:
                self.assertFalse(set(RAW_CHANNELS).intersection(row))
                self.assertEqual({key: row[key] for key in expected}, expected)
            with manager.full_specimen_path.open(encoding="gb18030", newline="") as stream:
                self.assertEqual(list(csv.DictReader(stream)), rows)
            for row in manager.rows:
                self.assertEqual(tuple(row[name] for name in RAW_CHANNELS), USB_EXPECTED_ADC)
            evidence = manager.driver.interface_quality_metadata["esr"]
            self.assertEqual(evidence["data_mode"], "raw_adc")
            replayed = list(replay_source_capture(evidence["raw_file"]))
            self.assertGreaterEqual(len(replayed), len(rows))
            self.assertEqual(tuple(replayed[0][name] for name in RAW_CHANNELS), USB_EXPECTED_ADC)

    def test_saved_strain_uses_each_frames_zero_and_preserves_missing_rows(self):
        from acquisition import AcquisitionConfig
        from real_acquisition import ChannelSample, ChannelQuality, UnifiedFrame
        from test_esr1001_usb import UsbIntegrationTests
        candidate = fixture_vendor_candidate()
        config = AcquisitionConfig(acquisition_mode="real", dataset_schema="new_collection_v13_0",
            interfaces=[dict(UsbIntegrationTests.config().interfaces[0],
                channel_settings={"esr_vendor_candidate": candidate})], processing_mode="capture_only")
        record = source_record(2)
        record["vendor_candidate_digest"] = content_digest(candidate)
        baseline = dict(zip(RAW_CHANNELS, (value - 10 for value in USB_EXPECTED_ADC)))
        zero = {"id": "zero", "adc_values": baseline,
                "vendor_candidate_values": calibrated_values(baseline, candidate)}
        record.update(measurement_record(record, None, zero=zero, vendor_candidate=candidate))
        row = dict(zip(RAW_CHANNELS, USB_EXPECTED_ADC))
        unchanged = copy.deepcopy(row)
        for quality, expected in ((ChannelQuality.MEASURED_NEW, "0.1"),
                                  (ChannelQuality.HELD_WITHIN_FRESHNESS, "0.1"),
                                  (ChannelQuality.MISSING, ""), (ChannelQuality.STALE, ""),
                                  (ChannelQuality.INVALID, "")):
            for remote in (False, True):
                with self.subTest(quality=quality, remote=remote):
                    metadata = {"adc_source": record} if remote else {
                        "latest_record": record, "vendor_candidate_snapshot": candidate}
                    frame = UnifiedFrame(capture_sequence=0, target_monotonic=1, target_wall_time=1,
                        channels={name: ChannelSample(interface_id="esr", channel_name=name,
                            value=value, quality=quality, metadata=metadata) for name, value in row.items()})
                    saved = config.saved_row(row, frame)
                    self.assertEqual(saved, {f"应变{i}": expected for i in range(1, 5)})
                    self.assertEqual(row, unchanged)
    @unittest.skipUnless(shutil.which("node"), "需要Node验证应变通道显示投影")
    def test_vendor_display_replaces_original_strain_slots_without_mutating_adc(self):
        script = r'''
const fs=require('fs'),assert=require('assert');
const source=fs.readFileSync('visualization_app/static/app.js','utf8');
eval(source.slice(source.indexOf('function esrChannelContext('),source.indexOf('const esrRelativeHistory')));
eval(source.slice(source.indexOf('function esrDisplayBasis('),source.indexOf('function renderEsrNumeric(')));
const evidence={capture_generation:'driver',connection_generation:1,data_mode:'raw_adc',vendor_candidate_digest:'candidate',
 vendor_candidate_snapshot:{channels:Array.from({length:4},()=>({K:.007456,B:420.95}))},zero:null,
 latest_record:{receive_sequence:2,host_monotonic:2,CH1_ADC_signed:5008,vendor_candidate_values:{应变1:458.289648},display_vendor_candidate_values:{应变1:'458.3'}}};
const status={capture_generation:'capture',running:false,interfaces:[{id:'esr1001_strain',usb_evidence:evidence}]};
const raw={id:65,name:'CH1_ADC_signed',unit:'adc_counts',actual:[5000,5008],actual_current:5008,x_observed:[-1,0],
 source_observations:[1,2].map((n)=>({capture_generation:'capture',interface_id:'esr1001_strain',source_sequence:n,received_monotonic:n,quality:'measured_new'}))};
const unchanged=structuredClone(raw);
const pressure={id:4,name:'压力',unit:'N',actual:[1,2]};
const channels=esrSensorDisplayChannels([pressure,{id:5,name:'应变1',unit:'με'},raw],status);
assert.equal(channels.length,2);assert.strictEqual(channels[0],pressure);
const projected=channels[1];
assert.equal(projected.id,65);assert.equal(projected.name,'CH1_ADC_signed');assert.equal(projected.display_name,'应变1 · CH1');
assert.equal(projected.unit,'με');assert.equal(projected.actual_current,458.289648);
assert.equal(projected.esr_display.text,'458.3');assert.equal(projected.esr_display.historical,true);
assert.deepStrictEqual(projected.actual,raw.actual.map(v=>v*.007456+420.95));
assert.equal(projected.prediction_enabled,false);assert.equal(projected.startup_curve,undefined);
assert.deepStrictEqual(raw,unchanged);
assert.deepStrictEqual(esrSensorDisplayChannels(channels,status),channels);
const one=esrSensorDisplayChannels([{...raw,actual:[5008],source_observations:[raw.source_observations[1]]}],status)[0];
assert.equal(one.esr_display.curve_ready,false);
assert.equal(projected.esr_display.curve_ready,true);
const startup=esrSensorDisplayChannels([{...raw,actual:[5008],source_observations:[raw.source_observations[1]],
 startup_curve:{actual:raw.actual,x_observed:raw.x_observed,source_observations:raw.source_observations}}],status)[0];
assert.equal(startup.esr_display.curve_ready,true);
assert.deepStrictEqual(startup.actual,projected.actual);
const gap=esrSensorDisplayChannels([{...raw,source_observations:[raw.source_observations[0],{...raw.source_observations[1],quality:'stale'}]}],status)[0];
assert.equal(gap.actual[1],null);assert.equal(gap.esr_display.curve_ready,false);
const foreign=esrSensorDisplayChannels([{...raw,source_observations:raw.source_observations.map(s=>({...s,capture_generation:'old'}))}],status);
assert.equal(foreign[0].unit,'adc_counts');
evidence.zero={id:'zero',receive_sequence:1,vendor_candidate_values:{应变1:458.23}};
evidence.latest_record={...evidence.latest_record,zero_id:'zero',vendor_candidate_relative_values:{相对应变1:.059648},display_vendor_candidate_relative_values:{相对应变1:'0.1'}};
const relative=esrSensorDisplayChannels([raw],status)[0];
assert.equal(relative.actual[0],null);assert.equal(relative.esr_display.text,'0.1');
assert(Math.abs(relative.actual[1]-(5008*.007456+420.95-458.23))<1e-10);
'''
        subprocess.run([shutil.which("node"), "-e", script], cwd=Path(__file__).resolve().parents[1], check=True, timeout=20)

    def test_shared_plan_rejects_stale_page_that_removes_vendor_snapshot(self):
        from pre_sensor_config import InterfacePlanStore
        with tempfile.TemporaryDirectory() as root:
            store = InterfacePlanStore(Path(root) / "plans.json")
            payload = {"active": "esr1001_raw_adc", "plans": {"esr1001_raw_adc": {
                "interfaces": [{"id": "esr", "channel_settings": {}}]}}}
            store.save(payload)
            stale, revision = store.load_with_revision()
            current = copy.deepcopy(stale)
            current["plans"]["esr1001_raw_adc"]["interfaces"][0]["channel_settings"] = {
                "esr_vendor_candidate": fixture_vendor_candidate()}
            updated_revision = store.save(current, expected_revision=revision, require_revision=True)
            self.assertNotEqual(revision, updated_revision)
            with self.assertRaisesRegex(ValueError, "interface_plan_conflict"):
                store.save(stale, expected_revision=revision, require_revision=True)
            with self.assertRaisesRegex(ValueError, "interface_plan_conflict"):
                store.save(stale, require_revision=True)
            self.assertEqual(store.load(), current)

    def test_replay_rejects_foreign_zero_generation_and_raw_unit_or_digest(self):
        with tempfile.TemporaryDirectory() as root:
            driver = self.driver()
            writer = RawCaptureWriter(root, {"calibration_snapshot": driver.calibration,
                "calibration_digest": driver.calibration_digest, "capture_generation": driver.capture_generation})
            writer.submit(dict(driver.latest_record, **measurement_record(driver.latest_record, driver.calibration)))
            zero = self.set_zero(driver)
            second = source_record(2)
            writer.submit(dict(second, **measurement_record(second, driver.calibration, zero)))
            writer.close({})
            original = writer.path.read_text(encoding="utf-8")
            self.assertEqual(len(list(replay_source_capture(writer.path))), 2)
            for field in ("zero_generation", "unit"):
                rows = list(csv.DictReader(original.splitlines()))
                if field == "zero_generation":
                    foreign = json.loads(rows[1]["zero_snapshot_json"])
                    foreign["capture_generation"] = "another-capture"
                    rows[1]["zero_snapshot_json"] = json.dumps(foreign)
                else:
                    rows[1]["unit"] = "με"
                with writer.path.open("w", encoding="utf-8", newline="") as stream:
                    output = csv.DictWriter(stream, fieldnames=rows[0].keys())
                    output.writeheader(); output.writerows(rows)
                with self.subTest(field=field), self.assertRaises(ValueError):
                    list(replay_source_capture(writer.path))
            writer.path.write_text(original, encoding="utf-8")
            metadata = json.loads(writer.metadata_path.read_text(encoding="utf-8"))
            metadata["calibration_snapshot"] = None
            writer.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaises(ValueError):
                list(replay_source_capture(writer.path))
            raw = source_record()
            raw["calibration_digest"] = "orphaned-digest"
            raw_writer = RawCaptureWriter(root, {"calibration_snapshot": None, "calibration_digest": "orphaned-digest"})
            raw_writer.submit(dict(raw, **measurement_record(raw, None)))
            raw_writer.close({})
            with self.assertRaises(ValueError):
                list(replay_source_capture(raw_writer.path))
            with self.assertRaises(ValueError):
                verify_source_evidence({"record": dict(raw, unit="με"), "calibration_snapshot": None})

    @unittest.skipUnless(shutil.which("node"), "需要Node验证比较口径提示")
    def test_numeric_comparison_basis_distinguishes_units_and_zero(self):
        script = r'''
const fs=require('fs'), assert=require('assert');
const source=fs.readFileSync('visualization_app/static/app.js','utf8');
eval(source.slice(source.indexOf('function esrComparisonBasis('),source.indexOf('function renderEsrNumeric(')));
const raw=esrComparisonBasis({data_mode:'raw_adc',zero:null});
assert(raw.includes('adc_counts'));assert(raw.includes('不能直接比较'));assert(raw.includes('K/B'));
assert(esrComparisonBasis({data_mode:'raw_adc',zero:{id:'z'}}).includes('相对ADC'));
const candidate=esrComparisonBasis({data_mode:'raw_adc',vendor_candidate_snapshot:{unit:'με'},zero:null});
assert(candidate.includes('厂商候选微应变'));assert(candidate.includes('不进入模型'));
assert(esrComparisonBasis({data_mode:'microstrain',calibration_snapshot:{},zero:null}).includes('标定绝对值'));
assert(esrComparisonBasis({data_mode:'microstrain',calibration_snapshot:{},zero:{id:'z'}}).includes('单帧零点'));
assert(source.includes('esrComparisonBasis(evidence)'));
assert(source.includes('display_vendor_candidate_values'));
assert(source.includes('厂商候选显示 (με，未确认)'));
'''
        subprocess.run([shutil.which("node"), "-e", script], cwd=Path(__file__).resolve().parents[1], check=True, timeout=20)


    def test_reported_context_is_preserved_without_enabling_calibration(self):
        from acquisition import AcquisitionConfig
        from pre_sensor_config import InterfacePlanStore
        context = {"source": "operator", "focus_channel": 2, "external_power_reported": True,
                   "channels": {"CH2": {"model": "fixture-EB", "resistance_ohm": 350,
                   "expected_bridge": "full", "switch_verified": False, "connection_state": "connected"}}}
        driver = Esr1001UsbDriver("fixture", measurement_context=context)
        context["channels"]["CH2"]["model"] = "changed"
        snapshot = driver.quality_metadata["measurement_context"]
        self.assertEqual(snapshot["channels"]["CH2"]["model"], "fixture-EB")
        self.assertEqual(driver.calibration_digest, "")
        self.assertIsNone(driver.calibration)
        self.assertEqual(driver.bridge, "")
        self.assertEqual(measurement_record(source_record(), driver.calibration)["calibrated_values"], {})
        interface = {"id": "esr", "role": "esr1001_strain", "driver": "esr1001_usb_ttl", "endpoint": "COM91",
                     "data_mode": "raw_adc", "channel_settings": {"esr_context": snapshot}}
        config = AcquisitionConfig(dataset_schema="esr1001_raw_adc", processing_mode="capture_only", interfaces=[interface])
        self.assertEqual(config.interfaces[0]["channel_settings"]["esr_context"], snapshot)
        self.assertEqual(config.interfaces[0].get("bridge", ""), "")
        with tempfile.TemporaryDirectory() as root:
            store = InterfacePlanStore(Path(root)/"plans.json")
            plan = {"active": "esr1001_raw_adc", "plans": {"esr1001_raw_adc": {"interfaces": config.interfaces}}}
            store.save(plan)
            self.assertEqual(store.load()["plans"]["esr1001_raw_adc"]["interfaces"][0]["channel_settings"]["esr_context"], snapshot)
            writer = RawCaptureWriter(root, {"measurement_context": snapshot})
            record = source_record()
            record["calibration_digest"] = ""
            record.update(measurement_record(record, None))
            writer.submit(record)
            writer.close(driver.quality_metadata)
            self.assertEqual(json.loads(writer.metadata_path.read_text(encoding="utf-8"))["measurement_context"], snapshot)
            driver.measurement_context = {"channels": {"CH2": {"model": "another-sensor"}}}
            self.assertEqual(list(replay_source_capture(writer.path))[0]["measurement_context"], snapshot)
            from mysql_storage import MySQLCaptureStore, MySQLSettings
            from test_mysql_identity import _FakeConnection
            for database in ("local_target", "server_target"):
                store = MySQLCaptureStore(MySQLSettings(enabled=True, database=database))
                connection = _FakeConnection()
                with patch.object(store, "_connect", return_value=("fake", connection)):
                    result = store.save_layer(config, rows=[], layer_file=None, full_specimen_file=None,
                        timestamp_file=None, folder_path=None, summary={"data_quality": {"esr1001_usb": {"esr": {
                            "raw_file": str(writer.path), "saved_source_rows": 1, "calibration_snapshot": None,
                            "measurement_context": driver.measurement_context}}}})
                self.assertTrue(result["ok"], result)
                inserted = [v for sql,v in connection.cursor_instance.executed if sql.startswith("INSERT INTO afp_source_evidence")]
                self.assertEqual(json.loads(inserted[0][0][-1])["measurement_context"], snapshot)

    @unittest.skipUnless(shutil.which("node"), "需要Node验证现场信息显示")
    def test_context_display_keeps_unverified_switch_and_faults_explicit(self):
        script = r'''
const fs=require('fs'),assert=require('assert');
const source=fs.readFileSync('visualization_app/static/app.js','utf8');
eval(source.slice(source.indexOf('function esrChannelContext('),source.indexOf('const esrRelativeHistory')));
const context={channels:{CH2:{model:'fixture-EB',resistance_ohm:350,expected_bridge:'full',switch_verified:false,connection_state:'connected'},CH1:{connection_state:'wiring_fault'},CH3:{connection_state:'disconnected'}}};
assert(esrChannelContext(context,2).includes('350Ω'));
assert(esrChannelContext(context,2).includes('实际拨码未核验'));
assert(esrChannelContext(context,1).includes('接线损坏'));
assert(esrChannelContext(context,3).includes('未接传感器'));
assert(esrChannelContext({},2).includes('未核验'));
const html=fs.readFileSync('visualization_app/static/index.html','utf8');
assert(html.indexOf('id="esrNumericPanel"')>html.indexOf('<template id="esrDiagnosticsTemplate">'));
assert(!html.slice(html.indexOf('<main class="main-content">')).includes('id="esrNumericPanel"'));
assert(!html.includes('id="esrRelativeTrends"'));
assert(source.includes('usbOptions.append(document.getElementById("esrDiagnosticsTemplate").content.cloneNode(true))'));
assert.strictEqual(html.match(/id="esrNumericPanel"/g).length,1);
'''
        subprocess.run([shutil.which("node"), "-e", script], cwd=Path(__file__).resolve().parents[1], check=True, timeout=20)
    @unittest.skipUnless(shutil.which("node"), "需要Node验证ADC饱和提示")
    def test_sensor_cards_distinguish_adc_rails_from_valid_measurements(self):
        script = r'''
const fs=require('fs'),assert=require('assert');
const source=fs.readFileSync('visualization_app/static/app.js','utf8');
eval(source.slice(source.indexOf('function sensorCardMeasurementState('),source.indexOf('function renderSensorCardsInto(')));
for(const value of [8388607,-8388608]) {
 const channel={name:'CH2_ADC_signed',unit:'adc_counts',actual_current:value,quality:'有效',actual:[value,value]};
 const before=JSON.stringify(channel), result=sensorCardMeasurementState(channel);
 assert(result.warning);assert(result.label.includes('饱和'));assert(result.label.includes('量值不可用'));
 assert(result.detail.includes('清零不能恢复量程'));assert.strictEqual(JSON.stringify(channel),before);
}
for(const value of [0,8388606,-8388607]) {
 const result=sensorCardMeasurementState({name:'CH2_ADC_signed',unit:'adc_counts',actual_current:value});
 assert(!result.warning);assert(result.label.includes('未标定'));
}
assert.strictEqual(sensorCardMeasurementState({name:'温度1',unit:'℃',actual_current:8388607}).label,'有效');
assert.strictEqual(sensorCardMeasurementState({name:'CH2_ADC_signed',unit:'adc_counts',actual_current:null,quality:'过期'}).label,'过期');
assert(source.includes('card.classList.toggle("adc-saturated", measurement.warning)'));
'''
        subprocess.run([shutil.which("node"), "-e", script], cwd=Path(__file__).resolve().parents[1], check=True, timeout=20)

    def test_readonly_import_confirm_and_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "Config.xml"
            xml = '<System><Seting><Unit>με</Unit><Pec>0.0</Pec><PGA>6</PGA><Speed>6</Speed><SendNum>1</SendNum><FilterNum>0</FilterNum>'
            xml += ''.join(f'<K{i}>0.007456</K{i}><B{i}>420.95</B{i}>' for i in range(1, 5)) + '</Seting></System>'
            path.write_text(xml, encoding="utf-8")
            original = path.read_bytes()
            candidate = read_vendor_candidate(path)
            self.assertFalse(candidate["confirmed"])
            self.assertEqual(candidate["channels"], fixture_profile()["channels"])
            conditions = {"confirmed": True, "device_binding": {"location": "fixture"}, "gain_code": 6,
                "bridge": "quarter", "channels": [{"connected": True, "bridge": "quarter", "resistance_ohm": 120,
                    "calibration_basis": "独立参考源证书TEST，仅夹具"} for _ in range(4)]}
            profile = confirm_vendor_candidate(candidate, conditions)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(load_calibration(profile, binding=profile["device_binding"], parameters={"gain_code": 6}, bridge="quarter")[0], profile)
            with self.assertRaises(UsbProtocolError):
                load_calibration(profile, binding=profile["device_binding"], parameters={"gain_code": 5}, bridge="quarter")
            for key, replacement in (("confirmed", False), ("channels", [{"connected": False}] * 4)):
                with self.assertRaises(ValueError):
                    confirm_vendor_candidate(candidate, dict(conditions, **{key: replacement}))
            tampered = copy.deepcopy(candidate); tampered["channels"][0]["K"] = 1
            with self.assertRaises(ValueError):
                confirm_vendor_candidate(tampered, conditions)
            path.write_text(xml.replace("420.95", "420.96"), encoding="utf-8")
            with self.assertRaises(ValueError):
                confirm_vendor_candidate(candidate, conditions)

    def test_vendor_candidate_display_is_separate_from_calibration_and_model(self):
        candidate, digest = normalize_vendor_candidate(fixture_vendor_candidate())
        record = source_record()
        record["calibration_digest"] = ""
        record["vendor_candidate_digest"] = digest
        computed = measurement_record(record, None, vendor_candidate=candidate)
        expected = calibrated_values(record, candidate)
        self.assertEqual(computed["calibrated_values"], {})
        self.assertEqual(computed["relative_values"], {})
        self.assertEqual(computed["vendor_candidate_values"], expected)
        self.assertEqual(computed["display_vendor_candidate_values"]["应变2"], vendor_display(expected["应变2"]))
        self.assertEqual(computed["range_assessment"], ["range_unverified"] * 4)
        driver = Esr1001UsbDriver("fixture", vendor_candidate=candidate)
        self.assertIsNone(driver.calibration)
        self.assertEqual(driver.calibration_digest, "")
        self.assertEqual(driver.quality_metadata["vendor_candidate_state"], "available_unconfirmed")
        self.assertEqual(driver.quality_metadata["vendor_candidate_digest"], digest)
        from acquisition import AcquisitionConfig
        config = AcquisitionConfig(dataset_schema="esr1001_raw_adc", processing_mode="capture_only",
            interfaces=[{"id": "esr", "role": "esr1001_strain", "driver": "esr1001_usb_ttl",
                "endpoint": "COM91", "data_mode": "raw_adc",
                "channel_settings": {"esr_vendor_candidate": candidate}}])
        interface = config.interfaces[0]
        self.assertEqual(interface["channel_settings"]["esr_vendor_candidate"], candidate)
        self.assertEqual(interface["vendor_candidate_digest"], digest)
        self.assertIsNone(interface.get("calibration_profile"))

    def test_vendor_candidate_zero_save_replay_and_tamper_rejection(self):
        candidate, digest = normalize_vendor_candidate(fixture_vendor_candidate())
        with tempfile.TemporaryDirectory() as root:
            driver = Esr1001UsbDriver("fixture", vendor_candidate=candidate)
            driver.started = True
            driver.connection_generation = 1
            driver.latest_record = source_record()
            driver.latest_record["calibration_digest"] = ""
            driver.latest_record["vendor_candidate_digest"] = digest
            writer = RawCaptureWriter(root, {"calibration_snapshot": None, "calibration_digest": "",
                "vendor_candidate_snapshot": candidate, "vendor_candidate_digest": digest,
                "capture_generation": driver.capture_generation})
            writer.submit(dict(driver.latest_record, **measurement_record(
                driver.latest_record, None, vendor_candidate=candidate)))
            zero = self.set_zero(driver)
            self.assertEqual(zero["calibrated_values"], {})
            self.assertEqual(zero["vendor_candidate_values"], calibrated_values(driver.latest_record, candidate))
            second = source_record(2)
            second["calibration_digest"] = ""
            second["vendor_candidate_digest"] = digest
            second[RAW_CHANNELS[1]] += 10
            raw = bytearray(USB_DATA)
            raw[7:10] = (second[RAW_CHANNELS[1]] & 0xffffff).to_bytes(3, "big")
            second["raw_hex"] = raw.hex()
            second.update(measurement_record(second, None, zero, candidate))
            self.assertEqual(second["relative_values"]["CH2_ADC_relative"], 10)
            self.assertAlmostEqual(second["vendor_candidate_relative_values"]["相对应变2"], .07456)
            self.assertEqual(second["calibrated_values"], {})
            writer.submit(second)
            writer.close(driver.quality_metadata)
            rows = list(replay_source_capture(writer.path))
            self.assertAlmostEqual(rows[1]["vendor_candidate_relative_values"]["相对应变2"], .07456)
            verify_source_evidence({"record": rows[1], "calibration_snapshot": None,
                "vendor_candidate_snapshot": candidate, "capture_generation": driver.capture_generation})
            metadata = json.loads(writer.metadata_path.read_text(encoding="utf-8"))
            metadata["vendor_candidate_snapshot"]["channels"][0]["K"] = 1
            writer.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaises(ValueError):
                list(replay_source_capture(writer.path))

    def driver(self):
        driver = Esr1001UsbDriver("fixture", data_mode="microstrain")
        driver.calibration = fixture_profile()
        driver.calibration_digest = content_digest(driver.calibration)
        driver.started = True
        driver.connection_generation = 1
        driver.latest_record = source_record()
        return driver

    def set_zero(self, driver):
        return driver.set_zero(expected_sequence=driver.latest_record["receive_sequence"], connection_generation=1,
            capture_generation=driver.capture_generation, calibration_digest=driver.calibration_digest)

    def test_zero_latest_frame_no_command_no_overwrite_and_invalidation(self):
        driver = self.driver()
        zero = self.set_zero(driver)
        absolute = calibrated_values(driver.latest_record, driver.calibration)
        self.assertEqual(zero["calibrated_values"], absolute)
        self.assertEqual(zero["adc_values"], {k: driver.latest_record[k] for k in RAW_CHANNELS})
        self.assertEqual(list(driver.command_evidence), [])
        next_record = source_record(2)
        next_record[RAW_CHANNELS[0]] += 10
        computed = measurement_record(next_record, driver.calibration, zero)
        self.assertAlmostEqual(computed["relative_values"]["相对应变1"], .07456)
        self.assertEqual(computed["calibrated_values"], calibrated_values(next_record, driver.calibration))
        self.assertEqual(next_record[RAW_CHANNELS[0]], USB_EXPECTED_ADC[0] + 10)
        driver.close()
        self.assertIsNone(driver.zero)
        self.assertEqual(driver.zero_events[-1]["action"], "invalidate")
        with self.assertRaises(UsbProtocolError): self.set_zero(driver)

    def test_zero_refuses_stale_foreign_generation_and_sequence(self):
        for mutate in (lambda d: d.latest_record.update(host_monotonic=time.monotonic() - 1),
                       lambda d: setattr(d, "connection_generation", 2),
                       lambda d: setattr(d, "started", False)):
            driver = self.driver(); mutate(driver)
            with self.assertRaises(UsbProtocolError): self.set_zero(driver)
        driver = self.driver()
        with self.assertRaises(UsbProtocolError):
            driver.set_zero(expected_sequence=0, connection_generation=1, capture_generation=driver.capture_generation,
                            calibration_digest=driver.calibration_digest)

    def test_raw_zero_preserves_adc_noise_and_replays_without_calibration(self):
        with tempfile.TemporaryDirectory() as root:
            driver = self.driver()
            driver.data_mode = "raw_adc"
            driver.calibration = None
            driver.calibration_digest = ""
            driver.latest_record["calibration_digest"] = ""
            writer = RawCaptureWriter(root, {"calibration_snapshot": None, "calibration_digest": ""})
            driver.recorder = writer
            writer.submit(dict(driver.latest_record, **measurement_record(driver.latest_record, None)))
            zero = self.set_zero(driver)
            self.assertEqual(zero["calibrated_values"], {})
            self.assertEqual(zero["relative_unit"], "adc_counts")
            self.assertEqual(list(driver.command_evidence), [])
            with self.assertRaises(UsbProtocolError): self.set_zero(driver)
            for sequence, delta in ((2, 0), (3, 2573), (4, -811)):
                # CH2以外通道保持原始帧，CH2使用独立编码的已知输入。
                record = source_record(sequence)
                record["calibration_digest"] = ""
                record[RAW_CHANNELS[1]] += delta
                raw = bytearray(USB_DATA)
                raw[7:10] = (record[RAW_CHANNELS[1]] & 0xffffff).to_bytes(3, "big")
                record["raw_hex"] = raw.hex()
                record.update(measurement_record(record, None, zero))
                self.assertEqual(record["relative_values"]["CH2_ADC_relative"], delta)
                self.assertEqual(record["display_relative_values"]["CH2_ADC_relative"], str(delta))
                self.assertEqual(record["calibrated_values"], {})
                writer.submit(record)
            driver.close()
            writer.close(driver.quality_metadata)
            rows = list(replay_source_capture(writer.path))
            self.assertEqual([r["relative_values"].get("CH2_ADC_relative") for r in rows], [None, 0, 2573, -811])
            for row in rows:
                verify_source_evidence({"record": row, "calibration_snapshot": None, "capture_generation": driver.capture_generation})
            tampered = copy.deepcopy(rows[-1])
            tampered["zero_snapshot"]["calibrated_values"] = {"应变1": 0}
            with self.assertRaises(ValueError):
                verify_source_evidence({"record": tampered, "calibration_snapshot": None, "capture_generation": driver.capture_generation})

    @unittest.skipUnless(shutil.which("node"), "需要Node验证页面源观测窗口")
    def test_relative_trend_rejects_held_stale_and_foreign_zero(self):
        script = r'''
const fs = require('fs'), assert = require('assert');
const source = fs.readFileSync('visualization_app/static/app.js', 'utf8');
eval(source.slice(source.indexOf('function esrDisplayBasis('), source.indexOf('function renderEsrNumeric(')));
const history = {key: '', points: []};
const e = {capture_generation:'capture', connection_generation:1, calibration_digest:'', zero:{id:'zero'},
  latest_record:{receive_sequence:2, host_monotonic:1, zero_id:'zero', relative_values:{CH2_ADC_relative:0}}};
updateEsrRelativeHistory(history,e,true);
assert.strictEqual(history.points.length,1);
updateEsrRelativeHistory(history,e,true);
assert.strictEqual(history.points.length,1);
e.latest_record={...e.latest_record, receive_sequence:3, host_monotonic:1.1, relative_values:{CH2_ADC_relative:2573}};
updateEsrRelativeHistory(history,e,false);
assert.strictEqual(history.points.length,1);
updateEsrRelativeHistory(history,e,true);
assert.strictEqual(history.points[1].values.CH2_ADC_relative,2573);
e.latest_record={...e.latest_record,receive_sequence:4,host_monotonic:1.5};
updateEsrRelativeHistory(history,e,true);
assert.deepStrictEqual(history.points[2].values,{});
e.zero={id:'another'};
updateEsrRelativeHistory(history,e,true);
assert.strictEqual(history.points.length,0);
e.connection_generation=2;
updateEsrRelativeHistory(history,e,false);
assert.strictEqual(history.points.length,0);
'''
        subprocess.run([shutil.which("node"), "-e", script], cwd=Path(__file__).resolve().parents[1], check=True, timeout=20)

    @unittest.skipUnless(shutil.which("node"), "需要Node验证厂商候选主显示")
    def test_vendor_candidate_is_primary_four_channel_display_and_drift_is_observation_only(self):
        script = r'''
const fs = require('fs'), assert = require('assert');
const source = fs.readFileSync('visualization_app/static/app.js', 'utf8');
eval(source.slice(source.indexOf('function esrDisplayBasis('), source.indexOf('function renderEsrNumeric(')));
const history = {key: '', points: []};
const evidence = {capture_generation:'capture', connection_generation:1, calibration_digest:'',
  vendor_candidate_digest:'candidate', vendor_candidate_snapshot:{unit:'με'}, zero:null,
  latest_record:{receive_sequence:1, host_monotonic:1,
    vendor_candidate_values:{应变1:10.04,应变2:-18640.14,应变3:-90.46,应变4:59.44},
    display_vendor_candidate_values:{应变1:'10.0',应变2:'-18640.1',应变3:'-90.5',应变4:'59.4'}}};
let basis = esrDisplayBasis(evidence);
assert.strictEqual(basis.unit, 'με');
assert.strictEqual(basis.title, '厂商候选应变');
assert.strictEqual(basis.display(2), '-18640.1');
updateEsrRelativeHistory(history,evidence,true);
assert.strictEqual(esrObservedRange(history.points,basis.key(2)).ready,false);
evidence.latest_record={...evidence.latest_record,receive_sequence:2,host_monotonic:2,
  vendor_candidate_values:{应变1:10.14,应变2:-18639.94,应变3:-90.36,应变4:59.54}};
updateEsrRelativeHistory(history,evidence,true);
assert.deepStrictEqual(history.points.map(p => p.values['应变2']).filter(Number.isFinite),[-18640.14,-18639.94]);
assert.strictEqual(history.points.at(-1).values['CH2_ADC_relative'], undefined);
const range=esrObservedRange(history.points,basis.key(2));
assert.strictEqual(range.ready,true);
assert.strictEqual(range.count,2);
assert.strictEqual(range.minimum,-18640.14);
assert.strictEqual(range.maximum,-18639.94);
evidence.zero={id:'zero'};
evidence.latest_record={...evidence.latest_record,receive_sequence:3,host_monotonic:3,zero_id:'zero',
  vendor_candidate_relative_values:{相对应变1:.1,相对应变2:.2,相对应变3:.1,相对应变4:.1},
  display_vendor_candidate_relative_values:{相对应变1:'0.1',相对应变2:'0.2',相对应变3:'0.1',相对应变4:'0.1'}};
basis=esrDisplayBasis(evidence);
assert.strictEqual(basis.title, '厂商候选相对应变');
assert.strictEqual(basis.display(2), '0.2');
updateEsrRelativeHistory(history,evidence,true);
assert.deepStrictEqual(history.points.map(p => p.values['相对应变2']),[.2]);
assert.deepStrictEqual(evidence.latest_record.vendor_candidate_values,
  {应变1:10.14,应变2:-18639.94,应变3:-90.36,应变4:59.54});
'''
        subprocess.run([shutil.which("node"), "-e", script], cwd=Path(__file__).resolve().parents[1], check=True, timeout=20)

    def test_display_rounding_only_and_unbounded_measurement(self):
        for value, text in ((1.25, "1.3"), (-1.25, "-1.3"), (1.249999999, "1.2"),
                            (-1.249999999, "-1.2"), (.05, "0.1"), (-.05, "-0.1")):
            self.assertEqual(vendor_display(value), text)
        record = source_record()
        record[RAW_CHANNELS[0]] = -8388608
        profile = fixture_profile()
        profile["version"] = 2
        profile["channel_conditions"] = [{"range_microstrain": [-100, 100], "range_basis": "测试量程证书", "reference_input": "测试已知输入"}] * 4
        values = measurement_record(record, profile)
        self.assertLess(values["calibrated_values"]["应变1"], -10000)
        self.assertTrue(values["adc_at_rail"][0])
        self.assertEqual(values["range_assessment"][0], "outside_reference_range")
        self.assertEqual(measurement_record(record, None)["range_assessment"], ["range_unverified"] * 4)

    @unittest.skipUnless(sys.platform == "win32", "需要Windows .NET Framework数字格式化")
    def test_same_adc_matches_dotnet_framework_vendor_formula_and_rounding(self):
        script = r'''
$culture=[Globalization.CultureInfo]::InvariantCulture
$rows=@(-8388608,-7149977,-9254,-1,0,1,68794,8388607) | ForEach-Object {
  $adc=[double]$_; $act=$adc*0.007456+420.95; $zero=[double](-9254)*0.007456+420.95; $show=$act-$zero
  @{adc=$adc;act=$act;show=$show;text=$show.ToString('0.0',$culture)}
}
@{rows=@($rows);rounding=@(@(1.25,-1.25,1.249999999,-1.249999999,0.05,-0.05) | ForEach-Object {([double]$_).ToString('0.0',$culture)})} | ConvertTo-Json -Depth 4 -Compress
'''
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20, check=True)
        reference = json.loads(result.stdout)
        profile = fixture_profile()
        zero = {"id": "fixture", "calibrated_values": {f"应变{i}": -9254 * .007456 + 420.95 for i in range(1, 5)}}
        for row in reference["rows"]:
            record = {name: int(row["adc"]) for name in RAW_CHANNELS}
            computed = measurement_record(record, profile, zero)
            self.assertAlmostEqual(computed["calibrated_values"]["应变1"], row["act"], places=9)
            self.assertAlmostEqual(computed["relative_values"]["相对应变1"], row["show"], places=9)
            self.assertEqual(computed["display_relative_values"]["相对应变1"], row["text"])
        self.assertEqual([vendor_display(x) for x in (1.25,-1.25,1.249999999,-1.249999999,.05,-.05)], reference["rounding"])

    def test_capture_replay_and_mysql_source_evidence_use_historical_snapshot(self):
        from acquisition import SimulatorDriver
        with tempfile.TemporaryDirectory() as root:
            driver = self.driver()
            writer = RawCaptureWriter(root, {"calibration_snapshot": driver.calibration, "calibration_digest": driver.calibration_digest})
            driver.recorder = writer
            first = dict(driver.latest_record, **measurement_record(driver.latest_record, driver.calibration))
            writer.submit(first)
            zero = self.set_zero(driver)
            second = source_record(2)
            second.update(measurement_record(second, driver.calibration, zero))
            writer.submit(second)
            driver.close()
            writer.close(driver.quality_metadata)
            rows = list(replay_source_capture(writer.path))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(v == 0 for v in rows[1]["relative_values"].values()))
            self.assertTrue(writer.path.with_suffix(".events.jsonl").is_file())
            for row in rows:
                verify_source_evidence({"record": row, "calibration_snapshot": driver.calibration, "capture_generation": driver.capture_generation})
            replay = SimulatorDriver(writer.path, ["应变2"])
            replay.open()
            self.assertEqual(replay.read_sample()["应变2"], calibrated_values(first, fixture_profile())["应变2"])
            replay.close()
            metadata = json.loads(writer.metadata_path.read_text(encoding="utf-8"))
            metadata["calibration_snapshot"]["channels"][0]["K"] = 1
            writer.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self.assertRaises(ValueError): list(replay_source_capture(writer.path))

    def test_stopped_status_is_historical_and_zero_command_is_rejected(self):
        from acquisition import AcquisitionManager, AcquisitionConfig
        from acquisition_start import AcquisitionStartMixin
        with tempfile.TemporaryDirectory() as root:
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            manager.config = AcquisitionConfig(dataset_schema="esr1001_raw_adc", acquisition_mode="real",
                interfaces=[{"id": "esr", "role": "esr1001_strain", "driver": "esr1001_usb_ttl", "endpoint": "COM91", "physical_interface_id": "serial:COM91", "physical_interface_kind": "serial", "data_mode": "raw_adc", "enabled": True}], processing_mode="capture_only")
            manager.channel_observed[RAW_CHANNELS[0]] = 3
            manager.capture_phase = "completed"; manager.finalization_complete = True
            manager._start_job = {"state": "running", "started_monotonic": time.monotonic() - 10}
            status = manager.status()
            self.assertFalse(status["running"])
            self.assertEqual(status["start_operation"]["state"], "completed")
            self.assertFalse(any("正在采集" in x["message"] for x in status["interfaces"]))
            with self.assertRaises(ValueError): manager.esr_numeric_action({"action": "zero"})

    def test_mysql_writes_source_evidence_to_each_target_and_rejects_loss(self):
        from acquisition import AcquisitionConfig
        from mysql_storage import MySQLCaptureStore, MySQLSettings
        from test_mysql_identity import _FakeConnection
        record = source_record()
        candidate, candidate_digest = normalize_vendor_candidate(fixture_vendor_candidate())
        record["vendor_candidate_digest"] = candidate_digest
        record.update(measurement_record(record, fixture_profile(), vendor_candidate=candidate))
        evidence = {"interface_id": "esr", "record": record, "calibration_snapshot": fixture_profile(),
            "vendor_candidate_snapshot": candidate, "capture_generation": "fixture",
            "measurement_context": {"source": "operator", "channels": {"CH2": {"switch_verified": False}}}}
        config = AcquisitionConfig(specimen_id="fixture", selected_sensors=["温度1"])
        for target in ("local_target", "server_target"):
            store = MySQLCaptureStore(MySQLSettings(enabled=True, database=target))
            connection = _FakeConnection()
            with patch.object(store, "_connect", return_value=("fake", connection)):
                result = store.save_layer(config, rows=[{"温度1": 1}], layer_file=None, full_specimen_file=None,
                    timestamp_file=None, folder_path=None, summary={}, source_records=[evidence])
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["saved_source_rows"], 1)
            inserted = [values for sql, values in connection.cursor_instance.executed if sql.startswith("INSERT INTO afp_source_evidence")]
            saved = json.loads(inserted[0][0][-1])
            self.assertEqual(saved["record"]["calibrated_values"], record["calibrated_values"])
            self.assertEqual(saved["record"]["vendor_candidate_values"], record["vendor_candidate_values"])
            self.assertEqual(saved["vendor_candidate_snapshot"], candidate)
            self.assertEqual(saved["calibration_snapshot"], fixture_profile())
            self.assertEqual(saved["measurement_context"], evidence["measurement_context"])
        store = MySQLCaptureStore(MySQLSettings(enabled=True))
        with patch.object(store, "_connect", return_value=("fake", _FakeConnection())):
            result = store.save_layer(config, rows=[], layer_file=None, full_specimen_file=None,
                timestamp_file=None, folder_path=None, summary={"data_quality": {"esr1001_usb": {"esr": {"saved_source_rows": 1}}}}, source_records=[])
        self.assertFalse(result["ok"])

    def test_helper_ack_preserves_all_source_frames_before_finalization(self):
        from acquisition import AcquisitionManager, AcquisitionConfig
        from local_capture_agent import LocalCaptureAgent
        with tempfile.TemporaryDirectory() as root:
            candidate, candidate_digest = normalize_vendor_candidate(fixture_vendor_candidate())
            writer = RawCaptureWriter(root, {"vendor_candidate_snapshot": candidate,
                "vendor_candidate_digest": candidate_digest, "capture_generation": "helper-fixture"})
            for sequence in range(1, 202):
                record = source_record(sequence); record["calibration_digest"] = ""
                record["vendor_candidate_digest"] = candidate_digest
                record.update(measurement_record(record, None, vendor_candidate=candidate)); writer.submit(record)
            writer.close({})
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            manager.config = AcquisitionConfig(dataset_schema="esr1001_raw_adc", acquisition_mode="real",
                interfaces=[{"id": "esr", "role": "esr1001_strain", "driver": "esr1001_usb_ttl", "endpoint": "COM91", "physical_interface_id": "serial:COM91", "physical_interface_kind": "serial", "data_mode": "raw_adc"}], processing_mode="capture_only")
            manager.driver = SimpleNamespace(workers=[], interface_quality_metadata={"esr": {"driver": "esr1001_usb_ttl",
                "raw_file": str(writer.path), "saved_source_rows": 201, "calibration_snapshot": None,
                "vendor_candidate_snapshot": candidate, "vendor_candidate_digest": candidate_digest,
                "capture_generation": "helper-fixture",
                "measurement_context": {"source": "operator", "channels": {"CH2": {"switch_verified": False}}}}})
            manager.finalization_complete = True
            agent = LocalCaptureAgent(manager=manager, simulation_cache_root=Path(root))
            agent._capture_uuid = "fixture"
            agent._local_stopped_at = time.time(); agent._flush_state = "flushing"
            first = agent.next_sample_batch()
            self.assertEqual(len(first["esr_source_records"]), 200)
            self.assertFalse(first["esr_source_records"][0]["measurement_context"]["channels"]["CH2"]["switch_verified"])
            self.assertEqual(first["esr_source_records"][0]["vendor_candidate_snapshot"], candidate)
            self.assertTrue(first["esr_source_records"][0]["record"]["vendor_candidate_values_json"])
            self.assertEqual(first, agent.next_sample_batch())
            self.assertFalse(first["status"]["finalization_complete"])
            self.assertTrue(agent.ack_sample_batch("fixture", first["sequence"]))
            self.assertEqual(agent._flush_state, "flushing")
            last = agent.next_sample_batch()
            self.assertEqual(len(last["esr_source_records"]), 1)
            self.assertEqual(last["esr_source_records"][0]["record"]["receive_sequence"], "201")
            self.assertTrue(agent.ack_sample_batch("fixture", last["sequence"]))
            self.assertEqual(agent._flush_state, "completed")


if __name__ == "__main__":
    unittest.main()
