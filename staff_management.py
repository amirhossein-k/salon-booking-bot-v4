"""Manager-only employee settings and calendar operations for Vercel/MongoDB.

Routes use the existing dashboard authentication and CSRF scheme. Settings are
stored as one document so one Telegram identity cannot be assigned concurrently
to different employees. Calendar guards serialize changes with bookings.
"""
import copy
from datetime import timedelta


def validate_staff_config(config):
    """Validate assignments and schedules without external dependencies."""
    staff = config["staff"]
    known = {service["id"] for service in config["services"]}
    identities = set()
    for item in staff:
        if not isinstance(item.get("name"), str) or not 1 <= len(item["name"].strip()) <= 100:
            raise ValueError("نام آرایشگر باید بین ۱ تا ۱۰۰ نویسه باشد.")
        uid = item.get("telegram_id")
        if uid is not None:
            if type(uid) is not int or not 0 < uid < 2**52:
                raise ValueError("شناسه تلگرام باید عدد صحیح مثبت باشد.")
            if uid in identities:
                raise ValueError("یک شناسه تلگرام نمی‌تواند برای دو آرایشگر ثبت شود.")
            if uid in config["admin_ids"]:
                raise ValueError("شناسه مدیر برای کارکنان ثبت نشود؛ مدیر دسترسی همه را دارد.")
            identities.add(uid)
        if type(item.get("panel_enabled", True)) is not bool:
            raise ValueError("وضعیت دسترسی نامعتبر است.")
        services = item.get("services")
        if (not isinstance(services, list) or not services
                or not all(isinstance(s, str) and s in known for s in services)
                or len(set(services)) != len(services)):
            raise ValueError("حداقل یک خدمت معتبر و بدون تکرار انتخاب کن.")
        hours = item.get("weekly_hours")
        if not isinstance(hours, dict) or any(k not in {str(i) for i in range(7)} for k in hours):
            raise ValueError("برنامه هفتگی نامعتبر است.")
        for intervals in hours.values():
            if not isinstance(intervals, list) or len(intervals) > 8:
                raise ValueError("حداکثر هشت بازه برای هر روز مجاز است.")
            parsed = []
            for interval in intervals:
                if not isinstance(interval, list) or len(interval) != 2:
                    raise ValueError("هر بازه باید ساعت شروع و پایان داشته باشد.")
                values = []
                for value in interval:
                    if (not isinstance(value, str) or len(value) != 5
                            or value[2] != ":" or not (value[:2] + value[3:]).isascii()
                            or not (value[:2] + value[3:]).isdigit()):
                        raise ValueError("ساعت را به صورت HH:MM وارد کن.")
                    h, m = map(int, value.split(":"))
                    if not 0 <= h < 24 or not 0 <= m < 60:
                        raise ValueError("ساعت معتبر نیست.")
                    values.append(h * 60 + m)
                if values[0] >= values[1]:
                    raise ValueError("شروع بازه باید قبل از پایان باشد.")
                parsed.append(values)
            parsed.sort()
            if any(left[1] > right[0] for left, right in zip(parsed, parsed[1:])):
                raise ValueError("بازه‌های ساعات کاری نباید همپوشانی داشته باشند.")


def effective_config(base, document):
    config = copy.deepcopy(base)
    if document:
        overrides = document.get("items", {})
        for item in config["staff"]:
            if item["id"] in overrides:
                for field in ("name", "telegram_id", "panel_enabled", "services", "weekly_hours"):
                    if field in overrides[item["id"]]:
                        item[field] = copy.deepcopy(overrides[item["id"]][field])
    validate_staff_config(config)
    return config


def update_staff(store, actor, sid, changes):
    from core import BookingError
    if actor not in store.config["admin_ids"]:
        raise BookingError("فقط مدیر مجاز به تغییر کارکنان است.")
    allowed = {"name", "telegram_id", "panel_enabled", "services", "weekly_hours"}
    if not changes or set(changes) - allowed:
        raise ValueError("فیلد نامعتبر است.")
    if sid not in store.staff:
        raise ValueError("آرایشگر پیدا نشد.")

    def work():
        # Shared document serializes all assignments; guard serializes calendars.
        document = store.db.staff_settings.find_one({"_id": "staff"}, **store.kwargs())
        config = effective_config(store.base_config, document)
        current = next(item for item in config["staff"] if item["id"] == sid)
        before = copy.deepcopy(current)
        current.update(copy.deepcopy(changes))
        current["name"] = current["name"].strip() if isinstance(current.get("name"), str) else current.get("name")
        validate_staff_config(config)
        store.guard(sid)
        items = copy.deepcopy((document or {}).get("items", {}))
        items[sid] = {key: current.get(key, True if key == "panel_enabled" else None) for key in allowed}
        store.db.staff_settings.replace_one(
            {"_id": "staff"}, {"_id": "staff", "items": items,
                              "revision": (document or {}).get("revision", 0) + 1,
                              "updated": store.now(), "actor": actor},
            upsert=True, **store.kwargs())
        store.audit_event(actor, "update_staff", None, {"staff_id": sid,
                          "before": before, "after": copy.deepcopy(current)})
        return {"ok": True}
    return store.transaction(work)


