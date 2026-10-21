"""保留 usbvideo 的目标身份、USB 描述符及进程隔离 XU 诊断。"""
from __future__ import annotations

import ctypes
import hashlib
import json
import re
import struct
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import BinaryIO, Iterator


_probe_lock = threading.Lock()


def iter_raw_samples(stream: BinaryIO, *, max_sample_bytes: int = 16 * 1024 * 1024) -> Iterator[bytes]:
    """读取探针的 uint32小端长度+载荷记录；不把回调序号解释成源序号。"""
    while True:
        size_bytes = stream.read(4)
        if not size_bytes:
            return
        if len(size_bytes) != 4:
            raise ValueError('truncated_raw_sample_length')
        size = struct.unpack('<I', size_bytes)[0]
        if not 0 < size <= max_sample_bytes:
            raise ValueError('invalid_raw_sample_length')
        payload = bytearray()
        while len(payload) < size:
            chunk = stream.read(size - len(payload))
            if not chunk:
                raise ValueError('truncated_raw_sample_payload')
            payload.extend(chunk)
        yield bytes(payload)


def validate_target_location(identity: dict, current: dict) -> None:
    """已有物理约束必须同时成立；移动后须重新发现并选择。"""
    for key in ('root_instance_id', 'parent_hub_instance_id'):
        if identity.get(key) and str(identity[key]).casefold() != str(current.get(key, '')).casefold():
            raise ValueError('uvc_target_location_changed:' + key)
    if identity.get('port_number') is not None:
        port = identity['port_number']
        if isinstance(port, bool) or not str(port).isdigit() or int(port) != current['port_number']:
            raise ValueError('uvc_target_location_changed:port_number')
    if identity.get('location_paths'):
        expected = {str(p).casefold() for p in identity['location_paths']}
        actual = {str(p).casefold() for p in current['location_paths']}
        if not expected.intersection(actual):
            raise ValueError('uvc_target_location_changed:location_paths')


def parse_configuration_descriptor(raw: bytes) -> dict:
    if len(raw) < 9 or raw[1] != 2 or struct.unpack_from('<H', raw, 2)[0] != len(raw):
        raise ValueError('invalid_usb_configuration_length')
    interfaces, units, endpoints = [], [], []
    current = None
    pos = 0
    while pos < len(raw):
        if pos + 2 > len(raw) or raw[pos] < 2 or pos + raw[pos] > len(raw):
            raise ValueError('truncated_usb_descriptor')
        d = raw[pos:pos+raw[pos]]
        if d[1] == 4:
            if len(d) < 9:
                raise ValueError('invalid_usb_interface_descriptor')
            current = {'number':d[2], 'alternate':d[3], 'class':d[5], 'subclass':d[6]}
            interfaces.append(current)
        elif d[1] == 0x24 and current and current['class'] == 14 and current['subclass'] == 1 and len(d) >= 3 and d[2] == 6:
            if len(d) < 24 or len(d) < 23 + d[21]:
                raise ValueError('invalid_xu_descriptor')
            control_size_index = 22 + d[21]
            if len(d) <= control_size_index:
                raise ValueError('truncated_xu_controls')
            control_size = d[control_size_index]
            bitmap_start = control_size_index + 1
            if len(d) < bitmap_start + control_size + 1:
                raise ValueError('truncated_xu_controls')
            units.append({'interface_number':current['number'], 'unit_id':d[3],
                          'guid':str(uuid.UUID(bytes_le=d[4:20])), 'num_controls':d[20],
                          'control_bitmap':d[bitmap_start:bitmap_start+control_size].hex()})
        elif d[1] == 5:
            if len(d) < 7 or current is None:
                raise ValueError('invalid_endpoint_descriptor')
            endpoints.append({'interface_number':current['number'], 'address':d[2],
                              'attributes':d[3], 'max_packet_size':struct.unpack_from('<H', d, 4)[0]})
        pos += len(d)
    return {'interfaces':interfaces, 'extension_units':units, 'endpoints':endpoints,
            'configuration_sha256':hashlib.sha256(raw).hexdigest()}


