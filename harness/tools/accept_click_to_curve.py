"""Run visible desktop acceptance against the single fixed AFP runtime."""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import psutil
from PIL import ImageGrab
from measure_click_to_curve import DesktopPage

RUNTIME = Path("D:/AFP_Runtime/AFP_Integrated_System_Modular")
EXE = RUNTIME / "AFP_Integrated_System_Modular.exe"
RUNTIME_PORTS = frozenset({8770, 8771, 9223})
EXPECTED_CHANNELS_BY_INTERFACE = {
    "thermocouple_64ch": tuple(f"温度{index}" for index in range(1, 65)),
    "plc_process": ("温度", "压力", "张力"),
    "abb_motion": ("线速度", "ABB_X", "ABB_Y", "ABB_Z"),
    "esr1001_strain": tuple(f"CH{index}_ADC_signed" for index in range(1, 5)),
}
EXPECTED_CHANNELS = tuple(
    channel
    for interface_id in ("plc_process", "abb_motion", "thermocouple_64ch", "esr1001_strain")
    for channel in EXPECTED_CHANNELS_BY_INTERFACE[interface_id]
)
EXPECTED_INTERFACE_BY_CHANNEL = {
    channel: interface_id
    for interface_id, channels in EXPECTED_CHANNELS_BY_INTERFACE.items()
    for channel in channels
}


def validate_configuration(config):
    errors = []
    enabled = {
        item.get("id")
        for item in config.get("interfaces") or []
        if item.get("enabled") is not False
    }
    expected_interfaces = set(EXPECTED_CHANNELS_BY_INTERFACE)
    if enabled != expected_interfaces:
        errors.append("enabled_interfaces")
    assignments = config.get("interface_channel_assignments") or {}
    for interface_id, expected in EXPECTED_CHANNELS_BY_INTERFACE.items():
        if tuple(assignments.get(interface_id) or ()) != expected:
            errors.append(f"assignment:{interface_id}")
    if set(assignments) != expected_interfaces:
        errors.append("assignment_interfaces")
    return {"valid": not errors, "errors": errors}


def validate_round_trace(trace, backend):
    errors = []
    selected = list(trace.get("selected_channels") or [])
    selected_exact = (
        len(selected) == len(EXPECTED_CHANNELS)
        and len(set(selected)) == len(selected)
        and set(selected) == set(EXPECTED_CHANNELS)
    )
    if not selected_exact:
        errors.append("selected_channels_exact")
    request_id = trace.get("start_request_id")
    generation = trace.get("capture_generation")
    if not request_id or backend.get("start_request_id") != request_id:
        errors.append("start_request_id")
    if not generation or backend.get("capture_generation") != generation:
        errors.append("capture_generation")
    origin = next(
        (
            event.get("server_monotonic")
            for event in backend.get("capture_timeline", {}).get("events", [])
            if event.get("stage") == "capture_prepare"
        ),
        None,
    )
    if not isinstance(origin, (int, float)):
        errors.append("capture_prepare")
    proofs = trace.get("observations") or {}
    presentations = trace.get("presentations") or {}
    source_errors = []
    presentation_errors = []
    for name in EXPECTED_CHANNELS:
        points = proofs.get(name) or []
        expected_interface = EXPECTED_INTERFACE_BY_CHANNEL[name]
        if any(point.get("interface_id") != expected_interface for point in points):
            source_errors.append(f"source_interface:{name}")
        identities = {
            (point.get("source_sequence"), point.get("received_monotonic"))
            for point in points
        }
        source_valid = len(points) == 2 and len(identities) == 2 and all(
            point.get("quality") == "measured_new"
            and point.get("after_start") is True
            and point.get("protocol_ok") is not False
            and point.get("stable") is not False
            and point.get("capture_generation") == generation
            and point.get("start_request_id") == request_id
            and point.get("source_sequence") is not None
            and isinstance(point.get("received_monotonic"), (int, float))
            and isinstance(origin, (int, float))
            and point["received_monotonic"] >= origin
            for point in points
        )
        if not source_valid:
            source_errors.append(f"source_observations:{name}")
        presentation = presentations.get(name) or {}
        if name.startswith("温度") and name.removeprefix("温度").isdigit():
            group = (int(name.removeprefix("温度")) - 1) // 8
            expected_surface = "thermocouple64_group"
            expected_presentation_channels = [f"温度{group * 8 + offset}" for offset in range(1, 9)]
        else:
            expected_surface = "sensor_card"
            expected_presentation_channels = [name]
        if presentation.get("surface") != expected_surface:
            presentation_errors.append(f"presentation_surface:{name}")
        if presentation.get("channels") != expected_presentation_channels:
            presentation_errors.append(f"presentation_channels:{name}")
        if any(not isinstance(presentation.get(key), (int, float)) or presentation[key] <= 0
               for key in ("canvas_width", "canvas_height", "client_width", "client_height")):
            presentation_errors.append(f"presentation_size:{name}")
    errors.extend(source_errors)
    errors.extend(presentation_errors)
    source_provenance_valid = selected_exact and not any(
        error in {"start_request_id", "capture_generation", "capture_prepare"}
        or error.startswith("source_")
        for error in errors
    )
    presentation_valid = selected_exact and not presentation_errors
    return {
        "valid": bool(trace.get("complete") and source_provenance_valid and presentation_valid),
        "channel_count": len(selected),
        "source_provenance_valid": source_provenance_valid,
        "presentation_valid": presentation_valid,
        "errors": errors,
    }


