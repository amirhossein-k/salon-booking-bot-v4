"""Local authenticated management dashboard; publish only behind HTTPS.

Actual revenue is the manually reconciled net amount for completed appointments.
Period selection is by appointment start date, not payment collection date.
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core import Store, BookingError
from jalali import from_gregorian, new_year, numeric, persian_digits

ACTIVE = ("confirmed", "completed")


def merge(intervals):
    result = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if result and a <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], b))
        else:
            result.append((a, b))
    return result


def length(intervals):
    return sum(b - a for a, b in merge(intervals))


def intersection(left, right):
    return merge([(max(a, c), min(b, d)) for a, b in left for c, d in right
                  if max(a, c) < min(b, d)])


def solar_date(value):
    """Parse YYYY/MM/DD Solar Hijri, with Persian or Latin digits."""
    value = value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    parts = value.replace("-", "/").split("/")
    if len(parts) != 3:
        raise ValueError("تاریخ را به شکل ۱۴۰۵/۰۷/۱۴ وارد کن.")
    y, m, d = map(int, parts)
    if not 1 <= m <= 12 or not 1 <= d <= 31:
        raise ValueError("تاریخ شمسی نامعتبر است.")
    offset = (m - 1) * 31 if m <= 6 else 186 + (m - 7) * 30
    day = new_year(y) + timedelta(days=offset + d - 1)
    if from_gregorian(day) != (y, m, d):
        raise ValueError("این روز در تقویم شمسی وجود ندارد.")
    return day


class Analytics:
    def __init__(self, store):
        self.store = store
        with store.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS payments(
                    booking_id INTEGER PRIMARY KEY, amount_toman INTEGER NOT NULL,
                    note TEXT NOT NULL, updated INTEGER NOT NULL, actor INTEGER NOT NULL);
            """)

    def set_payment(self, actor, bid, amount, note):
        if actor not in self.store.config["admin_ids"]:
            raise BookingError("فقط مدیر مجاز به ثبت مبلغ است.")
        if type(amount) is not int or not 0 <= amount <= 10**12:
            raise BookingError("مبلغ باید عدد صحیح و غیرمنفی به تومان باشد.")
        if not isinstance(note, str) or not 1 <= len(note.strip()) <= 400:
            raise BookingError("توضیح ثبت یا اصلاح مبلغ الزامی است.")
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            b = c.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
            if not b or b["status"] != "completed":
                raise BookingError("مبلغ دریافتی فقط برای نوبت انجام‌شده قابل ثبت است.")
            old = c.execute("SELECT amount_toman FROM payments WHERE booking_id=?", (bid,)).fetchone()
            c.execute("""INSERT INTO payments VALUES (?,?,?,?,?)
                ON CONFLICT(booking_id) DO UPDATE SET amount_toman=excluded.amount_toman,
                note=excluded.note,updated=excluded.updated,actor=excluded.actor""",
                (bid, amount, note.strip(), self.store.now(), actor))
            self.store.audit(c, actor, "reconcile_payment", bid,
                             json.dumps({"old": old[0] if old else None, "new": amount,
                                         "note": note.strip()}, ensure_ascii=False))

    def report(self, first, last, sid=None):
        s = self.store
        if first > last or (last - first).days >= 93:
            raise ValueError("بازه باید معتبر و حداکثر ۹۳ روز باشد.")
        if sid and sid not in s.staff:
            raise ValueError("آرایشگر نامعتبر است.")
        start, end = s.stamp(first, "00:00"), s.stamp(last + timedelta(days=1), "00:00")
        with s.connect() as c:
            # Include intersecting appointments for occupancy; period KPIs use start date.
            all_bookings = [dict(r) for r in c.execute(
                """SELECT b.*,p.amount_toman,p.note AS payment_note,p.updated AS payment_updated
                FROM bookings b LEFT JOIN payments p ON p.booking_id=b.id
                WHERE b.start<? AND b.end>? ORDER BY b.start DESC""", (end, start))]
            blocks = [dict(r) for r in c.execute(
                "SELECT * FROM blocks WHERE start<? AND end>?", (end, start))]
        all_bookings = [b for b in all_bookings if not sid or b["staff_id"] == sid]
        bookings = [b for b in all_bookings if start <= b["start"] < end]
        daily = []
        staff_rows = []
        for staff in s.staff.values():
            if sid and staff["id"] != sid:
                continue
            staff_bookings = [b for b in bookings if b["staff_id"] == staff["id"]]
            hours, blocked, occupied, outside = 0, 0, 0, 0
            day = first
            while day <= last:
                working = merge([(s.stamp(day, a), s.stamp(day, b))
                                 for a, b in staff["weekly_hours"].get(str(day.weekday()), [])])
                busy = merge([(max(start, b["start"]), min(end, b["end"]))
                              for b in all_bookings if b["staff_id"] == staff["id"] and b["status"] in ACTIVE])
                closed = merge([(r["start"], r["end"]) for r in blocks if r["staff_id"] == staff["id"]])
                capacity = length(working)
                blocked_now = length(intersection(working, closed))
                reserved_now = length(intersection(working, busy))
                # Existing bookings in now-modified hours can be outside the current schedule.
                ds, de = s.stamp(day, "00:00"), s.stamp(day + timedelta(days=1), "00:00")
                active_day = intersection(busy, [(ds, de)])
                outside += length(active_day) - length(intersection(working, active_day))
                overlap = length(intersection(intersection(working, busy), closed))
                hours += capacity
                blocked += blocked_now
                occupied += reserved_now - overlap
                day_bookings = [b for b in staff_bookings
                                if datetime.fromtimestamp(b["start"], s.tz).date() == day]
                daily.append({"day": day.isoformat(), "label": numeric(day), "staff_id": staff["id"],
                              "available_minutes": (capacity - blocked_now) // 60,
                              "occupied_minutes": (reserved_now - overlap) // 60,
                              "revenue": sum(b["amount_toman"] or 0 for b in day_bookings
                                             if b["status"] == "completed"),
                              "cancelled": sum(b["status"] in ("cancelled_staff", "cancelled_customer")
                                               for b in day_bookings)})
                day += timedelta(days=1)
            available = (hours - blocked) // 60
            used = occupied // 60
            cancelled = [b for b in staff_bookings if b["status"] in ("cancelled_staff", "cancelled_customer")]
            staff_rows.append({
                "id": staff["id"], "name": staff["name"],
                "services": "، ".join(s.services[x]["name"] for x in staff["services"]),
                "scheduled_minutes": hours // 60, "blocked_minutes": blocked // 60,
                "available_minutes": available, "occupied_minutes": used,
                "free_minutes": max(0, available - used),
                "utilization": round(100 * used / available, 1) if available else None,
                "outside_schedule_minutes": outside // 60,
                "revenue": sum(b["amount_toman"] or 0 for b in staff_bookings if b["status"] == "completed"),
                "cancelled": len(cancelled), "appointments": len(staff_bookings),
                "cancel_rate": round(100 * len(cancelled) / len(staff_bookings), 1) if staff_bookings else 0,
            })
        states = {key: sum(b["status"] == key for b in bookings)
                  for key in ("confirmed", "completed", "cancelled_staff", "cancelled_customer", "no_show")}
        actual = sum(b["amount_toman"] or 0 for b in bookings if b["status"] == "completed")
        valued = [b for b in bookings if b["status"] in ACTIVE]
        unknown = sum(s.services[b["service_id"]].get("price_toman") is None for b in valued)
        estimate = sum(s.services[b["service_id"]].get("price_toman") or 0 for b in valued)
        total_capacity = sum(r["available_minutes"] for r in staff_rows)
        total_used = sum(r["occupied_minutes"] for r in staff_rows)
        recent = []
        for b in bookings:
            recent.append({
                "id": b["id"], "staff_id": b["staff_id"], "staff_name": s.staff[b["staff_id"]]["name"],
                "service": s.services[b["service_id"]]["name"], "customer_name": b["customer_name"],
                "date": s.fmt(b["start"]), "status": b["status"], "reason": b["reason"],
                "amount_toman": b["amount_toman"], "payment_note": b["payment_note"]})
        return {
            "mode": "live", "salon_name": s.config["salon_name"],
            "period": {"from": numeric(first), "to": numeric(last)},
            "staff_options": [{"id": x["id"], "name": x["name"]} for x in s.staff.values()],
            "summary": {"revenue": actual, "estimated_value": estimate,
                        "unpriced_appointments": unknown,
                        "unreconciled_completed": sum(b["status"] == "completed" and b["amount_toman"] is None
                                                      for b in bookings),
                        "appointments": len(bookings), "statuses": states,
                        "cancel_rate": round(100 * (states["cancelled_staff"] + states["cancelled_customer"])
                                             / len(bookings), 1) if bookings else 0,
                        "available_minutes": total_capacity, "occupied_minutes": total_used,
                        "free_minutes": max(0, total_capacity - total_used),
                        "utilization": round(100 * total_used / total_capacity, 1) if total_capacity else None},
            "staff": staff_rows, "daily": daily, "bookings": recent}


