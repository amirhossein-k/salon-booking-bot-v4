import json
import time
import uuid

from pymongo import ReturnDocument

from bot import Bot, Telegram, TelegramError
from core import BookingError
from dashboard import Analytics


class QueuedTelegram:
    """UI responses are stored in the same transaction as the booking."""
    def __init__(self, store):
        self.store = store

    def send(self, uid, text, rows=None, reply_markup=None):
        if rows is not None:
            reply_markup = {"inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row] for row in rows]}
        self.store.queue_message(uid, text, reply_markup)
        return {"queued": True}

    def call(self, method, **data):
        if method == "answerCallbackQuery":
            return True
        raise RuntimeError("Only queued messages are supported in transactions")


class WebhookBot(Bot):
    def callback(self, uid, data):
        if data == "outbox":
            if uid not in self.db.config["admin_ids"]:
                raise BookingError("فقط مدیر دسترسی دارد.")
            pending, failed = self.db.notification_stats()
            missing = [s["name"] for s in self.db.staff.values() if not s.get("telegram_id")]
            self.send(uid, f"اعلان ارسال‌نشده: {pending}\nبا سابقهٔ خطا: {failed}\n"
                      "کارکنان بدون شناسه: " + ("، ".join(missing) or "هیچ‌کس"))
            return
        return super().callback(uid, data)


def bot_factory(store):
    return WebhookBot(store, QueuedTelegram(store))


def deliver(store, telegram, seconds=12, limit=20):
    """Short bounded worker with per-message leases; at-least-once delivery."""
    deadline, sent = time.monotonic() + seconds, 0
    for _ in range(limit):
        if time.monotonic() > deadline:
            break
        now, token = store.now(), uuid.uuid4().hex
        row = store.db.outbox.find_one_and_update(
            {"sent": None, "cancelled": None, "next_try": {"$lte": now},
             "lease_until": {"$lte": now}},
            {"$set": {"lease_until": now + 90, "lease_token": token}},
            sort=[("created", 1), ("_id", 1)], return_document=ReturnDocument.AFTER)
        if not row:
            break
        selector = {"_id": row["_id"], "lease_token": token}
        if row.get("kind") == "reminder":
            b = store.db.bookings.find_one({"_id": row["booking_id"]})
            if (not b or b["status"] != "confirmed" or b["start"] <= now
                    or row.get("expires", 0) <= now
                    or row["minutes_before"] not in store.config.get("reminder_minutes", [1440, 120])):
                store.db.outbox.update_one(selector, {"$set": {"cancelled": now, "lease_until": 0}})
                continue
        try:
            telegram.send(row["chat_id"], row["text"], reply_markup=row.get("markup"))
        except TelegramError as error:
            delay = max(error.retry_after, min(3600, 15 * 2 ** min(row["attempts"], 8)))
            store.db.outbox.update_one(selector, {"$inc": {"attempts": 1},
                "$set": {"next_try": now + delay, "lease_until": 0,
                         "last_error": f"Telegram code {error.code}"}})
        else:
            store.db.outbox.update_one(selector, {"$set": {"sent": store.now(), "lease_until": 0}})
            sent += 1
        time.sleep(0.04)
    return sent


class MongoAnalytics(Analytics):
    def __init__(self, store):
        self.store = store

    def source_rows(self, start, end):
        query = {"start": {"$lt": end}, "end": {"$gt": start}}
        bookings = list(self.store.db.bookings.find(query).sort("start", -1).limit(20001))
        if len(bookings) > 20000:
            raise ValueError("داده‌های بازه زیاد است؛ بازهٔ کوتاه‌تری انتخاب کن.")
        blocks = list(self.store.db.blocks.find(query).limit(20001))
        if len(blocks) > 20000:
            raise ValueError("بازهٔ کوتاه‌تری انتخاب کن.")
        for b in bookings:
            for field in ("amount_toman", "payment_note", "payment_updated"):
                b.setdefault(field, None)
        return bookings, blocks

    def set_payment(self, actor, bid, amount, note):
        s = self.store
        if actor not in s.config["admin_ids"]:
            raise BookingError("فقط مدیر مجاز به ثبت مبلغ است.")
        if type(amount) is not int or not 0 <= amount <= 10**12:
            raise BookingError("مبلغ باید عدد صحیح و غیرمنفی به تومان باشد.")
        if not isinstance(note, str) or not 1 <= len(note.strip()) <= 400:
            raise BookingError("توضیح ثبت یا اصلاح مبلغ الزامی است.")
        def work():
            b = s.get_booking(actor, bid)
            if b["status"] != "completed":
                raise BookingError("مبلغ فقط برای نوبت انجام‌شده قابل ثبت است.")
            s.db.bookings.update_one({"_id": bid}, {"$set": {
                "amount_toman": amount, "payment_note": note.strip(),
                "payment_updated": s.now(), "payment_actor": actor}}, **s.kwargs())
            s.audit_event(actor, "reconcile_payment", bid, json.dumps({
                "old": b.get("amount_toman"), "new": amount, "note": note.strip()}, ensure_ascii=False))
        return s.transaction(work)
