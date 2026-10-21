from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from typing import Any, Mapping, Sequence

from visualization_app.pre_sensor_preflight import (
    Probe,
    probe_helper_readiness,
    probe_mysql_readiness,
    run_preflight,
)


def _json_mapping(name: str, environment: Mapping[str, str]) -> Mapping[str, Any] | None:
    raw = str(environment.get(name) or "").strip()
    if not raw:
        return None
    value = json.loads(raw)
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must contain a JSON object")
    return value


def evaluate(check: str, environment: Mapping[str, str]) -> dict[str, Any]:
    if check == "second-windows":
        helper_status = _json_mapping("AFP_SECOND_WINDOWS_HELPER_STATUS", environment)
        context: dict[str, Any] = {"mode": "remote_helper"}
        if helper_status is not None:
            context.update(
                helper_status_provider=lambda: dict(helper_status),
                helper_evidence_tier="two_machine_environment",
            )
        probe = Probe(
            "environment.second_windows",
            "第二台 Windows Helper 和网络环境",
            probe_helper_readiness,
            timeout_seconds=60,
            external_environment=True,
        )
    elif check == "target-mysql":
        mysql_profile = _json_mapping("AFP_MYSQL_PROFILE", environment)
        context = {
            "mysql_profile": mysql_profile,
            "mysql_evidence_tier": (
                "target_environment" if mysql_profile is not None else "unavailable"
            ),
        }
        probe = Probe(
            "environment.target_mysql",
            "目标 MySQL 事务与恢复环境",
            probe_mysql_readiness,
            timeout_seconds=60,
            external_environment=True,
        )
    else:
        raise ValueError(f"unsupported environment check: {check}")
    report = run_preflight(
        (probe,),
        context=context,
        candidate_identity="environment-readiness",
        mode="remote_helper",
    )
    payload = asdict(report.checks[0])
    payload["schema_version"] = 1
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one external AFP environment probe")
    parser.add_argument(
        "--check", required=True, choices=("second-windows", "target-mysql")
    )
    args = parser.parse_args(argv)
    try:
        payload = evaluate(args.check, os.environ)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        payload = {
            "schema_version": 1,
            "check_id": f"environment.{args.check.replace('-', '_')}",
            "title": "外部环境配置",
            "status": "failed",
            "evidence": str(exc),
            "remediation": "修正外部环境配置后重新运行该检查",
            "duration_seconds": 0.0,
            "external_environment": True,
            "details": {},
        }
    print(json.dumps(payload, ensure_ascii=False))
    return {"passed": 0, "failed": 1, "blocked": 2}[str(payload["status"])]


if __name__ == "__main__":
    raise SystemExit(main())
