"""Download the pinned public source and build a separate corrected project ZIP."""
import argparse
import ast
import hashlib
import io
import re
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile

COMMIT = "2b72f48cab57dc3a1aaa3725c72c022ca4417434"
URL = f"https://codeload.github.com/amirhossein-k/salon-booking-bot-v4/zip/{COMMIT}"
HERE = Path(__file__).resolve().parent


def replace_once(text, old, new, filename):
    if text.count(old) != 1:
        raise ValueError(f"Unexpected source in {filename}; refusing an unsafe patch.")
    return text.replace(old, new, 1)


def replace_one_of(text, variants, new, filename):
    matches = [(old, text.count(old)) for old in variants if text.count(old)]
    if len(matches) != 1 or matches[0][1] != 1:
        found = ", ".join(str(count) for _, count in matches) or "0"
        raise ValueError(f"Unexpected source in {filename}; weekly-hours marker matches: {found}.")
    return text.replace(matches[0][0], new, 1)


def replace_weekly_hours_notice(text, new, filename):
    pattern = (
        r'"تغییر برنامه(?:ٔ|‌ٔ)? ?هفتگی در config\.json و با راه‌اندازی مجدد '
        r'انجام می(?:‌)?شود؛ "'
    )
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise ValueError(f"Unexpected source in {filename}; weekly-hours marker matches: {len(matches)}.")
    start, end = matches[0].span()
    return text[:start] + new + text[end:]


