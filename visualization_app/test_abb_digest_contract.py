from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import unittest
import urllib3.util.connection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))

from acquisition import (  # noqa: E402
    AcquisitionConfig,
    AcquisitionManager,
    AbbRobotDriver,
    classify_interface_probe_error,
)


def _digest_fields(value: str) -> dict[str, str]:
    return {
        key: raw.strip().strip('"')
        for key, raw in re.findall(r"(\w+)=(\"[^\"]*\"|[^,]+)", value)
    }


class DigestFixture:
    def __init__(
        self,
        *,
        username: str = "operator",
        password: str = "secret",
        mode: str = "ok",
        rotate_nonce: bool = False,
    ) -> None:
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args) -> None:
                pass

            def _challenge(self, *, stale: bool = False) -> None:
                suffix = ", stale=true" if stale else ""
                self.send_response(401)
                self.send_header(
                    "WWW-Authenticate",
                    f'Digest realm="robot", nonce="{fixture.nonce}", '
                    f'algorithm=MD5, qop="auth"{suffix}',
                )
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:
                fixture.client_ports.append(self.client_address[1])
                authorization = self.headers.get("Authorization")
                fixture.authorization_headers.append(authorization)
                if fixture.mode == "session_limit":
                    body = b"Too many sessions 70/70"
                    self.send_response(503)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/logout" and fixture.mode == "logout_failed":
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if fixture.mode == "missing":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not authorization or not authorization.startswith("Digest "):
                    self._challenge()
                    return
                fields = _digest_fields(authorization[7:])
                if fixture.rotate_nonce and fixture.successes and fields.get("nonce") != "nonce-b":
                    fixture.nonce = "nonce-b"
                    self._challenge(stale=True)
                    return
                ha1 = hashlib.md5(
                    f"{fixture.username}:robot:{fixture.password}".encode("utf-8")
                ).hexdigest()
                uri = fields.get("uri", self.path)
                ha2 = hashlib.md5(f"GET:{uri}".encode("utf-8")).hexdigest()
                expected = hashlib.md5(
                    (
                        f"{ha1}:{fields.get('nonce', '')}:{fields.get('nc', '')}:"
                        f"{fields.get('cnonce', '')}:{fields.get('qop', '')}:{ha2}"
                    ).encode("utf-8")
                ).hexdigest()
                if (
                    fields.get("username") != fixture.username
                    or fields.get("response") != expected
                    or fields.get("nonce") != fixture.nonce
                ):
                    self._challenge()
                    return
                fixture.successes += 1
                session_cookie = None
                if fixture.mode == "sessions":
                    cookie = self.headers.get("Cookie", "")
                    match = re.search(r"(?:^|;\s*)ABBCX=([^;]+)", cookie)
                    session = match.group(1) if match else None
                    if self.path == "/logout":
                        fixture.sessions.discard(session)
                        fixture.logouts += 1
                        self.send_response(204)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if session not in fixture.sessions:
                        fixture.sessions_created += 1
                        if fixture.sessions_created > 2:
                            self.send_response(503)
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        session_cookie = f"session-{fixture.sessions_created}"
                        fixture.sessions.add(session_cookie)
                if fixture.mode == "timeout":
                    time.sleep(0.2)
                body = (
                    b"not-json"
                    if fixture.mode == "invalid_json"
                    else json.dumps(
                        {"_embedded": {"_state": [{"x": "1", "y": "2", "z": "3"}]}}
                    ).encode("utf-8")
                )
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    if session_cookie:
                        self.send_header("Set-Cookie", f"ABBCX={session_cookie}; Path=/")
                        self.send_header("Set-Cookie", "-http-session-=transport; Path=/")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass

        self.username = username
        self.password = password
        self.mode = mode
        self.rotate_nonce = rotate_nonce
        self.nonce = "nonce-a"
        self.successes = 0
        self.sessions_created = 0
        self.sessions = set()
        self.logouts = 0
        self.authorization_headers: list[str | None] = []
        self.client_ports = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1.0)