def install_routes(app, new_store, authorized, deny, csrf_token, payload, env, root):
    from fastapi import Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from starlette.concurrency import run_in_threadpool
    from core import BookingError
    from dashboard import solar_date

    def failure(error):
        return JSONResponse({"error": str(error)}, status_code=400)

    def actor_for(store):
        actor = int(env("DASHBOARD_ADMIN_ID"))
        if actor not in store.config["admin_ids"]:
            raise BookingError("شناسه مدیر داشبورد در تنظیمات مدیران وجود ندارد.")
        return actor

    async def body(request):
        if not authorized(request):
            return None, deny()
        import hmac
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), csrf_token()):
            return None, JSONResponse({"error": "درخواست معتبر نیست؛ صفحه را تازه کن."}, status_code=403)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return None, JSONResponse({"error": "نوع درخواست نامعتبر است."}, status_code=415)
        return await payload(request, 16384), None

    @app.get("/staff")
    def staff_page(request: Request):
        if not authorized(request):
            return deny()
        return HTMLResponse((root / "staff-management.html").read_text(encoding="utf-8"))

    @app.get("/api/staff")
    def get_staff(request: Request):
        if not authorized(request):
            return deny()
        try:
            store = new_store()
            actor_for(store)
            return {"staff": list(store.staff.values()), "services": list(store.services.values()),
                    "csrf": csrf_token(), "timezone": str(store.tz)}
        except (BookingError, ValueError) as error:
            return failure(error)

    @app.put("/api/staff/{sid}")
    async def save_staff(sid: str, request: Request):
        try:
            data, denied = await body(request)
            if denied is not None:
                return denied
            def save():
                store = new_store()
                return update_staff(store, actor_for(store), sid, data)
            return await run_in_threadpool(save)
        except (BookingError, ValueError, TypeError) as error:
            return failure(error)

    @app.get("/api/staff/{sid}/calendar")
    def calendar(sid: str, request: Request):
        if not authorized(request):
            return deny()
        try:
            store = new_store()
            actor = actor_for(store)
            day = solar_date(request.query_params["day"])
            store.require_manage(actor, sid)
            bookings = store.bookings(actor, sid, day)
            ds = store.stamp(day, "00:00")
            de = store.stamp(day + timedelta(days=1), "00:00")
            blocks = list(store.db.blocks.find({"staff_id": sid,
                          "start": {"$lt": de}, "end": {"$gt": ds}}).sort("start", 1).limit(101))
            if len(blocks) > 100:
                raise ValueError("تعداد بازه‌ها زیاد است.")
            def visible(row):
                row = dict(row)
                row.pop("_id", None)
                row["start_label"] = store.fmt(row["start"])
                row["end_label"] = store.fmt(row["end"])
                return row
            return {"bookings": [visible(b) for b in bookings],
                    "blocks": [visible(b) for b in blocks]}
        except (BookingError, ValueError, KeyError) as error:
            return failure(error)

    @app.post("/api/staff/{sid}/calendar")
    async def change_calendar(sid: str, request: Request):
        try:
            data, denied = await body(request)
            if denied is not None:
                return denied
            def change():
                store = new_store()
                actor = actor_for(store)
                store.require_manage(actor, sid)
                action = data.get("action")
                if action == "status":
                    bid = data.get("booking_id")
                    if type(bid) is not int:
                        raise ValueError("شناسه نوبت نامعتبر است.")
                    booking = store.get_booking(actor, bid)
                    if booking["staff_id"] != sid:
                        raise BookingError("این نوبت متعلق به آرایشگر انتخاب‌شده نیست.")
                    reason = data.get("reason")
                    if not isinstance(reason, str):
                        raise ValueError("توضیح الزامی است.")
                    store.change_status(actor, bid, data.get("status"), reason)
                elif action == "block":
                    day = solar_date(data.get("day", ""))
                    start, end = data.get("start"), data.get("end")
                    reason = data.get("reason")
                    if not all(isinstance(v, str) for v in (start, end, reason)):
                        raise ValueError("ساعت و دلیل را وارد کن.")
                    store.block(actor, sid, store.stamp(day, start), store.stamp(day, end), reason)
                elif action == "unblock":
                    bid = data.get("block_id")
                    if type(bid) is not int:
                        raise ValueError("شناسه بازه نامعتبر است.")
                    row = store.db.blocks.find_one({"_id": bid})
                    if not row or row["staff_id"] != sid:
                        raise BookingError("این بازه متعلق به آرایشگر انتخاب‌شده نیست.")
                    store.unblock(actor, bid)
                else:
                    raise ValueError("عملیات نامعتبر است.")
                return {"ok": True}
            return await run_in_threadpool(change)
        except (BookingError, ValueError, TypeError, KeyError, AttributeError) as error:
            return failure(error)
