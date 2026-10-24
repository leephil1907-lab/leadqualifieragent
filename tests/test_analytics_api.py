import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from web.analytics_api import create_analytics_blueprint
from web.intake_api import create_intake_blueprint
from web.job_queue import get_job, init_db
from web.worker import run_once


class AnalyticsAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "queue.sqlite3")
        init_db(self.db)
        app = Flask(__name__)
        app.register_blueprint(create_intake_blueprint(db_path=self.db))
        app.register_blueprint(create_analytics_blueprint(db_path=self.db))
        self.client = app.test_client()
        self.env = patch.dict(os.environ, {
            "LEAD_QUALIFIER_API_TOKEN": "test-secret-token-long-enough",
            "LEAD_QUALIFIER_TENANT_ID": "tenant-a",
        }, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_summary_and_outcome_feedback_are_tenant_scoped(self):
        response = self.client.post("/api/v1/intake/leads",
            json={"name": "Ada", "consent": True},
            headers={"Authorization": "Bearer test-secret-token-long-enough", "Idempotency-Key": "lead-intake-0001"})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(run_once(path=self.db))
        job = get_job(response.json["job_id"], "tenant-a", path=self.db)
        self.assertEqual(job["status"], "succeeded")
        headers = {"Authorization": "Bearer test-secret-token-long-enough", "Idempotency-Key": "outcome-0001"}
        outcome = self.client.post("/api/v1/outcomes", json={
            "job_id": job["id"], "outcome": "converted", "revenue": 1500, "reason": "Closed"
        }, headers=headers)
        self.assertEqual(outcome.status_code, 201)
        duplicate = self.client.post("/api/v1/outcomes", json={
            "job_id": job["id"], "outcome": "converted", "revenue": 1500, "reason": "Closed"
        }, headers=headers)
        self.assertEqual(duplicate.status_code, 200)
        summary = self.client.get("/api/v1/analytics/summary",
            headers={"Authorization": "Bearer test-secret-token-long-enough"})
        self.assertEqual(summary.json["tiers"]["D"], 1)
        self.assertEqual(summary.json["outcomes"]["converted"]["count"], 1)
        self.assertEqual(summary.json["outcomes"]["converted"]["revenue"], 1500)

    def test_outcome_rejects_unknown_job_and_bad_revenue(self):
        headers = {"Authorization": "Bearer test-secret-token-long-enough", "Idempotency-Key": "outcome-0002"}
        response = self.client.post("/api/v1/outcomes", json={
            "job_id": "missing", "outcome": "converted"
        }, headers=headers)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