def validate_stop_status(status, expected_capture_uuid):
    errors = []
    if status.get("running"):
        errors.append("running")
    if status.get("capture_phase") != "completed":
        errors.append("capture_phase")
    if status.get("capture_uuid") != expected_capture_uuid:
        errors.append("capture_uuid")
    if status.get("capture_saved") is not True:
        errors.append("capture_saved")
    if status.get("finalization_complete") is not True:
        errors.append("finalization_complete")
    if status.get("persistence_queue_depth") != 0:
        errors.append("persistence_queue_depth")
    if not isinstance(status.get("sample_count"), int) or status["sample_count"] <= 0:
        errors.append("sample_count")
    if status.get("last_error"):
        errors.append("last_error")
    for key in ("raw_file", "layer_file", "full_specimen_file"):
        if not status.get(key):
            errors.append(key)
    integrity = status.get("file_integrity") or {}
    for key in ("layer_file", "whole_specimen_file"):
        evidence = integrity.get(key) or {}
        if not isinstance(evidence.get("bytes"), int) or evidence["bytes"] <= 0:
            errors.append(f"file_integrity:{key}")
        digest = evidence.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            errors.append(f"file_digest:{key}")
    return {"valid": not errors, "errors": errors}


def validate_readiness_reuse(before, backend):
    errors = []
    if before.get("ok") is not True:
        errors.append("readiness_before")
    if not before.get("session_id") or backend.get("readiness_session_id") != before.get("session_id"):
        errors.append("readiness_session_id")
    if backend.get("readiness_version") != before.get("version"):
        errors.append("readiness_version")
    if backend.get("readiness_session_valid") is not True:
        errors.append("readiness_session_valid")
    if backend.get("readiness_invalid_reason"):
        errors.append("readiness_invalid_reason")
    return {"valid": not errors, "errors": errors}


def build_endurance_snapshot(frontend, backend, at):
    sources = {}
    for channel in frontend.get("channels") or []:
        name = channel.get("source_channel_name") or channel.get("name")
        if name not in EXPECTED_INTERFACE_BY_CHANNEL:
            continue
        observations = [
            point
            for point in [
                *((channel.get("startup_curve") or {}).get("source_observations") or []),
                *(channel.get("source_observations") or []),
            ]
            if isinstance(point, dict)
        ]
        if observations:
            sources[name] = max(
                observations,
                key=lambda point: (
                    float(point.get("received_monotonic") or float("-inf")),
                    int(point.get("source_sequence") or -1),
                ),
            )

    interfaces = {
        item.get("id"): {
            key: item.get(key)
            for key in ("ok", "state", "last_error", "reconnect_attempts")
        }
        for item in backend.get("interfaces") or []
        if item.get("id") in EXPECTED_CHANNELS_BY_INTERFACE
    }
    esr = next(
        (
            item
            for item in backend.get("interfaces") or []
            if item.get("id") == "esr1001_strain"
        ),
        {},
    )
    esr_evidence = esr.get("usb_evidence") or {}
    return {
        "at": at,
        "running": backend.get("running"),
        "capture_generation": backend.get("capture_generation"),
        "sample_count": backend.get("sample_count"),
        "persistence_queue_depth": backend.get("persistence_queue_depth"),
        "persistence_failed": backend.get("persistence_failed"),
        "last_error": backend.get("last_error"),
        "sources": sources,
        "interfaces": interfaces,
        "esr_unit": esr_evidence.get("unit"),
        "esr_writer_error": esr_evidence.get("writer_error") or "",
    }


