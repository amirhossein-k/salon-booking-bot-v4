"""Route boundary checks with dependency stubs, not an HTTP integration test."""
import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from staff_management import install_routes
from test_staff_management import BookingError, FakeStore


class Response:
    def __init__(self, content=None, status_code=200, **kwargs):
        self.content = content
        self.status_code = status_code


class App:
    def __init__(self):
        self.routes = {}

    def register(self, method, path):
        def wrap(func):
            self.routes[(method, path)] = func
            return func
        return wrap

    def get(self, path):
        return self.register("GET", path)

    def put(self, path):
        return self.register("PUT", path)

    def post(self, path):
        return self.register("POST", path)


class RoutesTests(unittest.TestCase):
    def setUp(self):
        fastapi = types.ModuleType("fastapi")
        fastapi.Request = type("Request", (), {})
        responses = types.ModuleType("fastapi.responses")
        responses.HTMLResponse = responses.JSONResponse = Response
        concurrency = types.ModuleType("starlette.concurrency")
        async def thread(func, *args):
            return func(*args)
        concurrency.run_in_threadpool = thread
        core = types.ModuleType("core")
        core.BookingError = BookingError
        dashboard = types.ModuleType("dashboard")
        dashboard.solar_date = lambda v: v
        self.modules = patch.dict(sys.modules, {
            "fastapi": fastapi, "fastapi.responses": responses,
            "starlette.concurrency": concurrency, "core": core, "dashboard": dashboard
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.app = App()
        self.store = FakeStore()
        self.store.services = {"hair": {"id": "hair"}, "nails": {"id": "nails"}}
        self.actor = "900"
        self.payload_reads = 0
        async def payload(request, maximum):
            self.payload_reads += 1
            return request.data
        install_routes(
            self.app, lambda: self.store, lambda r: r.logged_in,
            lambda: Response(status_code=401), lambda: "correct-token",
            payload, lambda name: self.actor, Path(__file__).parent)

    def request(self, logged_in=True, token="correct-token", data=None, mime="application/json"):
        return types.SimpleNamespace(logged_in=logged_in, headers={
            "x-csrf-token": token, "content-type": mime
        }, data=data or {}, query_params={})

    def put(self, request, sid="one"):
        return asyncio.run(self.app.routes["PUT", "/api/staff/{sid}"](sid, request))

    def post(self, request, sid="one"):
        return asyncio.run(self.app.routes["POST", "/api/staff/{sid}/calendar"](sid, request))

    def test_unauthenticated_list_denied(self):
        result = self.app.routes["GET", "/api/staff"](self.request(logged_in=False))
        self.assertEqual(result.status_code, 401)

    def test_unauthenticated_page_denied(self):
        result = self.app.routes["GET", "/staff"](self.request(logged_in=False))
        self.assertEqual(result.status_code, 401)

    def test_unauthenticated_put_does_not_read_payload(self):
        result = self.put(self.request(logged_in=False))
        self.assertEqual(result.status_code, 401)
        self.assertEqual(self.payload_reads, 0)
        self.assertIsNone(self.store.db.staff_settings.doc)

    def test_csrf_required_for_settings(self):
        result = self.put(self.request(token="wrong", data={"telegram_id": 333}))
        self.assertEqual(result.status_code, 403)
        self.assertIsNone(self.store.db.staff_settings.doc)

    def test_csrf_required_for_calendar(self):
        result = self.post(self.request(token="wrong", data={"action": "unblock", "block_id": 1}))
        self.assertEqual(result.status_code, 403)

    def test_json_required(self):
        result = self.put(self.request(mime="text/plain"))
        self.assertEqual(result.status_code, 415)

    def test_invalid_dashboard_admin_cannot_save(self):
        self.actor = "111"
        result = self.put(self.request(data={"telegram_id": 333}))
        self.assertEqual(result.status_code, 400)
        self.assertIsNone(self.store.db.staff_settings.doc)

    def test_manager_can_save(self):
        result = self.put(self.request(data={"telegram_id": 333}))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.store.db.staff_settings.doc["items"]["one"]["telegram_id"], 333)

    def test_cross_staff_status_is_rejected(self):
        self.store.require_manage = lambda actor, sid: None
        self.store.get_booking = lambda actor, bid: {"staff_id": "two"}
        result = self.post(self.request(data={"action": "status", "booking_id": 1, "status": "completed", "reason": "test"}))
        self.assertEqual(result.status_code, 400)

    def test_cross_staff_unblock_is_rejected(self):
        self.store.require_manage = lambda actor, sid: None
        self.store.db.blocks = types.SimpleNamespace(find_one=lambda q: {"staff_id": "two"})
        result = self.post(self.request(data={"action": "unblock", "block_id": 1}))
        self.assertEqual(result.status_code, 400)

    def test_unknown_operation_rejected(self):
        self.store.require_manage = lambda actor, sid: None
        result = self.post(self.request(data={"action": "delete_everything"}))
        self.assertEqual(result.status_code, 400)


if __name__ == "__main__":
    unittest.main()
