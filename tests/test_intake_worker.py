import hashlib
import hmac
import json
import os
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

from web.intake_api import create_intake_blueprint
from web.review_api import create_review_blueprint
from web.job_queue import enqueue, get_job, init_db
from web.worker import process_job, run_once


class AuthenticatedIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "queue.sqlite3")
        init_db(self.db)
        app = Flask(__name__)
        app.register_blueprint(create_intake_blueprint(db_path=self.db))
        app.register_blueprint(create_review_blueprint(db_path=self.db))
        self.client = app.test_client()
        self.env = patch.dict(os.environ, {
            "LEAD_QUALIFIER_API_TOKEN": "test-secret-token-long-enough",
            "LEAD_QUALIFIER_TENANT_ID": "tenant-test",
            "LEAD_QUALIFIER_RATE_LIMIT_PER_MINUTE": "60",
            "LEAD_QUALIFIER_REVIEW_TOKENS": json.dumps([{"token": "review-secret-token", "tenant_id": "tenant-test", "scopes": ["review"]}]),
        }, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def post(self, body=None, key="request-0001", token="test-secret-token-long-enough"):
        headers = {"Idempotency-Key": key}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post("/api/v1/intake/leads", json=body or {}, headers=headers)

    def test_authentication_required(self):
        self.assertEqual(self.post(token="wrong-token").status_code, 401)
        self.assertEqual(self.post(token="").status_code, 401)

    def test_consent_and_idempotency_required(self):
        body = {"name": "Ada", "email": "ada@example.com"}
        self.assertEqual(self.post(body).status_code, 400)
        response = self.client.post("/api/v1/intake/leads",
            json={**body, "consent": True},
            headers={"Authorization": "Bearer test-secret-token-long-enough"})
        self.assertEqual(response.status_code, 400)

    def test_intake_queues_normalized_lead_and_duplicate_is_idempotent(self):
        body = {"name": "Ada", "email": "ADA@example.com", "website": "EXAMPLE.com",
                "consent": True, "consent_version": "v1"}
        first = self.post(body)
        second = self.post(body)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json["duplicate"])
        self.assertEqual(first.json["job_id"], second.json["job_id"])
        job = get_job(first.json["job_id"], "tenant-test", path=self.db)
        self.assertEqual(job["payload"]["lead"]["email"], "ada@example.com")
        self.assertEqual(job["payload"]["lead"]["website"], "https://example.com")


    def test_distinct_idempotency_keys_deduplicate_same_email(self):
        first = self.post({"name": "Ada", "email": "ada@example.com", "consent": True}, key="lead-submit-001")
        second = self.post({"name": "Ada Lovelace", "email": "ADA@example.com", "consent": True}, key="lead-submit-002")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json["duplicate"])
        self.assertEqual(second.json["duplicate_reason"], "matching_email")
        self.assertEqual(first.json["job_id"], second.json["job_id"])

    def test_reusing_idempotency_key_with_different_payload_is_conflict(self):
        self.assertEqual(self.post({"name": "Ada", "consent": True}).status_code, 202)
        response = self.post({"name": "Grace", "consent": True})
        self.assertEqual(response.status_code, 409)

    def test_tenant_is_configured_server_side_not_client_supplied(self):
        response = self.post({"name": "Ada", "tenant_id": "attacker", "consent": True})
        self.assertEqual(response.status_code, 202)
        job = get_job(response.json["job_id"], "tenant-test", path=self.db)
        self.assertNotIn("tenant_id", job["payload"]["lead"])

    def test_multi_tenant_token_map_resolves_distinct_tenants(self):
        os.environ["LEAD_QUALIFIER_API_TOKENS"] = json.dumps([
            {"token": "tenant-one-secret", "tenant_id": "tenant-one"},
            {"token": "tenant-two-secret", "tenant_id": "tenant-two"},
        ])
        one = self.client.post("/api/v1/intake/leads", json={"name": "One", "consent": True},
            headers={"Authorization": "Bearer tenant-one-secret", "Idempotency-Key": "request-one"})
        two = self.client.post("/api/v1/intake/leads", json={"name": "Two", "consent": True},
            headers={"Authorization": "Bearer tenant-two-secret", "Idempotency-Key": "request-two"})
        self.assertEqual(one.status_code, 202)
        self.assertEqual(two.status_code, 202)
        self.assertNotEqual(one.json["job_id"], two.json["job_id"])
        self.assertIsNone(get_job(one.json["job_id"], "tenant-two", path=self.db))

    def test_job_status_is_tenant_scoped_and_does_not_expose_payload(self):
        response = self.post({"name": "Ada", "consent": True})
        status = self.client.get("/api/v1/intake/jobs/" + response.json["job_id"],
            headers={"Authorization": "Bearer test-secret-token-long-enough"})
        self.assertEqual(status.status_code, 200)
        self.assertNotIn("payload", status.json["job"])
        os.environ["LEAD_QUALIFIER_TENANT_ID"] = "other-tenant"
        denied = self.client.get("/api/v1/intake/jobs/" + response.json["job_id"],
            headers={"Authorization": "Bearer test-secret-token-long-enough"})
        self.assertEqual(denied.status_code, 404)

    def test_worker_uses_existing_deterministic_score_and_requires_review(self):
        result = process_job({"job_type": "qualify_lead", "payload": {
            "lead": {"name": "Example", "company": "Example", "website": "https://example.com",
                     "email": "a@example.com", "industry": "services", "pain_point": "More leads",
                     "goal": "Increase enquiries", "budget": "1000", "timeline": "soon",
                     "authority": "decision_maker", "preferred_next_step": "book"}
        }})
        self.assertIn("tier", result["qualification"])
        self.assertEqual(result["qualification"]["scoring_version"], "baseline-v1")
        self.assertTrue(result["review"]["required"])
        self.assertFalse(result["external_actions_allowed"])


    @patch("web.worker.requests.post")
    def test_telegram_alert_is_a_durable_retryable_queue_job(self, post):
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
        os.environ["TELEGRAM_ADMIN_CHAT_ID"] = "admin-chat"
        response = self.post({"name": "Ada", "company": "Example Plumbing", "consent": True},
                             key="telegram-lead-001")
        self.assertEqual(response.status_code, 202)
        self.assertTrue(run_once(path=self.db))
        notification, created = enqueue("tenant-test", "telegram_notification", {"text": "unused"},
            "telegram-notification:" + response.json["job_id"], path=self.db)
        self.assertFalse(created)
        self.assertEqual(notification["status"], "queued")
        post.return_value = Mock()
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {"ok": True, "result": {"message_id": 77}}
        self.assertTrue(run_once(path=self.db))
        delivered = get_job(notification["id"], "tenant-test", path=self.db)
        self.assertEqual(delivered["status"], "succeeded")
        self.assertEqual(delivered["result"]["notification"], "sent")
        self.assertIn("Example Plumbing", delivered["payload"]["text"])

    def test_worker_completes_job_and_review_is_audited(self):
        response = self.post({"name": "Ada", "consent": True})
        self.assertTrue(run_once(path=self.db))
        job = get_job(response.json["job_id"], "tenant-test", path=self.db)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["result"]["qualification"]["scoring_version"], "baseline-v1")
        reviews = self.client.get("/api/v1/reviews", headers={"Authorization": "Bearer review-secret-token"})
        self.assertEqual(len(reviews.json["reviews"]), 1)
        decision = self.client.post("/api/v1/reviews/" + response.json["job_id"],
            json={"decision": "approved", "reviewer": "agent-owner", "reason": "Verified intake details"},
            headers={"Authorization": "Bearer review-secret-token"})
        self.assertEqual(decision.status_code, 200)
        self.assertFalse(decision.json["external_actions_triggered"])
        second = self.client.post("/api/v1/reviews/" + response.json["job_id"],
            json={"decision": "rejected", "reviewer": "agent-owner", "reason": "Second decision"},
            headers={"Authorization": "Bearer review-secret-token"})
        self.assertEqual(second.status_code, 409)



    def test_authorized_html_audit_is_queued_and_processed_without_network(self):
        html = "<html><head><title>Small</title></head><body><h2>Services</h2></body></html>"
        response = self.client.post("/api/v1/audits/html-snapshot",
            json={"html": html, "authorized_to_audit": True},
            headers={"Authorization": "Bearer test-secret-token-long-enough",
                     "Idempotency-Key": "audit-request-001"})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(run_once(path=self.db))
        job = get_job(response.json["job_id"], "tenant-test", path=self.db)
        self.assertEqual(job["status"], "succeeded")
        self.assertFalse(job["result"]["audit"]["network_checks_performed"])
        self.assertIn("missing_viewport", {x["code"] for x in job["result"]["audit"]["findings"]})

    def test_signed_whatsapp_webhook_requires_fresh_valid_signature_and_consent(self):
        secret = "webhook-secret-for-tests"
        os.environ["LEAD_QUALIFIER_WEBHOOK_SECRETS"] = json.dumps([
            {"channel": "whatsapp", "secret": secret, "tenant_id": "tenant-test"}
        ])
        timestamp = str(int(time.time()))
        raw = json.dumps({"name": "Ada", "email": "ada@example.com", "consent": True},
                         separators=(",", ":")).encode("utf-8")
        signed = timestamp.encode("utf-8") + b"." + raw
        signature = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        headers = {"X-Webhook-Timestamp": timestamp, "X-Webhook-Signature": "sha256=" + signature,
                   "Idempotency-Key": "webhook-request-1", "Content-Type": "application/json"}
        response = self.client.post("/api/v1/webhooks/whatsapp", data=raw, headers=headers)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(get_job(response.json["job_id"], "tenant-test", path=self.db)["payload"]["lead"]["source"], "whatsapp")
        bad = dict(headers)
        bad["X-Webhook-Signature"] = "sha256=bad"
        self.assertEqual(self.client.post("/api/v1/webhooks/whatsapp", data=raw, headers=bad).status_code, 401)

    def test_intake_token_cannot_access_human_review_routes(self):
        response = self.client.get("/api/v1/reviews",
            headers={"Authorization": "Bearer test-secret-token-long-enough"})
        self.assertEqual(response.status_code, 401)

    def test_rate_limit_enforced(self):
        os.environ["LEAD_QUALIFIER_RATE_LIMIT_PER_MINUTE"] = "1"
        first = self.post({"name": "Ada", "consent": True}, key="request-rate-1")
        second = self.post({"name": "Grace", "consent": True}, key="request-rate-2")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 429)


if __name__ == "__main__":
    unittest.main()
