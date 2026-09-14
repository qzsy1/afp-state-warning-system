from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Any, Mapping


_NON_LAN_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("198.18.0.0/15", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)


@dataclass(frozen=True, slots=True)
class LanWebConfig:
    enabled: bool = True
    bind_host: str = "0.0.0.0"
    port: int = 8770
    open_desktop_window: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "LanWebConfig":
        raw = dict(value or {})
        port = int(raw.get("port", 8770))
        if not 1024 <= port <= 65535:
            raise ValueError("局域网端口必须在 1024-65535 之间")
        bind_host = str(raw.get("bind_host", "0.0.0.0")).strip() or "127.0.0.1"
        if bind_host not in {"0.0.0.0", "127.0.0.1", "localhost"}:
            raise ValueError("局域网绑定地址只允许 0.0.0.0 或本机地址")
        return cls(
            enabled=bool(raw.get("enabled", True)),
            bind_host=bind_host,
            port=port,
            open_desktop_window=bool(raw.get("open_desktop_window", True)),
        )


def _iter_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        address = str(info[4][0]).strip()
        try:
            if ipaddress.ip_address(address).version == 4:
                addresses.add(address)
        except ValueError:
            continue
    return sorted(addresses)


def discover_lan_urls(port: int, bind_host: str = "0.0.0.0") -> list[str]:
    if bind_host not in {"0.0.0.0", ""}:
        try:
            address = ipaddress.ip_address(bind_host)
        except ValueError:
            return []
        if address.is_loopback or address.is_link_local:
            return []
        return [f"http://{address}:{int(port)}/"]

    candidates: list[ipaddress.IPv4Address] = []
    for value in _iter_ipv4_addresses():
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if (
            address.version != 4
            or address.is_loopback
            or address.is_link_local
            or not address.is_private
            or any(address in network for network in _NON_LAN_NETWORKS)
        ):
            continue
        candidates.append(address)
    candidates.sort(key=int)
    return [f"http://{address}:{int(port)}/" for address in candidates]


def safe_network_status(
    config: Mapping[str, Any] | LanWebConfig,
    urls: list[str],
    started_at: float | None,
    *,
    now: float | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    if isinstance(config, LanWebConfig):
        values: Mapping[str, Any] = {
            "enabled": config.enabled,
            "bind_host": config.bind_host,
            "port": config.port,
        }
    else:
        values = config
    current = time.time() if now is None else float(now)
    started = current if started_at is None else float(started_at)
    uptime = round(max(0.0, current - started), 1)
    return {
        "enabled": bool(values.get("enabled", True)),
        "bind_host": str(values.get("bind_host", "0.0.0.0")),
        "port": int(values.get("port", 8770)),
        "urls": [str(url) for url in urls],
        "desktop_url": f"http://127.0.0.1:{int(values.get('port', 8770))}/",
        "service_uptime_seconds": uptime,
        "error": str(error) if error else None,
    }