def make_handler(analytics, username, password, actor, html_path):
    csrf = secrets.token_urlsafe(32)
    expected = hashlib.sha256((username + ":" + password).encode()).digest()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Do not log Authorization, URL query, tokens, names or amounts.
            print(f"dashboard: {self.command} response")

        def authorized(self):
            auth = self.headers.get("Authorization", "")
            try:
                raw = base64.b64decode(auth.split(" ", 1)[1], validate=True)
                ok = auth.startswith("Basic ") and hmac.compare_digest(hashlib.sha256(raw).digest(), expected)
            except (ValueError, IndexError):
                ok = False
            if not ok:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Salon Manager", charset="UTF-8"')
                self.send_header("Content-Length", "0")
                self.end_headers()
            return ok

        def respond(self, code, content, mime="application/json; charset=utf-8"):
            if isinstance(content, dict):
                content = json.dumps(content, ensure_ascii=False).encode()
            elif isinstance(content, str):
                content = content.encode()
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(content)

        def get_report(self):
            params = parse_qs(urlparse(self.path).query)
            first = solar_date(params.get("from", [numeric(analytics.store.today() - timedelta(days=6))])[0])
            last = solar_date(params.get("to", [numeric(analytics.store.today())])[0])
            return analytics.report(first, last, params.get("staff", [""])[0] or None)

        def do_GET(self):
            if not self.authorized():
                return
            path = urlparse(self.path).path
            try:
                if path == "/":
                    html = Path(html_path).read_text(encoding="utf-8")
                    html = html.replace("const LIVE = false;", "const LIVE = true;")
                    self.respond(200, html, "text/html; charset=utf-8")
                elif path in ("/mobile", "/mobile/"):
                    mobile = Path(html_path).with_name("dashboard-mobile.html")
                    html = mobile.read_text(encoding="utf-8")
                    html = html.replace("const LIVE = false;", "const LIVE = true;")
                    self.respond(200, html, "text/html; charset=utf-8")
                elif path == "/api/report":
                    report = self.get_report()
                    report["csrf"] = csrf
                    self.respond(200, report)
                elif path == "/api/export":
                    report = self.get_report()
                    output = io.StringIO()
                    writer = csv.writer(output)
                    writer.writerow(["آرایشگر", "درآمد ثبت‌شده (تومان)", "نوبت", "لغو",
                                     "ظرفیت قابل استفاده (دقیقه)", "زمان اشغال (دقیقه)", "درصد اشغال"])
                    for row in report["staff"]:
                        # Prevent spreadsheet formula injection in editable staff names.
                        name = row["name"]
                        if name.lstrip().startswith(("=", "+", "-", "@")):
                            name = "'" + name
                        writer.writerow([name, row["revenue"], row["appointments"], row["cancelled"],
                                         row["available_minutes"], row["occupied_minutes"], row["utilization"]])
                    self.respond(200, "\ufeff" + output.getvalue(), "text/csv; charset=utf-8")
                else:
                    self.respond(404, {"error": "مسیر پیدا نشد."})
            except (ValueError, BookingError) as error:
                self.respond(400, {"error": str(error)})
            except Exception:
                self.respond(500, {"error": "گزارش بارگذاری نشد؛ تنظیمات و دیتابیس را بررسی کن."})

        def do_POST(self):
            if not self.authorized():
                return
            if urlparse(self.path).path != "/api/payment":
                self.respond(404, {"error": "مسیر پیدا نشد."})
                return
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf):
                self.respond(403, {"error": "درخواست معتبر نیست؛ صفحه را تازه کن."})
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.respond(415, {"error": "نوع درخواست نامعتبر است."})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ValueError("اندازهٔ درخواست نامعتبر است.")
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict) or type(payload.get("booking_id")) is not int:
                    raise ValueError("شناسهٔ نوبت نامعتبر است.")
                analytics.set_payment(actor, payload["booking_id"], payload.get("amount_toman"),
                                      payload.get("note"))
                self.respond(200, {"ok": True})
            except (ValueError, BookingError, UnicodeDecodeError) as error:
                self.respond(400, {"error": str(error)})
            except Exception:
                self.respond(500, {"error": "مبلغ ثبت نشد؛ وضعیت دیتابیس را بررسی کن."})

    return Handler


def main():
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if len(password) < 16 or password == "REPLACE_WITH_A_UNIQUE_LONG_PASSWORD":
        raise SystemExit("Set a unique DASHBOARD_PASSWORD of at least 16 characters.")
    username = os.environ.get("DASHBOARD_USER", "manager")
    config = json.loads(Path(os.environ.get("CONFIG_PATH", "config.json")).read_text(encoding="utf-8"))
    actor = int(os.environ.get("DASHBOARD_ADMIN_ID", config["admin_ids"][0]))
    if actor not in config["admin_ids"]:
        raise SystemExit("DASHBOARD_ADMIN_ID must be configured as an admin")
    db = os.environ.get("DB_PATH", "data/salon.sqlite3")
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    analytics = Analytics(Store(db, config))
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    html = os.environ.get("DASHBOARD_HTML", "dashboard.html")
    if not Path(html).is_file():
        raise SystemExit("dashboard.html is missing")
    print("Dashboard ready. Local use/SSH tunnel only; HTTPS required before public access.")
    ThreadingHTTPServer((host, port), make_handler(analytics, username, password, actor, html)).serve_forever()


if __name__ == "__main__":
    main()
