import json
import io
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import acquisition
import uvc_windows as w


class WindowsUvcTests(unittest.TestCase):
    def test_transport_records_preserve_boundaries_and_reject_truncation(self):
        records = struct.pack('<I', 3) + b'abc' + struct.pack('<I', 2) + b'de'
        self.assertEqual(list(w.iter_raw_samples(io.BytesIO(records))), [b'abc', b'de'])
        self.assertEqual(list(w.iter_raw_samples(io.BytesIO())), [])
        for raw, error in [(b'\x01', 'length'), (bytes(4), 'length'),
                           (struct.pack('<I', 17*1024*1024), 'length'),
                           (struct.pack('<I', 4)+b'ab', 'payload')]:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, error):
                list(w.iter_raw_samples(io.BytesIO(raw)))

    def test_physical_constraints_reject_moved_device_with_same_serial(self):
        current = {'root_instance_id': 'USB\\target', 'parent_hub_instance_id': 'USB\\hub',
                   'port_number': 4, 'location_paths': ['ROOT#USB(9)#USB(4)']}
        w.validate_target_location(dict(current), current)
        for field, value in [('root_instance_id', 'USB\\other'), ('parent_hub_instance_id', 'USB\\other'),
                             ('port_number', 3), ('port_number', True),
                             ('location_paths', ['ROOT#USB(9)#USB(3)'])]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                w.validate_target_location({field: value}, current)

    def test_moved_target_rejected_before_hub_or_camera_access(self):
        records = self.records()
        root = records['USB\\VID_2BDF&PID_0102\\B']
        root.parent_instance_id = 'USB\\hub'
        root.location_paths = ('ROOT#USB(9)#USB(3)',)
        api = SimpleNamespace(enumerate_devices=lambda: records,
                              enumerate_hub_interfaces=lambda: [{'instance_id': 'USB\\hub', 'device_path': 'hub'}])
        with patch('windows_usb_topology._WindowsApi', return_value=api):
            with self.assertRaisesRegex(ValueError, 'port_number'):
                w.read_target_descriptor({'serial': 'B', 'container_id': 'two', 'port_number': 4})

    def test_manager_uses_driver_state_service_for_windows_diagnostics(self):
        config=acquisition.AcquisitionConfig(acquisition_mode='real',real_acquisition_mode='local_direct',
            driver='multi_interface',dataset_schema='new_collection_v13_0',processing_mode='capture_only',
            selected_sensors=['ROI平均温度'],interfaces=[{'id':'uvc','role':'thermal_uvc','driver':'uvc_thermal',
            'enabled':True,'physical_verified':True,'physical_interface_id':'uvc:target',
            'physical_interface_kind':'usb_uvc','device_identity':{'container_id':'one'},
            'driver_state':{'service':'usbvideo'}}])
        manager=acquisition.AcquisitionManager(capture_root=Path('D:/AFP_Capture'),recovery_scan=False)
        with patch.object(acquisition,'probe_windows_uvc',return_value={'status':'temperature_format_unconfirmed','temperature_ready':False}) as probe:
            result=manager.test_connection(config)
        self.assertEqual(probe.call_count,1)
        interface=result['interfaces'][0]
        self.assertEqual(interface['state'],'device_profile_incomplete')
        self.assertFalse(interface['driver_diagnostics']['temperature_ready'])

    def descriptor(self):
        raw=bytes.fromhex('09020000020104803209040000010e010005'
                          '1d24060a41769ea204dee3478b2bf4341aff003b0f010204ff03000000'
                          '0705830310000809040100010e0200060b24060203000100000000'
                          '07058102000204')
        raw=bytearray(raw);struct.pack_into('<H',raw,2,len(raw));return bytes(raw)

    def test_xu_only_in_video_control_interface_not_mjpeg(self):
        result=w.parse_configuration_descriptor(self.descriptor())
        self.assertEqual(result['extension_units'],[{'interface_number':0,'unit_id':10,
                         'guid':'a29e7641-de04-47e3-8b2b-f4341aff003b','num_controls':15,'control_bitmap':'ff030000'}])
        self.assertEqual(result['endpoints'][-1]['address'],0x81)
        for raw in [self.descriptor()[:-1],bytes(9),self.descriptor()+b'\x00\x00']:
            with self.assertRaises(ValueError):w.parse_configuration_descriptor(raw)

    def records(self):
        records={}
        for serial,container in [('A','one'),('B','two')]:
            root='USB\\VID_2BDF&PID_0102\\'+serial
            cam='USB\\VID_2BDF&PID_0102&MI_00\\'+serial
            records[root]=SimpleNamespace(instance_id=root,service='usbccgp')
            records[cam]=SimpleNamespace(instance_id=cam,parent_instance_id=root,service='usbvideo',container_id=container)
        return records

    def test_identity_fields_all_constrain_target_no_first_camera(self):
        camera,root=w.resolve_target({'serial':'B','container_id':'{two}'},self.records())
        self.assertTrue(root.instance_id.endswith('B'))
        for identity in [{},{'serial':'B','container_id':'one'},{'instance_id':camera.instance_id,'serial':'A'}]:
            with self.assertRaises(ValueError):w.resolve_target(identity,self.records())

    def test_two_same_model_cameras_keep_separate_topology(self):
        topology={'devices':[{'id':'a','vid':0x2bdf,'pid':0x102,'container_id':'one','parent_port_id':'port1'},
                             {'id':'b','vid':0x2bdf,'pid':0x102,'container_id':'two','parent_port_id':'port2'}]}
        cameras=[{'vid':0x2bdf,'pid':0x102,'instance_id':'cam-b','container_id':'two'},
                 {'vid':0x2bdf,'pid':0x102,'instance_id':'cam-a','container_id':'one'}]
        acquisition._merge_camera_pnp_with_usb_topology(topology,cameras)
        self.assertEqual([c['instance_id'] for c in topology['devices']],['cam-a','cam-b'])
        self.assertEqual(cameras[0]['parent_port_id'],'port2')

    def test_unmatched_ambiguous_camera_does_not_gain_identity(self):
        topology={'devices':[{'id':'a','vid':0x2bdf,'pid':0x102,'container_id':'one'},
                             {'id':'b','vid':0x2bdf,'pid':0x102,'container_id':'two'}]}
        acquisition._merge_camera_pnp_with_usb_topology(topology,[{'vid':0x2bdf,'pid':0x102,'instance_id':'unknown'}])
        self.assertNotIn('instance_id',topology['devices'][0])

    def test_probe_reports_timeout_reaped_and_never_temperature_ready(self):
        descriptor={'identity':{'instance_id':'target'},'extension_units':[{'guid':'guid'}]}
        with patch.object(w.sys,'platform','win32'),patch.object(w,'read_target_descriptor',return_value=descriptor), \
             patch.object(Path,'is_file',return_value=True),patch.object(Path,'read_bytes',return_value=b'fixture'), \
             patch.object(w.subprocess,'run',side_effect=subprocess.TimeoutExpired('probe',8)):
            result=w.probe_windows_uvc({'serial':'B'},executable=Path('fixture'))
        self.assertEqual(result['status'],'native_process_timeout')
        self.assertTrue(result['process_reaped'])
        self.assertFalse(result['temperature_ready'])

    def test_probe_xu_success_does_not_approve_profile_or_roi(self):
        descriptor={'identity':{'instance_id':'target'},'extension_units':[{'guid':'guid'}]}
        completed=SimpleNamespace(returncode=0,stdout=json.dumps({'status':'probed'}).encode(),stderr=b'')
        with patch.object(w.sys,'platform','win32'),patch.object(w,'read_target_descriptor',return_value=descriptor), \
             patch.object(Path,'is_file',return_value=True),patch.object(Path,'read_bytes',return_value=b'fixture'), \
             patch.object(w.subprocess,'run',return_value=completed):
            result=w.probe_windows_uvc({'serial':'B'},executable=Path('fixture'))
        self.assertFalse(result['temperature_ready'])
        self.assertEqual(result['status'],'temperature_format_unconfirmed')


if __name__=='__main__':unittest.main()