def resolve_target(identity: dict, records) -> tuple:
    """身份字段同时约束候选；重定位由调用方刷新选择后重新检查。"""
    wanted_instance = str(identity.get('instance_id') or identity.get('pnp_instance_id') or '').casefold()
    wanted_serial = str(identity.get('serial') or '').casefold()
    wanted_container = str(identity.get('container_id') or '').strip('{}').casefold()
    if not (wanted_instance or wanted_serial or wanted_container):
        raise ValueError('uvc_target_identity_required')
    cameras = [r for r in records.values() if r.service.casefold() == 'usbvideo'
               and 'VID_2BDF&PID_0102&MI_' in r.instance_id.upper()]
    matches = []
    for camera in cameras:
        root = records.get(camera.parent_instance_id.upper())
        if root is None:
            continue
        serial = root.instance_id.rsplit('\\', 1)[-1]
        if wanted_instance and camera.instance_id.casefold() != wanted_instance:
            continue
        if wanted_serial and serial.casefold() != wanted_serial:
            continue
        if wanted_container and camera.container_id.strip('{}').casefold() != wanted_container:
            continue
        matches.append((camera, root))
    if len(matches) != 1:
        raise ValueError('uvc_target_identity_missing_or_ambiguous')
    return matches[0]


def read_target_descriptor(identity: dict) -> dict:
    from windows_usb_topology import _WindowsApi, _ctl_code
    api = _WindowsApi()
    camera, root = resolve_target(identity, api.enumerate_devices())
    hubs = [h for h in api.enumerate_hub_interfaces()
            if h['instance_id'].casefold() == root.parent_instance_id.casefold()]
    ports = {int(m.group(1)) for loc in root.location_paths for m in [re.search(r'#USB\((\d+)\)$', loc)] if m}
    if len(hubs) != 1 or len(ports) != 1:
        raise ValueError('uvc_parent_port_missing_or_ambiguous')
    port = ports.pop()
    current_identity = {'instance_id':camera.instance_id, 'serial':root.instance_id.rsplit('\\',1)[-1],
                        'root_instance_id':root.instance_id, 'container_id':camera.container_id,
                        'parent_instance_id':root.parent_instance_id,
                        'parent_hub_instance_id':root.parent_instance_id, 'port_number':port,
                        'location_paths':list(root.location_paths), 'service':camera.service}
    validate_target_location(identity, current_identity)
    handle = api.open_hub(hubs[0]['device_path'])
    try:
        def read(length):
            buffer = ctypes.create_string_buffer(12 + length)
            struct.pack_into('<IBBHHH', buffer, 0, port, 0x80, 6, 0x0200, 0, length)
            actual = api.ioctl(handle, _ctl_code(260), buffer)
            return buffer.raw[12:actual]
        header = read(9)
        if len(header) != 9:
            raise ValueError('truncated_usb_configuration_header')
        total = struct.unpack_from('<H', header, 2)[0]
        if not 9 <= total <= 65535:
            raise ValueError('invalid_usb_configuration_length')
        result = parse_configuration_descriptor(read(total))
    finally:
        api.close_handle(handle)
    result['identity'] = current_identity
    return result


def probe_windows_uvc(identity: dict, *, executable: Path | None = None, timeout: float = 8.0) -> dict:
    result = {'backend':'windows_usbvideo_ikscontrol', 'temperature_ready':False,
              'accuracy_acceptance':'not_accepted'}
    if not sys.platform.startswith('win'):
        return dict(result, status='unsupported_platform')
    if not _probe_lock.acquire(blocking=False):
        return dict(result, status='port_busy', stage='single_flight')
    try:
        result['stage'] = 'usb_target_descriptor'
        descriptor = read_target_descriptor(identity)
        result.update(descriptor)
        units = descriptor['extension_units']
        if len(units) != 1:
            raise ValueError('uvc_extension_unit_missing_or_ambiguous')
        if executable is None or not executable.is_file():
            return dict(result, status='driver_incompatible', stage='load_windows_probe')
        result['probe_sha256'] = hashlib.sha256(executable.read_bytes()).hexdigest()
        result['stage'] = 'windows_xu_read'
        completed = subprocess.run([str(executable),descriptor['identity']['instance_id'],units[0]['guid']],
                                   capture_output=True, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
        result['process_exit_code'] = completed.returncode
        if not completed.stdout:
            return dict(result, status='native_process_failed', stderr=completed.stderr.decode('utf-8','replace')[-2000:])
        native = json.loads(completed.stdout)
        result['native'] = native
        result['status'] = 'temperature_format_unconfirmed' if native.get('status') == 'probed' else 'windows_xu_failed'
        result['message'] = '接口诊断不构成温度就绪；需适用固件的帧结构、换算和来源序号证据'
        return result
    except subprocess.TimeoutExpired:
        # subprocess.run 终止并回收本次子进程后才返回，不允许残留原生调用重入。
        return dict(result, status='native_process_timeout', process_reaped=True)
    except (OSError, ValueError) as exc:
        return dict(result, status='uvc_diagnostic_failed', error=str(exc))
    finally:
        _probe_lock.release()
