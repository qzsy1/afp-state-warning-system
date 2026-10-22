"""Persist browser monotonic render measurements linked to the active physical session."""
import hashlib
import json
import math


def save_render_evidence(manager, payload):
    trace = payload.get("trace") or {}
    with manager.lock:
        config = manager.config
        if config is None or config.acquisition_mode != "real" or manager.capture_record_dir is None:
            raise ValueError("render_evidence_requires_real_capture")
        if trace.get("start_request_id") != config.start_request_id or trace.get("capture_generation") != config.capture_generation:
            raise ValueError("render_evidence_session_mismatch")
        if not trace.get("complete") or not trace.get("input", {}).get("valid") or not trace.get("input", {}).get("trusted"):
            raise ValueError("render_evidence_requires_trusted_input_and_display")
        if set(trace.get("selected_channels", [])) != set(config.selected_sensors):
            raise ValueError("render_evidence_channel_mismatch")
        start = trace["input"].get("timestamp")
        displayed = trace.get("displayed", {})
        if not isinstance(start, (int, float)) or not math.isfinite(start) or set(displayed) != set(config.selected_sensors):
            raise ValueError("render_evidence_clock_invalid")
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < start for v in displayed.values()):
            raise ValueError("render_evidence_clock_invalid")
        identity = lambda sample: (sample.get("source_sequence"), sample.get("received_monotonic"), sample.get("interface_id"))
        known_by_channel = {name: {identity(point) for point in points}
                            for name, points in manager._curve_start_observations.items()}
        cache = getattr(manager.driver, "cache", None)
        if cache is not None:
            for sample in cache.events():
                if (sample.received_monotonic >= (manager._capture_start_monotonic or 0)
                        and sample.value is not None and sample.quality.value == "measured_new"
                        and sample.protocol_ok is not False and sample.metadata.get("current_stream_stable") is not False):
                    known_by_channel.setdefault(sample.channel_name, set()).add(
                        (sample.source_sequence, sample.received_monotonic, sample.interface_id))
        for name in config.selected_sensors:
            points = trace.get("observations", {}).get(name, [])
            if len(points) < 2 or len({p.get("source_sequence") for p in points}) < 2:
                raise ValueError("render_evidence_requires_new_source_points")
            if any(p.get("capture_generation") != config.capture_generation or p.get("start_request_id") != config.start_request_id
                   or p.get("quality") != "measured_new" or p.get("after_start") is not True or p.get("protocol_ok") is False for p in points):
                raise ValueError("render_evidence_source_invalid")
            if not all(identity(point) in known_by_channel.get(name, set()) for point in points):
                raise ValueError("render_evidence_unknown_source")
        elapsed = max(displayed.values()) - start
        trace = dict(trace, elapsed_ms=elapsed, target_ms=3000, maximum_ms=3000, pass_=elapsed <= 3000)
        trace["pass"] = trace.pop("pass_")
        matrix = payload.get("matrix")
        kind = "matrix" if matrix else "curve"
        if matrix and (matrix.get("start_request_id") != config.start_request_id or matrix.get("capture_generation") != config.capture_generation
                       or not matrix.get("visible") or not matrix.get("connection_generation")):
            raise ValueError("render_evidence_matrix_session_invalid")
        identity = hashlib.sha256(config.start_request_id.encode()).hexdigest()[:24]
        path = manager.capture_record_dir / f"startup-{kind}-{identity}.json"
        evidence = {"measurement": "browser_performance_ms", "trace": trace, "matrix": matrix,
                    "capture_uuid": manager.capture_uuid, "server_timeline": manager.capture_timeline(),
                    "configuration": manager._public_config(config)}
        if not path.exists():
            with path.open("x", encoding="utf-8") as stream:
                json.dump(evidence, stream, ensure_ascii=False, allow_nan=False, indent=2)
        return {"ok": True, "elapsed_ms": elapsed, "pass": elapsed <= 3000, "path": str(path)}
