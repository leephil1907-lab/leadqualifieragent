import unittest

from web.intake_validation import IntakeValidationError, normalize_lead, normalize_website


class IntakeValidationTests(unittest.TestCase):
    def test_normalizes_email_website_and_default_source(self):
        lead = normalize_lead({
            "name": "  Ada  ", "email": " ADA@Example.COM ",
            "website": "Example.COM/path?q=1#fragment", "unexpected": "discard",
        })
        self.assertEqual(lead["name"], "Ada")
        self.assertEqual(lead["email"], "ada@example.com")
        self.assertEqual(lead["website"], "https://example.com/path?q=1")
        self.assertEqual(lead["source"], "website_form")
        self.assertNotIn("unexpected", lead)

    def test_company_can_supply_missing_name(self):
        self.assertEqual(normalize_lead({"company": "Acme"})["name"], "Acme")

    def test_rejects_missing_identity_and_bad_email(self):
        with self.assertRaises(IntakeValidationError):
            normalize_lead({"email": "a@example.com"})
        with self.assertRaises(IntakeValidationError):
            normalize_lead({"name": "Ada", "email": "not-an-email"})

    def test_rejects_unsafe_or_malformed_website_values(self):
        for value in ("javascript:alert(1)", "https://user:pass@example.com", "https://", "example .com"):
            with self.subTest(value=value), self.assertRaises(IntakeValidationError):
                normalize_website(value)

    def test_rejects_non_object_and_overlong_fields(self):
        with self.assertRaises(IntakeValidationError):
            normalize_lead(["not", "a", "mapping"])
        with self.assertRaises(IntakeValidationError):
            normalize_lead({"name": "x" * 201})


if __name__ == "__main__":
    unittest.main()
