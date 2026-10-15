from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class PublicIngressReport:
    overall_status: str
    generation: int
    recovery_owner: str
    public_edge_ok: bool
    helper_ok: bool
    non_sensitive_error: str


def adapt_public_ingress_report(payload: Mapping[str, Any]) -> PublicIngressReport:
    """Expose production readiness output without duplicating its decisions."""
    public_edge = payload.get("public_edge")
    helper = payload.get("helper")
    if not isinstance(public_edge, Mapping) or not isinstance(helper, Mapping):
        raise ValueError("public ingress report is missing layered status")

    return PublicIngressReport(
        overall_status=str(payload.get("overall_status") or "unknown"),
        generation=int(payload.get("generation") or 0),
        recovery_owner=str(payload.get("recovery_owner") or ""),
        public_edge_ok=public_edge.get("ok") is True,
        helper_ok=helper.get("ok") is True,
        non_sensitive_error=str(payload.get("non_sensitive_error") or ""),
    )
