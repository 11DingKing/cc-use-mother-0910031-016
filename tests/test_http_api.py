"""HTTP 接口端到端测试（标准库 urllib，无第三方依赖）。"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from booking import FakeClock
from booking.http_app import build_server, create_service

T0 = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
OPS = {"X-Operator-Token": "test-token"}


class ApiClient:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, body=None, headers=None,
             raw_body=None):
        url = f"{self.base}{path}"
        if raw_body is not None:
            data = raw_body
        else:
            data = None if body is None else json.dumps(
                body, ensure_ascii=False).encode("utf-8")
        hdr = {"Content-Type": "application/json"}
        hdr.update(headers or {})
        req = Request(url, data=data, headers=hdr, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def school(self, school_id: str) -> "ApiClient":
        return _ScopedClient(self, {"X-School-Id": school_id})


class _ScopedClient:
    def __init__(self, parent: ApiClient, headers: dict) -> None:
        self.parent = parent
        self.headers = headers

    def call(self, method: str, path: str, body=None, headers=None):
        hdr = dict(self.headers)
        hdr.update(headers or {})
        return self.parent.call(method, path, body, hdr)


class HttpWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(T0)
        service = create_service(
            clock=self.clock,
            confirm_window_hours=24,
            waitlist_ttl_days=7,
            max_group_size=20,
            max_holds=3,
        )
        self.httpd = build_server("127.0.0.1", 0, service=service,
                                  operator_token="test-token")
        self.port = self.httpd.server_address[1]
        self.thread = __import__("threading").Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.api = ApiClient(f"http://127.0.0.1:{self.port}")

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _seed_sessions(self, capacity: int = 40) -> None:
        for sid, date, acc in [
            ("S0501", "2026-05-01", []),
            ("S0502", "2026-05-02", []),
            ("S0503", "2026-05-03", []),
            ("S0504", "2026-05-04", ["电梯"]),
        ]:
            status, _ = self.api.call("POST", "/admin/sessions",
                                      {"id": sid, "date": date, "label": "上午",
                                       "capacity": capacity, "accessibility": acc},
                                      headers=OPS)
            self.assertEqual(status, 201)

    def test_完整工作流_申请确认缩减候补改期重复请求超时(self) -> None:
        self._seed_sessions(capacity=60)
        a = self.api.school("SCH-A")

        # 1) 电话受理提交：3 个备选日期，45 人 → 3 组，全部暂占
        status, resp = a.call("POST", "/api/requests", {
            "school_name": "阳光小学", "contact": "王老师",
            "headcount": 45, "grades": "3-4年级", "theme": "自然科普",
            "accessibility": [], "candidate_session_ids": ["S0501", "S0502", "S0503"],
            "idempotency_key": "call-2026-10-04-01",
        })
        self.assertEqual(status, 201, resp)
        req = resp["request"]
        rid = req["id"]
        self.assertEqual(len([o for o in req["options"] if o["state"] == "已暂占"]), 3)
        self.assertEqual(req["group_count"], 3)
        self.assertIsNotNone(req["confirm_deadline"])

        # 2) 重复来电同一业务键：返回原单，不重复占位
        status, resp2 = a.call("POST", "/api/requests", {
            "school_name": "阳光小学", "contact": "王老师",
            "headcount": 45, "grades": "3-4年级", "theme": "自然科普",
            "accessibility": [], "candidate_session_ids": ["S0501", "S0502", "S0503"],
            "idempotency_key": "call-2026-10-04-01",
        })
        self.assertEqual(status, 200)
        self.assertTrue(resp2["replayed"])
        self.assertEqual(resp2["request"]["id"], rid)

        # 3) 确认 S0502，其余暂占自动释放
        status, resp = a.call("POST", f"/api/requests/{rid}/confirm",
                              {"session_id": "S0502"})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["request"]["status"], "已排定")
        confirmed = [o for o in resp["request"]["options"] if o["state"] == "已确认"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["session_id"], "S0502")

        # 4) 部分班级取消 → 缩减到 30 人（2 组），空余 15 座触发候补递补
        b = self.api.school("SCH-B")
        status, rb = b.call("POST", "/api/requests", {
            "school_name": "晨溪小学", "contact": "李老师",
            "headcount": 20, "grades": "5年级", "theme": "红色教育",
            "accessibility": [], "candidate_session_ids": ["S0502"],
        })
        self.assertEqual(status, 201, rb)
        bid = rb["request"]["id"]
        # S0502 已确认 45，剩 15 个名额 → B 要 20 人被拒，主动候补
        status, wl = b.call("POST", f"/api/requests/{bid}/waitlist",
                            {"session_id": "S0502", "note": "只要这一天"})
        self.assertEqual(status, 201, wl)
        status, resp = a.call("POST", f"/api/requests/{rid}/reduce",
                              {"headcount": 30})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["request"]["headcount"], 30)
        self.assertEqual(resp["request"]["group_count"], 2)
        # B 自动递补为暂占并获得确认期限
        status, bview = b.call("GET", f"/api/requests/{bid}")
        self.assertEqual(status, 200)
        self.assertEqual(bview["status"], "待确认")
        self.assertEqual(bview["options"][0]["state"], "已暂占")
        self.assertIsNotNone(bview["confirm_deadline"])

        # 5) B 确认
        status, bview = b.call("POST", f"/api/requests/{bid}/confirm",
                               {"session_id": "S0502"})
        self.assertEqual(status, 200, bview)
        self.assertEqual(bview["request"]["status"], "已排定")

        # 6) A 改期到 S0503（容量充足），已排定单场改期自动重新确认
        status, resp = a.call("POST", f"/api/requests/{rid}/reschedule",
                              {"candidate_session_ids": ["S0503"]})
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["request"]["status"], "已排定")
        self.assertEqual(resp["request"]["confirmed_session_id"], "S0503")

        # 7) 校验容量（场次容量 60）：S0502=20(B确认), S0503=30(A确认)
        status, sessions = self.api.call("GET", "/admin/sessions", headers=OPS)
        usage = {s["id"]: (s["used"], s["available"]) for s in sessions["sessions"]}
        self.assertEqual(usage["S0502"], (20, 40))
        self.assertEqual(usage["S0503"], (30, 30))

        # 8) B 也改期走，S0502 清空
        status, bview = b.call("POST", f"/api/requests/{bid}/reschedule",
                               {"candidate_session_ids": ["S0501"]})
        self.assertEqual(status, 200)
        status, sessions = self.api.call("GET", "/admin/sessions", headers=OPS)
        usage = {s["id"]: (s["used"], s["available"]) for s in sessions["sessions"]}
        self.assertEqual(usage["S0502"], (0, 60))
        self.assertEqual(usage["S0501"], (20, 40))

    def test_超时与租户隔离与运营解释(self) -> None:
        self._seed_sessions()
        a = self.api.school("SCH-A")
        b = self.api.school("SCH-B")

        # A 暂占 S0501 全部容量
        status, ra = a.call("POST", "/api/requests", {
            "school_name": "阳光小学", "contact": "王老师", "headcount": 40,
            "grades": "3年级", "theme": "自然科普", "accessibility": [],
            "candidate_session_ids": ["S0501"],
        })
        aid = ra["request"]["id"]
        # B 被拒后候补
        status, rb = b.call("POST", "/api/requests", {
            "school_name": "晨溪小学", "contact": "李老师", "headcount": 40,
            "grades": "5年级", "theme": "红色教育", "accessibility": [],
            "candidate_session_ids": ["S0501"],
        })
        bid = rb["request"]["id"]
        self.assertEqual(rb["request"]["options"][0]["reason"],
                         "剩余名额0不足40人")
        status, _ = b.call("POST", f"/api/requests/{bid}/waitlist",
                           {"session_id": "S0501"})
        self.assertEqual(status, 201)

        # 学校列表只含本校
        status, mine = a.call("GET", "/api/requests")
        self.assertEqual({r["id"] for r in mine["requests"]}, {aid})
        # B 访问 A 的单：404
        status, err = b.call("GET", f"/api/requests/{aid}")
        self.assertEqual(status, 404)
        # 无凭证 401
        status, err = self.api.call("GET", "/api/requests")
        self.assertEqual(status, 401)
        # 运营令牌错误 401
        status, err = self.api.call("GET", "/admin/requests",
                                    headers={"X-Operator-Token": "wrong"})
        self.assertEqual(status, 401)

        # 推进时钟：A 确认超时 → 取消并递补 B
        self.clock.advance(hours=24, seconds=1)
        status, expired = self.api.call("POST", "/admin/expire", headers=OPS)
        self.assertEqual(status, 200)
        self.assertIn(aid, expired["expired_request_ids"])
        status, bview = b.call("GET", f"/api/requests/{bid}")
        self.assertEqual(bview["status"], "待确认")
        self.assertEqual(bview["options"][0]["state"], "已暂占")
        # B 再超时未确认 → 取消，名额彻底释放
        self.clock.advance(hours=24, seconds=1)
        status, _ = self.api.call("POST", "/admin/expire", headers=OPS)
        status, bview = b.call("GET", f"/api/requests/{bid}")
        self.assertEqual(bview["status"], "已取消")
        status, sessions = self.api.call("GET", "/admin/sessions", headers=OPS)
        s1 = next(s for s in sessions["sessions"] if s["id"] == "S0501")
        self.assertEqual(s1["used"], 0)

        # 运营解释接口：拒绝原因 + 决策链
        status, explanation = self.api.call(
            "GET", f"/admin/requests/{bid}/explain", headers=OPS)
        self.assertEqual(status, 200)
        self.assertTrue(explanation["decision_chain"])
        self.assertTrue(any(
            "候补" == d["trigger"] for d in explanation["decision_chain"]))
        status, overview = self.api.call(
            "GET", "/admin/waitlist?session_id=S0501", headers=OPS)
        self.assertEqual(status, 200)
        self.assertEqual(len(overview["waitlist"]), 1)

    def test_无障碍需求与候补校验(self) -> None:
        self._seed_sessions()
        c = self.api.school("SCH-C")
        status, resp = c.call("POST", "/api/requests", {
            "school_name": "星光小学", "contact": "赵老师", "headcount": 20,
            "grades": "1-2年级", "theme": "安全教育",
            "accessibility": ["电梯"],
            "candidate_session_ids": ["S0501", "S0504"],
        })
        self.assertEqual(status, 201)
        self.assertEqual(resp["request"]["options"][0]["state"], "已拒绝")
        self.assertIn("电梯", resp["request"]["options"][0]["reason"])
        self.assertEqual(resp["request"]["options"][1]["state"], "已暂占")
        rid = resp["request"]["id"]
        # 不能候补不满足无障碍的场次
        status, err = c.call("POST", f"/api/requests/{rid}/waitlist",
                             {"session_id": "S0501"})
        self.assertEqual(status, 400)
        # 无刚需学校：场次有名额时不允许候补
        d = self.api.school("SCH-D")
        status, rd = d.call("POST", "/api/requests", {
            "school_name": "朝阳小学", "contact": "孙老师", "headcount": 20,
            "grades": "6年级", "theme": "历史人文", "accessibility": [],
            "candidate_session_ids": ["S0502"],
        })
        self.assertEqual(status, 201)
        status, err = d.call(
            "POST", f"/api/requests/{rd['request']['id']}/waitlist",
            {"session_id": "S0503"})
        self.assertEqual(status, 409)

    def test_错误JSON返回400(self) -> None:
        self._seed_sessions()
        status, _ = self.api.call(
            "POST", "/api/requests", raw_body=b"{bad json",
            headers={"X-School-Id": "SCH-A", "Content-Type": "application/json"})
        self.assertEqual(status, 400)


    def test_运营履约流转与退出候补(self) -> None:
        self._seed_sessions()
        a = self.api.school("SCH-A")
        status, resp = a.call("POST", "/api/requests", {
            "school_name": "阳光小学", "contact": "王老师", "headcount": 20,
            "grades": "3年级", "theme": "自然科普", "accessibility": [],
            "candidate_session_ids": ["S0501"],
        })
        rid = resp["request"]["id"]
        status, resp = a.call("POST", f"/api/requests/{rid}/confirm",
                              {"session_id": "S0501"})
        self.assertEqual(status, 200)
        status, resp = self.api.call(
            "POST", f"/admin/requests/{rid}/execute", headers=OPS)
        self.assertEqual(status, 200)
        self.assertEqual(resp["request"]["status"], "执行中")
        status, resp = self.api.call(
            "POST", f"/admin/requests/{rid}/settle", headers=OPS)
        self.assertEqual(status, 200)
        self.assertEqual(resp["request"]["status"], "已结算")
        # 无运营令牌被拒
        status, _ = self.api.call(
            "POST", f"/admin/requests/{rid}/execute")
        self.assertEqual(status, 401)

    def test_候补与退出候补(self) -> None:
        self._seed_sessions()
        a = self.api.school("SCH-A")
        b = self.api.school("SCH-B")
        a.call("POST", "/api/requests", {
            "school_name": "阳光小学", "contact": "王老师", "headcount": 40,
            "grades": "3年级", "theme": "自然科普", "accessibility": [],
            "candidate_session_ids": ["S0501"],
        })
        status, rb = b.call("POST", "/api/requests", {
            "school_name": "晨溪小学", "contact": "李老师", "headcount": 10,
            "grades": "5年级", "theme": "红色教育", "accessibility": [],
            "candidate_session_ids": ["S0501"],
        })
        bid = rb["request"]["id"]
        status, _ = b.call("POST", f"/api/requests/{bid}/waitlist",
                           {"session_id": "S0501"})
        self.assertEqual(status, 201)
        # 重复候补冲突
        status, _ = b.call("POST", f"/api/requests/{bid}/waitlist",
                           {"session_id": "S0501"})
        self.assertEqual(status, 409)
        # 退出候补
        status, resp = b.call("POST", f"/api/requests/{bid}/waitlist/S0501")
        self.assertEqual(status, 200)
        self.assertEqual(resp["request"]["status"], "筹备")


if __name__ == "__main__":
    unittest.main()
