import json
import os
import unittest
from unittest.mock import Mock, patch

from web.llm_extractor import ExtractionNotConfigured, extract_lead_facts


class LLMExtractorTests(unittest.TestCase):
    def test_fails_closed_without_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ExtractionNotConfigured):
                extract_lead_facts("Budget is $2,000.")

    @patch("web.llm_extractor.requests.post")
    def test_only_accepts_exact_source_evidence_and_does_not_score(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {"choices": [{"message": {"content": json.dumps({
            "candidates": [
                {"field": "budget", "value": "$2,000", "evidence": "Budget is $2,000", "confidence": 0.99},
                {"field": "goal", "value": "Double revenue", "evidence": "not in source", "confidence": 0.99}
            ]
        })}}]}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True):
            result = extract_lead_facts("Budget is $2,000.")
        self.assertEqual(result["accepted"]["budget"]["value"], "$2,000")
        self.assertNotIn("goal", result["accepted"])
        self.assertFalse(result["scoring_performed"])
        self.assertTrue(result["requires_human_review"])
        self.assertEqual(post.call_args.kwargs["timeout"], (3, 20))


if __name__ == "__main__":
    unittest.main()
