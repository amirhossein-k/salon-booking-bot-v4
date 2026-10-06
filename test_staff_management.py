import copy
import sys
import types
import unittest
from unittest.mock import patch

from staff_management import effective_config, update_staff, validate_staff_config


BASE = {
    "admin_ids": [900],
    "services": [{"id": "hair"}, {"id": "nails"}],
    "staff": [
        {"id": "one", "name": "اول", "telegram_id": 111, "services": ["hair"],
         "weekly_hours": {"0": [["09:00", "18:00"]]}},
        {"id": "two", "name": "دوم", "telegram_id": 222, "services": ["nails"],
         "weekly_hours": {"0": [["09:00", "18:00"]]}}
    ]
}


class BookingError(Exception):
    pass


class Collection:
    def __init__(self):
        self.doc = None

    def find_one(self, selector, **kwargs):
        return copy.deepcopy(self.doc)

    def replace_one(self, selector, document, **kwargs):
        self.doc = copy.deepcopy(document)


class FakeStore:
    def __init__(self):
        self.base_config = copy.deepcopy(BASE)
        self.config = copy.deepcopy(BASE)
        self.staff = {s["id"]: s for s in self.config["staff"]}
        self.db = types.SimpleNamespace(staff_settings=Collection())
        self.events = []
        self.guards = []

    def kwargs(self):
        return {}

    def guard(self, sid):
        self.guards.append(sid)

    def now(self):
        return 100

    def audit_event(self, *args):
        self.events.append(args)

    def transaction(self, run):
        return run()


class StaffTests(unittest.TestCase):
    def config(self, **changes):
        result = copy.deepcopy(BASE)
        result["staff"][0].update(changes)
        return result

    def reject(self, **changes):
        with self.assertRaises(ValueError):
            validate_staff_config(self.config(**changes))

    def test_valid_config(self):
        validate_staff_config(BASE)

    def test_duplicate_identity(self):
        self.reject(telegram_id=222)

    def test_admin_identity_rejected(self):
        self.reject(telegram_id=900)

    def test_boolean_identity_rejected(self):
        self.reject(telegram_id=True)

    def test_string_identity_rejected(self):
        self.reject(telegram_id="111")

    def test_negative_identity_rejected(self):
        self.reject(telegram_id=-100)

    def test_missing_identity_allowed(self):
        validate_staff_config(self.config(telegram_id=None))

    def test_panel_flag_requires_bool(self):
        self.reject(panel_enabled="false")

    def test_blank_name(self):
        self.reject(name="  ")

    def test_invalid_service(self):
        self.reject(services=["unknown"])

    def test_duplicate_service(self):
        self.reject(services=["hair", "hair"])

    def test_empty_services(self):
        self.reject(services=[])

    def test_overlapping_hours(self):
        self.reject(weekly_hours={"0": [["09:00", "12:00"], ["11:00", "18:00"]]})

    def test_adjacent_hours_allowed(self):
        validate_staff_config(self.config(weekly_hours={"0": [["09:00", "12:00"], ["12:00", "18:00"]]}))

    def test_reversed_hours(self):
        self.reject(weekly_hours={"0": [["18:00", "09:00"]]})

    def test_invalid_hours(self):
        self.reject(weekly_hours={"0": [["09:00", "24:00"]]})

    def test_closed_day(self):
        validate_staff_config(self.config(weekly_hours={"0": []}))

    def test_settings_override_without_mutating_config(self):
        before = copy.deepcopy(BASE)
        result = effective_config(BASE, {"items": {"one": {"telegram_id": 333, "panel_enabled": False}}})
        self.assertEqual(result["staff"][0]["telegram_id"], 333)
        self.assertFalse(result["staff"][0]["panel_enabled"])
        self.assertEqual(BASE, before)

    def run_update(self, store, actor, sid, changes):
        fake = types.ModuleType("core")
        fake.BookingError = BookingError
        with patch.dict(sys.modules, {"core": fake}):
            return update_staff(store, actor, sid, changes)

    def test_manager_update_persists_and_audits(self):
        store = FakeStore()
        self.run_update(store, 900, "one", {"telegram_id": 333})
        self.assertEqual(store.db.staff_settings.doc["items"]["one"]["telegram_id"], 333)
        self.assertEqual(store.guards, ["one"])
        self.assertEqual(len(store.events), 1)
        self.assertEqual(store.db.staff_settings.doc["revision"], 1)

    def test_employee_cannot_change_settings(self):
        store = FakeStore()
        with self.assertRaises(BookingError):
            self.run_update(store, 111, "one", {"telegram_id": 333})
        self.assertIsNone(store.db.staff_settings.doc)

    def test_duplicate_assignment_does_not_write(self):
        store = FakeStore()
        with self.assertRaises(ValueError):
            self.run_update(store, 900, "one", {"telegram_id": 222})
        self.assertIsNone(store.db.staff_settings.doc)
        self.assertEqual(store.guards, [])

    def test_cannot_change_admin_ids(self):
        store = FakeStore()
        with self.assertRaises(ValueError):
            self.run_update(store, 900, "one", {"admin_ids": [111]})
        self.assertIsNone(store.db.staff_settings.doc)

    def test_stored_assignments_are_rechecked(self):
        store = FakeStore()
        self.run_update(store, 900, "one", {"telegram_id": 333})
        with self.assertRaises(ValueError):
            self.run_update(store, 900, "two", {"telegram_id": 333})
        self.assertEqual(store.db.staff_settings.doc["revision"], 1)


if __name__ == "__main__":
    unittest.main()