def validate_endurance(snapshots, required_seconds):
    errors = []
    if len(snapshots) < 2:
        errors.append("snapshots")
        observed_seconds = 0.0
    else:
        last_at = snapshots[-1].get("at")
        observed_seconds = (
            float(last_at)
            if isinstance(last_at, (int, float))
            else 0.0
        )
    if observed_seconds < required_seconds:
        errors.append("observed_seconds")

    generations = {
        snapshot.get("capture_generation")
        for snapshot in snapshots
        if snapshot.get("capture_generation")
    }
    if len(generations) != 1:
        errors.append("capture_generation")
    if any(snapshot.get("running") is not True for snapshot in snapshots):
        errors.append("running")

    sample_counts = [snapshot.get("sample_count") for snapshot in snapshots]
    if (
        len(sample_counts) < 2
        or any(not isinstance(value, int) or value < 0 for value in sample_counts)
        or sample_counts[-1] <= sample_counts[0]
    ):
        errors.append("sample_count")

    queue_depths = [snapshot.get("persistence_queue_depth") for snapshot in snapshots]
    valid_depths = (
        len(queue_depths) >= 2
        and all(isinstance(value, int) and value >= 0 for value in queue_depths)
    )
    queue_never_drained = valid_depths and queue_depths[-1] > queue_depths[0] and all(
        later >= earlier for earlier, later in zip(queue_depths, queue_depths[1:])
    )
    if not valid_depths or queue_never_drained:
        errors.append("persistence_queue_depth")
    if any(snapshot.get("persistence_failed") is True for snapshot in snapshots):
        errors.append("persistence_failed")
    if any(snapshot.get("last_error") for snapshot in snapshots):
        errors.append("last_error")

    source_identities = {name: set() for name in EXPECTED_CHANNELS}
    for snapshot in snapshots:
        sources = snapshot.get("sources") or {}
        if set(sources) != set(EXPECTED_CHANNELS):
            errors.append("source_channels_exact")
        for name in EXPECTED_CHANNELS:
            source = sources.get(name) or {}
            identity = (source.get("source_sequence"), source.get("received_monotonic"))
            valid_source = (
                source.get("interface_id") == EXPECTED_INTERFACE_BY_CHANNEL[name]
                and source.get("source_sequence") is not None
                and isinstance(source.get("received_monotonic"), (int, float))
                and source.get("quality") == "measured_new"
                and source.get("protocol_ok") is not False
                and source.get("stable") is not False
            )
            if valid_source:
                source_identities[name].add(identity)
            else:
                errors.append(f"source_valid:{name}")

        interfaces = snapshot.get("interfaces") or {}
        if set(interfaces) != set(EXPECTED_CHANNELS_BY_INTERFACE):
            errors.append("interfaces_exact")
        for interface_id in EXPECTED_CHANNELS_BY_INTERFACE:
            interface = interfaces.get(interface_id) or {}
            if (
                interface.get("ok") is not True
                or interface.get("state") != "ok"
                or interface.get("last_error")
                or int(interface.get("reconnect_attempts") or 0) > 0
            ):
                errors.append(f"interface_health:{interface_id}")
        if snapshot.get("esr_unit") != "adc_counts":
            errors.append("esr_unit")
        if snapshot.get("esr_writer_error"):
            errors.append("esr_writer_error")

    for name, identities in source_identities.items():
        if len(identities) < 2:
            errors.append(f"source_progress:{name}")
    errors = list(dict.fromkeys(errors))
    return {
        "valid": not errors,
        "errors": errors,
        "observed_seconds": observed_seconds,
        "snapshot_count": len(snapshots),
        "sample_count_start": sample_counts[0] if sample_counts else None,
        "sample_count_end": sample_counts[-1] if sample_counts else None,
        "queue_depth_max": max(queue_depths) if valid_depths else None,
        "source_identity_counts": {
            name: len(identities) for name, identities in source_identities.items()
        },
    }


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_delivery(page, pid):
    process_path = Path(psutil.Process(pid).exe()).resolve()
    if process_path != EXE.resolve():
        raise RuntimeError("Measurement process is outside the fixed runtime")
    manifest = RUNTIME / "SHA256SUMS.txt"
    entries = {}
    for line in manifest.read_text(encoding="utf-8-sig").splitlines():
        digest, relative = line.split(" *", 1)
        path = (RUNTIME / relative).resolve()
        if not path.is_relative_to(RUNTIME.resolve()) or sha(path).lower() != digest.lower():
            raise RuntimeError("Runtime digest mismatch: " + relative)
        entries[relative] = digest.lower()
    revision = sha(manifest)
    application = page.evaluate("state.bootstrap.application")
    if application.get("runtime_revision") != revision:
        raise RuntimeError("Running revision does not match the fixed runtime")
    assets = {}
    for name in ("app.js", "curve_readiness.js", "index.html"):
        url = "http://127.0.0.1:8771/" + ("" if name == "index.html" else name)
        with urllib.request.urlopen(url, timeout=10) as response:
            served = hashlib.sha256(response.read()).hexdigest()
        disk = RUNTIME / "app/ui/static" / name
        if not disk.exists():
            disk = RUNTIME / "app/ui" / name
        if served != sha(disk):
            raise RuntimeError("Served resource mismatch: " + name)
        assets[name] = served
    return dict(process_path=str(process_path), runtime_revision=revision,
                application=application, verified_files=len(entries), assets=assets)


