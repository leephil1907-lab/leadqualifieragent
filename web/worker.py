"""Runnable worker for durable lead-qualification jobs.

Run with: python -m web.worker
SQLite is intended for one host and modest throughput; migrate to PostgreSQL or a
managed queue before deploying multiple worker/application instances.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any

from web.job_queue import claim_next, complete, fail, init_db, recover_stale
from web.lead_qualifier import _score
from web.website_audit import audit_html_snapshot

LOG = logging.getLogger("lead_qualifier.worker")
_STOP = False


def process_job(job: dict[str, Any]) -> dict[str, Any]:
    """Process a qualification job without external side effects."""
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Job payload must be an object")
    if job.get("job_type") == "audit_html_snapshot":
        html = payload.get("html")
        if not isinstance(html, str):
            raise ValueError("Audit job payload must contain an HTML snapshot")
        return {
            "audit": audit_html_snapshot(html),
            "review": {"required": True, "status": "pending",
                       "reason": "Review audit findings before making recommendations or changes."},
            "external_actions_allowed": False,
        }
    if job.get("job_type") != "qualify_lead":
        raise ValueError("Unsupported job type")
    lead = payload.get("lead")
    if not isinstance(lead, dict):
        raise ValueError("Job payload must contain a lead object")
    qualification = _score(lead)
    # This is a workflow guard, not a change to the scoring result.
    return {
        "qualification": qualification,
        "lead": lead,
        "review": {
            "required": True,
            "status": "pending",
            "reason": "Human review is required before external actions.",
        },
        "external_actions_allowed": False,
    }


def run_once(*, path: str | None = None) -> bool:
    job = claim_next(path=path)
    if job is None:
        return False
    tenant = job["tenant_id"]
    try:
        result = process_job(job)
        if not complete(job["id"], tenant, result, path=path):
            LOG.error("Could not complete job %s: job is no longer processing", job["id"])
    except Exception as exc:
        LOG.exception("Job %s failed", job.get("id"))
        fail(job["id"], tenant, f"{type(exc).__name__}: {exc}", path=path)
    return True


def run_forever(*, path: str | None = None, poll_seconds: float = 1.0,
                lease_seconds: float = 300.0) -> None:
    global _STOP
    _STOP = False
    init_db(path)
    recover_stale(path=path, lease_seconds=lease_seconds)

    def stop(_signum: int, _frame: object) -> None:
        global _STOP
        _STOP = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    delay = max(0.1, min(float(poll_seconds), 60.0))
    while not _STOP:
        try:
            worked = run_once(path=path)
            if not worked:
                recover_stale(path=path, lease_seconds=lease_seconds)
                time.sleep(delay)
        except Exception:
            LOG.exception("Worker loop failure")
            time.sleep(delay)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    run_forever()
