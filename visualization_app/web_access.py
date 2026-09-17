from __future__ import annotations

import ipaddress
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Literal, Mapping


Role = Literal["guest", "authorized", "lan_operator", "local_admin"]

REAL_ACCESS_ROLES = frozenset({"authorized", "lan_operator", "local_admin"})
HELPER_BACKED_ROLES = frozenset({"authorized", "lan_operator"})


def lan_session_id(guest_id: str) -> str:
    """Return a stable per-browser LAN session without sharing admin state."""

    value = str(guest_id or "").strip()
    if not value:
        raise ValueError("LAN会话缺少浏览器标识")
    return f"lan-{value}"


def uses_local_capture_helper(role: str) -> bool:
    """Remote real-control roles execute hardware on their visitor computer."""

    return str(role or "") in HELPER_BACKED_ROLES


PUBLIC_GET = {
    "/api/health",
    "/api/network/status",
    "/api/auth/session",
    "/api/public/device-status",
    "/api/simulation/status",
    "/api/simulation/dataset",
    "/api/simulation/live",
    "/api/simulation/ws",
    "/api/simulation/export-manifest",
    "/api/simulation/export-file",
    "/api/simulation/download",
    "/api/helper/ws",
    "/api/agent/defaults",
    "/api/bootstrap",
}
PUBLIC_POST = {
    "/api/auth/login",
    "/api/simulation/start",
    "/api/simulation/stop",
    "/api/simulation/select-source",
    "/api/simulation/upload-source",
    "/api/simulation/process-parameters",
    "/api/agent/diagnose",
    "/api/helper/pair/complete",
    "/api/helper/poll",
    "/api/helper/result",
    "/api/helper/samples",
}
AUTHORIZED_GET = {
    "/api/real/control/status",
    "/api/training/status",
    "/api/training/defaults",
    "/api/bootstrap",
    "/api/mysql/defaults",
    "/api/acquisition/status",
    "/api/acquisition/save-status",
    "/api/acquisition/discover",
    "/api/helper/status",
    "/api/helper/result",
    "/api/live",
    "/api/live/ws",
    "/api/view",
    "/api/realtime",
    "/api/acquisition/export-manifest",
    "/api/acquisition/export-file",
    "/api/agent/diagnose/result",
}
AUTHORIZED_POST = {
    "/api/real/control/acquire",
    "/api/real/control/heartbeat",
    "/api/real/control/release",
    "/api/auth/logout",
    "/api/acquisition/test",
    "/api/acquisition/reset-check",
    "/api/acquisition/start",
    "/api/acquisition/stop",
    "/api/acquisition/process-parameters",
    "/api/acquisition/select-folder",
    "/api/acquisition/select-source",
    "/api/acquisition/integrate",
    "/api/training/import",
    "/api/training/start",
    "/api/training/stop",
    "/api/training/select-file",
    "/api/mysql/test",
    "/api/mysql/preflight",
    "/api/mysql/relation-map",
    "/api/mysql/query",
    "/api/mysql/export-csv",
    "/api/helper/pair/start",
    "/api/helper/pair/complete",
    "/api/helper/command",
    "/api/prediction-model/select-file",
    "/api/prediction-model/inspect",
    "/api/agent/diagnose/start",
}
AUTHORIZED_PREFIXES = ("/api/acquisition/", "/api/training/", "/api/mysql/", "/api/real/")
AUTHORIZED_EXACT = {
    "/api/agent/diagnose",
    "/api/agent/diagnose/start",
    "/api/auth/logout",
}
LOCAL_ADMIN_PREFIX = "/api/admin/"
LOCAL_ADMIN_GET = {"/api/admin/status", "/api/admin/security/settings"}
LOCAL_ADMIN_POST = {
    "/api/admin/security/settings",
    "/api/admin/security/configure",
    "/api/admin/security/revoke-all",
    "/api/admin/security/revoke",
    "/api/admin/simulation/cleanup",
    "/api/admin/real-control/takeover",
}


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    role: Role
    session_id: str | None
    guest_id: str


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    error: str = ""
    status: int = 200


class PermissionPolicy:
    """Allow only explicitly classified API routes."""

    @staticmethod
    def _known(method: str, path: str) -> bool:
        method = str(method).upper()
        if path.startswith(LOCAL_ADMIN_PREFIX):
            return True
        if method == "GET":
            return path in PUBLIC_GET or path in AUTHORIZED_GET
        if method == "POST":
            return path in PUBLIC_POST or path in AUTHORIZED_POST
        return False

    def authorize(
        self, method: str, path: str, identity: RequestIdentity
    ) -> AccessDecision:
        method = str(method).upper()
        path = str(path)
        if not self._known(method, path):
            return AccessDecision(False, "route_not_found", 404)
        if path.startswith(LOCAL_ADMIN_PREFIX):
            if identity.role == "local_admin":
                return AccessDecision(True)
            return AccessDecision(False, "local_admin_required", 403)
        public_routes = PUBLIC_GET if method == "GET" else PUBLIC_POST
        if path in public_routes:
            return AccessDecision(True)
        if identity.role in REAL_ACCESS_ROLES:
            return AccessDecision(True)
        return AccessDecision(False, "real_access_required", 403)


