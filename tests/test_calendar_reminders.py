import copy
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bot import Bot
from core import Store
from jalali import from_gregorian, new_year, numeric, label
from test_core import FakeTelegram


class CalendarTests(unittest.TestCase):
    def test_reference_dates_and_leap_esfand(self):
        anchors = {
            date(2023, 3, 21): (1402, 1, 1),
            date(2024, 3, 20): (1403, 1, 1),
            date(2025, 3, 20): (1403, 12, 30),
            date(2025, 3, 21): (1404, 1, 1),
            date(2026, 3, 20): (1404, 12, 29),
            date(2026, 3, 21): (1405, 1, 1),
            date(2026, 10, 6): (1405, 7, 14),
            date(2000, 1, 1): (1378, 10, 11),
        }
        for day, expected in anchors.items():
            with self.subTest(day=day):
                self.assertEqual(from_gregorian(day), expected)

    def test_farsi_labels(self):
        self.assertEqual(numeric(date(2026, 10, 6)), "۱۴۰۵/۰۷/۱۴")
        self.assertEqual(label(date(2026, 10, 6)), "سه‌شنبه ۱۴ مهر")

    def test_all_dates_1900_2100_contiguous_and_valid(self):
        day = date(1900, 1, 1)
        end = date(2100, 12, 31)
        while day <= end:
            y, m, d = from_gregorian(day)
            self.assertTrue(1 <= m <= 12)
            lengths = [31] * 6 + [30] * 5 + [(new_year(y + 1) - new_year(y)).days - 336]
            self.assertIn(lengths[-1], [29, 30])
            self.assertTrue(1 <= d <= lengths[m - 1])
            expected = new_year(y) + timedelta(days=sum(lengths[:m - 1]) + d - 1)
            self.assertEqual(day, expected)
            day += timedelta(days=1)

    def test_supported_range(self):
        with self.assertRaises(ValueError):
            from_gregorian(date(1800, 1, 1))


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "db.sqlite")
        self.config = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        self.config["staff"][2]["telegram_id"] = 222
        self.clock_value = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
        self.store = Store(self.path, self.config, lambda: self.clock_value)
        self.day = self.store.today() + timedelta(days=2)
        self.start = self.store.stamp(self.day, "10:00")
        self.bid = self.store.book(111, "nail1", "nails", self.start, "مشتری", "09123456789")

    def tearDown(self):
        self.tmp.cleanup()

    def at(self, stamp):
        self.clock_value = datetime.fromtimestamp(stamp, timezone.utc)

    def reminder_rows(self):
        with self.store.connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT r.*,o.* FROM reminders r JOIN outbox o ON o.id=r.outbox_id ORDER BY o.id")]

    def test_due_times_and_customer_only(self):
        self.at(self.start - 86400 - 1)
        self.assertEqual(self.store.schedule_reminders(), 0)
        self.at(self.start - 86400)
        self.assertEqual(self.store.schedule_reminders(), 1)
        rows = self.reminder_rows()
        self.assertEqual(rows[0]["chat_id"], 111)
        self.assertIn("یادآوری نوبت (۲۴ ساعت قبل)", rows[0]["text"])
        self.assertIn(numeric(self.day), rows[0]["text"])
        self.at(self.start - 7200)
        self.assertEqual(self.store.schedule_reminders(), 1)
        self.assertEqual(len(self.reminder_rows()), 2)

    def test_restart_and_repeated_scheduling_no_duplicates(self):
        self.at(self.start - 86400 + 15)
        self.assertEqual(self.store.schedule_reminders(), 1)
        self.assertEqual(self.store.schedule_reminders(), 0)
        restarted = Store(self.path, self.config, lambda: self.clock_value)
        self.assertEqual(restarted.schedule_reminders(), 0)
        self.assertEqual(len(self.reminder_rows()), 1)

    def test_cancel_before_due_suppresses_reminders(self):
        self.store.change_status(111, self.bid, "cancelled_customer", "لغو توسط مشتری")
        self.at(self.start - 86400)
        self.assertEqual(self.store.schedule_reminders(), 0)
        self.at(self.start - 7200)
        self.assertEqual(self.store.schedule_reminders(), 0)

    def test_cancel_queued_reminder_before_delivery(self):
        self.at(self.start - 86400)
        self.store.schedule_reminders()
        self.store.change_status(222, self.bid, "cancelled_staff", "هماهنگی تلفنی")
        self.assertIsNotNone(self.reminder_rows()[0]["cancelled"])
        tg = FakeTelegram()
        Bot(self.store, tg).flush_outbox()
        self.assertFalse(any("یادآوری نوبت" in m[1] for m in tg.messages))

    def test_outage_grace_window_and_skip_stale(self):
        self.at(self.start - 86400 + 29 * 60)
        self.assertEqual(self.store.schedule_reminders(), 1)
        self.at(self.start - 86400 + 31 * 60)
        self.store.schedule_reminders()
        self.assertIsNotNone(self.reminder_rows()[0]["cancelled"])
        self.at(self.start - 7200 + 31 * 60)
        self.assertEqual(self.store.schedule_reminders(), 0)
        self.assertEqual(len(self.reminder_rows()), 1)

    def test_no_reminders_at_or_after_appointment(self):
        self.at(self.start - 7200)
        self.store.schedule_reminders()
        self.at(self.start)
        self.store.schedule_reminders()
        tg = FakeTelegram()
        Bot(self.store, tg).flush_outbox()
        self.assertFalse(any("یادآوری نوبت" in m[1] for m in tg.messages))
        self.assertEqual(self.store.schedule_reminders(), 0)

    def test_short_notice_booking_does_not_emit_past_deadline(self):
        # Even if a booking is created inside the catch-up window, do not send a
        # reminder whose nominal deadline predates creation.
        self.at(self.start - 86400 + 60)
        short_bid = self.store.book(112, "nail2", "nails", self.start, "مشتری دوم", "09123456789")
        self.store.schedule_reminders()
        self.assertFalse(any(r["booking_id"] == short_bid for r in self.reminder_rows()))
        self.at(self.start - 7200)
        self.store.schedule_reminders()
        self.assertEqual([r["minutes_before"] for r in self.reminder_rows() if r["booking_id"] == short_bid], [120])

    def test_simultaneous_scheduler_checks_enqueue_once(self):
        self.at(self.start - 86400)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.store.schedule_reminders(), range(6)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.reminder_rows()), 1)

    def test_disable_reminders_and_cancel_pending(self):
        self.at(self.start - 86400)
        self.store.schedule_reminders()
        self.store.config["reminder_minutes"] = []
        self.assertEqual(self.store.schedule_reminders(), 0)
        self.assertIsNotNone(self.reminder_rows()[0]["cancelled"])

    def test_custom_offset(self):
        self.store.config["reminder_minutes"] = [60]
        self.at(self.start - 3600)
        self.assertEqual(self.store.schedule_reminders(), 1)
        self.assertIn("۱ ساعت قبل", self.reminder_rows()[0]["text"])

    def test_flush_rechecks_staleness_without_scheduler(self):
        self.at(self.start - 7200)
        self.store.schedule_reminders()
        self.at(self.start - 7200 + 31 * 60)
        tg = FakeTelegram()
        Bot(self.store, tg).flush_outbox()
        self.assertFalse(any("یادآوری نوبت" in m[1] for m in tg.messages))

    def test_retry_then_delivery_marks_sent(self):
        self.at(self.start - 7200)
        self.store.schedule_reminders()
        tg = FakeTelegram()
        bot = Bot(self.store, tg)
        tg.fail = True
        bot.flush_outbox()
        self.assertIsNone(self.reminder_rows()[0]["sent"])
        self.at(self.start - 7200 + 60)
        tg.fail = False
        bot.flush_outbox()
        self.assertIsNotNone(self.reminder_rows()[0]["sent"])

    def test_old_outbox_schema_migrates_without_data_loss(self):
        oldpath = str(Path(self.tmp.name) / "old.sqlite")
        with sqlite3.connect(oldpath) as c:
            c.execute("""CREATE TABLE outbox(
                id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_try INTEGER NOT NULL DEFAULT 0,
                sent INTEGER, last_error TEXT)""")
            c.execute("INSERT INTO outbox(chat_id,text) VALUES (111,'old notification')")
        upgraded = Store(oldpath, self.config, lambda: self.clock_value)
        with upgraded.connect() as c:
            row = c.execute("SELECT * FROM outbox").fetchone()
            self.assertEqual(row["text"], "old notification")
            self.assertIsNone(row["cancelled"])
        Store(oldpath, self.config, lambda: self.clock_value)

    def test_invalid_offsets_rejected(self):
        for offsets in [[0], [-1], [120, 120], ["120"], None]:
            config = copy.deepcopy(self.config)
            config["reminder_minutes"] = offsets
            with self.subTest(offsets=offsets), self.assertRaises(ValueError):
                Store(self.path, config)

    def test_upgraded_existing_booking_gets_future_reminder(self):
        # Rebuild the pre-upgrade outbox schema while preserving old bookings.
        with sqlite3.connect(self.path) as c:
            c.executescript("""
                DROP TABLE reminders;
                DROP INDEX outbox_pending;
                ALTER TABLE outbox RENAME TO outbox_new;
                CREATE TABLE outbox(
                    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_try INTEGER NOT NULL DEFAULT 0,
                    sent INTEGER, last_error TEXT);
                INSERT INTO outbox(id,chat_id,text,attempts,next_try,sent,last_error)
                    SELECT id,chat_id,text,attempts,next_try,sent,last_error FROM outbox_new;
                DROP TABLE outbox_new;
            """)
        upgraded = Store(self.path, self.config, lambda: self.clock_value)
        self.assertEqual(upgraded.get_booking(111, self.bid)["phone"], "09123456789")
        self.at(self.start - 86400)
        self.assertEqual(upgraded.schedule_reminders(), 1)
        with upgraded.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 1)

    def test_calendar_in_user_menus_and_booking_text(self):
        tg = FakeTelegram()
        bot = Bot(self.store, tg)
        bot.home(111)
        self.assertIn("تاریخ‌ها شمسی", tg.messages[-1][1])
        bot.day_picker(111, "day|nail1|nails", "روز را انتخاب کن")
        rows = tg.messages[-1][2]
        self.assertIn("مهر", rows[0][0][0])
        self.assertIn("2026-", rows[0][0][1])  # Internal callback format stays backwards-compatible.
        bot.agenda(222, "nail1", self.day)
        self.assertIn(numeric(self.day), tg.messages[-3][1])
        self.assertIn(numeric(self.day), self.store.booking_text(self.store.get_booking(111, self.bid)))


if __name__ == "__main__":
    unittest.main()
