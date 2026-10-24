"""Shared bearer-token authentication and tenant resolution for service APIs."""
from __future__ import annotations

import hmac
import json
import os
from typing import Any


def resolve_bearer_tenant(authorization: str) -> tuple[str | None, str | None]:
    """Resolve a configured API token to its tenant without trusting request fields.

    Preferred configuration: LEAD_QUALIFIER_API_TOKENS='[{"token":"...","tenant_id":"..."}]'.
    Backward-compatible single-tenant configuration uses LEAD_QUALIFIER_API_TOKEN and
    LEAD_QUALIFIER_TENANT_ID. Empty/malformed configuration fails closed.
    """
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return None, None

    raw = os.getenv("LEAD_QUALIFIER_API_TOKENS", "").strip()
    if raw:
        try:
            entries: Any = json.loads(raw)
        except (TypeError, ValueError):
            return None, None
        if not isinstance(entries, list):
            return None, None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            token = entry.get("token")
            tenant = entry.get("tenant_id")
            if isinstance(token, str) and token and isinstance(tenant, str) and tenant.strip():
                if hmac.compare_digest(supplied, token):
                    return token, tenant.strip()
        return None, None

    token = os.getenv("LEAD_QUALIFIER_API_TOKEN", "").strip()
    tenant = os.getenv("LEAD_QUALIFIER_TENANT_ID", "").strip()
    if token and tenant and hmac.compare_digest(supplied, token):
        return token, tenant
    return None, None
