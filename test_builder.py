"""Exercise patch application on source-shaped fixtures, not a live GitHub copy."""
import ast
import tempfile
import unittest
from pathlib import Path

from build_fixed_project import patch, replace_once, replace_one_of, replace_weekly_hours_notice


class BuilderTests(unittest.TestCase):
    def test_unexpected_marker_fails_closed(self):
        with self.assertRaises(ValueError):
            replace_once("wrong", "expected", "new", "source.py")
        with self.assertRaises(ValueError):
            replace_once("twice twice", "twice", "new", "source.py")

    def test_weekly_hours_marker_accepts_spacing_variants(self):
        result = replace_one_of(
            '"تغییر برنامه هفتگی در config.json و با راه‌اندازی مجدد انجام می‌شود؛ "',
            (
                '"تغییر برنامهٔ هفتگی در config.json و با راه‌اندازی مجدد انجام می‌شود؛ "',
                '"تغییر برنامه هفتگی در config.json و با راه‌اندازی مجدد انجام می‌شود؛ "',
            ),
            '"مدیریت از داشبورد؛ "',
            "bot.py",
        )
        self.assertEqual(result, '"مدیریت از داشبورد؛ "')

    def test_weekly_hours_marker_accepts_zero_width_joiners(self):
        source = '"تغییر برنامه هفتگی در config.json و با راه‌اندازی مجدد انجام می\u200cشود؛ "'
        self.assertEqual(
            replace_weekly_hours_notice(source, '"مدیریت از داشبورد؛ "', "bot.py"),
            '"مدیریت از داشبورد؛ "',
        )

    def test_patched_fixture_compiles_and_has_access_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "mongo_store.py").write_text(
                'import uuid\n'
                'class MongoStore:\n'
                '    def __init__(self, config):\n'
                '        self.config = config\n'
                '        self.active_session = None\n\n'
                '    def connect(self):\n'
                '        pass\n'
                '    def process_update(self, uid):\n'
                '        def work():\n'
                '            self.db.sessions.update_one({"_id": uid}, {"$inc": {"revision": 1}}, **self.kwargs())\n'
                '        return work\n', encoding="utf-8")
            (root / "mongo_runtime.py").write_text(
                'def deliver(store):\n'
                '    for row in []:\n'
                '        selector = {"_id": row["_id"], "lease_token": token}\n', encoding="utf-8")
            (root / "bot.py").write_text(
                'class Bot:\n'
                '    def callback(self, uid, action):\n'
                '        if action == "home":\n'
                '            pass\n'
                '        elif action == "manage":\n'
                '            rows = [[(s["name"], f"staff|{s[\'id\']}")] for s in self.db.manageable(uid)]\n'
                '        return "تغییر برنامهٔ هفتگی در config.json و با راه‌اندازی مجدد انجام می‌شود؛ "\n',
                encoding="utf-8")
            (root / "app.py").write_text('x = 1\n', encoding="utf-8")
            for name in ("dashboard.html", "dashboard-mobile.html"):
                (root / name).write_text("<html><body></body></html>", encoding="utf-8")
            patch(root)
            source = (root / "mongo_store.py").read_text(encoding="utf-8")
            tree = ast.parse(source)
            method = next(node for node in ast.walk(tree)
                          if isinstance(node, ast.FunctionDef) and node.name == "can_manage")
            namespace = {}
            exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), "access", "exec"), namespace)
            fake = type("Fake", (), {"staff": {"one": {"telegram_id": 111}}, "config": {"admin_ids": [900]}})()
            can = namespace["can_manage"]
            self.assertTrue(can(fake, 111, "one"))
            self.assertFalse(can(fake, 222, "one"))
            self.assertTrue(can(fake, 900, "one"))
            fake.staff["one"]["panel_enabled"] = False
            self.assertFalse(can(fake, 111, "one"))
            self.assertTrue(can(fake, 900, "one"))
            fake.staff["one"].update(telegram_id=333, panel_enabled=True)
            self.assertFalse(can(fake, 111, "one"))
            self.assertTrue(can(fake, 333, "one"))
            self.assertFalse(can(fake, 900, "unknown"))
            self.assertIn('self.refresh_staff_settings()', source)
            self.assertIn('install_routes(app', (root / "app.py").read_text())
            self.assertIn('not store.can_manage', (root / "mongo_runtime.py").read_text())
            self.assertIn('href="/staff"', (root / "dashboard.html").read_text())


if __name__ == "__main__":
    unittest.main()
