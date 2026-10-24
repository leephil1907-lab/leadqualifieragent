"""Small durable SQLite job queue with idempotency and dead-letter handling.

This is a storage primitive, not a background worker. Run workers separately and
claim jobs with claim_next(). Use a shared SQLite file only for modest workloads;
move the same contract to PostgreSQL before multi-instance/high-throughput use.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
import time
import uuid
from pathlib import Path
from typing import Any


def _database_path(path: str | None = None) -> str:
    value = path or os.environ.get("LEAD_QUALIFIER_DB", "instance/lead_qualifier.sqlite3")
    if value != ":memory:":
        Path(value).parent.mkdir(parents=True, exist_ok=True)
    return value


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(_database_path(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def connection(path: str | None = None):
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(path: str | None = None) -> None:
    with connection(path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS qualifier_jobs (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            job_type TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','processing','succeeded','dead')),
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            available_at REAL NOT NULL,
            locked_at REAL,
            last_error TEXT,
            result TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(tenant_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_qualifier_jobs_ready
            ON qualifier_jobs(status, available_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_qualifier_jobs_tenant
            ON qualifier_jobs(tenant_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS qualifier_job_events (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES qualifier_jobs(id),
            tenant_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_qualifier_job_events_job
            ON qualifier_job_events(tenant_id, job_id, created_at);
        """)


def _event(conn: sqlite3.Connection, job_id: str, tenant_id: str,
           event_type: str, detail: dict[str, Any], now: float) -> None:
    conn.execute(
        "INSERT INTO qualifier_job_events(id,job_id,tenant_id,event_type,detail,created_at) VALUES(?,?,?,?,?,?)",
        (uuid.uuid4().hex, job_id, tenant_id, event_type,
         json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
    )


def enqueue(tenant_id: str, job_type: str, payload: dict[str, Any],
            idempotency_key: str, *, path: str | None = None,
            max_attempts: int = 5, now: float | None = None) -> tuple[dict[str, Any], bool]:
    """Create one job per tenant/idempotency key; returns (job, created)."""
    tenant_id = str(tenant_id or "").strip()
    job_type = str(job_type or "").strip()
    key = str(idempotency_key or "").strip()
    if not tenant_id or len(tenant_id) > 128:
        raise ValueError("tenant_id is required and must be at most 128 characters")
    if not job_type or len(job_type) > 100:
        raise ValueError("job_type is required and must be at most 100 characters")
    if not key or len(key) > 200:
        raise ValueError("idempotency_key is required and must be at most 200 characters")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 20:
        raise ValueError("max_attempts must be between 1 and 20")
    stamp = time.time() if now is None else float(now)
    job_id = uuid.uuid4().hex
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """INSERT INTO qualifier_jobs
                (id,tenant_id,job_type,idempotency_key,payload,status,attempts,max_attempts,
                 available_at,created_at,updated_at)
                VALUES(?,?,?,?,?,'queued',0,?,?,?,?)""",
                (job_id, tenant_id, job_type, key,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 max_attempts, stamp, stamp, stamp),
            )
            _event(conn, job_id, tenant_id, "queued", {"job_type": job_type}, stamp)
            row = conn.execute("SELECT * FROM qualifier_jobs WHERE id=?", (job_id,)).fetchone()
            conn.commit()
            return _as_dict(row), True
        except sqlite3.IntegrityError:
            conn.rollback()
            row = conn.execute(
                "SELECT * FROM qualifier_jobs WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, key),
            ).fetchone()
            if row is None:
                raise
            return _as_dict(row), False
        except Exception:
            conn.rollback()
            raise


def _as_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    if item.get("result"):
        item["result"] = json.loads(item["result"])
    return item


