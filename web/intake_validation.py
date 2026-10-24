"""Strict, side-effect-free normalization for inbound lead payloads."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


FIELD_LIMITS = {
    "name": 200, "company": 200, "email": 254, "phone": 80,
    "website": 1000, "industry": 100, "pain_point": 2000, "goal": 1000,
    "budget": 200, "timeline": 200, "authority": 50,
    "preferred_next_step": 100, "source": 100,
}
EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


class IntakeValidationError(ValueError):
    """A safe, user-correctable intake validation error."""


def normalize_website(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if any(ord(ch) < 32 for ch in raw) or len(raw) > FIELD_LIMITS["website"]:
        raise IntakeValidationError("website must be a valid HTTP or HTTPS URL")
    candidate = raw if "://" in raw else "https://" + raw
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise IntakeValidationError("website must be a valid HTTP or HTTPS URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise IntakeValidationError("website must be a valid HTTP or HTTPS URL")
    if parsed.username or parsed.password:
        raise IntakeValidationError("website must not contain embedded credentials")
    if any(ch.isspace() for ch in hostname) or "." not in hostname or hostname.startswith(".") or hostname.endswith("."):
        raise IntakeValidationError("website must include a valid public-style hostname")
    netloc = hostname.lower()
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parsed.path or ""
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def normalize_lead(payload: object) -> dict[str, str]:
    """Return only recognized bounded fields; unknown client keys are discarded."""
    if not isinstance(payload, dict):
        raise IntakeValidationError("request body must be a JSON object")
    result: dict[str, str] = {}
    for key, limit in FIELD_LIMITS.items():
        value = payload.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, (str, int, float)):
            raise IntakeValidationError(f"{key} must be a string")
        text = str(value).strip()
        if len(text) > limit:
            raise IntakeValidationError(f"{key} exceeds the {limit}-character limit")
        if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
            raise IntakeValidationError(f"{key} contains invalid control characters")
        result[key] = text
    result["email"] = result["email"].lower()
    if result["email"] and not EMAIL_RE.fullmatch(result["email"]):
        raise IntakeValidationError("email must be a valid email address")
    result["website"] = normalize_website(result["website"])
    if not result["name"] and not result["company"]:
        raise IntakeValidationError("name or company is required")
    result["name"] = result["name"] or result["company"]
    result["source"] = result["source"] or "website_form"
    return result