class AbbDigestContractTests(unittest.TestCase):
    def test_subsequent_requests_reuse_the_negotiated_digest_challenge(self) -> None:
        with DigestFixture(mode="sessions") as fixture:
            driver = AbbRobotDriver(fixture.endpoint, username="operator", password="secret")
            for _ in range(3):
                driver.read_sample()
            driver.close()
        self.assertIsNone(fixture.authorization_headers[0])
        self.assertTrue(all(value and value.startswith("Digest ") for value in fixture.authorization_headers[1:]))

    def test_repeated_reads_reuse_cookies_and_close_only_its_own_session(self) -> None:
        with DigestFixture(mode="sessions") as fixture:
            fixture.sessions.add("other-client")
            driver = AbbRobotDriver(fixture.endpoint, username="operator", password="secret")
            for _ in range(80):
                self.assertEqual(driver._fetch_position(), (1.0, 2.0, 3.0))
            self.assertEqual(fixture.sessions_created, 1)
            self.assertEqual(len(fixture.sessions), 2)
            driver.close()
            driver.close()
            self.assertEqual(fixture.logouts, 1)
            self.assertEqual(fixture.sessions, {"other-client"})
            self.assertEqual(len(set(fixture.client_ports)), 1)

    def test_session_limit_and_logout_failure_keep_specific_errors(self) -> None:
        with DigestFixture(mode="session_limit") as fixture:
            driver = AbbRobotDriver(fixture.endpoint, username="operator", password="secret")
            with self.assertRaisesRegex(Exception, "session_limit_exceeded"):
                driver.read_sample()
        with DigestFixture(mode="sessions") as fixture:
            driver = AbbRobotDriver(fixture.endpoint, username="operator", password="secret")
            driver.read_sample()
            fixture.mode = "logout_failed"
            with self.assertRaisesRegex(Exception, "session_close_failed"):
                driver.close()

    def test_digest_requests_bind_the_configured_source_address(self) -> None:
        connect = urllib3.util.connection.create_connection
        with DigestFixture() as fixture, patch(
            "urllib3.util.connection.create_connection", wraps=connect
        ) as connections:
            driver = AbbRobotDriver(
                fixture.endpoint, username="operator", password="secret",
            )
            driver.source_address = "127.0.0.1"
            self.assertEqual(driver._fetch_position(), (1.0, 2.0, 3.0))
            self.assertEqual(driver.actual_source_address, "127.0.0.1")
        self.assertTrue(connections.call_args_list)
        self.assertTrue(all(
            call.kwargs["source_address"] == ("127.0.0.1", 0)
            for call in connections.call_args_list
        ))

    def test_industrial_rws_ignores_system_http_proxy(self) -> None:
        with DigestFixture() as fixture, patch.dict(os.environ, {
            "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": "",
        }):
            driver = AbbRobotDriver(
                fixture.endpoint, username="operator", password="secret", timeout=0.2
            )
            self.assertEqual(driver._fetch_position(), (1.0, 2.0, 3.0))
        self.assertEqual(fixture.successes, 1)

    def test_digest_challenge_succeeds_without_preemptive_basic(self) -> None:
        with DigestFixture() as fixture:
            driver = AbbRobotDriver(
                fixture.endpoint, username="operator", password="secret"
            )
            self.assertEqual(driver._fetch_position(), (1.0, 2.0, 3.0))

        self.assertIsNone(fixture.authorization_headers[0])
        self.assertTrue(
            any((value or "").startswith("Digest ") for value in fixture.authorization_headers)
        )
        self.assertFalse(
            any((value or "").startswith("Basic ") for value in fixture.authorization_headers)
        )

    def test_digest_session_recovers_from_nonce_update(self) -> None:
        with DigestFixture(rotate_nonce=True) as fixture:
            driver = AbbRobotDriver(
                fixture.endpoint, username="operator", password="secret"
            )
            self.assertEqual(driver._fetch_position(), (1.0, 2.0, 3.0))
            self.assertEqual(driver._fetch_position(), (1.0, 2.0, 3.0))

        self.assertEqual(fixture.successes, 2)
        self.assertTrue(any("nonce-b" in (value or "") for value in fixture.authorization_headers))

    def test_wrong_credentials_are_classified_and_redacted(self) -> None:
        with DigestFixture() as fixture:
            driver = AbbRobotDriver(
                fixture.endpoint, username="operator", password="wrong-secret"
            )
            with self.assertRaisesRegex(Exception, "authentication_failed") as caught:
                driver.read_sample()

        self.assertNotIn("wrong-secret", str(caught.exception))
        self.assertFalse(
            any((value or "").startswith("Basic ") for value in fixture.authorization_headers)
        )

    def test_missing_resource_invalid_json_and_timeout_are_classified(self) -> None:
        cases = {
            "missing": ("resource_not_found", 0.2),
            "invalid_json": ("invalid_json", 0.2),
            "timeout": ("transport_timeout", 0.05),
        }
        for mode, (code, timeout) in cases.items():
            with self.subTest(mode=mode), DigestFixture(mode=mode) as fixture:
                driver = AbbRobotDriver(
                    fixture.endpoint,
                    username="operator",
                    password="secret",
                    timeout=timeout,
                )
                with self.assertRaisesRegex(Exception, code):
                    driver.read_sample()

    def test_rws_failures_keep_stable_interface_status_codes(self) -> None:
        for code in (
            "authentication_failed",
            "resource_not_found",
            "resource_invalid",
            "invalid_json",
            "numeric_invalid",
            "transport_timeout",
            "transport_error",
            "http_error",
        ):
            with self.subTest(code=code):
                self.assertEqual(
                    classify_interface_probe_error("robot", f"{code}: detail"),
                    code,
                )
        self.assertEqual(
            classify_interface_probe_error("robot", "unknown failure"),
            "not_connected",
        )

    def test_interface_check_preserves_authentication_failure(self) -> None:
        with DigestFixture() as fixture:
            config = AcquisitionConfig(
                diagnostic_validation=True,
                acquisition_mode="real",
                real_acquisition_mode="local_direct",
                dataset_schema="new_collection_v11_3",
                selected_sensors=["ABB_X", "ABB_Y", "ABB_Z", "线速度"],
                interfaces=[{
                    "id": "abb_motion",
                    "role": "robot",
                    "driver": "abb_robot",
                    "endpoint": fixture.endpoint,
                    "username": "operator",
                    "password": "wrong-secret",
                    "physical_interface_id": "ethernet:以太网",
                    "physical_interface_kind": "ethernet",
                    "physical_verified": True,
                    "max_reconnect_attempts": 0,
                }],
                interface_channel_assignments={
                    "abb_motion": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"]
                },
            )
            route = {
                "ok": True,
                "status": "network_path_ready",
                "target_address": "127.0.0.1",
                "selected_interface_id": "ethernet:以太网",
                "actual_interface_id": "ethernet:以太网",
                "actual_source_address": "127.0.0.1",
                "interface_type": "physical",
                "target_subnet_match": True,
                "best_route": True,
            }
            with patch("acquisition.validate_network_path", return_value=route), patch(
                "acquisition.require_network_path", return_value=route
            ):
                result = AcquisitionManager().test_connection(config)

        self.assertFalse(result["ok"])
        self.assertEqual(result["interfaces"][0]["state"], "authentication_failed")
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("wrong-secret", serialized)

    def test_position_timestamp_and_two_point_speed_quality(self) -> None:
        times = iter((100.0, 102.0))
        positions = iter(((1.0, 2.0, 3.0), (4.0, 6.0, 3.0)))
        driver = AbbRobotDriver(
            "http://fixture", clock=lambda: next(times)
        )
        driver._fetch_position = lambda: next(positions)

        first = driver.read_sample()
        first_timestamp = driver.source_timestamp
        first_quality = dict(driver.quality_metadata)
        second = driver.read_sample()

        self.assertEqual(first, {"ABB_X": 1.0, "ABB_Y": 2.0, "ABB_Z": 3.0})
        self.assertEqual(first_timestamp, 100.0)
        self.assertEqual(first_quality["speed_quality"], "unverified_first_sample")
        self.assertEqual(
            second,
            {
                "ABB_X": 4.0,
                "ABB_Y": 6.0,
                "ABB_Z": 3.0,
                "线速度": 2.5,
            },
        )
        self.assertEqual(driver.source_timestamp, 102.0)
        self.assertEqual(driver.quality_metadata["speed_quality"], "measured_new")
        self.assertEqual(driver.quality_metadata["coordinate_system"], "Base")

    def test_zero_time_delta_keeps_speed_unverified_and_invalid_coordinates_fail(self) -> None:
        driver = AbbRobotDriver("http://fixture", clock=lambda: 100.0)
        positions = iter(((1.0, 2.0, 3.0), (2.0, 2.0, 3.0)))
        driver._fetch_position = lambda: next(positions)
        driver.read_sample()

        second = driver.read_sample()

        self.assertNotIn("线速度", second)
        self.assertEqual(
            driver.quality_metadata["speed_quality"], "unverified_zero_delta"
        )

        invalid = AbbRobotDriver("http://fixture", clock=lambda: 101.0)
        invalid._fetch_position = lambda: (float("nan"), 2.0, 3.0)
        with self.assertRaisesRegex(Exception, "numeric_invalid"):
            invalid.read_sample()

    def test_interface_check_waits_for_second_position_before_data_ready(self) -> None:
        class Driver:
            def __init__(self, *args, **kwargs) -> None:
                self.calls = 0

            def open(self) -> None:
                pass

            def read_sample(self):
                self.calls += 1
                if self.calls == 1:
                    return {"ABB_X": 1.0, "ABB_Y": 2.0, "ABB_Z": 3.0}
                return {
                    "ABB_X": 2.0,
                    "ABB_Y": 2.0,
                    "ABB_Z": 3.0,
                    "线速度": 10.0,
                }

            def close(self) -> None:
                pass

        config = AcquisitionConfig(
            diagnostic_validation=True,
            acquisition_mode="real",
            real_acquisition_mode="local_direct",
            dataset_schema="new_collection_v11_3",
            selected_sensors=["ABB_X", "ABB_Y", "ABB_Z", "线速度"],
            interfaces=[{
                "id": "abb_motion",
                "role": "robot",
                "driver": "abb_robot",
                "endpoint": "192.168.125.1",
                "physical_interface_id": "ethernet:以太网",
                "physical_interface_kind": "ethernet",
                "physical_verified": True,
            }],
            interface_channel_assignments={
                "abb_motion": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"]
            },
        )
        route = {
            "ok": True,
            "status": "network_path_ready",
            "target_address": "192.168.125.1",
            "selected_interface_id": "ethernet:以太网",
            "actual_interface_id": "ethernet:以太网",
            "actual_source_address": "192.168.125.209",
            "interface_type": "physical",
            "target_subnet_match": True,
            "best_route": True,
        }
        with patch("acquisition.validate_network_path", return_value=route), patch(
            "acquisition.MultiInterfaceDriver", Driver
        ):
            result = AcquisitionManager().test_connection(config)

        interface = result["interfaces"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(interface["state"], "data_ready")
        self.assertEqual(interface["missing_channels"], [])
        self.assertEqual(interface["channel_results"]["线速度"]["value"], 10.0)
        self.assertEqual(interface["channel_results"]["线速度"]["unit"], "mm/s")


if __name__ == "__main__":
    unittest.main()
