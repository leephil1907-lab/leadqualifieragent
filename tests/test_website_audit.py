import unittest

from web.website_audit import audit_html_snapshot


class WebsiteAuditTests(unittest.TestCase):
    def test_reports_evidence_based_on_page_findings(self):
        report = audit_html_snapshot("<html><head><title>Hi</title></head><body><h2>Services</h2></body></html>")
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("title_length", codes)
        self.assertIn("missing_meta_description", codes)
        self.assertIn("missing_viewport", codes)
        self.assertIn("missing_h1", codes)
        self.assertIn("no_obvious_contact_path", codes)
        self.assertFalse(report["network_checks_performed"])

    def test_detects_good_mobile_contact_basics(self):
        report = audit_html_snapshot("""<html><head><title>Professional Plumbing Services in Lagos</title>
        <meta name="description" content="Reliable plumbing repairs, installations and emergency services for homes and businesses in Lagos. Call our friendly team today.">
        <meta name="viewport" content="width=device-width, initial-scale=1"></head>
        <body><h1>Plumbing Services</h1><a href="tel:+2348000000000">Call us</a></body></html>""")
        codes = {item["code"] for item in report["findings"]}
        self.assertNotIn("missing_viewport", codes)
        self.assertNotIn("no_obvious_contact_path", codes)

    def test_rejects_oversized_snapshot(self):
        with self.assertRaises(ValueError):
            audit_html_snapshot("x" * 1_000_001)


if __name__ == "__main__":
    unittest.main()
