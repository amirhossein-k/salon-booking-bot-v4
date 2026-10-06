"""Contract tests with a tiny simulated Mongo repository.

This is not a MongoDB server and cannot prove transaction conflict semantics.
If PyMongo is absent, only its public constants/concern classes are stubbed.
"""
import copy
from contextlib import nullcontext
import importlib.util
import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

if importlib.util.find_spec("pymongo") is None:
    mod = types.ModuleType("pymongo")
    mod.ReturnDocument = types.SimpleNamespace(AFTER=True)
    mod.timeout = lambda seconds: nullcontext()
    class Concern:
        def __init__(self, *args, **kwargs):
            pass
    for name, cls in [("read_concern", "ReadConcern"), ("write_concern", "WriteConcern")]:
        child = types.ModuleType("pymongo." + name)
        setattr(child, cls, Concern)
        sys.modules["pymongo." + name] = child
    sys.modules["pymongo"] = mod

from core import BookingError
from mongo_store import MongoStore
from mongo_runtime import MongoAnalytics, bot_factory, deliver
from bot import TelegramError
from test_core import FakeTelegram


def matches(row, query):
    for key, test in query.items():
        if key == "$expr":
            minutes_seconds = test["$lte"][1]["$subtract"][1]
            if row["created"] > row["start"] - minutes_seconds:
                return False
            continue
        value = row.get(key)
        if isinstance(test, dict):
            for op, expected in test.items():
                if op == "$lt" and not value < expected:
                    return False
                if op == "$lte" and not value <= expected:
                    return False
                if op == "$gt" and not value > expected:
                    return False
                if op == "$gte" and not value >= expected:
                    return False
                if op == "$in" and value not in expected:
                    return False
        elif value != test:
            return False
    return True


class Cursor(list):
    def sort(self, key, direction):
        return Cursor(sorted(self, key=lambda r: r[key], reverse=direction < 0))

    def limit(self, count):
        return Cursor(self[:count])


class Collection:
    def __init__(self):
        self.rows = []

    def find(self, query, **kwargs):
        return Cursor(copy.deepcopy([r for r in self.rows if matches(r, query)]))

    def find_one(self, query, **kwargs):
        return next(iter(self.find(query)), None)

    def insert_one(self, row, **kwargs):
        row = copy.deepcopy(row)
        row.setdefault("_id", f"fake:{len(self.rows)}")
        if any(x.get("_id") == row.get("_id") for x in self.rows):
            raise RuntimeError("Duplicate key")
        self.rows.append(copy.deepcopy(row))

    def update_one(self, query, change, upsert=False, **kwargs):
        row = next((r for r in self.rows if matches(r, query)), None)
        existing = row is not None
        if row is None and upsert:
            row = copy.deepcopy(query)
            row.update(change.get("$setOnInsert", {}))
            self.rows.append(row)
        if row is not None:
            row.update(copy.deepcopy(change.get("$set", {})))
            for key, amount in change.get("$inc", {}).items():
                row[key] = row.get(key, 0) + amount
        return types.SimpleNamespace(matched_count=int(existing), upserted_id=row.get("_id") if row else None)

    def update_many(self, query, change, **kwargs):
        for row in self.find(query):
            self.update_one({"_id": row["_id"]}, change)

    def find_one_and_update(self, query, change, sort=None, **kwargs):
        rows = self.find(query)
        if sort:
            for key, direction in reversed(sort):
                rows = rows.sort(key, direction)
        if not rows:
            return None
        self.update_one({"_id": rows[0]["_id"]}, change)
        return self.find_one({"_id": rows[0]["_id"]})

    def replace_one(self, query, row, upsert=False, **kwargs):
        self.delete_one(query)
        self.insert_one(row)

    def delete_one(self, query, **kwargs):
        self.rows = [r for r in self.rows if not matches(r, query)]

    def count_documents(self, query, **kwargs):
        return len(self.find(query))


class FakeSession:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def with_transaction(self, callback, **kwargs):
        snapshot = {k: copy.deepcopy(c.rows) for k, c in self.db.cols.items()}
        try:
            return callback(self)
        except Exception:
            for key, rows in snapshot.items():
                self.db.cols[key].rows = rows
            raise


class FakeDB:
    def __init__(self):
        self.cols = {name: Collection() for name in
                     ("bookings", "blocks", "sessions", "guards", "counters", "outbox", "audit", "updates")}
        self.client = types.SimpleNamespace(start_session=lambda: FakeSession(self))

    def __getattr__(self, name):
        return self.cols[name]


class MongoContractTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        self.config["staff"][2]["telegram_id"] = 222
        self.config["services"][1]["price_toman"] = 850000
        self.clock = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
        self.db = FakeDB()
        for staff in self.config["staff"]:
            self.db.guards.insert_one({"_id": staff["id"], "revision": 0})
        for kind in ("bookings", "blocks"):
            self.db.counters.insert_one({"_id": kind, "value": 0})
        self.store = MongoStore(self.db, self.config, lambda: self.clock)
        self.admin = self.config["admin_ids"][0]
        self.day = self.store.today() + timedelta(days=2)
        self.start = self.store.stamp(self.day, "10:00")

    def book(self, sid="nail1"):
        return self.store.book(111, sid, "nails", self.start, "مشتری آزمایشی", "09123456789")

    def test_overlap_rejected_independent_staff_allowed(self):
        self.book()
        with self.assertRaises(BookingError):
            self.book()
        self.book("nail2")
        self.assertEqual(len(self.db.bookings.rows), 2)
        self.assertEqual(self.db.guards.find_one({"_id": "nail1"})["revision"], 1)

    def test_cancel_retains_history_releases_time_and_notifications(self):
        bid = self.book()
        self.store.change_status(111, bid, "cancelled_customer", "نمی‌توانم بیایم")
        self.assertEqual(self.store.get_booking(111, bid)["status"], "cancelled_customer")
        self.book()
        self.assertEqual(len(self.db.bookings.rows), 2)
        self.assertEqual(len(self.db.audit.rows), 3)

    def test_access_control(self):
        bid = self.book()
        with self.assertRaises(BookingError):
            self.store.get_booking(333, bid)
        with self.assertRaises(BookingError):
            self.store.change_status(111, bid, "cancelled_staff", "غیرمجاز")
        with self.assertRaises(BookingError):
            self.store.blocks(222, "nail2")

    def test_blocks_and_existing_booking_conflict(self):
        block = self.store.block(222, "nail1", self.start, self.start + 3600, "کار شخصی")
        with self.assertRaises(BookingError):
            self.book()
        self.store.unblock(222, block)
        self.book()
        with self.assertRaises(BookingError):
            self.store.block(222, "nail1", self.start, self.start + 3600, "مرخصی")

    def test_webhook_update_deduplicated(self):
        update = {"update_id": 100, "message": {"chat": {"type": "private"},
                  "from": {"id": 111}, "text": "/start"}}
        self.assertEqual(self.store.process_update(update, bot_factory), "processed")
        count = len(self.db.outbox.rows)
        self.assertEqual(self.store.process_update(update, bot_factory), "duplicate")
        self.assertEqual(len(self.db.outbox.rows), count)

    def test_webhook_confirm_booking_replay_no_extra_reservation(self):
        self.store.session(111, {"step": "confirm_booking", "staff": "nail1", "service": "nails",
                                "start": self.start, "name": "مشتری", "phone": "09123456789"})
        update = {"update_id": 101, "callback_query": {"id": "callback", "from": {"id": 111},
                  "message": {"chat": {"type": "private"}}, "data": "confirm_booking"}}
        self.store.process_update(update, bot_factory)
        self.store.process_update(update, bot_factory)
        self.assertEqual(len(self.db.bookings.rows), 1)
        self.assertEqual(self.store.session(111)["step"], "home")
        self.assertEqual(len(self.db.outbox.rows), 4)  # 3 notifications + UI confirmation

    def test_transaction_error_rolls_back_session_and_queued_reply(self):
        update = {"update_id": 102, "message": {"from": {"id": 111}, "text": "/start"}}
        class Broken:
            def update(_, payload):
                self.store.session(111, {"step": "broken"})
                self.store.queue_message(111, "should rollback")
                raise RuntimeError("test error")
        with self.assertRaises(RuntimeError):
            self.store.process_update(update, lambda _: Broken())
        self.assertEqual(self.store.session(111), {})
        self.assertFalse(self.db.outbox.rows)
        self.assertFalse(self.db.updates.rows)

    def test_reminder_key_persists_and_cancel_suppresses_delivery(self):
        bid = self.book()
        self.clock = datetime.fromtimestamp(self.start - 86400, timezone.utc)
        self.store.schedule_reminders()
        self.store.schedule_reminders()
        reminders = [r for r in self.db.outbox.rows if r.get("kind") == "reminder"]
        self.assertEqual(len(reminders), 1)
        self.store.change_status(111, bid, "cancelled_customer", "لغو")
        tg = FakeTelegram()
        deliver(self.store, tg, seconds=5)
        self.assertFalse(any("یادآوری نوبت" in m[1] for m in tg.messages))

    def test_worker_retries_network_failure(self):
        self.store.queue_message(111, "اعلان آزمایشی")
        tg = FakeTelegram()
        tg.fail = True
        deliver(self.store, tg)
        self.assertEqual(self.db.outbox.rows[0]["attempts"], 1)
        self.assertIsNone(self.db.outbox.rows[0]["sent"])
        self.clock += timedelta(minutes=2)
        tg.fail = False
        self.assertEqual(deliver(self.store, tg), 1)
        self.assertIsNotNone(self.db.outbox.rows[0]["sent"])

    def test_outbox_callback_uses_mongo_not_sql(self):
        bot_factory(self.store).callback(self.admin, "outbox")
        self.assertIn("اعلان ارسال‌نشده", self.db.outbox.rows[0]["text"])

    def test_mongo_analytics_receipts_and_capacity(self):
        bid = self.book()
        self.clock = datetime.fromtimestamp(self.start + 7200, timezone.utc)
        self.store.change_status(222, bid, "completed", "انجام شد")
        analytics = MongoAnalytics(self.store)
        analytics.set_payment(self.admin, bid, 800000, "تخفیف")
        result = analytics.report(self.day, self.day, "nail1")
        self.assertEqual(result["summary"]["revenue"], 800000)
        self.assertEqual(result["summary"]["estimated_value"], 850000)
        self.assertEqual(result["summary"]["occupied_minutes"], 90)
        self.assertEqual(result["summary"]["unreconciled_completed"], 0)
        self.assertEqual(result["bookings"][0]["amount_toman"], 800000)

    def test_payment_validation(self):
        bid = self.book()
        analytics = MongoAnalytics(self.store)
        with self.assertRaises(BookingError):
            analytics.set_payment(self.admin, bid, 1000, "رزرو انجام نشده")
        with self.assertRaises(BookingError):
            analytics.set_payment(222, bid, 1000, "غیرمجاز")
        with self.assertRaises(BookingError):
            analytics.set_payment(self.admin, bid, True, "نامعتبر")


if __name__ == "__main__":
    unittest.main()
