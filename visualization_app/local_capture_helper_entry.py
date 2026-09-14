"""Executable entry point for the local capture helper."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import fields
from typing import Any

from acquisition import AcquisitionConfig, MySQLSettings
from local_capture_agent import HelperTransport, LocalCaptureAgent


def _config_from_payload(payload: dict[str, Any]) -> AcquisitionConfig:
    allowed = {field.name for field in fields(AcquisitionConfig)}
    return AcquisitionConfig(**{key: value for key, value in payload.items() if key in allowed})


def dispatch_command(agent: LocalCaptureAgent, raw: str | bytes) -> dict[str, Any]:
    command = HelperTransport.decode_command(raw)
    name = command["command"]
    payload = command["payload"]
    if name == "discover":
        result = agent.discover()
    elif name == "status":
        result = agent.status()
    elif name == "mysql_preflight":
        result = agent.mysql_preflight(
            MySQLSettings.from_mapping(payload),
            write_test=bool(payload.get("write_test", False)),
        )
    elif name == "check_capture":
        result = agent.check_capture(_config_from_payload(payload))
    elif name == "start_capture":
        result = agent.start_capture(_config_from_payload(payload))
    elif name == "stop_capture":
        result = agent.stop_capture()
    else:  # decode_command already guards this; retain a defensive branch.
        raise ValueError("helper命令不在允许列表")
    return {
        "type": "result",
        "request_id": command["request_id"],
        "payload": result if isinstance(result, dict) else {"result": result},
    }


def run_forever(server_url: str, pairing_token: str, device_id: str) -> None:
    agent = LocalCaptureAgent()
    transport = HelperTransport(server_url, pairing_token, device_id=device_id)
    delay = 1.0
    while True:
        connection = None
        try:
            connection = transport.connect_once()
            connection.send(
                json.dumps(
                    transport.hello(
                        capabilities=agent.discover().get("capabilities", {})
                    ),
                    ensure_ascii=False,
                )
            )
            delay = 1.0
            while True:
                raw = connection.recv()
                if raw is None:
                    break
                response = dispatch_command(agent, raw)
                connection.send(json.dumps(response, ensure_ascii=False))
        except KeyboardInterrupt:
            return
        except Exception:
            time.sleep(delay)
            delay = min(delay * 2.0, 30.0)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="AFP本地采集辅助程序")
    parser.add_argument("--server", required=True, help="wss://辅助服务地址")
    parser.add_argument("--pairing-token", required=True)
    parser.add_argument("--device-id", default="local-helper")
    args = parser.parse_args()
    run_forever(args.server, args.pairing_token, args.device_id)


if __name__ == "__main__":
    main()
