import json
import tempfile
import unittest
from pathlib import Path

from web.job_queue import (
    claim_next, complete, enqueue, fail, get_job, init_db, list_events, recover_stale,
)


class DurableJobQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "jobs.sqlite3")
        init_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_enqueue_is_idempotent_per_tenant(self):
        first, created = enqueue("tenant-a", "qualify", {"email": "a@example.com"},
                                "request-1", path=self.db, now=100)
        second, created_again = enqueue("tenant-a", "qualify", {"email": "different@example.com"},
                                        "request-1", path=self.db, now=101)
        other_tenant, other_created = enqueue("tenant-b", "qualify", {},
                                               "request-1", path=self.db, now=101)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(other_created)
        self.assertNotEqual(first["id"], other_tenant["id"])

    def test_claim_and_complete_records_audit_events(self):
        job, _ = enqueue("tenant-a", "qualify", {"x": 1}, "key", path=self.db, now=10)
        claimed = claim_next(path=self.db, now=10)
        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["status"], "processing")
        self.assertEqual(claimed["attempts"], 1)
        self.assertIsNone(claim_next(path=self.db, now=10))
        self.assertTrue(complete(job["id"], "tenant-a", {"tier": "B"}, path=self.db, now=11))
        saved = get_job(job["id"], "tenant-a", path=self.db)
        self.assertEqual(saved["status"], "succeeded")
        self.assertEqual(saved["result"], {"tier": "B"})
        self.assertEqual([e["event_type"] for e in list_events(job["id"], "tenant-a", path=self.db)],
                         ["queued", "claimed", "succeeded"])

    def test_failure_schedules_exponential_retry_then_dead_letters(self):
        job, _ = enqueue("tenant-a", "qualify", {}, "key", path=self.db,
                         max_attempts=2, now=100)
        first_claim = claim_next(path=self.db, now=100)
        retry = fail(job["id"], "tenant-a", "temporary", path=self.db, now=100, base_delay=5)
        self.assertEqual(retry["status"], "queued")
        self.assertEqual(retry["available_at"], 105)
        self.assertIsNone(claim_next(path=self.db, now=104))
        second_claim = claim_next(path=self.db, now=105)
        self.assertEqual(second_claim["attempts"], 2)
        dead = fail(job["id"], "tenant-a", "still failing", path=self.db, now=105)
        self.assertEqual(dead["status"], "dead")
        self.assertEqual(dead["last_error"], "still failing")
        self.assertEqual(list_events(job["id"], "tenant-a", path=self.db)[-1]["event_type"], "dead_letter")

    def test_expired_worker_lease_recovers_or_dead_letters_job(self):
        retryable, _ = enqueue("tenant-a", "qualify", {}, "retry", path=self.db,
                               max_attempts=3, now=1)
        claim_next(path=self.db, now=1)
        counts = recover_stale(path=self.db, now=20, lease_seconds=10)
        self.assertEqual(counts, {"requeued": 1, "dead": 0})
        self.assertEqual(get_job(retryable["id"], "tenant-a", path=self.db)["status"], "queued")

        exhausted, _ = enqueue("tenant-a", "qualify", {}, "exhausted", path=self.db,
                               max_attempts=1, now=21)
        claimed = claim_next(path=self.db, now=21)
        self.assertEqual(claimed["id"], exhausted["id"])
        counts = recover_stale(path=self.db, now=40, lease_seconds=10)
        self.assertEqual(counts, {"requeued": 0, "dead": 1})
        self.assertEqual(get_job(exhausted["id"], "tenant-a", path=self.db)["status"], "dead")

    def test_wrong_tenant_cannot_read_or_complete_job(self):
        job, _ = enqueue("tenant-a", "qualify", {}, "key", path=self.db, now=1)
        claim_next(path=self.db, now=1)
        self.assertIsNone(get_job(job["id"], "tenant-b", path=self.db))
        self.assertFalse(complete(job["id"], "tenant-b", {}, path=self.db, now=2))
        self.assertIsNone(fail(job["id"], "tenant-b", "nope", path=self.db, now=2))

    def test_invalid_arguments_are_rejected(self):
        for args in [
            ("", "qualify", {}, "key"),
            ("tenant-a", "", {}, "key"),
            ("tenant-a", "qualify", [], "key"),
            ("tenant-a", "qualify", {}, ""),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                enqueue(*args, path=self.db)


if __name__ == "__main__":
    unittest.main()