def capture_original_ui_evidence(page, folder, hwnd):
    canvases = page.evaluate(r"""(async()=>{
      const rows=[
        ...document.querySelectorAll('#thermocouple64RawGroups canvas.thermocouple64-preview'),
        ...document.querySelectorAll('#sensorCards canvas.full-channel-chart'),
      ];
      const clippedRatio=(canvas)=>{
        const source=canvas.getBoundingClientRect();
        let left=Math.max(0,source.left),top=Math.max(0,source.top);
        let right=Math.min(innerWidth,source.right),bottom=Math.min(innerHeight,source.bottom);
        for(let parent=canvas.parentElement;parent;parent=parent.parentElement){
          const style=getComputedStyle(parent);
          if(!/(auto|scroll|hidden|clip)/.test(`${style.overflow} ${style.overflowX} ${style.overflowY}`))continue;
          const rect=parent.getBoundingClientRect();
          left=Math.max(left,rect.left);top=Math.max(top,rect.top);
          right=Math.min(right,rect.right);bottom=Math.min(bottom,rect.bottom);
        }
        const area=Math.max(0,right-left)*Math.max(0,bottom-top);
        return area/Math.max(1,source.width*source.height);
      };
      return await Promise.all(rows.map(async(canvas,index)=>{
        const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
        const bytes=blob?new Uint8Array(await blob.arrayBuffer()):new Uint8Array();
        const digest=bytes.length?Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)))
          .map(value=>value.toString(16).padStart(2,'0')).join(''):'';
        const context=canvas.getContext('2d'),width=canvas.width,height=canvas.height;
        let opaque=0,variation=new Set();
        if(width&&height){
          const pixels=context.getImageData(0,0,width,height).data;
          const step=Math.max(4,Math.floor(pixels.length/4096/4)*4);
          for(let offset=0;offset<pixels.length;offset+=step){
            if(pixels[offset+3])opaque+=1;
            if(variation.size<64)variation.add(`${pixels[offset]},${pixels[offset+1]},${pixels[offset+2]},${pixels[offset+3]}`);
          }
        }
        return {index,surface:canvas.classList.contains('thermocouple64-preview')?'thermocouple64_group':'sensor_card',
          channels:(canvas.dataset.channels||'').split(',').filter(Boolean),width,height,
          client_width:canvas.clientWidth,client_height:canvas.clientHeight,digest,
          sampled_opaque_pixels:opaque,sampled_color_count:variation.size,
          viewport_ratio:clippedRatio(canvas)};
      }));
    })()""")
    expected_sensor_channels = [
        *EXPECTED_CHANNELS_BY_INTERFACE["plc_process"],
        *EXPECTED_CHANNELS_BY_INTERFACE["abb_motion"],
        *EXPECTED_CHANNELS_BY_INTERFACE["esr1001_strain"],
    ]
    errors = []
    thermocouple = [item for item in canvases if item.get("surface") == "thermocouple64_group"]
    sensors = [item for item in canvases if item.get("surface") == "sensor_card"]
    if len(thermocouple) != 8:
        errors.append("thermocouple_canvas_count")
    if len(sensors) != len(expected_sensor_channels):
        errors.append("sensor_canvas_count")
    for group, item in enumerate(thermocouple):
        expected = [f"温度{group * 8 + offset}" for offset in range(1, 9)]
        if item.get("channels") != expected:
            errors.append(f"thermocouple_canvas_channels:{group}")
    if [item.get("channels", [None])[0] for item in sensors] != expected_sensor_channels:
        errors.append("sensor_canvas_channels")
    for item in canvases:
        if (item.get("width", 0) <= 0 or item.get("height", 0) <= 0
                or len(item.get("digest") or "") != 64
                or item.get("sampled_opaque_pixels", 0) <= 0
                or item.get("sampled_color_count", 0) < 2):
            errors.append(f"canvas_pixels:{item.get('surface')}:{item.get('index')}")
    scroll_state = page.evaluate(r"""({window_y:scrollY,
      tc:document.getElementById('thermocouple64OverviewViewport')?.scrollTop||0,
      sensors:document.getElementById('sensorCardsViewport')?.scrollTop||0})""")
    screenshots = []
    views = (
        ("thermocouple", "thermocouple64Panel", "thermocouple64OverviewViewport", 0),
        ("sensors-top", "otherSensorsPanel", "sensorCardsViewport", 0),
        ("sensors-bottom", "otherSensorsPanel", "sensorCardsViewport", -1),
    )
    for name, panel_id, viewport_id, position in views:
        page.evaluate(r"""(async()=>{const panel=document.getElementById(%s),viewport=document.getElementById(%s);
          panel?.scrollIntoView({block:'center',behavior:'instant'});
          if(viewport)viewport.scrollTop=%s<0?viewport.scrollHeight:%s;
          await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));return true;})()"""
                      % (json.dumps(panel_id), json.dumps(viewport_id), position, position))
        foreground = ctypes.windll.user32.GetForegroundWindow() == hwnd
        path = folder / f"visible-{name}.png"
        ImageGrab.grab().save(path, compress_level=1)
        screenshots.append({"view": name, "path": str(path), "foreground": foreground})
        if not foreground:
            errors.append(f"foreground:{name}")
    page.evaluate(r"""(async()=>{scrollTo(0,%s);
      const tc=document.getElementById('thermocouple64OverviewViewport');if(tc)tc.scrollTop=%s;
      const sensors=document.getElementById('sensorCardsViewport');if(sensors)sensors.scrollTop=%s;
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));return true;})()"""
                  % (scroll_state["window_y"], scroll_state["tc"], scroll_state["sensors"]))
    evidence = {"valid": not errors, "errors": errors, "canvases": canvases, "screenshots": screenshots}
    dump(folder / "original-ui-evidence.json", evidence)
    return evidence


