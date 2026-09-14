from __future__ import annotations

import ipaddress
import subprocess
from dataclasses import dataclass
from typing import Any, Mapping


def _port(value: Any, name: str, default: int) -> int:
    try:
        port = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise ValueError(f"{name} must be between 1024 and 65535")
    return port


@dataclass(frozen=True, slots=True)
class PublicWebConfig:
    """Runtime wiring for the public guest site and loopback admin site."""

    enabled: bool = False
    hostname: str = ""
    public_bind_host: str = "0.0.0.0"
    public_port: int = 8770
    local_admin_bind_host: str = "127.0.0.1"
    local_admin_port: int = 8771
    open_desktop_window: bool = True
    domain: str = ""
    cloudflare_tunnel_enabled: bool = False
    cloudflared_path: str = "cloudflared"
    cloudflare_tunnel_name: str = ""
    cloudflared_service: str = "cloudflared"
    guest_session_quota_mb: int = 256
    guest_total_quota_mb: int = 2048
    max_running_guest_sessions: int = 4

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "PublicWebConfig":
        values = dict(raw or {})
        hostname = str(values.get("hostname", values.get("domain", ""))).strip().lower()
        if values.get("enabled", False) and hostname:
            if "://" in hostname or "/" in hostname or ":" in hostname:
                raise ValueError("hostname must contain only a DNS name")
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                pass
            else:
                raise ValueError("hostname must be a DNS name, not an IP address")
        public_port = _port(values.get("public_port", values.get("port", 8770)), "public_port", 8770)
        admin_port = _port(values.get("local_admin_port", 8771), "local_admin_port", 8771)
        if public_port == admin_port:
            raise ValueError("public_port and local_admin_port must be different")
        public_host = str(values.get("public_bind_host", values.get("bind_host", "0.0.0.0"))).strip() or "0.0.0.0"
        admin_host = str(values.get("local_admin_bind_host", "127.0.0.1")).strip() or "127.0.0.1"
        if admin_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("local_admin_bind_host must stay on loopback")
        return cls(
            enabled=bool(values.get("enabled", False)),
            hostname=hostname,
            public_bind_host=public_host,
            public_port=public_port,
            local_admin_bind_host=admin_host,
            local_admin_port=admin_port,
            open_desktop_window=bool(values.get("open_desktop_window", True)),
            domain=str(values.get("domain", "")).strip(),
            cloudflare_tunnel_enabled=bool(values.get("cloudflare_tunnel_enabled", False)),
            cloudflared_path=str(values.get("cloudflared_path", "cloudflared")).strip() or "cloudflared",
            cloudflare_tunnel_name=str(values.get("cloudflare_tunnel_name", "")).strip(),
            cloudflared_service=str(values.get("cloudflared_service", "cloudflared")).strip() or "cloudflared",
            guest_session_quota_mb=int(values.get("guest_session_quota_mb", 256)),
            guest_total_quota_mb=int(values.get("guest_total_quota_mb", 2048)),
            max_running_guest_sessions=int(values.get("max_running_guest_sessions", 4)),
        )

    @property
    def origin_url(self) -> str:
        return f"http://127.0.0.1:{self.public_port}"


def public_web_urls(config: PublicWebConfig, addresses: list[str] | tuple[str, ...]) -> dict[str, Any]:
    public = [f"http://{address}:{config.public_port}/" for address in addresses if str(address).strip()]
    if config.hostname:
        public.insert(0, f"https://{config.hostname}/")
    return {
        "public": public,
        "local_admin": f"http://127.0.0.1:{config.local_admin_port}/",
    }


def inspect_cloudflared_service(service_name: str = "cloudflared") -> dict[str, object]:
    """Return a safe service summary without exposing installation output."""
    name = str(service_name or "cloudflared").strip() or "cloudflared"
    try:
        completed = subprocess.run(
            ["sc.exe", "query", name],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except FileNotFoundError:
        return {"installed": False, "running": False, "service_name": name, "error_code": "sc_unavailable"}
    except subprocess.TimeoutExpired:
        return {"installed": True, "running": False, "service_name": name, "error_code": "query_timeout"}
    installed = completed.returncode == 0
    output = (completed.stdout or "").upper()
    return {
        "installed": installed,
        "running": installed and "RUNNING" in output,
        "service_name": name,
        "error_code": None if installed else "service_not_found",
    }
