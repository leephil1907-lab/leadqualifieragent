import unittest

from web.standalone import create_app


class StandaloneAppTests(unittest.TestCase):
    def test_health_and_readiness_endpoints(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(client.get("/readyz").status_code, 200)


if __name__ == "__main__":
    unittest.main()
