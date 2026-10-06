"""Run locally only after deployment and database initialization."""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import Telegram, TelegramError


if __name__ == "__main__":
    base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    secret = os.environ["TELEGRAM_WEBHOOK_SECRET"]
    if urlparse(base).scheme != "https" or not urlparse(base).netloc:
        raise SystemExit("PUBLIC_BASE_URL must be your HTTPS production origin.")
    if len(secret) < 32 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in secret):
        raise SystemExit("Webhook secret must be >=32 characters, using A-Z, a-z, 0-9, _ or -.")
    telegram = Telegram(os.environ["BOT_TOKEN"])
    try:
        result = telegram.call("setWebhook", url=base + "/api/telegram", secret_token=secret,
                               max_connections=1, allowed_updates=["message", "callback_query"],
                               drop_pending_updates=False)
        info = telegram.call("getWebhookInfo")
    except TelegramError as error:
        raise SystemExit(f"Telegram setup failed (code {error.code}); check settings privately.") from None
    print("Webhook registered:", bool(result))
    print("Expected URL active:", info.get("url") == base + "/api/telegram")
    print("Pending updates:", info.get("pending_update_count", 0))
    print("Telegram reports an error:", bool(info.get("last_error_message")))
