"""Transactional booking and access control. Python 3.11+, no third-party packages."""
import json
import sqlite3
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from jalali import numeric, persian_digits, WEEKDAYS


class BookingError(Exception):
    pass


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Store:
    def __init__(self, path, config, clock=None):
        self.path, self.config = path, config
        self.tz = ZoneInfo(config.get("timezone", "Asia/Tehran"))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.services = {s["id"]: s for s in config["services"]}
        self.staff = {s["id"]: s for s in config["staff"]}
        self.validate()
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS bookings (
              id INTEGER PRIMARY KEY, staff_id TEXT NOT NULL, service_id TEXT NOT NULL,
              customer_id INTEGER NOT NULL, customer_name TEXT NOT NULL, phone TEXT NOT NULL,
              start INTEGER NOT NULL, end INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'confirmed', reason TEXT,
              changed_by INTEGER, created INTEGER NOT NULL, changed INTEGER);
            CREATE INDEX IF NOT EXISTS bookings_overlap ON bookings(staff_id,status,start,end);
            CREATE TABLE IF NOT EXISTS blocks (
              id INTEGER PRIMARY KEY, staff_id TEXT NOT NULL, start INTEGER NOT NULL,
              end INTEGER NOT NULL, reason TEXT NOT NULL, created_by INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(user_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(
              id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, next_try INTEGER NOT NULL DEFAULT 0,
              sent INTEGER, last_error TEXT);
            CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY, actor INTEGER NOT NULL, action TEXT NOT NULL,
              entity_id INTEGER, at INTEGER NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reminders(
              booking_id INTEGER NOT NULL, minutes_before INTEGER NOT NULL,
              outbox_id INTEGER NOT NULL UNIQUE,
              PRIMARY KEY(booking_id,minutes_before));
            """)
            c.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in c.execute("PRAGMA table_info(outbox)")}
            if "cancelled" not in columns:
                c.execute("ALTER TABLE outbox ADD COLUMN cancelled INTEGER")
            c.execute("CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(sent,cancelled,next_try)")

    def validate(self):
        if not self.config.get("admin_ids"):
            raise ValueError("At least one admin_id is required")
        if not all(type(uid) is int and uid > 0 for uid in self.config["admin_ids"]):
            raise ValueError("Admin IDs must be positive integers")
        if not 1 <= self.config.get("booking_days", 21) <= 31:
            raise ValueError("Booking horizon must be 1..31 days")
        if not 5 <= self.config.get("slot_step_minutes", 15) <= 120:
            raise ValueError("Slot step must be 5..120 minutes")
        if not 0 <= self.config.get("minimum_notice_minutes", 60) <= 10080:
            raise ValueError("Minimum notice must be 0..10080 minutes")
        reminders = self.config.get("reminder_minutes", [1440, 120])
        if not isinstance(reminders, list) or not all(
                type(m) is int and 1 <= m <= 43200 for m in reminders) or len(reminders) > 10:
            raise ValueError("reminder_minutes must contain at most 10 integer offsets, 1..43200")
        if len(set(reminders)) != len(reminders):
            raise ValueError("Duplicate reminder offsets")
        grace = self.config.get("reminder_grace_minutes", 30)
        if type(grace) is not int or not 1 <= grace <= 1440:
            raise ValueError("reminder_grace_minutes must be 1..1440")
        if len(self.staff) != len(self.config["staff"]) or len(self.services) != len(self.config["services"]):
            raise ValueError("Duplicate ids")
        for key in list(self.staff) + list(self.services):
            if not key or not key.isascii() or not key.replace("_", "").isalnum() or len(key) > 16:
                raise ValueError("IDs must be short ASCII alphanumeric or underscore")
        for s in self.services.values():
            if not 5 <= int(s["duration_minutes"]) <= 480:
                raise ValueError("Invalid service duration")
            if s.get("price_toman") is not None and (
                    type(s["price_toman"]) is not int or not 0 <= s["price_toman"] <= 10**12):
                raise ValueError("price_toman must be a nonnegative integer or null")
        for s in self.staff.values():
            if s.get("telegram_id") is not None and (
                    type(s["telegram_id"]) is not int or s["telegram_id"] <= 0):
                raise ValueError("Staff Telegram IDs must be positive integers or null")
            if not s["services"] or not set(s["services"]) <= self.services.keys():
                raise ValueError("Unknown staff service")
            for weekday, intervals in s["weekly_hours"].items():
                if str(weekday) not in [str(i) for i in range(7)]:
                    raise ValueError("Weekdays must be 0..6, Monday=0")
                previous = None
                for a, b in sorted(intervals):
                    aa, bb = time.fromisoformat(a), time.fromisoformat(b)
                    if aa >= bb or (previous is not None and aa < previous):
                        raise ValueError("Invalid or overlapping working intervals")
                    previous = bb

    def connect(self):
        c = sqlite3.connect(self.path, timeout=15, factory=ClosingConnection)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=15000")
        return c

    def now(self):
        return int(self.clock().timestamp())

    def today(self):
        return self.clock().astimezone(self.tz).date()

    def stamp(self, day, hhmm):
        return int(datetime.combine(day, time.fromisoformat(hhmm), self.tz).timestamp())

    def fmt(self, stamp):
        dt = datetime.fromtimestamp(stamp, self.tz)
        return f"{WEEKDAYS[dt.weekday()]} {numeric(dt.date())} ساعت {persian_digits(dt.strftime('%H:%M'))}"

    def can_manage(self, uid, sid):
        staff = self.staff.get(sid)
        return bool(staff and (uid in self.config["admin_ids"] or staff.get("telegram_id") == uid))

    def require_manage(self, uid, sid):
        if not self.can_manage(uid, sid):
            raise BookingError("اجازهٔ مدیریت این آرایشگر را نداری.")

    def manageable(self, uid):
        return [s for s in self.staff.values() if self.can_manage(uid, s["id"])]

    def session(self, uid, value=None):
        with self.connect() as c:
            if value is not None:
                c.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (uid, json.dumps(value)))
                return value
            row = c.execute("SELECT data FROM sessions WHERE user_id=?", (uid,)).fetchone()
            return json.loads(row[0]) if row else {}

    def meta(self, key, value=None):
        with self.connect() as c:
            if value is not None:
                c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def queue(self, c, uid, text):
        return c.execute("INSERT INTO outbox(chat_id,text) VALUES (?,?)", (uid, text)).lastrowid

    def cancel_reminders(self, c, bid):
        c.execute("""UPDATE outbox SET cancelled=?
            WHERE sent IS NULL AND cancelled IS NULL AND id IN
            (SELECT outbox_id FROM reminders WHERE booking_id=?)""", (self.now(), bid))

    def schedule_reminders(self):
        """Enqueue once per booking/offset; skip missed deadlines beyond a grace window.

        Existing databases need no data reset. Persistent keys survive restarts.
        Reminders are customer-only; staff receive booking/status notifications.
        """
        offsets = self.config.get("reminder_minutes", [1440, 120])
        grace = self.config.get("reminder_grace_minutes", 30) * 60
        now = self.now()
        queued = 0
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            # Expire pending reminders on cancellation, start time, or old deadline.
            rows = c.execute("""SELECT r.*, b.status, b.start FROM reminders r
                JOIN bookings b ON b.id=r.booking_id JOIN outbox o ON o.id=r.outbox_id
                WHERE o.sent IS NULL AND o.cancelled IS NULL""").fetchall()
            for row in rows:
                due = row["start"] - row["minutes_before"] * 60
                if (row["status"] != "confirmed" or row["start"] <= now or now > due + grace
                        or row["minutes_before"] not in offsets):
                    c.execute("UPDATE outbox SET cancelled=? WHERE id=?", (now, row["outbox_id"]))
            for minutes in offsets:
                # Created after the nominal reminder time? Do not send that reminder late.
                bookings = c.execute("""SELECT b.* FROM bookings b
                    WHERE b.status='confirmed' AND b.start>?
                      AND b.start-?<=? AND b.start-?>=?
                      AND b.created<=b.start-?
                      AND NOT EXISTS (SELECT 1 FROM reminders r
                          WHERE r.booking_id=b.id AND r.minutes_before=?)""",
                    (now, minutes * 60, now, minutes * 60, now - grace,
                     minutes * 60, minutes)).fetchall()
                for b in bookings:
                    duration = (f"{persian_digits(minutes // 60)} ساعت"
                                if minutes % 60 == 0 else f"{persian_digits(minutes)} دقیقه")
                    text = self.booking_text(b, f"یادآوری نوبت ({duration} قبل)")
                    text += "\nبرای مشاهده یا لغو نوبت، از /start و «نوبت‌های من» استفاده کن."
                    outbox_id = self.queue(c, b["customer_id"], text)
                    c.execute("INSERT INTO reminders VALUES (?,?,?)", (b["id"], minutes, outbox_id))
                    queued += 1
        return queued

    def audit(self, c, uid, action, entity, detail):
        c.execute("INSERT INTO audit(actor,action,entity_id,at,detail) VALUES (?,?,?,?,?)",
                  (uid, action, entity, self.now(), detail))

    def booking_text(self, b, heading="نوبت"):
        labels = {"confirmed": "رزرو قطعی", "cancelled_staff": "لغو توسط سالن",
                  "cancelled_customer": "لغو توسط مشتری", "no_show": "عدم حضور مشتری",
                  "completed": "انجام شد"}
        return (f"{heading} #{b['id']}\n"
                f"خدمت: {self.services[b['service_id']]['name']}\n"
                f"آرایشگر: {self.staff[b['staff_id']]['name']}\n"
                f"شروع: {self.fmt(b['start'])}\nپایان: {self.fmt(b['end'])}\n"
                f"نام: {b['customer_name']}\nتلفن: {b['phone']}\n"
                f"وضعیت: {labels[b['status']]}"
                + (f"\nتوضیح: {b['reason']}" if b["reason"] else ""))

    def slots(self, sid, service_id, day, c=None):
        if sid not in self.staff or service_id not in self.staff[sid]["services"]:
            raise BookingError("این خدمت برای این آرایشگر تعریف نشده.")
        if day < self.today() or day >= self.today() + timedelta(days=self.config.get("booking_days", 21)):
            return []
        own = c is None
        c = c or self.connect()
        try:
            duration = self.services[service_id]["duration_minutes"] * 60
            step = self.config.get("slot_step_minutes", 15) * 60
            earliest = self.now() + self.config.get("minimum_notice_minutes", 60) * 60
            result = []
            for a, b in self.staff[sid]["weekly_hours"].get(str(day.weekday()), []):
                start, end = self.stamp(day, a), self.stamp(day, b)
                while start + duration <= end:
                    busy = c.execute("""SELECT 1 FROM bookings
                        WHERE staff_id=? AND status IN ('confirmed','completed')
                        AND start<? AND end>? LIMIT 1""",
                        (sid, start + duration, start)).fetchone()
                    blocked = c.execute("""SELECT 1 FROM blocks WHERE staff_id=?
                        AND start<? AND end>? LIMIT 1""", (sid, start + duration, start)).fetchone()
                    if start >= earliest and not busy and not blocked:
                        result.append(start)
                    start += step
            return result
        finally:
            if own:
                c.close()

    def book(self, uid, sid, service_id, start, name, phone):
        if not name.strip() or len(name) > 100 or not phone or len(phone) > 20:
            raise BookingError("نام یا شماره تلفن نامعتبر است.")
        day = datetime.fromtimestamp(start, self.tz).date()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if start not in self.slots(sid, service_id, day, c):
                raise BookingError("این ساعت دیگر آزاد نیست؛ دوباره ساعت انتخاب کن.")
            duration = self.services[service_id]["duration_minutes"] * 60
            bid = c.execute("""INSERT INTO bookings
                (staff_id,service_id,customer_id,customer_name,phone,start,end,created)
                VALUES (?,?,?,?,?,?,?,?)""",
                (sid, service_id, uid, name.strip(), phone, start, start + duration, self.now())).lastrowid
            b = c.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
            text = self.booking_text(b, "نوبت جدید ثبت شد")
            recipients = {uid, *self.config["admin_ids"]}
            staff_uid = self.staff[sid].get("telegram_id")
            if staff_uid:
                recipients.add(staff_uid)
            for recipient in recipients:
                self.queue(c, recipient, text)
            self.audit(c, uid, "book", bid, text)
            return bid

    def get_booking(self, uid, bid):
        with self.connect() as c:
            b = c.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
        if not b or (b["customer_id"] != uid and not self.can_manage(uid, b["staff_id"])):
            raise BookingError("نوبت پیدا نشد یا دسترسی نداری.")
        return dict(b)

    def change_status(self, uid, bid, status, reason):
        if status not in ("cancelled_staff", "cancelled_customer", "no_show", "completed"):
            raise BookingError("وضعیت نامعتبر است.")
        if not reason.strip() or len(reason) > 400:
            raise BookingError("توضیح کوتاه و معتبر وارد کن.")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            b = c.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
            if not b:
                raise BookingError("نوبت پیدا نشد.")
            manager = self.can_manage(uid, b["staff_id"])
            if not manager and not (b["customer_id"] == uid and status == "cancelled_customer"):
                raise BookingError("اجازهٔ این تغییر را نداری.")
            if b["status"] != "confirmed":
                raise BookingError("این نوبت قبلاً تعیین تکلیف شده.")
            if status in ("no_show", "completed") and b["start"] > self.now():
                raise BookingError("قبل از زمان نوبت نمی‌توان انجام‌شده یا عدم حضور ثبت کرد.")
            c.execute("UPDATE bookings SET status=?,reason=?,changed_by=?,changed=? WHERE id=?",
                      (status, reason.strip(), uid, self.now(), bid))
            self.cancel_reminders(c, bid)
            updated = c.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
            text = self.booking_text(updated, "وضعیت نوبت تغییر کرد")
            recipients = {b["customer_id"], *self.config["admin_ids"]}
            if self.staff[b["staff_id"]].get("telegram_id"):
                recipients.add(self.staff[b["staff_id"]]["telegram_id"])
            for recipient in recipients:
                self.queue(c, recipient, text)
            self.audit(c, uid, status, bid, reason.strip())

    def block(self, uid, sid, start, end, reason):
        self.require_manage(uid, sid)
        if end <= start or end <= self.now() or end - start > 86400 * 2:
            raise BookingError("بازهٔ زمانی نامعتبر است.")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("""SELECT 1 FROM bookings WHERE staff_id=? AND status='confirmed'
                AND start<? AND end>?""", (sid, end, start)).fetchone():
                raise BookingError("در این بازه نوبت قطعی وجود دارد؛ اول با مشتری هماهنگ کن و نوبت را لغو کن.")
            entity = c.execute("INSERT INTO blocks(staff_id,start,end,reason,created_by) VALUES (?,?,?,?,?)",
                              (sid, start, end, reason, uid)).lastrowid
            self.audit(c, uid, "block", entity, f"{sid}: {start}..{end}, {reason}")
            return entity

    def unblock(self, uid, block_id):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM blocks WHERE id=?", (block_id,)).fetchone()
            if not row:
                raise BookingError("بازه پیدا نشد.")
            self.require_manage(uid, row["staff_id"])
            c.execute("DELETE FROM blocks WHERE id=?", (block_id,))
            self.audit(c, uid, "unblock", block_id, row["staff_id"])

    def blocks(self, uid, sid):
        self.require_manage(uid, sid)
        with self.connect() as c:
            return [dict(b) for b in c.execute(
                "SELECT * FROM blocks WHERE staff_id=? AND end>? ORDER BY start", (sid, self.now()))]

    def bookings(self, uid, sid=None, day=None):
        if sid:
            self.require_manage(uid, sid)
            where, params = "staff_id=?", [sid]
        else:
            where, params = "customer_id=?", [uid]
        if day:
            start = self.stamp(day, "00:00")
            end = self.stamp(day + timedelta(days=1), "00:00")
            where += " AND start>=? AND start<?"
            params += [start, end]
        with self.connect() as c:
            return [dict(b) for b in c.execute(
                f"SELECT * FROM bookings WHERE {where} ORDER BY start DESC LIMIT 100", params)]
