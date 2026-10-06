"""Actual FastAPI HTTP tests, skipped when runtime dependencies are unavailable."""
import base64
import importlib.util
import os
import unittest
from unittest.mock import patch, MagicMock

AVAILABLE = bool(importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx"))


@unittest.skipUnless(AVAILABLE, "Install requirements-test.txt to run actual FastAPI HTTP tests")
class FastAPIHTTPTests(unittest.TestCase):
    def setUp(self):
        import app as module
        from fastapi.testclient import TestClient
        self.module = module
        self.env = patch.dict(os.environ, {
            "DASHBOARD_USER": "manager", "DASHBOARD_PASSWORD": "a-strong-test-password",
            "CSRF_SECRET": "c" * 32, "CRON_SECRET": "r" * 32,
            "TELEGRAM_WEBHOOK_SECRET": "w" * 32, "BOT_TOKEN": "test-token",
            "VERCEL_ENV": "production"}, clear=False)
        self.env.start()
        self.client = TestClient(module.app, raise_server_exceptions=False)
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"manager:a-strong-test-password").decode()}

    def tearDown(self):
        self.client.close()
        self.env.stop()

    def test_dashboard_auth_and_mobile(self):
        self.assertEqual(self.client.get("/").status_code, 401)
        response = self.client.get("/mobile", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn("const LIVE = true;", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_webhook_and_cron_reject_missing_secrets_before_db(self):
        with patch.object(self.module, "new_store") as factory:
            self.assertEqual(self.client.post("/api/telegram", json={"update_id": 1}).status_code, 401)
            self.assertEqual(self.client.get("/api/cron").status_code, 401)
            factory.assert_not_called()

    def test_webhook_commit_and_worker(self):
        store = MagicMock()
        with patch.object(self.module, "new_store", return_value=store), \
             patch.object(self.module, "deliver", return_value=1), \
             patch.object(self.module, "Telegram"):
            response = self.client.post("/api/telegram", json={"update_id": 101, "message": {}},
                                        headers={"X-Telegram-Bot-Api-Secret-Token": "w" * 32})
            self.assertEqual(response.status_code, 200)
            store.process_update.assert_called_once()

    def test_payment_requires_csrf(self):
        response = self.client.post("/api/payment", headers=self.auth,
                                    json={"booking_id": 1, "amount_toman": 0, "note": "تست"})
        self.assertEqual(response.status_code, 403)

    def test_preview_does_not_process_production_webhook(self):
        with patch.dict(os.environ, {"VERCEL_ENV": "preview"}), \
             patch.object(self.module, "new_store") as factory:
            response = self.client.post("/api/telegram", json={"update_id": 1},
                                        headers={"X-Telegram-Bot-Api-Secret-Token": "w" * 32})
            self.assertEqual(response.status_code, 403)
            factory.assert_not_called()

    def test_invalid_payload_and_oversize(self):
        headers = {"X-Telegram-Bot-Api-Secret-Token": "w" * 32}
        self.assertEqual(self.client.post("/api/telegram", json=[], headers=headers).status_code, 400)
        self.assertEqual(self.client.post("/api/telegram", content=b"x" * 100001,
                                         headers=headers).status_code, 400)


if __name__ == "__main__":
    unittest.main()
