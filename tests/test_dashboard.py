import base64
import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from core import Store, BookingError
from dashboard import Analytics, solar_date, merge, length, intersection, make_handler
from jalali import numeric


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        self.config["services"][1]["price_toman"] = 850000
        self.config["staff"][2]["telegram_id"] = 222
        self.now = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
        self.db = Store(str(Path(self.tmp.name) / "db.sqlite"), self.config, lambda: self.now)
        self.a = Analytics(self.db)
        self.admin = self.config["admin_ids"][0]
        self.day = self.db.today()
        self.start = self.db.stamp(self.day, "10:00")

    def tearDown(self):
        self.tmp.cleanup()

    def book(self, sid="nail1", service="nails", start=None):
        return self.db.book(111, sid, service, start or self.start, "مشتری نمونه", "09123456789")

    def completed(self):
        bid = self.book()
        self.now += timedelta(hours=6)
        self.db.change_status(self.admin, bid, "completed", "انجام شد")
        return bid

    def report(self, sid="nail1"):
        return self.a.report(self.day, self.day, sid)

    def test_actual_revenue_is_not_estimate(self):
        self.book()
        r = self.report()
        self.assertEqual(r["summary"]["revenue"], 0)
        self.assertEqual(r["summary"]["estimated_value"], 850000)
        self.assertEqual(r["summary"]["unreconciled_completed"], 0)

    def test_reconcile_and_correct_amount_with_audit(self):
        bid = self.completed()
        self.assertEqual(self.report()["summary"]["unreconciled_completed"], 1)
        self.a.set_payment(self.admin, bid, 800000, "تسویه با تخفیف")
        self.assertEqual(self.report()["summary"]["revenue"], 800000)
        self.a.set_payment(self.admin, bid, 700000, "اصلاح پس از بازپرداخت")
        self.assertEqual(self.report()["summary"]["revenue"], 700000)
        with self.db.connect() as c:
            rows = c.execute("SELECT detail FROM audit WHERE action='reconcile_payment'").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(rows[-1][0])["old"], 800000)

    def test_non_admin_cannot_set_payment(self):
        bid = self.completed()
        with self.assertRaises(BookingError):
            self.a.set_payment(222, bid, 800000, "غیرمجاز")

    def test_cannot_record_uncompleted_payment(self):
        bid = self.book()
        with self.assertRaises(BookingError):
            self.a.set_payment(self.admin, bid, 800000, "نوبت هنوز انجام نشده")

    def test_amount_and_note_validation(self):
        bid = self.completed()
        for amount in (-1, 1.5, True, "850000", 10**13):
            with self.subTest(amount=amount), self.assertRaises(BookingError):
                self.a.set_payment(self.admin, bid, amount, "تست")
        with self.assertRaises(BookingError):
            self.a.set_payment(self.admin, bid, 1000, "")
        self.a.set_payment(self.admin, bid, 0, "خدمت رایگان")
        self.assertEqual(self.report()["summary"]["unreconciled_completed"], 0)

    def test_capacity_excludes_union_of_blocks(self):
        self.book()
        self.db.block(self.admin, "nail1", self.start + 2 * 3600, self.start + 4 * 3600, "مرخصی")
        self.db.block(self.admin, "nail1", self.start + 3 * 3600, self.start + 5 * 3600, "کار شخصی")
        r = self.report()["staff"][0]
        self.assertEqual(r["scheduled_minutes"], 540)
        self.assertEqual(r["blocked_minutes"], 180)
        self.assertEqual(r["available_minutes"], 360)
        self.assertEqual(r["occupied_minutes"], 90)
        self.assertEqual(r["free_minutes"], 270)
        self.assertEqual(r["utilization"], 25)

    def test_cancellation_breakdown_separate_no_show(self):
        b1 = self.book()
        b2 = self.book("nail2")
        b3 = self.book("lash1", "lashes")
        self.db.change_status(self.admin, b1, "cancelled_staff", "مرخصی")
        self.db.change_status(111, b2, "cancelled_customer", "تغییر برنامه")
        self.now += timedelta(hours=5)
        self.db.change_status(self.admin, b3, "no_show", "نیامد")
        r = self.a.report(self.day, self.day)
        self.assertEqual(r["summary"]["cancel_rate"], 66.7)
        self.assertEqual(r["summary"]["statuses"]["no_show"], 1)
        self.assertEqual(r["summary"]["occupied_minutes"], 0)

    def test_independent_staff_filter(self):
        self.book()
        self.book("nail2")
        r = self.report()
        self.assertEqual(len(r["staff"]), 1)
        self.assertEqual(len(r["bookings"]), 1)
        self.assertEqual(r["summary"]["occupied_minutes"], 90)

    def test_missing_price_not_fabricated(self):
        self.book("hair1", "hair")
        r = self.a.report(self.day, self.day, "hair1")
        self.assertEqual(r["summary"]["unpriced_appointments"], 1)
        self.assertEqual(r["summary"]["estimated_value"], 0)

    def test_closed_day_no_percentage(self):
        friday = self.day + timedelta(days=4)
        r = self.a.report(friday, friday, "nail1")
        self.assertIsNone(r["summary"]["utilization"])
        self.assertEqual(r["summary"]["available_minutes"], 0)

    def test_modified_schedule_reports_outside_minutes(self):
        self.book()
        self.db.staff["nail1"]["weekly_hours"]["0"] = [["12:00", "18:00"]]
        r = self.report()["staff"][0]
        self.assertEqual(r["outside_schedule_minutes"], 90)
        self.assertEqual(r["occupied_minutes"], 0)

    def test_daily_totals_match_summary(self):
        bid = self.completed()
        self.a.set_payment(self.admin, bid, 850000, "تسویه")
        r = self.a.report(self.day, self.day + timedelta(days=6))
        self.assertEqual(sum(d["revenue"] for d in r["daily"]), r["summary"]["revenue"])
        self.assertEqual(sum(d["occupied_minutes"] for d in r["daily"]), r["summary"]["occupied_minutes"])
        self.assertEqual(sum(s["revenue"] for s in r["staff"]), r["summary"]["revenue"])

    def test_range_validation_and_solar_dates(self):
        self.assertEqual(solar_date("۱۴۰۵/۰۷/۱۴"), date(2026, 10, 6))
        self.assertEqual(solar_date("1403/12/30"), date(2025, 3, 20))
        with self.assertRaises(ValueError):
            solar_date("1404/12/30")
        for a, b in [(self.day + timedelta(days=1), self.day), (self.day, self.day + timedelta(days=93))]:
            with self.assertRaises(ValueError):
                self.a.report(a, b)

    def test_interval_arithmetic(self):
        self.assertEqual(merge([(0, 20), (10, 30), (40, 50)]), [(0, 30), (40, 50)])
        self.assertEqual(length(intersection([(0, 60)], [(10, 20), (15, 30)])), 20)

    def test_http_auth_csrf_live_html_report_and_payment(self):
        bid = self.completed()
        handler = make_handler(self.a, "manager", "a-secure-test-password", self.admin, "dashboard.html")
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        auth = "Basic " + base64.b64encode(b"manager:a-secure-test-password").decode()
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(url)
            self.assertEqual(e.exception.code, 401)
            req = urllib.request.Request(url, headers={"Authorization": auth})
            with urllib.request.urlopen(req) as response:
                self.assertIn("const LIVE = true;", response.read().decode())
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            with urllib.request.urlopen(urllib.request.Request(url + "/mobile",
                                                               headers={"Authorization": auth})) as response:
                mobile = response.read().decode()
                self.assertIn("const LIVE = true;", mobile)
                self.assertIn('id="bookingList"', mobile)
                self.assertIn('data-page="capacity"', mobile)
            query = f"/api/report?from=1405/07/13&to=1405/07/13&staff=nail1"
            with urllib.request.urlopen(urllib.request.Request(url + query, headers={"Authorization": auth})) as r:
                report = json.load(r)
            self.assertEqual(report["mode"], "live")
            self.assertEqual(report["summary"]["appointments"], 1)
            body = json.dumps({"booking_id": bid, "amount_toman": 850000, "note": "تسویه"}).encode()
            headers = {"Authorization": auth, "Content-Type": "application/json"}
            req = urllib.request.Request(url + "/api/payment", data=body, headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 403)
            headers["X-CSRF-Token"] = report["csrf"]
            with urllib.request.urlopen(urllib.request.Request(url + "/api/payment", data=body, headers=headers)) as r:
                self.assertTrue(json.load(r)["ok"])
            self.assertEqual(self.report()["summary"]["revenue"], 850000)
            with urllib.request.urlopen(urllib.request.Request(url + "/api/export?from=1405/07/13&to=1405/07/13",
                                                               headers={"Authorization": auth})) as r:
                text = r.read().decode("utf-8-sig")
                self.assertIn("850000", text)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
