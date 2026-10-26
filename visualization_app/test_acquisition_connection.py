"""Connection reuse and capture boundaries without physical hardware."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
from acquisition_clock import monotonic
import unittest
from unittest.mock import patch

import acquisition as a
from test_acquisition_start import config
from test_m3232_pressure import driver_double, ZERO_FRAME


class ConnectionTests(unittest.TestCase):
    def wait_ready(self, manager):
        manager._connection_thread.join(3)
        self.assertTrue(manager.connection_status()["ready"], manager.connection_status())

    def test_standby_has_no_files_or_capture_and_start_stop_reuses_workers(self):
        class Device:
            opened = closed = 0
            protocol_verified = True
            quality_metadata = {}
            def __init__(self, *args):
                pass
            def open(self):
                Device.opened += 1
            def close(self):
                Device.closed += 1
            def read_sample(self):
                return {"温度": 10., "压力": 20.}

        with tempfile.TemporaryDirectory() as root, patch.object(a, "SerialJsonDriver", Device):
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            manager.connect_data(cfg)
            self.wait_ready(manager)
            self.assertEqual(list(Path(root).rglob('*')), [])
            self.assertEqual(manager.total_sample_count, 0)
            self.assertFalse(manager.capture_uuid)
            driver = manager._connection_driver
            workers = list(driver.workers)
            try:
                for layer in range(2):
                    current = deepcopy(cfg)
                    current.layer = layer
                    manager.start(current)
                    boundary = manager._capture_start_monotonic
                    self.assertIs(manager.driver, driver)
                    self.assertEqual(manager.driver.workers, workers)
                    self.assertTrue(all(s.received_monotonic >= boundary for s in driver.cache.events()))
                    stopped = manager.stop()
                    self.assertTrue(stopped['finalization_complete'], stopped['last_error'])
                    self.assertTrue(stopped['capture_saved'])
                    count = manager.total_sample_count
                    time.sleep(.03)
                    self.assertEqual(manager.total_sample_count, count)
                self.assertEqual(Device.opened, 2)
                self.assertEqual(Device.closed, 0)
            finally:
                manager.disconnect_data()
            self.assertGreaterEqual(Device.closed, 2)

    def test_missing_and_changed_connection_refuse_start(self):
        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            cfg._require_data_connection = True
            with self.assertRaisesRegex(RuntimeError, 'data_connection_not_ready'):
                manager.start(cfg)
            self.assertFalse(manager.capture_uuid)

    def test_stop_while_quality_unready_preserves_receivers_until_recovery(self):
        class Device:
            opened = closed = 0
            protocol_verified = True
            quality_metadata = {}
            def __init__(self, *args): pass
            def open(self): Device.opened += 1
            def close(self): Device.closed += 1
            def read_sample(self): return {"温度": 10., "压力": 20.}

        with tempfile.TemporaryDirectory() as root, patch.object(a, 'SerialJsonDriver', Device):
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            try:
                manager.connect_data(cfg)
                self.wait_ready(manager)
                manager.start(deepcopy(cfg))
                workers = list(manager._connection_driver.workers)
                manager._connection_boundary_grace = 0
                with patch.object(manager, '_connection_health', return_value='设备新鲜度暂未满足要求'):
                    manager._refresh_connected_readiness()
                    self.assertEqual(manager.connection_status()['state'], 'waiting_data')
                    stopped = manager.stop()
                    self.assertTrue(stopped['capture_saved'], stopped['last_error'])
                    self.assertTrue(stopped['finalization_complete'])
                    self.assertEqual(manager._connection_driver.workers, workers)
                    self.assertEqual(Device.closed, 0)
                    self.assertFalse(manager.connection_status()['ready'])
                    with self.assertRaisesRegex(RuntimeError, 'data_connection_not_ready'):
                        manager.require_data_connection(cfg)
                deadline = monotonic()+3
                while not manager.connection_status()['ready'] and monotonic()<deadline:
                    time.sleep(.01)
                self.assertTrue(manager.connection_status()['ready'])
                cfg.layer += 1
                manager.start(deepcopy(cfg))
                self.assertEqual(manager._connection_driver.workers, workers)
                self.assertEqual(Device.opened, 2)
                self.assertTrue(manager.stop()['capture_saved'])
            finally:
                manager.disconnect_data()

    def test_matrix_boundary_drops_complete_standby_and_crossing_half_frame(self):
        driver, port = driver_double()
        driver.open()
        port.data = ZERO_FRAME + ZERO_FRAME[:300]
        self.assertEqual(len(driver.read_samples()), 1)
        port.data = ZERO_FRAME
        driver.begin_capture_boundary()
        self.assertEqual(port.data, b'')
        port.data = ZERO_FRAME[300:] + ZERO_FRAME
        result = driver.read_samples()
        self.assertEqual(len(result), 1)
        self.assertEqual(driver.total_frames, 1)
        driver.clock = lambda: driver.started + 2.
        self.assertEqual(driver.quality_metadata['valid_frame_rate_hz'], .5)
        self.assertEqual(port.writes, [b'begin\n'])
        driver.close()

    def test_config_change_while_opening_cannot_restore_ready(self):
        entered, release = threading.Event(), threading.Event()
        class Device:
            protocol_verified = True
            quality_metadata = {}
            def __init__(self, *args): pass
            def open(self):
                entered.set()
                release.wait(2)
            def close(self): pass
            def read_sample(self): return {"温度": 10., "压力": 20.}
        with tempfile.TemporaryDirectory() as root, patch.object(a, 'SerialJsonDriver', Device):
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            try:
                manager.connect_data(config(root))
                self.assertTrue(entered.wait(2))
                manager.invalidate_data_connection('设备配置已变化')
                release.set()
                manager._connection_thread.join(3)
                self.assertFalse(manager.connection_status()['ready'])
                self.assertEqual(manager.connection_status()['state'], 'fault')
                self.assertEqual(list(Path(root).rglob('*')), [])
            finally:
                release.set()
                manager.disconnect_data()

    def test_config_change_invalidates_current_readiness(self):
        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            from real_acquisition import readiness_config_fingerprint
            for state in ['connected', 'waiting_data', 'connecting']:
                original = deepcopy(cfg)
                manager._connection.update(state=state, ready=state == 'connected',
                    fingerprint=readiness_config_fingerprint(original))
                changed = deepcopy(original)
                changed.interfaces[0]['baudrate'] = 9600
                with self.assertRaisesRegex(RuntimeError, 'data_connection_not_ready'):
                    manager.require_data_connection(changed)
                self.assertFalse(manager.connection_status()['ready'])
                self.assertEqual(manager.connection_status()['state'], 'fault')

    def test_esr_boundary_preserves_waits_and_rejects_old_pending_and_half_frame(self):
        from esr1001_usb import Esr1001UsbDriver, RAW_CHANNELS
        from esr1001_fixtures import USB_DATA
        from test_esr1001_usb import SerialStub
        device = Esr1001UsbDriver('COM_TEST')
        device.serial = SerialStub()
        device.serial.reset_input_buffer = device.serial.buffer.clear
        device.started = True
        device.started_monotonic = monotonic()
        device.last_observation = monotonic()-.08
        device.recent_intervals.extend([.08]*3)
        device._pending.append({'old': True})
        device.parser.buffer.extend(USB_DATA[:5])
        device.parser.synced = True
        device.serial.buffer.extend(USB_DATA[5:] + USB_DATA + USB_DATA[:5])
        device.begin_capture_boundary()
        self.assertFalse(device._pending)
        self.assertFalse(device.parser.buffer)
        device.serial.buffer.extend(USB_DATA[5:] + USB_DATA * 4)
        result = device.read_samples()
        self.assertEqual(len(result), 3)
        self.assertTrue(all(set(values) == set(RAW_CHANNELS) for values, _ in result))
        self.assertEqual(device.serial.writes, [])
        self.assertEqual(device.startup_policy, 'fixed')
        self.assertEqual(device.parser.discarded_bytes, 0)
        self.assertFalse(device.quality_metadata['current_stream_stable'])
        device.serial.buffer.extend(USB_DATA)
        device.read_samples()
        self.assertTrue(device.quality_metadata['current_stream_stable'])

    def test_disconnect_preserves_saved_source_counts_and_export_paths(self):
        from types import SimpleNamespace
        driver = a.MultiInterfaceDriver([], ['温度'])
        device = SimpleNamespace(quality_metadata={'driver': 'esr1001_usb_ttl', 'source_frames': 900, 'stop_error': ''})
        worker = SimpleNamespace(interface_id='esr', driver=device, stop=lambda **_: True,
            status=lambda: {'interface_id': 'esr', 'running': False})
        driver.workers = [worker]
        driver.interface_quality_metadata = {'esr': {'raw_file': 'capture.csv', 'source_frames': 12, 'saved_source_rows': 12}}
        driver.close()
        self.assertEqual(driver.interface_quality_metadata['esr']['raw_file'], 'capture.csv')
        self.assertEqual(driver.interface_quality_metadata['esr']['source_frames'], 12)
        self.assertEqual(driver.interface_quality_metadata['esr']['saved_source_rows'], 12)

    def test_health_does_not_compare_a_new_publication_to_an_older_clock_read(self):
        from types import SimpleNamespace
        from real_acquisition import ChannelSample
        clock = [100.]
        def publish():
            clock[0] += .01
            return {name: ChannelSample(interface_id=identity, channel_name=name, value=1.,
                source_sequence=1, protocol_ok=True, received_monotonic=clock[0])
                for identity, name in [('one', '温度'), ('two', '压力')]}
        cache = SimpleNamespace(snapshot=publish, latest=lambda name: publish()[name])
        states = [{'interface_id': identity, 'running': True, 'state': 'running', 'reconnect_attempts': 0,
                   'opened_monotonic': 99., 'last_error': ''} for identity in ['one', 'two']]
        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            manager._connection_config = config(root)
            manager._connection_driver = SimpleNamespace(cache=cache, workers=[object()], worker_status=lambda: states)
            with patch('acquisition_connection.monotonic', side_effect=lambda: clock[0]):
                self.assertEqual(manager._connection_health(), '')

    def test_quality_recovery_requires_two_new_observations_and_keeps_sampling_policy(self):
        from types import SimpleNamespace
        from real_acquisition import ChannelSample, ChannelSampleCache
        clock = [100.]
        cache = ChannelSampleCache()
        states = [{'interface_id': i, 'running': True, 'state': 'running', 'reconnect_attempts': 0,
                   'opened_monotonic': 90., 'last_error': ''} for i in ['one','two']]
        def publish(sequence, stable):
            for identity, name in [('one','温度'),('two','压力')]:
                cache.publish(ChannelSample(channel_name=name, interface_id=identity, value=1.,
                    received_monotonic=clock[0], source_sequence=sequence, protocol_ok=True,
                    metadata={'current_stream_stable': stable}))
        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            manager._connection_config = config(root)
            manager._connection_driver = SimpleNamespace(cache=cache, workers=[object()], worker_status=lambda: states)
            manager._connection.update(state='connected', ready=True, ready_attempts={'one':0,'two':0})
            with patch('acquisition_connection.monotonic', side_effect=lambda:clock[0]):
                publish(1,False);manager._refresh_connected_readiness()
                self.assertEqual(manager.connection_status()['state'],'waiting_data')
                self.assertFalse(manager.stop_event.is_set())
                clock[0]=100.1;publish(2,True);manager._refresh_connected_readiness()
                self.assertFalse(manager.connection_status()['ready'])
                clock[0]=100.2;publish(3,True);manager._refresh_connected_readiness()
                self.assertTrue(manager.connection_status()['ready'])
                states[0]['reconnect_attempts']=1;manager._refresh_connected_readiness()
                self.assertEqual(manager.connection_status()['state'],'fault')
                clock[0]=100.3;publish(4,True);manager._refresh_connected_readiness()
                self.assertFalse(manager.connection_status()['ready'])


if __name__ == '__main__':
    unittest.main()
