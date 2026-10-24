import unittest

from web.lead_qualifier import _score


class LeadQualifierScoringTests(unittest.TestCase):
    def test_empty_lead_does_not_receive_qualified_tier(self):
        score = _score({})
        self.assertEqual(score["fit_score"], 0)
        self.assertEqual(score["intent_score"], 0)
        self.assertEqual(score["tier"], "D")
        self.assertTrue(score["missing_fields"])
        self.assertIsNotNone(score["next_question"])

    def test_complete_high_intent_business_lead_can_reach_a_tier(self):
        lead = {
            "name": "Example Plumbing",
            "company": "Example Plumbing",
            "email": "owner@example.com",
            "website": "https://example.com",
            "industry": "Plumbing",
            "pain_point": "Our website does not generate enquiries",
            "goal": "Generate more booked jobs",
            "budget": "$2,000-$4,000",
            "timeline": "Within 30 days",
            "authority": "decision_maker",
            "preferred_next_step": "book a call",
        }
        score = _score(lead)
        self.assertEqual(score["fit_score"], 85)
        self.assertEqual(score["intent_score"], 100)
        self.assertEqual(score["tier"], "A")
        self.assertEqual(score["next_action"], "assign_sales_rep")
        self.assertEqual(score["missing_fields"], [])

    def test_missing_budget_prompts_one_question(self):
        lead = {
            "name": "Example Studio",
            "website": "https://example.org",
            "industry": "Design",
            "pain_point": "Need more qualified enquiries",
            "goal": "Increase consultations",
            "timeline": "This quarter",
            "authority": "decision_maker",
        }
        score = _score(lead)
        self.assertIn("budget", score["missing_fields"])
        self.assertIsInstance(score["next_question"], str)
        self.assertNotIn("budget", score["qualification_reasons"])

    def test_scores_are_bounded_and_repeatable(self):
        lead = {"website": "not a valid url", "pain_point": "need leads"}
        first = _score(lead)
        second = _score(lead)
        for field in ("fit_score", "intent_score", "total_score"):
            self.assertGreaterEqual(first[field], 0)
            self.assertLessEqual(first[field], 100)
            self.assertEqual(first[field], second[field])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