def is_secure_request(
    peer_host: str,
    headers: Mapping[str, str],
    access_context: str,
) -> bool:
    """Trust HTTPS forwarding only from the local Cloudflare connector."""

    peer = str(peer_host or "").strip().lower()
    loopback = peer in {"127.0.0.1", "::1", "localhost"}
    if str(access_context) == "local_admin" and loopback:
        return True
    # A browser running on the acquisition PC may use the public listener via
    # loopback for setup/testing.  This does not extend to LAN clients: their
    # peer address is non-loopback and still requires Cloudflare HTTPS.
    host = str(headers.get("Host", "")).split(":", 1)[0].strip().lower()
    if loopback and host in {"127.0.0.1", "localhost", "::1"}:
        return True
    forwarded = str(headers.get("X-Forwarded-Proto", "")).split(",", 1)[0]
    return bool(loopback and forwarded.strip().lower() == "https")


def is_lan_client(peer_host: str, headers: Mapping[str, str]) -> bool:
    """Return true for a direct private-network client, not a tunnel proxy.

    The public listener is shared by LAN and Cloudflare traffic.  Cloudflare
    connects to the origin over loopback and adds ``CF-Connecting-IP``; a
    direct LAN browser instead arrives with its private address and no proxy
    identity.  Keep loopback out of this check so the public loopback tests
    and the separate local-admin listener retain their existing semantics.
    """

    peer = str(peer_host or "").strip()
    if not peer or peer in {"127.0.0.1", "::1", "localhost"}:
        return False
    if str(headers.get("CF-Connecting-IP", "")).strip():
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return bool(address.is_private)


def is_trusted_quick_tunnel_request(
    peer_host: str,
    host: str,
    headers: Mapping[str, str],
) -> bool:
    """Accept a random Quick Tunnel host only from a local HTTPS proxy.

    Quick Tunnels use a new ``*.trycloudflare.com`` hostname on each run and
    therefore cannot be preconfigured in the application's host allow-list.
    ``cloudflared`` connects to the origin over loopback and forwards HTTPS;
    requiring both properties prevents a remote client from spoofing the
    forwarded headers to bypass the allow-list.
    """

    peer = str(peer_host or "").strip().lower()
    normalized_host = str(host or "").strip().lower().split(":", 1)[0].rstrip(".")
    forwarded = str(headers.get("X-Forwarded-Proto", "")).split(",", 1)[0]
    has_cloudflare_ip = bool(str(headers.get("CF-Connecting-IP", "")).strip())
    return (
        peer in {"127.0.0.1", "::1", "localhost"}
        and normalized_host.endswith(".trycloudflare.com")
        and forwarded.strip().lower() == "https"
        and has_cloudflare_ip
    )


class SlidingWindowLimiter:
    """Thread-safe fixed-count limiter with an optional temporary block."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._blocked_until: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def _prune(
        self, bucket: str, key: str, window_seconds: float, now: float
    ) -> deque[float]:
        events = self._events[(str(bucket), str(key))]
        cutoff = now - max(0.001, float(window_seconds))
        while events and events[0] <= cutoff:
            events.popleft()
        return events

    def allow(
        self,
        bucket: str,
        key: str,
        limit: int,
        window_seconds: float,
        *,
        now: float | None = None,
    ) -> bool:
        current = time.monotonic() if now is None else float(now)
        identity = (str(bucket), str(key))
        with self._lock:
            if self._blocked_until.get(identity, 0.0) > current:
                return False
            events = self._prune(bucket, key, window_seconds, current)
            if len(events) >= max(1, int(limit)):
                return False
            events.append(current)
            return True

    def count(
        self,
        bucket: str,
        key: str,
        window_seconds: float,
        *,
        now: float | None = None,
    ) -> int:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            return len(self._prune(bucket, key, window_seconds, current))

    def block(
        self,
        bucket: str,
        key: str,
        seconds: float,
        *,
        now: float | None = None,
    ) -> None:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            self._blocked_until[(str(bucket), str(key))] = current + max(
                0.0, float(seconds)
            )

    def blocked(
        self, bucket: str, key: str, *, now: float | None = None
    ) -> bool:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            return self._blocked_until.get((str(bucket), str(key)), 0.0) > current

    def clear(self, bucket: str, key: str) -> None:
        identity = (str(bucket), str(key))
        with self._lock:
            self._events.pop(identity, None)
            self._blocked_until.pop(identity, None)