def wait_page(timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            page = DesktopPage()
            if page.evaluate("!!state.bootstrap && !!window.AFP_CURVE_READINESS"):
                return page
            page.socket.close()
        except Exception:
            pass
        time.sleep(.3)
    raise RuntimeError("Formal desktop page unavailable")


def wait_prepared(page, pid, manual_check=False):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        ready = page.evaluate("({busy:state.hardwareCheckInProgress,ok:hardwareCheckIsCurrent(hardwareConfigFingerprint()),reason:controls.hardwareCheckStatus?.textContent})")
        if not ready["busy"]:
            if ready["ok"]:
                return ready
            if manual_check:
                try:
                    page.click("testSensorsButton", pid)
                except RuntimeError:
                    if not page.evaluate("state.hardwareCheckInProgress"):
                        if page.evaluate("!document.getElementById('testSensorsButton').getBoundingClientRect().width"):
                            time.sleep(.2)
                            continue
                        raise
                manual_check = False
                time.sleep(.2)
                continue
        time.sleep(.2)
    raise RuntimeError("Initial detection did not pass: " + str(ready))


def start_capture(page, pid, folder):
    with ThreadPoolExecutor(max_workers=1) as writer:
        return _start_capture(page, pid, folder, writer)


def _start_capture(page, pid, folder, writer):
    folder.mkdir(parents=True, exist_ok=True)
    previous_id = page.evaluate("window.afpCurveTrace?.id")
    configuration = page.evaluate("acquisitionConfig({allowUnverifiedPhysical:true})")
    configuration_validation = validate_configuration(configuration)
    if not configuration_validation["valid"]:
        raise RuntimeError("Formal 75-channel configuration mismatch: " + str(configuration_validation["errors"]))
    readiness_before = page.evaluate("""({
      ok:hardwareCheckIsCurrent(hardwareConfigFingerprint()),
      session_id:state.hardwareCheck?.readiness_session_id,
      version:state.hardwareCheck?.readiness_version,
      fingerprint:state.hardwareCheckFingerprint,
      check_in_progress:state.hardwareCheckInProgress})""")
    page.evaluate("""(()=>{window.afpDesktopLongTasks=[];
      window.afpDesktopObserver?.disconnect();
      if(!PerformanceObserver.supportedEntryTypes.includes('longtask'))return false;
      window.afpDesktopObserver=new PerformanceObserver(list=>{
        for(const e of list.getEntries())afpDesktopLongTasks.push({start:e.startTime,duration:e.duration});
        if(afpDesktopLongTasks.length>256)afpDesktopLongTasks.splice(0,afpDesktopLongTasks.length-256);
      });afpDesktopObserver.observe({type:'longtask'});return true;})()""")
    round_data = {
        "configuration_validation": configuration_validation,
        "readiness_before": readiness_before,
        "input": page.click("startAcquisitionButton", pid),
        "frames": [],
    }
    writes = []
    foreground_valid = True
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        probe = page.evaluate("""(()=>{const trace=window.afpCurveTrace;return {
          trace_id:trace?.id||null,complete:trace?.complete===true,
          status:state.acquisitionStatus?.capture_phase,
          status_request_id:state.acquisitionStatus?.start_request_id,
          curve:document.getElementById('captureCurveStatus')?.textContent};})()""")
        foreground = ctypes.windll.user32.GetForegroundWindow() == round_data["input"]["hwnd"]
        foreground_valid = foreground_valid and foreground
        complete = bool(
            probe.get("complete")
            and probe.get("trace_id")
            and probe.get("trace_id") != previous_id
        )
        failed = (
            probe.get("status_request_id") == probe.get("trace_id")
            and probe.get("status")
            in {"start_failed", "start_cancelled", "stop_timed_out"}
        )
        if not complete and not failed:
            time.sleep(.005)
            continue
        trace = page.evaluate("window.afpCurveTrace?.snapshot()") or {}
        snapshot = {
            "trace": trace,
            "status": probe.get("status"),
            "status_request_id": probe.get("status_request_id"),
            "curve": probe.get("curve"),
            "foreground": foreground,
        }
        screenshot = folder / f"frame-{len(round_data['frames']):03d}.png"
        snapshot.update(host_monotonic=time.monotonic(), screenshot=str(screenshot))
        snapshot["screenshot_started_perf_counter"] = time.perf_counter()
        bitmap = ImageGrab.grab()
        snapshot["screenshot_captured_perf_counter"] = time.perf_counter()
        writes.append(writer.submit(bitmap.save, screenshot, compress_level=1))
        round_data["frames"].append(snapshot)
        dump(folder / "events.json", round_data)
        if complete:
            physical_upper_bound = (snapshot["screenshot_captured_perf_counter"] - round_data["input"]["native_input_monotonic"]) * 1000
            round_data.update(elapsed_ms=trace["elapsed_ms"],
                              physical_frame_upper_bound_ms=physical_upper_bound,
                              pass_performance=trace["pass"] and physical_upper_bound <= 5000 and foreground_valid,
                              pass_target=trace.get("target_pass", trace["elapsed_ms"] <= 3000)
                              and physical_upper_bound <= 3000 and foreground_valid,
                              channel_count=len(trace["selected_channels"]))
            break
        if failed:
            break
    if not round_data["frames"]:
        trace = page.evaluate("window.afpCurveTrace?.snapshot()") or {}
        snapshot = page.evaluate("""({
          status:state.acquisitionStatus?.capture_phase,
          status_request_id:state.acquisitionStatus?.start_request_id,
          curve:document.getElementById('captureCurveStatus')?.textContent})""")
        snapshot.update(
            trace=trace,
            foreground=ctypes.windll.user32.GetForegroundWindow()
            == round_data["input"]["hwnd"],
            host_monotonic=time.monotonic(),
        )
        screenshot = folder / "frame-000.png"
        snapshot["screenshot"] = str(screenshot)
        snapshot["screenshot_started_perf_counter"] = time.perf_counter()
        bitmap = ImageGrab.grab()
        snapshot["screenshot_captured_perf_counter"] = time.perf_counter()
        writes.append(writer.submit(bitmap.save, screenshot, compress_level=1))
        round_data["frames"].append(snapshot)
    for job in writes:
        job.result()
    round_data["foreground_valid"] = foreground_valid
    backend = page.evaluate("(async()=>await(await fetch('/api/acquisition/status?acquisition_mode=real',{cache:'no-store'})).json())()")
    dump(folder / "backend-start.json", backend)
    final_trace = (round_data["frames"][-1].get("trace") or {}) if round_data["frames"] else {}
    validation = validate_round_trace(final_trace, backend)
    readiness_reuse = validate_readiness_reuse(readiness_before, backend)
    round_data.update(
        trace_validation=validation,
        readiness_reuse=readiness_reuse,
        source_provenance_valid=validation["source_provenance_valid"],
        presentation_valid=validation["presentation_valid"],
        capture_uuid=backend.get("capture_uuid"),
    )
    if not validation["valid"] or not readiness_reuse["valid"]:
        round_data["pass_performance"] = False
        round_data["pass_target"] = False
    if final_trace.get("complete"):
        round_data["original_ui_evidence"] = capture_original_ui_evidence(
            page, folder, round_data["input"]["hwnd"],
        )
        if not round_data["original_ui_evidence"]["valid"]:
            round_data["pass_performance"] = False
            round_data["pass_target"] = False
    dump(folder / "long-tasks.json", page.evaluate("window.afpDesktopLongTasks"))
    round_data["backend_status_file"] = str(folder / "backend-start.json")
    round_data.setdefault("pass_performance", False)
    round_data.setdefault("pass_target", False)
    dump(folder / "events.json", round_data)
    return {**{key: value for key, value in round_data.items() if key != "frames"},
            "frame_count": len(round_data["frames"]), "events_file": str(folder / "events.json")}


def collect_endurance_snapshot(page, started):
    payload = page.evaluate(r"""(async()=>{
      const response=await fetch('/api/acquisition/status?acquisition_mode=real',{cache:'no-store'});
      if(!response.ok)throw Error('Status unavailable during endurance');
      const backend=await response.json();
      const channels=(state.payload?.channels||[]).map(channel=>({
        name:channel.name,
        source_channel_name:channel.source_channel_name,
        source_observations:channel.source_observations||[],
        startup_curve:{source_observations:channel.startup_curve?.source_observations||[]},
      }));
      return {frontend:{channels},backend};
    })()""")
    return build_endurance_snapshot(
        payload["frontend"], payload["backend"], time.monotonic() - started,
    )


def stop_capture(page, pid, expected_capture_uuid=None):
    status = page.evaluate("(async()=>{const r=await fetch('/api/acquisition/status?acquisition_mode=real',{cache:'no-store'});if(!r.ok)throw Error('Status unavailable');const s=await r.json();renderAcquisitionStatus(s);return s;})()")
    if not status.get("running") and status.get("capture_phase") == "idle" and not status.get("capture_uuid"):
        return dict(status=status, files={})
    if (status.get("capture_phase") in {"start_failed", "start_cancelled"}
            and not status.get("raw_file") and not status.get("layer_file")
            and status.get("start_operation", {}).get("state") in {"failed", "cancelled"}):
        return dict(status=status, files={})
    if status.get("running") or status.get("start_operation", {}).get("state") in {"starting", "cancelling"}:
        page.click("stopAcquisitionButton", pid)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status = page.evaluate("state.acquisitionStatus")
        if not status.get("running") and status.get("finalization_complete"):
            files = {}
            filesystem_errors = []
            save_root = Path(status.get("save_root") or "").resolve()
            for key in ("raw_file", "layer_file", "full_specimen_file"):
                if status.get(key):
                    path = Path(status[key]).resolve()
                    if path.drive.upper() != "D:" or not path.is_file():
                        filesystem_errors.append(key)
                        continue
                    if save_root.drive and not path.is_relative_to(save_root):
                        filesystem_errors.append(f"outside_save_root:{key}")
                    files[key] = dict(path=str(path), size=path.stat().st_size, sha256=sha(path))
            validation = validate_stop_status(
                status,
                expected_capture_uuid or status.get("capture_uuid"),
            )
            validation["errors"].extend(filesystem_errors)
            validation["valid"] = not validation["errors"]
            return dict(status=status, files=files, **validation)
        time.sleep(.1)
    raise RuntimeError("Capture did not finish saving and releasing devices")


def launch():
    environment = dict(os.environ, WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9223 --remote-debugging-address=127.0.0.1")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return subprocess.Popen([str(EXE)], cwd=RUNTIME, env=environment, startupinfo=startup)


def listening_runtime_ports(connections=None):
    if connections is None:
        connections = psutil.net_connections(kind="tcp")
    listeners = []
    for connection in connections:
        local = getattr(connection, "laddr", None)
        port = getattr(local, "port", None)
        if (
            connection.status == psutil.CONN_LISTEN
            and port in RUNTIME_PORTS
        ):
            listeners.append({"port": port, "pid": connection.pid})
    return sorted(listeners, key=lambda item: (item["port"], item["pid"] or -1))


def close_runtime(page, pid):
    stopped = stop_capture(page, pid)
    if "valid" in stopped and stopped.get("valid") is not True:
        raise RuntimeError("Refusing cold restart after an invalid capture stop")
    page.socket.close()
    process = psutil.Process(pid)
    if Path(process.exe()).resolve() != EXE.resolve():
        raise RuntimeError("Refusing to close a different executable")
    children = process.children(recursive=True)
    process.terminate()
    _gone, alive = psutil.wait_procs([process, *children], timeout=10)
    for child in alive:
        try:
            child.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(alive, timeout=10)
    for child in alive:
        try:
            child.kill()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(alive, timeout=10)
    deadline = time.monotonic() + 20
    listeners = listening_runtime_ports()
    while not alive and listeners and time.monotonic() < deadline:
        time.sleep(.2)
        listeners = listening_runtime_ports()
    if alive or listeners:
        survivors = []
        for child in alive:
            try:
                survivors.append({"pid": child.pid, "exe": child.exe()})
            except psutil.Error:
                survivors.append({"pid": child.pid})
        raise RuntimeError(
            "Runtime did not fully exit: "
            + json.dumps({"processes": survivors, "listeners": listeners})
        )


def idle_wait_seconds(index, count, minimum, maximum):
    if count <= 1:
        return round(minimum)
    return round(minimum + (maximum - minimum) * index / (count - 1))


def statistics(rounds):
    def summarize(key):
        values = sorted(item[key] for item in rounds if isinstance(item.get(key), (int, float)))
        percentile = lambda fraction: values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None
        return dict(measured=len(values), p50_ms=percentile(.5), p95_ms=percentile(.95),
                    maximum_ms=max(values) if values else None)
    trace = summarize("elapsed_ms")
    physical = summarize("physical_frame_upper_bound_ms")
    return dict(
        total=len(rounds),
        failures=sum(not item.get("pass_performance", False) for item in rounds),
        target_exceedances=sum(not item.get("pass_target", False) for item in rounds),
        censored_failures=sum("elapsed_ms" not in item for item in rounds),
        trace=trace,
        physical_upper_bound=physical,
        measured=trace["measured"],
        p50_ms=trace["p50_ms"],
        p95_ms=trace["p95_ms"],
        maximum_ms=trace["maximum_ms"],
    )


def formal_acceptance_status(
    rounds,
    *,
    cold,
    warm,
    idle,
    idle_minimum,
    idle_maximum,
    endurance,
    required_endurance_seconds,
    max_target_exceedances=None,
):
    pass_hard_limit = (
        cold >= 10
        and warm >= 40
        and idle >= 10
        and len(rounds) >= 60
        and idle_minimum >= 120
        and idle_maximum >= 300
        and all(item.get("pass_performance") is True for item in rounds)
    )
    target_exceedances = sum(item.get("pass_target") is not True for item in rounds)
    pass_performance = (pass_hard_limit and max_target_exceedances is not None
                        and target_exceedances <= max_target_exceedances)
    pass_endurance = (
        required_endurance_seconds >= 600
        and isinstance(endurance, dict)
        and endurance.get("pass_performance") is True
        and isinstance(endurance.get("observed_seconds"), (int, float))
        and endurance["observed_seconds"] >= required_endurance_seconds
        and (endurance.get("endurance_validation") or {}).get("valid") is True
        and (endurance.get("stop") or {}).get("valid") is True
    )
    return {
        "pass_hard_limit": pass_hard_limit,
        "target_exceedances": target_exceedances,
        "max_target_exceedances": max_target_exceedances,
        "pass_performance": pass_performance,
        "pass_endurance": pass_endurance,
        "pass_acceptance": pass_performance and pass_endurance,
    }


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--cold", type=int, default=10)
    parser.add_argument("--warm", type=int, default=40)
    parser.add_argument("--idle", type=int, default=10)
    parser.add_argument("--idle-seconds", type=float, default=120)
    parser.add_argument("--idle-max-seconds", type=float, default=300)
    parser.add_argument("--endurance-seconds", type=float, default=600)
    parser.add_argument("--existing-pid", type=int)
    parser.add_argument("--max-target-exceedances", type=int, default=6)
    args = parser.parse_args()
    if args.max_target_exceedances is not None and not 0 <= args.max_target_exceedances <= 60:
        raise ValueError("Target exceedance allowance must be between 0 and 60")
    if any(p.resolve().drive.upper() != "D:" for p in (args.output, args.capture_root)):
        raise ValueError("Evidence and capture data must be on D:")
    if args.idle_max_seconds < args.idle_seconds:
        raise ValueError("Maximum idle interval cannot be shorter than the minimum")
    if (args.output / "acceptance.json").exists():
        raise ValueError("Acceptance output already exists")
    args.output.mkdir(parents=True, exist_ok=True)
    args.capture_root.mkdir(parents=True, exist_ok=True)
    probe = args.capture_root / ".write-probe"
    with probe.open("x") as stream:
        stream.write("ok")
    probe.unlink()
    manifest = json.loads((RUNTIME / "RELEASE-MANIFEST.json").read_text(encoding="utf-8-sig"))
    report = dict(candidate=manifest["candidate"], exe_sha256=sha(EXE), groups={}, rounds=[],
                  target_ms=3000, maximum_ms=5000,
                  max_target_exceedances=args.max_target_exceedances)
    pid = args.existing_pid
    page = wait_page() if pid else None
    if page:
        stop_capture(page, pid)
        if args.cold == 0:
            wait_prepared(page, pid, manual_check=True)
    try:
        for group, count in (("cold", args.cold), ("warm", args.warm), ("idle", args.idle)):
            report["groups"][group] = []
            for index in range(count):
                if group == "cold" or page is None:
                    if page:
                        close_runtime(page, pid)
                    opened = time.monotonic()
                    process = launch()
                    pid = process.pid
                    page = wait_page()
                    preparation = wait_prepared(page, pid, manual_check=True)
                    preparation_seconds = time.monotonic() - opened
                else:
                    preparation, preparation_seconds = None, None
                root = args.capture_root / (f"cold-{index:02d}" if group == "cold" else "repeated")
                root.mkdir(parents=True, exist_ok=True)
                if page.evaluate("controls.saveRoot.value") != str(root):
                    page.evaluate("controls.newLayer.value=0;true")
                page.evaluate("(()=>{controls.saveRoot.value=%s;controls.liveSpecimen.value='SUB3S-DESKTOP';controls.runId.value='SUB3S-DESKTOP';controls.autoProcessParameters.checked=false;controls.mysqlEnabled.checked=false;controls.mysqlLocalEnabled.checked=false;return true;})()" % json.dumps(str(root)))
                existing = [p for p in root.rglob("*") if p.is_file()]
                if "delivery" not in report:
                    report["delivery"] = verify_delivery(page, pid)
                idle_seconds = None
                if group == "idle":
                    idle_seconds = idle_wait_seconds(
                        index, count, args.idle_seconds, args.idle_max_seconds,
                    )
                    print(json.dumps(dict(group=group, index=index, state="idle_wait", seconds=idle_seconds)), flush=True)
                    deadline = time.monotonic() + idle_seconds
                    next_report = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        if not page.evaluate("hardwareCheckIsCurrent(hardwareConfigFingerprint())"):
                            raise RuntimeError("Detection evidence became invalid during idle wait")
                        remaining = max(0, deadline - time.monotonic())
                        if time.monotonic() >= next_report:
                            print(json.dumps(dict(group=group, index=index, state="idle_wait",
                                                  remaining_seconds=round(remaining, 1))), flush=True)
                            next_report += 30
                        time.sleep(min(1, remaining))
                elif group == "warm" and index >= count // 2:
                    time.sleep(5 + index % 11)
                entry = start_capture(page, pid, args.output / f"{group}-{index:02d}")
                entry.update(group=group, index=index, preparation_seconds=preparation_seconds,
                             preparation=preparation, process_path=psutil.Process(pid).exe(),
                             existing_files=len(existing), existing_bytes=sum(p.stat().st_size for p in existing),
                             idle_seconds=idle_seconds)
                if entry.get("channel_count") != 75:
                    entry["pass_performance"] = False
                entry["stop"] = stop_capture(page, pid, entry.get("capture_uuid"))
                if not entry["stop"].get("valid"):
                    entry["pass_performance"] = False
                report["rounds"].append(entry)
                report["groups"][group].append(entry)
                report["statistics"] = {name: statistics(values) for name, values in report["groups"].items()}
                dump(args.output / "acceptance.json", report)
                print(json.dumps({k: entry.get(k) for k in ("group", "index", "elapsed_ms", "pass_performance", "channel_count")}), flush=True)
        if args.endurance_seconds:
            entry = start_capture(page, pid, args.output / "endurance")
            started = time.monotonic()
            snapshots = []
            while True:
                snapshot = collect_endurance_snapshot(page, started)
                snapshots.append(snapshot)
                dump(args.output / "endurance" / "status.json", snapshots)
                elapsed = snapshot["at"]
                if not snapshot.get("running") or elapsed >= args.endurance_seconds:
                    break
                time.sleep(min(5, max(0, args.endurance_seconds - elapsed)))
            endurance_validation = validate_endurance(snapshots, args.endurance_seconds)
            stopped = stop_capture(page, pid, entry.get("capture_uuid"))
            entry.update(
                observed_seconds=snapshots[-1]["at"] if snapshots else 0,
                endurance_validation=endurance_validation,
                stop=stopped,
            )
            entry["pass_endurance"] = (
                entry.get("pass_performance") is True
                and endurance_validation["valid"]
                and stopped.get("valid") is True
                and args.endurance_seconds >= 600
            )
            report["endurance"] = entry
        report.update(formal_acceptance_status(
            report["rounds"],
            cold=args.cold,
            warm=args.warm,
            idle=args.idle,
            idle_minimum=args.idle_seconds,
            idle_maximum=args.idle_max_seconds,
            endurance=report.get("endurance"),
            required_endurance_seconds=args.endurance_seconds,
            max_target_exceedances=args.max_target_exceedances,
        ))
    except Exception as exc:
        report["error"] = str(exc)
        report["pass_performance"] = False
        if page:
            try:
                report["cleanup_stop"] = stop_capture(page, pid)
            except Exception as cleanup:
                report["cleanup_error"] = str(cleanup)
        raise
    finally:
        dump(args.output / "acceptance.json", report)
    print(json.dumps(dict(
        statistics=report.get("statistics"),
        pass_performance=report.get("pass_performance"),
        pass_endurance=report.get("pass_endurance"),
        pass_acceptance=report.get("pass_acceptance"),
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
