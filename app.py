"""Vercel FastAPI entrypoint. No polling, local database, or background threads."""
import base64
import csv
import hashlib
import hmac
import io
import json
import os
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from starlette.concurrency import run_in_threadpool

from bot import Telegram, TelegramError
from core import BookingError
from dashboard import solar_date
from jalali import numeric
from mongo_runtime import MongoAnalytics, bot_factory, deliver
from mongo_store import MongoStore

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
ROOT = Path(__file__).parent
HEADERS = {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'none'",
}


def env(name, minimum=1):
    value = os.environ.get(name, "")
    if len(value) < minimum or value.startswith(("PUT_", "REPLACE_", "YOUR_")):
        raise RuntimeError(f"Missing required setting: {name}")
    return value


@lru_cache(maxsize=1)
def client():
    return MongoClient(env("MONGODB_URI"), serverSelectionTimeoutMS=5000,
                       connectTimeoutMS=5000, socketTimeoutMS=10000,
                       maxPoolSize=10, retryWrites=True, appname="salon-vercel")


def new_store():
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    return MongoStore(client()[env("MONGODB_DB")], config)


def secure_equal(value, expected):
    return hmac.compare_digest(value.encode(), expected.encode())


def authorized(request):
    auth = request.headers.get("authorization", "")
    try:
        raw = base64.b64decode(auth.split(" ", 1)[1], validate=True).decode()
        expected = os.environ.get("DASHBOARD_USER", "manager") + ":" + env("DASHBOARD_PASSWORD", 16)
        return auth.startswith("Basic ") and secure_equal(raw, expected)
    except (ValueError, UnicodeDecodeError, IndexError):
        return False


def csrf_token():
    return hmac.new(env("CSRF_SECRET", 32).encode(), b"salon-dashboard-csrf-v1", hashlib.sha256).hexdigest()


def deny():
    return Response(status_code=401, headers={**HEADERS,
        "WWW-Authenticate": 'Basic realm="Salon Manager", charset="UTF-8"'})


def report(request, store):
    first = solar_date(request.query_params.get("from", numeric(store.today() - timedelta(days=6))))
    last = solar_date(request.query_params.get("to", numeric(store.today())))
    return MongoAnalytics(store).report(first, last, request.query_params.get("staff") or None)


@app.middleware("http")
async def secure_headers(request, call_next):
    response = await call_next(request)
    for key, value in HEADERS.items():
        response.headers[key] = value
    return response


@app.exception_handler(Exception)
async def server_error(request, error):
    # Never return database connection strings, exception URLs or tokens.
    return JSONResponse({"error": "سرویس آماده نیست؛ تنظیمات سرور و اتصال دیتابیس را بررسی کن."},
                        status_code=503, headers=HEADERS)


@app.get("/health")
def health():
    # Shallow health check, deliberately contains no secrets or personal data.
    return {"ok": True, "version": "vercel-mongodb-v1"}


@app.get("/")
@app.get("/mobile")
@app.get("/mobile/")
def dashboard(request: Request):
    if not authorized(request):
        return deny()
    filename = "dashboard-mobile.html" if request.url.path.startswith("/mobile") else "dashboard.html"
    html = (ROOT / filename).read_text(encoding="utf-8").replace("const LIVE = false;", "const LIVE = true;")
    return HTMLResponse(html, headers=HEADERS)


@app.get("/api/report")
def get_report(request: Request):
    if not authorized(request):
        return deny()
    try:
        result = report(request, new_store())
        result["csrf"] = csrf_token()
        return JSONResponse(result)
    except (ValueError, BookingError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


@app.get("/api/export")
def export(request: Request):
    if not authorized(request):
        return deny()
    try:
        result = report(request, new_store())
    except (ValueError, BookingError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["آرایشگر", "دریافتی ثبت‌شده تومان", "نوبت", "لغو", "ظرفیت دقیقه",
                     "اشغال دقیقه", "درصد اشغال"])
    for s in result["staff"]:
        name = s["name"]
        if name.lstrip().startswith(("=", "+", "-", "@")):
            name = "'" + name
        writer.writerow([name, s["revenue"], s["appointments"], s["cancelled"],
                         s["available_minutes"], s["occupied_minutes"], s["utilization"]])
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="salon-report.csv"'})


async def payload(request, maximum):
    # Stream with a cap even if Content-Length is omitted.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > maximum:
            raise ValueError("درخواست بیش از حد بزرگ است.")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("درخواست معتبر نیست.") from None
    if not isinstance(data, dict):
        raise ValueError("درخواست معتبر نیست.")
    return data


@app.post("/api/payment")
async def payment(request: Request):
    if not authorized(request):
        return deny()
    if not secure_equal(request.headers.get("x-csrf-token", ""), csrf_token()):
        return JSONResponse({"error": "درخواست معتبر نیست؛ صفحه را تازه کن."}, status_code=403)
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        return JSONResponse({"error": "نوع درخواست نامعتبر است."}, status_code=415)
    try:
        data = await payload(request, 4096)
        if type(data.get("booking_id")) is not int:
            raise ValueError("شناسهٔ نوبت نامعتبر است.")
        store = new_store()
        actor = int(env("DASHBOARD_ADMIN_ID"))
        await run_in_threadpool(MongoAnalytics(store).set_payment, actor, data["booking_id"],
                                data.get("amount_toman"), data.get("note"))
        return {"ok": True}
    except (ValueError, BookingError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


@app.post("/api/telegram")
async def webhook(request: Request):
    if os.environ.get("VERCEL_ENV") not in (None, "production"):
        return JSONResponse({"error": "Production webhook only"}, status_code=403)
    if not secure_equal(request.headers.get("x-telegram-bot-api-secret-token", ""),
                        env("TELEGRAM_WEBHOOK_SECRET", 32)):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        data = await payload(request, 100000)
        store = new_store()
        # The database callback retries automatically; it has no Telegram side effects.
        await run_in_threadpool(store.process_update, data, bot_factory)
    except ValueError:
        return JSONResponse({"error": "Invalid update"}, status_code=400)
    # A failed database transaction returns 503 through the handler so Telegram retries.
    # After commit, notification failure must not turn the update into a false failure.
    try:
        telegram = Telegram(env("BOT_TOKEN"))
        callback = data.get("callback_query")
        if callback:
            try:
                await run_in_threadpool(telegram.call, "answerCallbackQuery",
                                        callback_query_id=callback["id"])
            except (TelegramError, KeyError):
                pass
        await run_in_threadpool(deliver, store, telegram, seconds=10, limit=12)
    except (TelegramError, PyMongoError):
        pass  # Outbox persists; the cron worker retries.
    return {"ok": True}


@app.get("/api/cron")
def cron(request: Request):
    if os.environ.get("VERCEL_ENV") not in (None, "production"):
        return JSONResponse({"error": "Production scheduler only"}, status_code=403)
    if not secure_equal(request.headers.get("authorization", ""), "Bearer " + env("CRON_SECRET", 32)):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    store = new_store()
    store.schedule_reminders()
    sent = deliver(store, Telegram(env("BOT_TOKEN")), seconds=15, limit=30)
    return {"ok": True, "sent": sent}
