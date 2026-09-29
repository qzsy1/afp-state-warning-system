from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


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


_VIRTUAL_INTERFACE_MARKERS = (
    "hyper-v",
    "vethernet",
    "wsl",
    "docker",
    "tailscale",
    "zerotier",
    "wireguard",
    "vpn",
    "tunnel",
    "tap-",
    "vmware",
    "virtualbox",
    "loopback",
)
_CANDIDATE_CACHE_SECONDS = 5.0
_candidate_cache_lock = threading.Lock()
_candidate_cache_at = 0.0
_candidate_cache: list[dict[str, Any]] = []


def _default_route_ipv4() -> str | None:
    """Return the IPv4 selected by the OS default route without sending data."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


def _route_interface_ipv4s() -> set[str]:
    """Read active Windows IPv4 route interface addresses cheaply.

    A local proxy may install a lower-metric 198.18/15 route.  The UDP probe
    then reports that proxy address even though the physical LAN route remains
    usable.  ``route print`` lets us mark both active interfaces without
    guessing from the numeric address alone.
    """

    if sys.platform != "win32":
        return set()
    try:
        completed = subprocess.run(
            ["route", "print", "0.0.0.0"],
            capture_output=True,
            text=True,
            encoding="ascii",
            errors="ignore",
            timeout=1.0,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    addresses: set[str] = set()
    for line in completed.stdout.splitlines():
        fields = re.split(r"\s+", line.strip())
        if len(fields) < 5 or fields[0] != "0.0.0.0" or fields[1] != "0.0.0.0":
            continue
        try:
            interface = ipaddress.ip_address(fields[3])
        except ValueError:
            continue
        if interface.version == 4:
            addresses.add(str(interface))
    return addresses


def _interface_type(name: str) -> str:
    normalized = str(name or "").strip().lower()
    if any(marker in normalized for marker in _VIRTUAL_INTERFACE_MARKERS):
        return "virtual"
    if any(marker in normalized for marker in ("wi-fi", "wifi", "wlan", "wireless", "无线")):
        return "wifi"
    return "ethernet"


def _iter_ipv4_candidates() -> list[dict[str, Any]]:
    """Collect adapter metadata with a dependency-free fallback.

    psutil is optional in the external runtime.  When it is unavailable the
    OS-selected default-route address still wins, which prevents a Hyper-V
    host-only address from becoming the recommendation merely because it sorts
    first numerically.
    """

    global _candidate_cache_at, _candidate_cache
    now = time.monotonic()
    with _candidate_cache_lock:
        if _candidate_cache and now - _candidate_cache_at < _CANDIDATE_CACHE_SECONDS:
            return [dict(item) for item in _candidate_cache]

    default_address = _default_route_ipv4()
    route_interfaces = _route_interface_ipv4s()
    found: dict[str, dict[str, Any]] = {}
    try:
        import psutil  # type: ignore

        stats = psutil.net_if_stats()
        for name, addresses in psutil.net_if_addrs().items():
            interface_type = _interface_type(name)
            is_up = bool(getattr(stats.get(name), "isup", False))
            for item in addresses:
                if item.family != socket.AF_INET:
                    continue
                address = str(item.address or "").strip()
                if not address:
                    continue
                found[address] = {
                    "address": address,
                    "interface_name": str(name),
                    "interface_type": interface_type,
                    "is_up": is_up,
                    "has_default_gateway": address == default_address or address in route_interfaces,
                }
    except (ImportError, OSError, AttributeError):
        pass

    for address in _iter_ipv4_addresses():
        found.setdefault(
            address,
            {
                "address": address,
                "interface_name": "系统网络接口",
                "interface_type": "ethernet" if address == default_address else "unknown",
                "is_up": True,
                "has_default_gateway": address == default_address or address in route_interfaces,
            },
        )
    if default_address:
        found.setdefault(
            default_address,
            {
                "address": default_address,
                "interface_name": "系统默认路由",
                "interface_type": "ethernet",
                "is_up": True,
                "has_default_gateway": True,
            },
        )
    result = list(found.values())
    with _candidate_cache_lock:
        _candidate_cache = [dict(item) for item in result]
        _candidate_cache_at = time.monotonic()
    return result


def _valid_lan_address(value: Any) -> ipaddress.IPv4Address | None:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return None
    if (
        address.version != 4
        or address.is_loopback
        or address.is_link_local
        or not address.is_private
        or any(address in network for network in _NON_LAN_NETWORKS)
    ):
        return None
    return address


def discover_lan_endpoints(
    port: int,
    bind_host: str = "0.0.0.0",
    *,
    candidates: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return compatible URLs plus a deterministic, physically usable choice."""

    if bind_host not in {"0.0.0.0", ""}:
        address = _valid_lan_address(bind_host)
        if address is None:
            return {"recommended_url": None, "urls": [], "candidates": []}
        url = f"http://{address}:{int(port)}/"
        item = {
            "address": str(address),
            "url": url,
            "interface_name": "指定绑定地址",
            "interface_type": "explicit",
            "is_up": True,
            "has_default_gateway": False,
            "recommended": True,
        }
        return {"recommended_url": url, "urls": [url], "candidates": [item]}

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in candidates if candidates is not None else _iter_ipv4_candidates():
        address = _valid_lan_address(raw.get("address"))
        if address is None or str(address) in seen:
            continue
        seen.add(str(address))
        name = str(raw.get("interface_name") or "未知接口")
        interface_type = str(raw.get("interface_type") or _interface_type(name)).lower()
        virtual = interface_type in {"virtual", "tunnel", "vpn"} or any(
            marker in name.lower() for marker in _VIRTUAL_INTERFACE_MARKERS
        )
        normalized.append(
            {
                "address": str(address),
                "url": f"http://{address}:{int(port)}/",
                "interface_name": name,
                "interface_type": interface_type,
                "is_up": bool(raw.get("is_up", True)),
                "has_default_gateway": bool(raw.get("has_default_gateway", False)),
                "eligible": bool(raw.get("is_up", True)) and not virtual,
            }
        )

    def priority(item: Mapping[str, Any]) -> tuple[int, int, int, int]:
        address = ipaddress.ip_address(str(item["address"]))
        return (
            0 if item.get("eligible") else 1,
            0 if item.get("has_default_gateway") else 1,
            0 if item.get("interface_type") in {"ethernet", "wifi", "explicit"} else 1,
            int(address),
        )

    normalized.sort(key=priority)
    recommended = next((item for item in normalized if item["eligible"]), None)
    for item in normalized:
        item["recommended"] = item is recommended
        item.pop("eligible", None)
    return {
        "recommended_url": recommended["url"] if recommended else None,
        "urls": [str(item["url"]) for item in normalized],
        "candidates": normalized,
    }


def discover_lan_urls(port: int, bind_host: str = "0.0.0.0") -> list[str]:
    return list(discover_lan_endpoints(port, bind_host)["urls"])


def safe_network_status(
    config: Mapping[str, Any] | LanWebConfig,
    urls: list[str] | Mapping[str, Any],
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
    if isinstance(urls, Mapping):
        url_values = [str(url) for url in urls.get("urls", [])]
        recommended_url = urls.get("recommended_url")
        candidates = [dict(item) for item in urls.get("candidates", [])]
    else:
        url_values = [str(url) for url in urls]
        recommended_url = url_values[0] if url_values else None
        candidates = []
    return {
        "enabled": bool(values.get("enabled", True)),
        "bind_host": str(values.get("bind_host", "0.0.0.0")),
        "port": int(values.get("port", 8770)),
        "urls": url_values,
        "recommended_url": str(recommended_url) if recommended_url else None,
        "candidates": candidates,
        "desktop_url": f"http://127.0.0.1:{int(values.get('port', 8770))}/",
        "service_uptime_seconds": uptime,
        "error": str(error) if error else None,
    }
