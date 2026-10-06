"""SQLite online backup, including committed WAL data. Store backups privately."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

source = Path(os.environ.get("DB_PATH", "data/salon.sqlite3"))
if not source.exists():
    raise SystemExit("Database does not exist. Check DB_PATH.")
destination = Path(os.environ.get("BACKUP_DIR", "backups"))
destination.mkdir(parents=True, exist_ok=True)
target = destination / ("salon-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".sqlite3")
fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
print(f"Backup created: {target.name}")
