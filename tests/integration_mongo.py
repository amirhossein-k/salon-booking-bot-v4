"""Opt-in tests against YOUR disposable MongoDB replica-set database.

Run separately: python -m unittest discover -s tests -p integration_mongo.py -v
Existing data is never dropped. Test reservations remain in the test database.
"""
import json
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


@unittest.skipUnless(os.environ.get("TEST_MONGODB_URI") and os.environ.get("TEST_MONGODB_DB"),
                     "Set TEST_MONGODB_URI and TEST_MONGODB_DB for a disposable replica-set database")
class LiveMongoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pymongo import MongoClient
        from scripts.init_db import initialize
        cls.client = MongoClient(os.environ["TEST_MONGODB_URI"], serverSelectionTimeoutMS=5000)
        dbname = os.environ["TEST_MONGODB_DB"]
        if not dbname.startswith("salon_test_"):
            raise RuntimeError("Test database name MUST start with salon_test_")
        cls.db = cls.client[dbname]
        cls.config = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        suffix = uuid.uuid4().hex[:6]
        for staff in cls.config["staff"]:
            staff["id"] = staff["id"] + "_" + suffix
        initialize(cls.db, cls.config)
        cls.clock = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def store(self):
        from mongo_store import MongoStore
        return MongoStore(self.db, self.config, lambda: self.clock)

    def test_concurrent_same_staff_only_one_booking_commits(self):
        from core import BookingError
        sid = self.config["staff"][2]["id"]
        day = self.store().today() + timedelta(days=2)
        start = self.store().stamp(day, "10:00")
        def attempt(i):
            try:
                return self.store().book(90000000 + i, sid, "nails", start, "تست هم‌زمانی", "09123456789")
            except BookingError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(r is not None for r in result), 1)

    def test_booking_vs_block_only_one_commits(self):
        from core import BookingError
        sid = self.config["staff"][3]["id"]
        day = self.store().today() + timedelta(days=3)
        start = self.store().stamp(day, "10:00")
        def book():
            try:
                self.store().book(90010001, sid, "nails", start, "تست", "09123456789")
                return "book"
            except BookingError:
                return None
        def block():
            try:
                self.store().block(self.config["admin_ids"][0], sid, start, start + 3600, "تست هم‌زمان")
                return "block"
            except BookingError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(book), pool.submit(block)]
            result = [f.result() for f in futures]
        self.assertEqual(sum(r is not None for r in result), 1)

    def test_webhook_retry_one_database_result(self):
        from mongo_runtime import bot_factory
        update_id = int(uuid.uuid4().hex[:12], 16)
        update = {"update_id": update_id, "message": {"from": {"id": update_id},
                  "chat": {"type": "private"}, "text": "/start"}}
        self.assertEqual(self.store().process_update(update, bot_factory), "processed")
        self.assertEqual(self.store().process_update(update, bot_factory), "duplicate")
        self.assertEqual(self.db.updates.count_documents({"_id": update_id}), 1)


if __name__ == "__main__":
    unittest.main()
