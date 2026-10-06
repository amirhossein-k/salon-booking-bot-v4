"""MongoDB repository. Transactions require a replica set (including Atlas).

Every calendar mutation writes a pre-created per-staff guard document BEFORE
checking overlaps. Conflicting transactions retry from a new snapshot.
Telegram calls never run inside a retryable database transaction.
"""
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument, timeout
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from core import Store, BookingError
from jalali import persian_digits


class MongoStore(Store):
    def __init__(self, database, config, clock=None):
        self.db = database
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.tz = ZoneInfo(config.get("timezone", "Asia/Tehran"))
        self.services = {s["id"]: s for s in config["services"]}
        self.staff = {s["id"]: s for s in config["staff"]}
        self.validate()
        self.active_session = None

    def connect(self):
        raise RuntimeError("SQLite is not used by the Vercel deployment")

    def kwargs(self):
        return {"session": self.active_session} if self.active_session else {}

    def transaction(self, callback):
        if self.active_session:
            return callback()
        with self.db.client.start_session() as session, timeout(25):
            def run(current):
                self.active_session = current
                try:
                    return callback()
                finally:
                    self.active_session = None
            return session.with_transaction(
                run, read_concern=ReadConcern("snapshot"),
                write_concern=WriteConcern("majority"), max_commit_time_ms=10000)

    def guard(self, sid):
        result = self.db.guards.update_one({"_id": sid}, {"$inc": {"revision": 1}}, **self.kwargs())
        if not result.matched_count:
            raise RuntimeError("Run scripts/init_db.py before enabling bookings")

    def next_id(self, kind):
        row = self.db.counters.find_one_and_update(
            {"_id": kind}, {"$inc": {"value": 1}}, return_document=ReturnDocument.AFTER,
            **self.kwargs())
        if not row:
            raise RuntimeError("Run scripts/init_db.py")
        return row["value"]

    def session(self, uid, value=None):
        if value is not None:
            self.db.sessions.replace_one({"_id": uid}, {"_id": uid, "data": value},
                                        upsert=True, **self.kwargs())
            return value
        row = self.db.sessions.find_one({"_id": uid}, **self.kwargs())
        return row["data"] if row else {}

    def queue_message(self, uid, text, markup=None, key=None, **extra):
        document = {"_id": key or uuid.uuid4().hex, "chat_id": uid, "text": text,
                    "markup": markup, "created": self.now(), "next_try": 0,
                    "lease_until": 0, "sent": None, "cancelled": None, "attempts": 0, **extra}
        self.db.outbox.update_one({"_id": document["_id"]}, {"$setOnInsert": document},
                                 upsert=True, **self.kwargs())
        return document["_id"]

    def audit_event(self, uid, action, bid, detail):
        self.db.audit.insert_one({"actor": uid, "action": action, "entity_id": bid,
                                  "at": self.now(), "detail": detail}, **self.kwargs())

    def notify(self, b, heading):
        recipients = {b["customer_id"], *self.config["admin_ids"]}
        if self.staff[b["staff_id"]].get("telegram_id"):
            recipients.add(self.staff[b["staff_id"]]["telegram_id"])
        for uid in recipients:
            self.queue_message(uid, self.booking_text(b, heading),
                               key=f"booking:{b['id']}:{b['status']}:{uid}")

    def slots(self, sid, service_id, day, c=None):
        if sid not in self.staff or service_id not in self.staff[sid]["services"]:
            raise BookingError("این خدمت برای این آرایشگر تعریف نشده.")
        if day < self.today() or day >= self.today() + timedelta(days=self.config.get("booking_days", 21)):
            return []
        ds, de = self.stamp(day, "00:00"), self.stamp(day + timedelta(days=1), "00:00")
        query = {"staff_id": sid, "start": {"$lt": de}, "end": {"$gt": ds}}
        busy = list(self.db.bookings.find({**query, "status": {"$in": ["confirmed", "completed"]}},
                                         **self.kwargs()))
        blocks = list(self.db.blocks.find(query, **self.kwargs()))
        duration = self.services[service_id]["duration_minutes"] * 60
        step = self.config.get("slot_step_minutes", 15) * 60
        earliest = self.now() + self.config.get("minimum_notice_minutes", 60) * 60
        slots = []
        for a, b in self.staff[sid]["weekly_hours"].get(str(day.weekday()), []):
            start, end = self.stamp(day, a), self.stamp(day, b)
            while start + duration <= end:
                if start >= earliest and not any(
                        r["start"] < start + duration and r["end"] > start for r in busy + blocks):
                    slots.append(start)
                start += step
        return slots

    def book(self, uid, sid, service_id, start, name, phone):
        if not name.strip() or len(name) > 100 or not phone or len(phone) > 20:
            raise BookingError("نام یا شماره تلفن نامعتبر است.")
        day = datetime.fromtimestamp(start, self.tz).date()
        def work():
            self.guard(sid)
            if start not in self.slots(sid, service_id, day):
                raise BookingError("این ساعت دیگر آزاد نیست؛ دوباره ساعت انتخاب کن.")
            bid = self.next_id("bookings")
            b = {"_id": bid, "id": bid, "staff_id": sid, "service_id": service_id,
                 "customer_id": uid, "customer_name": name.strip(), "phone": phone,
                 "start": start, "end": start + self.services[service_id]["duration_minutes"] * 60,
                 "status": "confirmed", "reason": None, "created": self.now(),
                 "amount_toman": None, "payment_note": None, "payment_updated": None}
            self.db.bookings.insert_one(b, **self.kwargs())
            self.notify(b, "نوبت جدید ثبت شد")
            self.audit_event(uid, "book", bid, "رزرو نوبت")
            return bid
        return self.transaction(work)

    def get_booking(self, uid, bid):
        b = self.db.bookings.find_one({"_id": bid}, **self.kwargs())
        if not b or (b["customer_id"] != uid and not self.can_manage(uid, b["staff_id"])):
            raise BookingError("نوبت پیدا نشد یا دسترسی نداری.")
        return b

    def change_status(self, uid, bid, status, reason):
        if status not in ("cancelled_staff", "cancelled_customer", "no_show", "completed"):
            raise BookingError("وضعیت نامعتبر است.")
        if not reason.strip() or len(reason) > 400:
            raise BookingError("توضیح کوتاه و معتبر وارد کن.")
        def work():
            b = self.get_booking(uid, bid)
            self.guard(b["staff_id"])
            # Calendar guard can force transaction restart after concurrent changes.
            b = self.get_booking(uid, bid)
            if not self.can_manage(uid, b["staff_id"]) and status != "cancelled_customer":
                raise BookingError("اجازهٔ این تغییر را نداری.")
            if b["status"] != "confirmed":
                raise BookingError("این نوبت قبلاً تعیین تکلیف شده.")
            if status in ("no_show", "completed") and b["start"] > self.now():
                raise BookingError("قبل از شروع نوبت نمی‌توان این وضعیت را ثبت کرد.")
            change = {"status": status, "reason": reason.strip(), "changed_by": uid, "changed": self.now()}
            self.db.bookings.update_one({"_id": bid}, {"$set": change}, **self.kwargs())
            self.db.outbox.update_many({"booking_id": bid, "kind": "reminder", "sent": None},
                                      {"$set": {"cancelled": self.now()}}, **self.kwargs())
            b.update(change)
            self.notify(b, "وضعیت نوبت تغییر کرد")
            self.audit_event(uid, status, bid, reason.strip())
        return self.transaction(work)

    def block(self, uid, sid, start, end, reason):
        self.require_manage(uid, sid)
        if end <= start or end <= self.now() or end - start > 2 * 86400 or not 1 <= len(reason) <= 200:
            raise BookingError("بازهٔ زمانی یا دلیل نامعتبر است.")
        def work():
            self.guard(sid)
            if self.db.bookings.find_one({"staff_id": sid, "status": "confirmed",
                                         "start": {"$lt": end}, "end": {"$gt": start}}, **self.kwargs()):
                raise BookingError("در این بازه نوبت قطعی وجود دارد؛ اول با مشتری هماهنگ و نوبت را لغو کن.")
            entity = self.next_id("blocks")
            self.db.blocks.insert_one({"_id": entity, "id": entity, "staff_id": sid,
                                      "start": start, "end": end, "reason": reason,
                                      "created_by": uid}, **self.kwargs())
            self.audit_event(uid, "block", entity, reason)
            return entity
        return self.transaction(work)

    def unblock(self, uid, block_id):
        def work():
            row = self.db.blocks.find_one({"_id": block_id}, **self.kwargs())
            if not row:
                raise BookingError("بازه پیدا نشد.")
            self.require_manage(uid, row["staff_id"])
            self.guard(row["staff_id"])
            self.db.blocks.delete_one({"_id": block_id}, **self.kwargs())
            self.audit_event(uid, "unblock", block_id, row["staff_id"])
        return self.transaction(work)

    def blocks(self, uid, sid):
        self.require_manage(uid, sid)
        return list(self.db.blocks.find({"staff_id": sid, "end": {"$gt": self.now()}},
                                        **self.kwargs()).sort("start", 1).limit(100))

    def bookings(self, uid, sid=None, day=None):
        if sid:
            self.require_manage(uid, sid)
        query = {"staff_id": sid} if sid else {"customer_id": uid}
        if day:
            query["start"] = {"$gte": self.stamp(day, "00:00"),
                              "$lt": self.stamp(day + timedelta(days=1), "00:00")}
        return list(self.db.bookings.find(query, **self.kwargs()).sort("start", -1).limit(100))

    def schedule_reminders(self):
        now = self.now()
        grace = self.config.get("reminder_grace_minutes", 30) * 60
        count = 0
        for minutes in self.config.get("reminder_minutes", [1440, 120]):
            seconds = minutes * 60
            candidates = self.db.bookings.find({
                "status": "confirmed", "start": {"$gte": max(now + 1, now + seconds - grace),
                                                "$lte": now + seconds},
                "$expr": {"$lte": ["$created", {"$subtract": ["$start", seconds]}]}}).limit(200)
            for b in candidates:
                key = f"reminder:{b['id']}:{minutes}"
                duration = (f"{persian_digits(minutes // 60)} ساعت" if minutes % 60 == 0
                            else f"{persian_digits(minutes)} دقیقه")
                text = self.booking_text(b, f"یادآوری نوبت ({duration} قبل)")
                text += "\nبرای مشاهده یا لغو، از /start و «نوبت‌های من» استفاده کن."
                self.queue_message(b["customer_id"], text, key=key, kind="reminder",
                                   booking_id=b["id"], minutes_before=minutes,
                                   expires=min(b["start"], b["start"] - seconds + grace + 1))
                count += 1
        return count

    def notification_stats(self):
        q = {"sent": None, "cancelled": None}
        return (self.db.outbox.count_documents(q, **self.kwargs()),
                self.db.outbox.count_documents({**q, "attempts": {"$gt": 0}}, **self.kwargs()))

    def process_update(self, update, bot_factory):
        """Serialize each user's session; deduplicate Telegram retries atomically."""
        update_id = update.get("update_id")
        if type(update_id) is not int:
            raise ValueError("Invalid Telegram update")
        message = update.get("callback_query") or update.get("message") or {}
        uid = message.get("from", {}).get("id")
        if type(uid) is not int:
            return "ignored"
        # Ensure session document exists outside transaction, with atomic setOnInsert.
        self.db.sessions.update_one({"_id": uid}, {"$setOnInsert": {"data": {}}}, upsert=True)
        def work():
            self.db.sessions.update_one({"_id": uid}, {"$inc": {"revision": 1}}, **self.kwargs())
            if self.db.updates.find_one({"_id": update_id}, **self.kwargs()):
                return "duplicate"
            bot_factory(self).update(update)
            self.db.updates.insert_one({"_id": update_id, "at": datetime.now(timezone.utc)},
                                       **self.kwargs())
            return "processed"
        return self.transaction(work)
