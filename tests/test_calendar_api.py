import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

from web.calendar_api import create_calendar_blueprint
from web.intake_api import create_intake_blueprint
from web.job_queue import get_job, init_db
from web.review_api import create_review_blueprint
from web.worker import run_once


class CalendarAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "queue.sqlite3")
        init_db(self.db)
        app = Flask(__name__)
        app.register_blueprint(create_intake_blueprint(db_path=self.db))
        app.register_blueprint(create_review_blueprint(db_path=self.db))
        app.register_blueprint(create_calendar_blueprint(db_path=self.db))
        self.client = app.test_client()
        self.env = patch.dict(os.environ, {
            "LEAD_QUALIFIER_API_TOKENS": json.dumps([
                {"token": "booking-token", "tenant_id": "tenant-a", "scopes": ["booking"]},
                {"token": "intake-token", "tenant_id": "tenant-a", "scopes": ["intake", "status"]},
            ]),
            "LEAD_QUALIFIER_API_TOKEN": "intake-token",
            "LEAD_QUALIFIER_TENANT_ID": "tenant-a",
            "LEAD_QUALIFIER_REVIEW_TOKENS": json.dumps([{"token": "review-token", "tenant_id": "tenant-a", "scopes": ["review"]}]),
            "CAL_COM_API_KEY": "test-cal-key",
            "CAL_COM_EVENT_TYPE_ID": "123",
        }, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _approved_job(self):
        r = self.client.post("/api/v1/intake/leads", json={
            "name": "Ada", "email": "ada@example.com", "consent": True,
            "consent_version": "v1", "consent_purpose": "lead_qualification"
        }, headers={"Authorization": "Bearer intake-token", "Idempotency-Key": "calendar-lead-001"})
        self.assertEqual(r.status_code, 202)
        self.assertTrue(run_once(path=self.db))
        job = get_job(r.json["job_id"], "tenant-a", path=self.db)
        self.client.post("/api/v1/reviews/" + job["id"],
            json={"decision": "approved", "reviewer": "founder", "reason": "Verified request"},
            headers={"Authorization": "Bearer review-token"})
        return job["id"]

    @patch("web.calendar_api.requests.post")
    def test_booking_requires_review_approval_and_is_idempotent(self, post):
        job_id = self._approved_job()
        post.return_value = Mock()
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {"data": {"uid": "booking-123", "status": "accepted", "start": "2027-01-01T10:00:00Z"}}
        response = self.client.post("/api/v1/calendar/bookings", json={
            "job_id": job_id, "start": "2027-01-01T10:00:00Z", "timeZone": "Africa/Lagos"
        }, headers={"Authorization": "Bearer booking-token", "Idempotency-Key": "booking-request-001"})
        self.assertEqual(response.status_code, 201)
        again = self.client.post("/api/v1/calendar/bookings", json={
            "job_id": job_id, "start": "2027-01-01T10:00:00Z", "timeZone": "Africa/Lagos"
        }, headers={"Authorization": "Bearer booking-token", "Idempotency-Key": "booking-request-001"})
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json["duplicate"])
        self.assertEqual(post.call_count, 1)



    def test_booking_requires_explicit_booking_consent(self):
        job_id = self._approved_job()
        response = self.client.post("/api/v1/calendar/bookings", json={
            "job_id": job_id, "start": "2027-01-01T10:00:00Z", "timeZone": "Africa/Lagos"
        }, headers={"Authorization": "Bearer booking-token", "Idempotency-Key": "booking-no-consent-001"})
        self.assertEqual(response.status_code, 400)

    @patch("web.calendar_api.requests.post")
    def test_booking_timeout_is_marked_uncertain_and_not_retried(self, post):
        job_id = self._approved_job()
        import requests
        post.side_effect = requests.Timeout("response timed out")
        headers = {"Authorization": "Bearer booking-token", "Idempotency-Key": "booking-timeout-001"}
        body = {"job_id": job_id, "start": "2027-01-01T10:00:00Z", "timeZone": "Africa/Lagos", "booking_consent": True, "booking_consent_version": "v1", "booking_consent_source": "website_chat"}
        first = self.client.post("/api/v1/calendar/bookings", json=body, headers=headers)
        self.assertEqual(first.status_code, 502)
        self.assertEqual(first.json["status"], "uncertain")
        second = self.client.post("/api/v1/calendar/bookings", json=body, headers=headers)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(post.call_count, 1)

    def test_intake_token_cannot_book_and_pending_review_blocks_booking(self):
        r = self.client.post("/api/v1/intake/leads", json={"name": "Ada", "consent": True},
            headers={"Authorization": "Bearer intake-token", "Idempotency-Key": "calendar-lead-002"})
        self.assertEqual(r.status_code, 202)
        self.assertTrue(run_once(path=self.db))
        response = self.client.post("/api/v1/calendar/bookings", json={
            "job_id": r.json["job_id"], "start": "2027-01-01T10:00:00Z", "timeZone": "Africa/Lagos"
        }, headers={"Authorization": "Bearer booking-token", "Idempotency-Key": "booking-request-002"})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
