"""HTTP 服务端端到端测试。"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from booking_service.clock import FixedClock
from booking_service.http_app import BookingHTTPServerApp
from booking_service.service import BookingService

from datetime import datetime, timezone

T0 = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
D1, D2 = "2026-10-20", "2026-10-21"


class HttpIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(T0)
        self.service = BookingService(clock=self.clock, confirm_ttl=timedelta(hours=24), max_held_plans=2)
        self.app = BookingHTTPServerApp(self.service, admin_key="secret")
        _, self.port = self.app.start_background("127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.app.stop()

    def _request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers or {}
        )
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self) -> None:
        # 运营登记学校与场次
        admin = {"X-Admin-Key": "secret"}
        status, _ = self._request("POST", "/api/ops/schools", {"school_id": "S1", "name": "一中"}, admin)
        self.assertEqual(status, 201)
        status, _ = self._request("POST", "/api/ops/schools", {"school_id": "S2", "name": "二中"}, admin)
        self.assertEqual(status, 201)
        for sid, date in (("A", D1), ("B", D1), ("C", D2), ("D", D2)):
            status, _ = self._request(
                "POST", "/api/ops/sessions",
                {"session_id": sid, "date": date, "time_slot": "上午", "theme": "海洋馆", "capacity": 60},
                admin,
            )
            self.assertEqual(status, 201)

        # S1 申请 100 人两个日期，只暂占有限方案
        status, body = self._request(
            "POST", "/api/schools/applications",
            {
                "idempotency_key": "http-1", "people_count": 100, "age_band": "13-15",
                "theme": "海洋馆", "preferred_dates": [D1, D2], "split_sizes": [60, 40],
            },
            {"X-School-ID": "S1"},
        )
        self.assertEqual(status, 201)
        request_id = body["request_id"]
        self.assertEqual([p["state"] for p in body["plans"]], ["held", "held"])

        # 重复请求返回同一意向
        status, body = self._request(
            "POST", "/api/schools/applications",
            {
                "idempotency_key": "http-1", "people_count": 100, "age_band": "13-15",
                "theme": "海洋馆", "preferred_dates": [D1, D2], "split_sizes": [60, 40],
            },
            {"X-School-ID": "S1"},
        )
        self.assertEqual(body["request_id"], request_id)

        # S2 无法查看 S1 的意向
        status, body = self._request("GET", f"/api/schools/requests/{request_id}", headers={"X-School-ID": "S2"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "FORBIDDEN")

        # S1 确认 D2 方案
        _, detail = self._request("GET", f"/api/schools/requests/{request_id}", headers={"X-School-ID": "S1"})
        target = next(p for p in detail["plans"] if p["date"] == D2)
        status, confirmed = self._request(
            "POST", f"/api/schools/requests/{request_id}/confirm",
            {"plan_id": target["plan_id"]}, {"X-School-ID": "S1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "已排定")

        # S2 此时申请 D2 只能进入候补；运营解释可见原因
        status, wl = self._request(
            "POST", "/api/schools/applications",
            {"idempotency_key": "http-2", "people_count": 30, "age_band": "12",
             "theme": "海洋馆", "preferred_dates": [D2]},
            {"X-School-ID": "S2"},
        )
        self.assertIn(wl["status"], ("候补",))
        waiter_id = wl["request_id"]
        # 无运营密钥被拒
        status, body = self._request("GET", f"/api/ops/requests/{waiter_id}/explanation")
        self.assertEqual(status, 403)

        status, explanation = self._request(
            "GET", f"/api/ops/requests/{waiter_id}/explanation", headers=admin
        )
        self.assertEqual(status, 200)
        self.assertTrue(explanation["waitlist"])
        self.assertTrue(explanation["decisions"])

        # S1 缩减 60 → 40，释放 20 席仍不足 30；再取消整个班级后 S2 递补
        split_id = confirmed["splits"][0]["split_id"]
        status, _ = self._request(
            "POST", f"/api/schools/requests/{request_id}/reduce",
            {"split_id": split_id, "new_size": 40}, {"X-School-ID": "S1"},
        )
        self.assertEqual(status, 200)
        _, still_waiting = self._request("GET", f"/api/schools/requests/{waiter_id}", headers={"X-School-ID": "S2"})
        self.assertEqual(still_waiting["status"], "候补")

        status, _ = self._request(
            "POST", f"/api/schools/requests/{request_id}/cancel", headers={"X-School-ID": "S1"}
        )
        self.assertEqual(status, 200)
        _, promoted = self._request("GET", f"/api/schools/requests/{waiter_id}", headers={"X-School-ID": "S2"})
        self.assertEqual(promoted["status"], "待确认")

        # 运营审计含递补事件
        _, audit = self._request("GET", "/api/ops/audit", headers=admin)
        kinds = {e["event_type"] for e in audit["events"]}
        self.assertIn("promoted", kinds)
        self.assertIn("duplicate_request", kinds)

        # 超时任务：推进时钟后调用运营 sweep
        self.clock.advance(hours=25)
        status, swept = self._request("POST", "/api/ops/sweep", headers=admin)
        self.assertEqual(status, 200)
        self.assertIn(waiter_id, swept["expired_request_ids"])


if __name__ == "__main__":
    unittest.main()
