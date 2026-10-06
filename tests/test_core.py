import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

from core import Store, BookingError
from bot import Bot, TelegramError


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.fail = False

    def send(self, uid, text, rows=None, reply_markup=None):
        if self.fail:
            raise TelegramError(403, "Forbidden")
        self.messages.append((uid, text, rows, reply_markup))
        return {"message_id": len(self.messages)}

    def call(self, method, **kwargs):
        return True


class BookingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        self.config["staff"][2]["telegram_id"] = 222
        self.clock_value = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
        self.store = Store(str(Path(self.tmp.name) / "test.sqlite"), copy.deepcopy(self.config),
                           lambda: self.clock_value)
        self.day = self.store.today()
        self.start = self.store.stamp(self.day, "10:00")
        self.admin = self.config["admin_ids"][0]

    def tearDown(self):
        self.tmp.cleanup()

    def book(self, uid=111, sid="nail1", start=None):
        return self.store.book(uid, sid, "nails", self.start if start is None else start,
                               "مشتری آزمایشی", "09123456789")

    def test_different_staff_independent(self):
        self.book()
        self.book(112, "nail2")
        self.assertEqual(len(self.store.bookings(self.admin, "nail1", self.day)), 1)
        self.assertEqual(len(self.store.bookings(self.admin, "nail2", self.day)), 1)

    def test_overlap_rejected(self):
        self.book()
        with self.assertRaises(BookingError):
            self.book(112, start=self.start + 15 * 60)

    def test_adjacent_allowed(self):
        self.book()
        self.book(112, start=self.start + 90 * 60)

    def test_concurrent_same_slot_only_one_wins(self):
        def attempt(uid):
            try:
                return self.book(uid)
            except BookingError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(1000, 1008)))
        self.assertEqual(sum(x is not None for x in results), 1)

    def test_cancel_retains_history_releases_slot(self):
        bid = self.book()
        self.store.change_status(111, bid, "cancelled_customer", "نمی‌توانم بیایم")
        b = self.store.get_booking(111, bid)
        self.assertEqual(b["status"], "cancelled_customer")
        self.assertEqual(b["reason"], "نمی‌توانم بیایم")
        self.book(112)

    def test_unauthorized_change_and_view_rejected(self):
        bid = self.book()
        with self.assertRaises(BookingError):
            self.store.change_status(333, bid, "cancelled_customer", "غیرمجاز")
        with self.assertRaises(BookingError):
            self.store.get_booking(333, bid)
        with self.assertRaises(BookingError):
            self.store.change_status(111, bid, "cancelled_staff", "غیرمجاز")

    def test_staff_only_own_calendar(self):
        self.assertTrue(self.store.can_manage(222, "nail1"))
        self.assertFalse(self.store.can_manage(222, "nail2"))
        with self.assertRaises(BookingError):
            self.store.bookings(222, "nail2", self.day)
        with self.assertRaises(BookingError):
            self.store.block(222, "nail2", self.start, self.start + 3600, "غیرمجاز")

    def test_block_conflicts_with_existing_booking(self):
        self.book()
        with self.assertRaises(BookingError):
            self.store.block(222, "nail1", self.start, self.start + 3600, "مرخصی")

    def test_block_unblock(self):
        block_id = self.store.block(222, "nail1", self.start, self.start + 3600, "مرخصی")
        with self.assertRaises(BookingError):
            self.book()
        self.store.unblock(222, block_id)
        self.book()

    def test_block_cannot_be_removed_by_other_staff(self):
        block_id = self.store.block(222, "nail1", self.start, self.start + 3600, "مرخصی")
        with self.assertRaises(BookingError):
            self.store.unblock(333, block_id)

    def test_working_hours_duration_and_closed_day(self):
        slots = self.store.slots("nail1", "nails", self.day)
        self.assertIn(self.start, slots)
        self.assertNotIn(self.store.stamp(self.day, "17:00"), slots)
        friday = self.day + timedelta(days=4)
        self.assertEqual(self.store.slots("nail1", "nails", friday), [])

    def test_notice_and_horizon(self):
        self.assertNotIn(self.store.stamp(self.day, "08:00"),
                         self.store.slots("nail1", "nails", self.day))
        self.assertEqual(self.store.slots("nail1", "nails", self.day + timedelta(days=21)), [])
        self.assertEqual(self.store.slots("nail1", "nails", self.day - timedelta(days=1)), [])

    def test_notification_recipients_and_audit(self):
        bid = self.book()
        with self.store.connect() as c:
            recipients = {r[0] for r in c.execute("SELECT chat_id FROM outbox")}
            audits = c.execute("SELECT COUNT(*) FROM audit WHERE entity_id=?", (bid,)).fetchone()[0]
        self.assertEqual(recipients, {111, 222, self.admin})
        self.assertEqual(audits, 1)

    def test_no_show_before_time_rejected_and_after_time_retained(self):
        bid = self.book()
        with self.assertRaises(BookingError):
            self.store.change_status(222, bid, "no_show", "نیامد")
        self.clock_value += timedelta(hours=5)
        self.store.change_status(222, bid, "no_show", "نیامد و تلفنی اطلاع داد")
        self.assertEqual(self.store.get_booking(111, bid)["status"], "no_show")
        with self.assertRaises(BookingError):
            self.store.change_status(222, bid, "cancelled_staff", "تغییر دوباره")

    def test_complete_retains_occupied_interval(self):
        bid = self.book()
        self.clock_value += timedelta(hours=5)
        self.store.change_status(222, bid, "completed", "خدمت انجام شد")
        self.assertEqual(self.store.get_booking(111, bid)["status"], "completed")

    def test_booking_ui_and_duplicate_confirm(self):
        tg = FakeTelegram()
        bot = Bot(self.store, tg)
        bot.callback(111, f"day|nail1|nails|{self.day.isoformat()}")
        bot.callback(111, f"slot|{self.start}")
        bot.message(111, {"text": "مشتری آزمایشی"})
        bot.message(111, {"text": "۰۹۱۲۳۴۵۶۷۸۹"})
        bot.callback(111, "confirm_booking")
        self.assertEqual(len(self.store.bookings(111)), 1)
        with self.assertRaises(BookingError):
            bot.callback(111, "confirm_booking")

    def test_notification_failure_is_durable_and_retryable(self):
        self.book()
        tg = FakeTelegram()
        bot = Bot(self.store, tg)
        tg.fail = True
        bot.flush_outbox()
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM outbox WHERE sent IS NULL AND attempts=1")
                             .fetchone()[0], 3)
        self.clock_value += timedelta(minutes=2)
        tg.fail = False
        bot.flush_outbox()
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM outbox WHERE sent IS NULL").fetchone()[0], 0)

    def test_private_chat_only(self):
        tg = FakeTelegram()
        bot = Bot(self.store, tg)
        bot.update({"message": {"chat": {"type": "group"}, "from": {"id": 111}, "text": "/start"}})
        self.assertEqual(tg.messages, [])

    def test_config_rejects_overlapping_weekly_shifts(self):
        config = copy.deepcopy(self.config)
        config["staff"][0]["weekly_hours"]["0"] = [["09:00", "12:00"], ["11:00", "18:00"]]
        with self.assertRaises(ValueError):
            Store(str(Path(self.tmp.name) / "bad.sqlite"), config)

    def test_session_survives_restart(self):
        self.store.session(111, {"step": "name", "start": self.start})
        other = Store(self.store.path, self.config, lambda: self.clock_value)
        self.assertEqual(other.session(111)["step"], "name")

    def test_cancel_ui_requires_confirmation(self):
        bid = self.book()
        bot = Bot(self.store, FakeTelegram())
        bot.callback(222, f"status|{bid}|cancelled_staff")
        bot.message(222, {"text": "تماس گرفتم و هماهنگ شد"})
        self.assertEqual(self.store.get_booking(111, bid)["status"], "confirmed")
        bot.callback(222, "confirm_status")
        self.assertEqual(self.store.get_booking(111, bid)["status"], "cancelled_staff")

    def test_full_day_block_ui_and_confirmation(self):
        bot = Bot(self.store, FakeTelegram())
        bot.callback(222, f"blockday|nail1|{self.day.isoformat()}")
        bot.message(222, {"text": "تمام‌روز مرخصی"})
        self.assertIn(self.start, self.store.slots("nail1", "nails", self.day))
        bot.callback(222, "confirm_block")
        self.assertEqual(self.store.slots("nail1", "nails", self.day), [])
        block = self.store.blocks(222, "nail1")[0]
        bot.callback(222, f"unblock|{block['id']}|nail1")
        self.assertEqual(len(self.store.blocks(222, "nail1")), 1)
        bot.callback(222, "confirm_unblock")
        self.assertEqual(self.store.blocks(222, "nail1"), [])

    def test_all_menu_callback_data_fit_telegram_limit(self):
        tg = FakeTelegram()
        bot = Bot(self.store, tg)
        uid = self.admin
        for data in ["home", "services", "service|nails", "days|nail1|nails", "manage", "staff|nail1",
                     "agenda_days|nail1", "block_days|nail1", f"day|nail1|nails|{self.day.isoformat()}"]:
            bot.callback(uid, data)
        for _, _, rows, _ in tg.messages:
            if rows:
                for row in rows:
                    for _, data in row:
                        self.assertLessEqual(len(data.encode("utf-8")), 64)

    def test_config_rejects_string_telegram_id(self):
        config = copy.deepcopy(self.config)
        config["staff"][0]["telegram_id"] = "123456"
        with self.assertRaises(ValueError):
            Store(str(Path(self.tmp.name) / "bad.sqlite"), config)


if __name__ == "__main__":
    unittest.main()