def claim_next(*, path: str | None = None, now: float | None = None) -> dict[str, Any] | None:
    """Atomically claim the oldest ready job; concurrent workers cannot claim it twice."""
    stamp = time.time() if now is None else float(now)
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT * FROM qualifier_jobs
               WHERE status='queued' AND available_at<=?
               ORDER BY available_at,created_at,id LIMIT 1""", (stamp,)
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        updated = conn.execute(
            """UPDATE qualifier_jobs SET status='processing',attempts=attempts+1,
               locked_at=?,updated_at=? WHERE id=? AND status='queued'""",
            (stamp, stamp, row["id"]),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return None
        _event(conn, row["id"], row["tenant_id"], "claimed",
               {"attempt": row["attempts"] + 1}, stamp)
        claimed = conn.execute("SELECT * FROM qualifier_jobs WHERE id=?", (row["id"],)).fetchone()
        conn.commit()
        return _as_dict(claimed)


def complete(job_id: str, tenant_id: str, result: dict[str, Any] | None = None,
             *, path: str | None = None, now: float | None = None) -> bool:
    stamp = time.time() if now is None else float(now)
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM qualifier_jobs WHERE id=? AND tenant_id=?",
            (job_id, tenant_id),
        ).fetchone()
        if row is None or row["status"] != "processing":
            conn.rollback()
            return False
        conn.execute(
            """UPDATE qualifier_jobs SET status='succeeded',result=?,locked_at=NULL,
               last_error=NULL,updated_at=? WHERE id=? AND tenant_id=?""",
            (json.dumps(result or {}, ensure_ascii=False, sort_keys=True), stamp, job_id, tenant_id),
        )
        _event(conn, job_id, tenant_id, "succeeded", {}, stamp)
        conn.commit()
        return True


def fail(job_id: str, tenant_id: str, error: str, *, path: str | None = None,
         now: float | None = None, base_delay: float = 5.0, max_delay: float = 3600.0) -> dict[str, Any] | None:
    """Retry with capped exponential backoff; exhausted jobs become dead letters."""
    stamp = time.time() if now is None else float(now)
    safe_error = str(error or "Unknown job error")[:1000]
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM qualifier_jobs WHERE id=? AND tenant_id=? AND status='processing'",
            (job_id, tenant_id),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        exhausted = row["attempts"] >= row["max_attempts"]
        status = "dead" if exhausted else "queued"
        delay = min(max_delay, base_delay * (2 ** max(0, row["attempts"] - 1)))
        available = stamp if exhausted else stamp + max(0.0, delay)
        conn.execute(
            """UPDATE qualifier_jobs SET status=?,available_at=?,locked_at=NULL,last_error=?,
               updated_at=? WHERE id=? AND tenant_id=?""",
            (status, available, safe_error, stamp, job_id, tenant_id),
        )
        _event(conn, job_id, tenant_id, "dead_letter" if exhausted else "retry_scheduled",
               {"attempt": row["attempts"], "error": safe_error, "retry_at": available}, stamp)
        result = conn.execute("SELECT * FROM qualifier_jobs WHERE id=?", (job_id,)).fetchone()
        conn.commit()
        return _as_dict(result)


def recover_stale(*, path: str | None = None, now: float | None = None,
                 lease_seconds: float = 300.0) -> dict[str, int]:
    """Recover jobs whose worker lease expired, recording every recovery.

    Jobs that have exhausted attempts become dead; others return to the queue.
    A worker should use a lease longer than its expected processing duration.
    """
    stamp = time.time() if now is None else float(now)
    lease = max(1.0, float(lease_seconds))
    cutoff = stamp - lease
    counts = {"requeued": 0, "dead": 0}
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT * FROM qualifier_jobs
               WHERE status='processing' AND locked_at IS NOT NULL AND locked_at<=?
               ORDER BY locked_at,id""", (cutoff,)
        ).fetchall()
        for row in rows:
            exhausted = row["attempts"] >= row["max_attempts"]
            status = "dead" if exhausted else "queued"
            conn.execute(
                """UPDATE qualifier_jobs SET status=?,available_at=?,locked_at=NULL,
                   last_error=?,updated_at=? WHERE id=? AND status='processing'""",
                (status, stamp, "Worker lease expired", stamp, row["id"]),
            )
            event_type = "dead_letter" if exhausted else "lease_expired"
            _event(conn, row["id"], row["tenant_id"], event_type,
                   {"attempt": row["attempts"], "lease_seconds": lease}, stamp)
            counts["dead" if exhausted else "requeued"] += 1
        conn.commit()
    return counts


def get_job(job_id: str, tenant_id: str, *, path: str | None = None) -> dict[str, Any] | None:
    with connection(path) as conn:
        row = conn.execute(
            "SELECT * FROM qualifier_jobs WHERE id=? AND tenant_id=?", (job_id, tenant_id)
        ).fetchone()
        return _as_dict(row) if row else None


def list_events(job_id: str, tenant_id: str, *, path: str | None = None) -> list[dict[str, Any]]:
    with connection(path) as conn:
        rows = conn.execute(
            """SELECT event_type,detail,created_at FROM qualifier_job_events
               WHERE job_id=? AND tenant_id=? ORDER BY created_at,rowid""",
            (job_id, tenant_id),
        ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]


def list_pending_reviews(tenant_id: str, *, path: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """List completed qualification jobs awaiting a human decision for one tenant."""
    safe_limit = max(1, min(int(limit), 200))
    with connection(path) as conn:
        rows = conn.execute(
            """SELECT id,job_type,result,created_at,updated_at FROM qualifier_jobs
               WHERE tenant_id=? AND status='succeeded' ORDER BY updated_at DESC LIMIT ?""",
            (tenant_id, safe_limit * 5),
        ).fetchall()
    pending = []
    for row in rows:
        result = json.loads(row["result"] or "{}")
        review = result.get("review") if isinstance(result, dict) else None
        if isinstance(review, dict) and review.get("required") and review.get("status") == "pending":
            pending.append({
                "job_id": row["id"], "job_type": row["job_type"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "qualification": result.get("qualification", {}),
                "lead": result.get("lead", {}),
            })
            if len(pending) >= safe_limit:
                break
    return pending


def decide_review(job_id: str, tenant_id: str, decision: str, reviewer: str,
                  reason: str, *, path: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Record a tenant-scoped human decision; never trigger an external action."""
    decision = str(decision or "").strip().lower()
    reviewer = str(reviewer or "").strip()[:200]
    reason = str(reason or "").strip()[:1000]
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    if not reviewer or not reason:
        raise ValueError("reviewer and reason are required")
    stamp = time.time() if now is None else float(now)
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM qualifier_jobs WHERE id=? AND tenant_id=? AND status='succeeded'",
            (job_id, tenant_id),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise LookupError("Job not found")
        result = json.loads(row["result"] or "{}")
        review = result.get("review") if isinstance(result, dict) else None
        if not isinstance(review, dict) or not review.get("required"):
            conn.rollback()
            raise ValueError("This job does not require human review")
        if review.get("status") != "pending":
            conn.rollback()
            raise ValueError("A review decision has already been recorded")
        review.update({"status": decision, "reviewer": reviewer, "reason": reason, "decided_at": stamp})
        conn.execute("UPDATE qualifier_jobs SET result=?,updated_at=? WHERE id=? AND tenant_id=?",
                     (json.dumps(result, ensure_ascii=False, sort_keys=True), stamp, job_id, tenant_id))
        _event(conn, job_id, tenant_id, "review_" + decision,
               {"reviewer": reviewer, "reason": reason}, stamp)
        conn.commit()
        return {"job_id": job_id, "decision": decision, "reviewer": reviewer,
                "reason": reason, "decided_at": stamp, "external_actions_allowed": False}