def patch(root):
    file = root / "mongo_store.py"
    text = file.read_text(encoding="utf-8")
    text = replace_once(text, "import uuid\n", "import uuid\nimport copy\n", file.name)
    text = replace_once(
        text, "        self.config = config\n",
        "        self.base_config = copy.deepcopy(config)\n"
        "        self.config = copy.deepcopy(config)\n", file.name)
    text = replace_once(
        text, "        self.active_session = None\n\n    def connect(self):",
        "        self.active_session = None\n"
        "        self.refresh_staff_settings()\n\n"
        "    def refresh_staff_settings(self):\n"
        "        from staff_management import effective_config\n"
        "        settings = self.db.staff_settings.find_one({'_id': 'staff'}, **self.kwargs())\n"
        "        self.config = effective_config(self.base_config, settings)\n"
        "        self.staff = {s['id']: s for s in self.config['staff']}\n"
        "        self.validate()\n\n"
        "    def can_manage(self, uid, sid):\n"
        "        staff = self.staff.get(sid)\n"
        "        return bool(staff and (uid in self.config['admin_ids'] or\n"
        "                    (staff.get('panel_enabled', True) and\n"
        "                     staff.get('telegram_id') == uid)))\n\n"
        "    def connect(self):", file.name)
    text = replace_once(
        text,
        '            self.db.sessions.update_one({"_id": uid}, {"$inc": {"revision": 1}}, **self.kwargs())\n',
        '            self.db.sessions.update_one({"_id": uid}, {"$inc": {"revision": 1}}, **self.kwargs())\n'
        '            self.refresh_staff_settings()\n', file.name)
    file.write_text(text, encoding="utf-8")

    file = root / "mongo_runtime.py"
    text = file.read_text(encoding="utf-8")
    text = replace_once(
        text, '        selector = {"_id": row["_id"], "lease_token": token}\n',
        '        selector = {"_id": row["_id"], "lease_token": token}\n'
        '        # Recheck recipients for old as well as new queued booking notifications.\n'
        '        if str(row["_id"]).startswith("booking:"):\n'
        '            try:\n'
        '                bid = int(str(row["_id"]).split(":")[1])\n'
        '            except (ValueError, IndexError):\n'
        '                bid = None\n'
        '            booking = store.db.bookings.find_one({"_id": bid})\n'
        '            store.refresh_staff_settings()\n'
        '            recipient = row["chat_id"]\n'
        '            if not booking or (recipient != booking["customer_id"] and\n'
        '                               not store.can_manage(recipient, booking["staff_id"])):\n'
        '                store.db.outbox.update_one(selector, {"$set": {"cancelled": now, "lease_until": 0}})\n'
        '                continue\n', file.name)
    file.write_text(text, encoding="utf-8")

    file = root / "bot.py"
    text = file.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '        elif action == "manage":\n'
        '            rows = [[(s["name"], f"staff|{s[\'id\']}")] for s in self.db.manageable(uid)]\n',
        '        elif action == "manage":\n'
        '            allowed = self.db.manageable(uid)\n'
        '            if uid not in self.db.config["admin_ids"] and len(allowed) == 1:\n'
        '                return self.callback(uid, f"staff|{allowed[0][\'id\']}")\n'
        '            rows = [[(s["name"], f"staff|{s[\'id\']}")] for s in allowed]\n', file.name)
    text = replace_weekly_hours_notice(
        text,
        '"در نسخه آنلاین، مدیر برنامه هفتگی را از بخش کارکنان داشبورد تغییر می‌دهد؛ "',
        file.name)
    file.write_text(text, encoding="utf-8")

    file = root / "app.py"
    text = file.read_text(encoding="utf-8")
    text += (
        "\n\n# Authenticated employee management, registered after app initialization.\n"
        "from staff_management import install_routes\n"
        "install_routes(app, new_store, authorized, deny, csrf_token, payload, env, ROOT)\n"
    )
    file.write_text(text, encoding="utf-8")
    for name in ("dashboard.html", "dashboard-mobile.html"):
        file = root / name
        text = file.read_text(encoding="utf-8")
        link = (
            '<a href="/staff" style="position:fixed;bottom:18px;left:18px;'
            'z-index:10000;background:#14677a;color:white;padding:12px 18px;'
            'border-radius:12px;text-decoration:none;font:16px sans-serif">'
            'مدیریت کارکنان</a>\n'
        )
        text = replace_once(text, "</body>", link + "</body>", name)
        file.write_text(text, encoding="utf-8")
    for name in ("staff_management.py", "staff-management.html"):
        shutil.copy2(HERE / name, root / name)
    shutil.copy2(HERE / "README.fa.md", root / "STAFF-MANAGEMENT.fa.md")
    (root / "tests").mkdir(exist_ok=True)
    for name in ("test_staff_management.py", "test_routes.py"):
        shutil.copy2(HERE / name, root / "tests" / name)
    for file in root.rglob("*.py"):
        ast.parse(file.read_text(encoding="utf-8"), filename=str(file))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-zip", type=Path, help="Use the original pinned GitHub ZIP instead of downloading.")
    parser.add_argument("--output", type=Path, default=Path("salon-booking-bot-v4-fixed.zip"))
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output already exists. Choose a different --output; nothing was overwritten.")
    if args.source_zip:
        raw = args.source_zip.read_bytes()
    else:
        print("Downloading pinned GitHub source:", COMMIT)
        request = urllib.request.Request(URL, headers={"User-Agent": "SalonStaffFix/1.0"})
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise SystemExit("Archive too large.")
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            total = sum(item.file_size for item in archive.infolist())
            if total > 80 * 1024 * 1024:
                raise SystemExit("Unpacked source too large.")
            for item in archive.infolist():
                target = (temp / item.filename).resolve()
                if not target.is_relative_to(temp.resolve()):
                    raise SystemExit("Unsafe archive path.")
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise SystemExit("Archive symlinks are not allowed.")
            archive.extractall(temp)
        roots = [p for p in temp.iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise SystemExit("Unexpected archive layout.")
        root = roots[0]
        # Verify the expected snapshot before any source patch.
        expected = {
            "app.py": "f2bacab7ee7fde3573d7d5952344b3cce8ea4b3d",
            "bot.py": "9bc466cb6b73b0594b5d4b14af656fb10e188e61",
            "mongo_store.py": "f240a3174a28931527db6f723885157f4a8ff81b",
            "mongo_runtime.py": "4f0ec0651fdbe2f6d8587ab1a5390a26f78bc903",
            "dashboard.html": "d8f59be81e4b7eab0ab41ad496bf834632f8de52",
            "dashboard-mobile.html": "c7baa9b2203fde579d2125428655f6aba18582c5",
        }
        for name, sha in expected.items():
            data = (root / name).read_bytes()
            actual = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            if actual != sha:
                raise SystemExit(f"{name} does not match the reviewed snapshot. No ZIP created.")
        patch(root)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.output, "x", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(root.rglob("*")):
                if file.is_file():
                    archive.write(file, Path("salon-booking-bot-v4-fixed") / file.relative_to(root))
    print("Created:", args.output.resolve())
    print("Python syntax verified. Run deployment and database tests before production.")


if __name__ == "__main__":
    main()
