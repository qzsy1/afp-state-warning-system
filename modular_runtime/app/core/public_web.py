from __future__ import annotations

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

    enabled: bool = True
    public_bind_host: str = "0.0.0.0"
    public_port: int = 8770
    local_admin_bind_host: str = "127.0.0.1"
    local_admin_port: int = 8771
    open_desktop_window: bool = True
    domain: str = ""
    cloudflare_tunnel_enabled: bool = False
    cloudflared_path: str = "cloudflared"
    cloudflare_tunnel_name: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "PublicWebConfig":
        values = dict(raw or {})
        public_port = _port(values.get("public_port", values.get("port", 8770)), "public_port", 8770)
        admin_port = _port(values.get("local_admin_port", 8771), "local_admin_port", 8771)
        if public_port == admin_port:
            raise ValueError("public_port and local_admin_port must be different")
        public_host = str(values.get("public_bind_host", values.get("bind_host", "0.0.0.0"))).strip() or "0.0.0.0"
        admin_host = str(values.get("local_admin_bind_host", "127.0.0.1")).strip() or "127.0.0.1"
        if admin_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("local_admin_bind_host must stay on loopback")
        return cls(
            enabled=bool(values.get("enabled", True)),
            public_bind_host=public_host,
            public_port=public_port,
            local_admin_bind_host=admin_host,
            local_admin_port=admin_port,
            open_desktop_window=bool(values.get("open_desktop_window", True)),
            domain=str(values.get("domain", "")).strip(),
            cloudflare_tunnel_enabled=bool(values.get("cloudflare_tunnel_enabled", False)),
            cloudflared_path=str(values.get("cloudflared_path", "cloudflared")).strip() or "cloudflared",
            cloudflare_tunnel_name=str(values.get("cloudflare_tunnel_name", "")).strip(),
        )


def public_web_urls(config: PublicWebConfig, addresses: list[str] | tuple[str, ...]) -> dict[str, Any]:
    public = [f"http://{address}:{config.public_port}/" for address in addresses if str(address).strip()]
    if config.domain:
        public.insert(0, f"https://{config.domain.rstrip('/')}/")
    return {
        "public": public,
        "local_admin": f"http://127.0.0.1:{config.local_admin_port}/",
    }
