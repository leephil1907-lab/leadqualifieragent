"""Shared scoped bearer-token authentication and tenant resolution."""
from __future__ import annotations

import hmac
import json
import os
from typing import Any


def resolve_bearer_tenant(authorization: str, *, purpose: str = "intake") -> tuple[str | None, str | None]:
    """Resolve a token to a tenant; request bodies never determine tenant identity.

    Configure JSON token maps as arrays of {token, tenant_id}; API tokens may optionally
    include scopes, e.g. ["intake", "status"]. Review endpoints use the separate
    LEAD_QUALIFIER_REVIEW_TOKENS map, never the intake token by default.
    """
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return None, None

    if purpose == "review":
        raw = os.getenv("LEAD_QUALIFIER_REVIEW_TOKENS", "").strip()
        if not raw:
            return None, None
    else:
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
            scopes = entry.get("scopes", ["intake", "status"])
            if purpose == "review" and purpose not in scopes:
                continue
            if purpose != "review" and purpose not in scopes:
                continue
            if isinstance(token, str) and token and isinstance(tenant, str) and tenant.strip():
                if hmac.compare_digest(supplied, token):
                    return token, tenant.strip()
        return None, None

    # Backward-compatible, single-tenant intake/status token only.
    if purpose == "review":
        return None, None
    token = os.getenv("LEAD_QUALIFIER_API_TOKEN", "").strip()
    tenant = os.getenv("LEAD_QUALIFIER_TENANT_ID", "").strip()
    if token and tenant and hmac.compare_digest(supplied, token):
        return token, tenant
    return None, None
