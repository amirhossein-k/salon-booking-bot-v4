"""Telegram long-polling UI. Never put a bot token in this file."""
import json
import logging
import os
import re
import socket
import time as sleep_time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from core import Store, BookingError
from jalali import numeric, label, persian_digits

LOG = logging.getLogger("salon")


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=0):
        self.code, self.retry_after = code, retry_after
        super().__init__(description)


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **data):
        req = urllib.request.Request(self.base + method,
                                     json.dumps(data).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                result = json.loads(error.read())
            except (ValueError, UnicodeDecodeError):
                raise TelegramError(error.code, "Telegram HTTP request failed") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            # Do not log exception URLs: those contain the secret token.
            raise TelegramError(0, "Telegram connection failed") from None
        if not result.get("ok"):
            raise TelegramError(result.get("error_code", 0), result.get("description", "API error"),
                                result.get("parameters", {}).get("retry_after", 0))
        return result["result"]

    def send(self, uid, text, rows=None, reply_markup=None):
        if rows is not None:
            reply_markup = {"inline_keyboard": [
                [{"text": label, "callback_data": value} for label, value in row] for row in rows]}
        data = {"chat_id": uid, "text": text}
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        return self.call("sendMessage", **data)


class Bot:
    def __init__(self, store, tg):
        self.db, self.tg = store, tg

    def send(self, uid, text, rows=None):
        return self.tg.send(uid, text, rows)

    def home(self, uid):
        self.db.session(uid, {"step": "home"})
        rows = [[("رزرو نوبت", "services"), ("نوبت‌های من", "mine")]]
        if self.db.manageable(uid):
            rows += [[("پنل آرایشگر / مدیر", "manage")]]
        rows += [[("شناسهٔ تلگرام من", "identity")]]
        self.tg.send(uid, f"به {self.db.config['salon_name']} خوش آمدی.\n"
                     "تاریخ‌ها شمسی و ساعت‌ها به وقت تهران هستند.\n"
                     "نام و تلفن برای مدیریت نوبت در اختیار آرایشگر و مدیر سالن قرار می‌گیرد.",
                     rows, reply_markup=None)

    def day_picker(self, uid, prefix, title, count=None):
        count = count or self.db.config.get("booking_days", 21)
        rows = []
        for offset in range(count):
            day = self.db.today() + timedelta(days=offset)
            if offset % 2 == 0:
                rows.append([])
            day_label = label(day)
            rows[-1].append((day_label, f"{prefix}|{day.isoformat()}"))
        rows.append([("منوی اصلی", "home")])
        self.send(uid, title, rows)

    def show_slots(self, uid, sid, service_id, day):
        values = self.db.slots(sid, service_id, day)
        self.db.session(uid, {"step": "slot", "staff": sid, "service": service_id, "day": day.isoformat()})
        rows = []
        for i, value in enumerate(values):
            if i % 4 == 0:
                rows.append([])
            time_label = persian_digits(datetime.fromtimestamp(value, self.db.tz).strftime("%H:%M"))
            rows[-1].append((time_label, f"slot|{value}"))
        rows += [[("روز دیگر", f"days|{sid}|{service_id}"), ("منوی اصلی", "home")]]
        self.send(uid, ("ساعت شروع را انتخاب کن. فقط بازه‌های کاملاً آزاد نمایش داده می‌شوند."
                        if values else "برای این روز نوبت آزاد وجود ندارد."), rows)

    def show_booking(self, uid, bid):
        b = self.db.get_booking(uid, bid)
        rows = []
        if b["status"] == "confirmed":
            if self.db.can_manage(uid, b["staff_id"]):
                rows = [[("لغو توسط سالن", f"status|{bid}|cancelled_staff")],
                        [("مشتری لغو کرده", f"status|{bid}|cancelled_customer")],
                        [("عدم حضور مشتری", f"status|{bid}|no_show"), ("انجام شد", f"status|{bid}|completed")]]
            else:
                rows = [[("لغو نوبت من", f"status|{bid}|cancelled_customer")]]
        rows.append([("منوی اصلی", "home")])
        self.send(uid, self.db.booking_text(b), rows)

    def agenda(self, uid, sid, day):
        self.db.require_manage(uid, sid)
        bookings = self.db.bookings(uid, sid, day)
        self.send(uid, f"برنامهٔ {self.db.staff[sid]['name']} در {numeric(day)}\n"
                  "همهٔ وضعیت‌ها، از جمله نوبت‌های لغوشده، نمایش داده می‌شوند.")
        if not bookings:
            self.send(uid, "نوبتی ثبت نشده.")
        for b in reversed(bookings):
            self.send(uid, self.db.booking_text(b), [[("جزئیات / تعیین وضعیت", f"view|{b['id']}")]])
        rows = [[(s["name"], f"free|{sid}|{s['id']}|{day.isoformat()}")]
                for s in self.db.services.values() if s["id"] in self.db.staff[sid]["services"]]
        rows += [[("مسدودکردن زمان این روز", f"blockday|{sid}|{day.isoformat()}")],
                 [("روز دیگر", f"agenda_days|{sid}"), ("منوی اصلی", "home")]]
        self.send(uid, "برای دیدن ساعت‌های آزاد، خدمت را انتخاب کن:", rows)

    def normalize_phone(self, text):
        text = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
        text = re.sub(r"[\s()\-]", "", text)
        if text.startswith("00"):
            text = "+" + text[2:]
        if not re.fullmatch(r"\+?[0-9]{8,15}", text):
            raise BookingError("شماره تلفن معتبر وارد کن؛ مثلاً 09123456789.")
        return text

    def callback(self, uid, data):
        parts = data.split("|")
        action = parts[0]
        state = self.db.session(uid)
        if action == "home":
            self.home(uid)
        elif action == "identity":
            self.send(uid, f"شناسهٔ عددی تلگرام شما: {uid}")
        elif action == "services":
            self.db.session(uid, {"step": "services"})
            self.send(uid, "خدمت را انتخاب کن:", [[(s["name"] + f" ({s['duration_minutes']} دقیقه)",
                                                    f"service|{s['id']}")] for s in self.db.services.values()])
        elif action == "service":
            service = parts[1]
            if service not in self.db.services:
                raise BookingError("خدمت معتبر نیست.")
            self.send(uid, "آرایشگر را انتخاب کن:", [[(s["name"], f"days|{s['id']}|{service}")]
                      for s in self.db.staff.values() if service in s["services"]])
        elif action == "days":
            sid, service = parts[1:3]
            if sid not in self.db.staff or service not in self.db.staff[sid]["services"]:
                raise BookingError("آرایشگر یا خدمت نامعتبر است.")
            self.day_picker(uid, f"day|{sid}|{service}", "روز نوبت را انتخاب کن:")
        elif action == "day":
            self.show_slots(uid, parts[1], parts[2], date.fromisoformat(parts[3]))
        elif action == "slot":
            if state.get("step") != "slot":
                raise BookingError("این دکمه قدیمی است؛ دوباره رزرو را شروع کن.")
            start = int(parts[1])
            if start not in self.db.slots(state["staff"], state["service"], date.fromisoformat(state["day"])):
                raise BookingError("این ساعت دیگر آزاد نیست؛ روز و ساعت را دوباره انتخاب کن.")
            state.update(step="name", start=start)
            self.db.session(uid, state)
            self.send(uid, "نام و نام خانوادگی مشتری را بنویس.")
        elif action == "confirm_booking":
            if state.get("step") != "confirm_booking":
                raise BookingError("این درخواست قبلاً ثبت شده یا منقضی است.")
            bid = self.db.book(uid, state["staff"], state["service"], state["start"], state["name"], state["phone"])
            self.db.session(uid, {"step": "home"})
            self.send(uid, f"نوبت #{bid} قطعی شد. جزئیات از بخش نوبت‌های من قابل مشاهده است.",
                      [[("نوبت‌های من", "mine"), ("منوی اصلی", "home")]])
        elif action == "mine":
            bookings = self.db.bookings(uid)
            if not bookings:
                self.send(uid, "هنوز نوبتی نداری.", [[("رزرو نوبت", "services")]])
            else:
                self.send(uid, "آخرین نوبت‌های شما (حداکثر ۱۰۰ مورد):",
                          [[(f"#{b['id']} | {self.db.fmt(b['start'])}", f"view|{b['id']}")] for b in bookings[:50]]
                          + [[("منوی اصلی", "home")]])
        elif action == "view":
            self.show_booking(uid, int(parts[1]))
        elif action == "status":
            bid, status = int(parts[1]), parts[2]
            b = self.db.get_booking(uid, bid)
            if not self.db.can_manage(uid, b["staff_id"]) and status != "cancelled_customer":
                raise BookingError("اجازهٔ این تغییر را نداری.")
            if b["status"] != "confirmed":
                raise BookingError("این نوبت قبلاً تعیین تکلیف شده.")
            self.db.session(uid, {"step": "reason", "bid": bid, "status": status})
            self.send(uid, "دلیل یا توضیح را بنویس؛ در مرحلهٔ بعد تأیید نهایی می‌گیری.\n"
                      "برای لغو توسط سالن، اول با مشتری هماهنگ کن؛ ربات تماس تلفنی نمی‌گیرد.")
        elif action == "confirm_status":
            if state.get("step") != "confirm_status":
                raise BookingError("درخواست قدیمی است.")
            self.db.change_status(uid, state["bid"], state["status"], state["reason"])
            self.db.session(uid, {"step": "home"})
            self.send(uid, "وضعیت و دلیل ثبت شد؛ اعلان‌ها در صف ارسال قرار گرفتند.",
                      [[("جزئیات نوبت", f"view|{state['bid']}"), ("منوی اصلی", "home")]])
        elif action == "manage":
            rows = [[(s["name"], f"staff|{s['id']}")] for s in self.db.manageable(uid)]
            if not rows:
                raise BookingError("حساب شما به عنوان آرایشگر تعریف نشده.")
            if uid in self.db.config["admin_ids"]:
                rows.append([("وضعیت اعلان‌ها", "outbox")])
            self.send(uid, "پنل کارکنان؛ هر آرایشگر فقط برنامهٔ خودش را مدیریت می‌کند:", rows)
        elif action == "staff":
            sid = parts[1]
            self.db.require_manage(uid, sid)
            self.send(uid, self.db.staff[sid]["name"], [
                [("نوبت‌ها و ساعت‌های آزاد", f"agenda_days|{sid}")],
                [("مسدودکردن روز یا ساعت", f"block_days|{sid}")],
                [("بازه‌های مسدود / بازکردن", f"blocks|{sid}")],
                [("ساعات کاری هفتگی", f"hours|{sid}")], [("منوی اصلی", "home")]])
        elif action in ("agenda_days", "block_days"):
            sid = parts[1]
            self.db.require_manage(uid, sid)
            prefix = "agenda" if action == "agenda_days" else "blockday"
            self.day_picker(uid, f"{prefix}|{sid}", "روز را انتخاب کن:", count=31)
        elif action == "agenda":
            self.agenda(uid, parts[1], date.fromisoformat(parts[2]))
        elif action == "free":
            sid, service, day = parts[1], parts[2], date.fromisoformat(parts[3])
            self.db.require_manage(uid, sid)
            slots = self.db.slots(sid, service, day)
            self.send(uid, "ساعت‌های آزاد برای " + self.db.services[service]["name"] + ":\n" +
                      ("، ".join(persian_digits(datetime.fromtimestamp(x, self.db.tz).strftime("%H:%M")) for x in slots)
                       if slots else "ساعت آزاد قابل رزرو وجود ندارد.") +
                      "\nساعت‌های خارج از افق رزرو یا حداقل فاصلهٔ رزرو نمایش داده نمی‌شوند.",
                      [[("برنامهٔ روز", f"agenda|{sid}|{day.isoformat()}")]])
        elif action == "blockday":
            sid, day = parts[1], date.fromisoformat(parts[2])
            self.db.require_manage(uid, sid)
            self.db.session(uid, {"step": "block_input", "staff": sid, "day": day.isoformat()})
            self.send(uid, "برای بستن کل روز بنویس: تمام‌روز دلیل\n"
                      "برای بازهٔ خاص بنویس: 13:00 15:30 دلیل\n"
                      "نمونه: 13:00 15:30 کار شخصی\n"
                      "نوبت‌های قبلی خودکار لغو نمی‌شوند.")
        elif action == "confirm_block":
            if state.get("step") != "confirm_block":
                raise BookingError("درخواست قدیمی است.")
            self.db.block(uid, state["staff"], state["start"], state["end"], state["reason"])
            self.db.session(uid, {"step": "home"})
            self.send(uid, "زمان مسدود شد؛ مشتری نمی‌تواند در آن بازه نوبت بگیرد.",
                      [[("بازه‌های مسدود", f"blocks|{state['staff']}"), ("منوی اصلی", "home")]])
        elif action == "blocks":
            sid = parts[1]
            blocks = self.db.blocks(uid, sid)
            if not blocks:
                self.send(uid, "بازهٔ مسدودی نداری.", [[("پنل آرایشگر", f"staff|{sid}")]])
            for block in blocks:
                self.send(uid, f"از {self.db.fmt(block['start'])}\nتا {self.db.fmt(block['end'])}\n"
                          f"دلیل: {block['reason']}", [[("بازکردن این بازه", f"unblock|{block['id']}|{sid}")]])
        elif action == "unblock":
            block_id, sid = int(parts[1]), parts[2]
            if not any(b["id"] == block_id for b in self.db.blocks(uid, sid)):
                raise BookingError("بازه پیدا نشد.")
            self.db.session(uid, {"step": "confirm_unblock", "block_id": block_id})
            self.send(uid, "این محدودیت حذف شود و بازه دوباره قابل رزرو باشد؟",
                      [[("بله، باز شود", "confirm_unblock"), ("انصراف", "home")]])
        elif action == "confirm_unblock":
            if state.get("step") != "confirm_unblock":
                raise BookingError("درخواست قدیمی است.")
            self.db.unblock(uid, state["block_id"])
            self.db.session(uid, {"step": "home"})
            self.send(uid, "بازه باز شد.", [[("پنل", "manage")]])
        elif action == "hours":
            sid = parts[1]
            self.db.require_manage(uid, sid)
            weekdays = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]
            lines = [weekdays[i] + ": " + ("، ".join(f"{a} تا {b}" for a, b in
                     self.db.staff[sid]["weekly_hours"].get(str(i), [])) or "تعطیل") for i in range(7)]
            self.send(uid, "\n".join(lines) + "\nتغییر برنامهٔ هفتگی در config.json و با راه‌اندازی مجدد انجام می‌شود؛ "
                      "برای تعطیلی موردی از مسدودکردن زمان استفاده کن.")
        elif action == "outbox":
            if uid not in self.db.config["admin_ids"]:
                raise BookingError("فقط مدیر دسترسی دارد.")
            with self.db.connect() as c:
                pending = c.execute("SELECT COUNT(*) FROM outbox WHERE sent IS NULL AND cancelled IS NULL").fetchone()[0]
                failed = c.execute("SELECT COUNT(*) FROM outbox WHERE sent IS NULL AND cancelled IS NULL AND attempts>0").fetchone()[0]
            missing = [s["name"] for s in self.db.staff.values() if not s.get("telegram_id")]
            self.send(uid, f"اعلان ارسال‌نشده: {pending}\nبا سابقهٔ خطا: {failed}\n"
                      "آرایشگران بدون شناسهٔ تلگرام: " + ("، ".join(missing) or "هیچ‌کس") +
                      "\nکارکنان باید ربات را Start کنند و شناسه‌شان در تنظیمات ثبت شود.")
        else:
            raise BookingError("دکمه نامعتبر است؛ از /start استفاده کن.")

    def message(self, uid, msg):
        text = msg.get("text", "").strip()
        if text in ("/start", "/cancel", "/menu") or text.startswith("/start "):
            self.home(uid)
            return
        if text == "/id":
            self.send(uid, f"شناسهٔ عددی تلگرام شما: {uid}")
            return
        state = self.db.session(uid)
        step = state.get("step")
        if step == "name":
            if not 2 <= len(text) <= 100:
                raise BookingError("نام و نام خانوادگی را بین ۲ تا ۱۰۰ نویسه وارد کن.")
            state.update(name=text, step="phone")
            self.db.session(uid, state)
            self.tg.send(uid, "شماره تماس مشتری را بنویس یا شمارهٔ خودت را با دکمهٔ زیر ارسال کن.",
                         reply_markup={"keyboard": [[{"text": "ارسال شمارهٔ خودم", "request_contact": True}]],
                                       "resize_keyboard": True, "one_time_keyboard": True})
        elif step == "phone":
            contact = msg.get("contact")
            if contact and contact.get("user_id") != uid:
                raise BookingError("شمارهٔ خودت را با دکمه بفرست یا شمارهٔ مشتری را تایپ کن.")
            phone = self.normalize_phone(contact["phone_number"] if contact else text)
            state.update(phone=phone, step="confirm_booking")
            self.db.session(uid, state)
            self.tg.send(uid, "شماره ثبت شد.", reply_markup={"remove_keyboard": True})
            self.send(uid, f"خدمت: {self.db.services[state['service']]['name']}\n"
                      f"آرایشگر: {self.db.staff[state['staff']]['name']}\n"
                      f"زمان: {self.db.fmt(state['start'])}\nنام: {state['name']}\nتلفن: {phone}\n"
                      "تا زمان تأیید، این ساعت برای شما نگه داشته نمی‌شود.",
                      [[("تأیید و ثبت نوبت", "confirm_booking"), ("انصراف", "home")]])
        elif step == "reason":
            if not 1 <= len(text) <= 400:
                raise BookingError("توضیح را بین ۱ تا ۴۰۰ نویسه وارد کن.")
            state.update(reason=text, step="confirm_status")
            self.db.session(uid, state)
            labels = {"cancelled_staff": "لغو توسط سالن", "cancelled_customer": "لغو توسط مشتری",
                      "no_show": "عدم حضور", "completed": "انجام شد"}
            self.send(uid, f"نوبت #{state['bid']}\nوضعیت: {labels[state['status']]}\nتوضیح: {text}\n"
                      "تغییر ثبت و به مشتری، آرایشگر و مدیر اطلاع داده شود؟",
                      [[("تأیید تغییر وضعیت", "confirm_status"), ("انصراف", "home")]])
        elif step == "block_input":
            normalized = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
            day = date.fromisoformat(state["day"])
            if normalized.startswith(("تمام‌روز ", "تمام روز ")):
                reason = normalized.replace("تمام‌روز ", "", 1).replace("تمام روز ", "", 1).strip()
                start = self.db.stamp(day, "00:00")
                end = self.db.stamp(day + timedelta(days=1), "00:00")
            else:
                fields = normalized.split(maxsplit=2)
                if len(fields) != 3 or not all(re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", t) for t in fields[:2]):
                    raise BookingError("قالب درست: 13:00 15:30 دلیل")
                start, end = self.db.stamp(day, fields[0]), self.db.stamp(day, fields[1])
                reason = fields[2].strip()
            if end <= start or not 1 <= len(reason) <= 200:
                raise BookingError("بازه یا دلیل نامعتبر است.")
            state.update(step="confirm_block", start=start, end=end, reason=reason)
            self.db.session(uid, state)
            self.send(uid, f"بستن بازه از {self.db.fmt(start)} تا {self.db.fmt(end)}\nدلیل: {reason}",
                      [[("تأیید مسدودکردن", "confirm_block"), ("انصراف", "home")]])
        else:
            self.send(uid, "از دکمه‌ها استفاده کن یا /start را بفرست.",
                      [[("منوی اصلی", "home")]])

    def update(self, update):
        callback = update.get("callback_query")
        msg = callback.get("message") if callback else update.get("message")
        if not msg:
            return
        user = callback.get("from") if callback else msg.get("from")
        if not user or user.get("is_bot"):
            return
        uid = user["id"]
        if msg.get("chat", {}).get("type") != "private":
            if callback:
                self.tg.call("answerCallbackQuery", callback_query_id=callback["id"],
                             text="برای حفظ حریم خصوصی، در گفتگوی خصوصی با ربات کار کن.")
            return
        if callback:
            self.tg.call("answerCallbackQuery", callback_query_id=callback["id"])
        try:
            if callback:
                self.callback(uid, callback.get("data", ""))
            else:
                self.message(uid, msg)
        except (BookingError, ValueError, KeyError, IndexError) as error:
            text = str(error) if isinstance(error, BookingError) else "ورودی معتبر نیست؛ دوباره انتخاب کن."
            self.send(uid, text, [[("منوی اصلی", "home")]])

    def flush_outbox(self):
        with self.db.connect() as c:
            rows = c.execute("""SELECT * FROM outbox WHERE sent IS NULL AND cancelled IS NULL
                             AND next_try<=? ORDER BY id LIMIT 15""",
                             (self.db.now(),)).fetchall()
        for row in rows:
            # Refresh reminder validity just before transmission.
            # An already-delivered reminder cannot be recalled after later cancellation.
            with self.db.connect() as c:
                current = c.execute("""SELECT o.cancelled,r.minutes_before,b.status,b.start
                    FROM outbox o LEFT JOIN reminders r ON r.outbox_id=o.id
                    LEFT JOIN bookings b ON b.id=r.booking_id WHERE o.id=?""", (row["id"],)).fetchone()
                if current["cancelled"] is not None:
                    continue
                if current["minutes_before"] is not None:
                    now = self.db.now()
                    due = current["start"] - current["minutes_before"] * 60
                    if (current["status"] != "confirmed" or current["start"] <= now
                            or now > due + self.db.config.get("reminder_grace_minutes", 30) * 60
                            or current["minutes_before"] not in self.db.config.get("reminder_minutes", [1440, 120])):
                        c.execute("UPDATE outbox SET cancelled=? WHERE id=?", (now, row["id"]))
                        continue
            try:
                self.tg.send(row["chat_id"], row["text"])
            except TelegramError as error:
                delay = max(error.retry_after, min(3600, 15 * 2 ** min(row["attempts"], 8)))
                with self.db.connect() as c:
                    c.execute("UPDATE outbox SET attempts=attempts+1,next_try=?,last_error=? WHERE id=?",
                              (self.db.now() + delay, f"Telegram error code {error.code}", row["id"]))
            else:
                with self.db.connect() as c:
                    c.execute("UPDATE outbox SET sent=?,last_error=NULL WHERE id=?", (self.db.now(), row["id"]))
            sleep_time.sleep(0.05)

    def run(self):
        identity = self.tg.call("getMe")
        webhook = self.tg.call("getWebhookInfo")
        if webhook.get("url"):
            raise RuntimeError("Webhook is active. Stop the old bot and remove its webhook before polling.")
        LOG.info("Bot @%s ready", identity["username"])
        offset = int(self.db.meta("update_offset") or 0)
        while True:
            try:
                self.db.schedule_reminders()
                self.flush_outbox()
                updates = self.tg.call("getUpdates", offset=offset, timeout=20,
                                       allowed_updates=["message", "callback_query"])
                for update in updates:
                    try:
                        self.update(update)
                    except TelegramError as error:
                        if error.code == 0 or error.code == 429 or error.code >= 500:
                            raise
                        LOG.warning("Reply failed: Telegram code %s", error.code)
                    offset = update["update_id"] + 1
                    self.db.meta("update_offset", offset)
            except TelegramError as error:
                LOG.warning("Telegram request failed: code %s", error.code)
                sleep_time.sleep(max(5, error.retry_after))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise SystemExit("Set BOT_TOKEN in the environment. Never paste it into source files.")
    config_path = os.environ.get("CONFIG_PATH", "config.json")
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if config.get("slot_step_minutes", 15) < 5 or not 1 <= config.get("booking_days", 21) <= 31:
        raise SystemExit("Invalid slot step or booking horizon")
    db_path = os.environ.get("DB_PATH", "data/salon.sqlite3")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    StoreInstance = Store(db_path, config)
    for staff in StoreInstance.staff.values():
        if not staff.get("telegram_id"):
            LOG.warning("Staff %s has no Telegram ID; staff notification is not enabled.", staff["id"])
    Bot(StoreInstance, Telegram(token)).run()


if __name__ == "__main__":
    main()
