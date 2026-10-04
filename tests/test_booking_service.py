"""预约受理核心领域服务测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from booking_service.clock import FixedClock
from booking_service.errors import BookingError
from booking_service.models import (
    PLAN_ALTERNATIVE,
    PLAN_CONFIRMED,
    PLAN_EXPIRED,
    PLAN_HELD,
    PLAN_RELEASED,
    STATUS_PENDING_CONFIRMATION,
    STATUS_SCHEDULED,
    STATUS_WAITLISTED,
)
from booking_service.service import BookingService

T0 = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
D1, D2, D3 = "2026-10-20", "2026-10-21", "2026-10-22"


def make_service(**kwargs) -> tuple[BookingService, FixedClock]:
    clock = FixedClock(T0)
    service = BookingService(clock=clock, confirm_ttl=timedelta(hours=24), **kwargs)
    service.register_school("S1", "第一中学")
    service.register_school("S2", "第二中学")
    service.create_session("A", D1, "上午", "海洋馆", 60)
    service.create_session("B", D1, "下午", "海洋馆", 60)
    service.create_session("C", D2, "上午", "海洋馆", 60)
    service.create_session("C2", D2, "下午", "海洋馆", 60)
    service.create_session("D", D3, "上午", "海洋馆", 60)
    service.create_session("D3b", D3, "下午", "海洋馆", 60)
    # 无障碍场次容量较小
    service.create_session("E", D1, "上午", "海洋馆", 20, accessible=True)
    return service, clock


class ApplyAndHoldTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service(max_held_plans=2)

    def test_multiple_dates_but_only_limited_holds(self) -> None:
        req = self.service.apply(
            school_id="S1",
            idempotency_key="k1",
            people_count=100,
            age_band="12-14",
            theme="海洋馆",
            preferred_dates=[D1, D2, D3],
            split_sizes=[60, 40],
        )
        states = [p.state for p in req.plans]
        self.assertEqual(states, [PLAN_HELD, PLAN_HELD, PLAN_ALTERNATIVE])
        self.assertEqual(req.status, STATUS_PENDING_CONFIRMATION)
        # 只有前两个方案占名额；D3 场次可售仍为满额
        self.assertEqual(self.service.session_availability("D")["available"], 60)
        # D1 的两个场次合计暂占 100
        d1 = {s["session_id"]: s for s in self.service.ops_sessions()}
        self.assertEqual(d1["A"]["held"] + d1["B"]["held"], 100)

    def test_duplicate_idempotent_request_returns_same_request(self) -> None:
        first = self.service.apply(
            school_id="S1", idempotency_key="dup", people_count=10, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        second = self.service.apply(
            school_id="S1", idempotency_key="dup", people_count=10, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        self.assertEqual(first.request_id, second.request_id)
        self.assertEqual(len(first.plans), 1)  # 没有重复建方案/重复占位

    def test_split_sizes_must_match_total(self) -> None:
        with self.assertRaises(BookingError) as ctx:
            self.service.apply(
                school_id="S1", idempotency_key="bad", people_count=50, age_band="x",
                theme="海洋馆", preferred_dates=[D1], split_sizes=[30, 30],
            )
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST")


class ConfirmationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service(max_held_plans=2)

    def test_confirm_releases_other_holds_atomically(self) -> None:
        req = self.service.apply(
            school_id="S1", idempotency_key="c1", people_count=100, age_band="x",
            theme="海洋馆", preferred_dates=[D1, D2], split_sizes=[60, 40],
        )
        held = [p for p in req.plans if p.state == PLAN_HELD]
        self.assertEqual(len(held), 2)
        confirmed = self.service.confirm(req.request_id, "S1", held[1].plan_id)
        self.assertEqual(confirmed.status, STATUS_SCHEDULED)
        self.assertEqual(confirmed.confirmed_plan_id, held[1].plan_id)
        self.assertEqual(held[0].state, PLAN_RELEASED)
        self.assertEqual(held[1].state, PLAN_CONFIRMED)
        # D1 全部释放
        self.assertEqual(self.service.session_availability("A")["confirmed"], 0)
        self.assertEqual(self.service.session_availability("B")["confirmed"], 0)
        # D2 两个场次合计确认 100
        self.assertEqual(
            self.service.session_availability("C")["confirmed"]
            + self.service.session_availability("C2")["confirmed"],
            100,
        )

    def test_confirm_is_idempotent(self) -> None:
        req = self.service.apply(
            school_id="S1", idempotency_key="ci", people_count=10, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        plan_id = req.plans[0].plan_id
        once = self.service.confirm(req.request_id, "S1", plan_id)
        twice = self.service.confirm(req.request_id, "S1", plan_id)
        self.assertEqual(once.confirmed_plan_id, twice.confirmed_plan_id)

    def test_alternative_plan_can_be_confirmed_and_releases_holds(self) -> None:
        service = self.service
        # 三个日期，仅前两个暂占；确认第三个备选方案应成功并释放前两个
        req = service.apply(
            school_id="S1", idempotency_key="alt", people_count=60, age_band="x",
            theme="海洋馆", preferred_dates=[D1, D2, D3],
        )
        alternative = next(p for p in req.plans if p.state == PLAN_ALTERNATIVE)
        confirmed = service.confirm(req.request_id, "S1", alternative.plan_id)
        self.assertEqual(confirmed.status, STATUS_SCHEDULED)
        self.assertEqual(service.session_availability("A")["held"], 0)
        self.assertEqual(service.session_availability("C")["held"], 0)
        self.assertEqual(service.session_availability("D")["confirmed"], 60)

    def test_alternative_plan_falls_back_to_sibling_session(self) -> None:
        service = self.service
        req = service.apply(
            school_id="S1", idempotency_key="alt2", people_count=60, age_band="x",
            theme="海洋馆", preferred_dates=[D1, D2, D3],
        )
        alternative = next(p for p in req.plans if p.state == PLAN_ALTERNATIVE and p.date == D3)
        # 其他学校先确认占满 D3 的 D 场，但 D3b 仍有空
        rival = service.apply(
            school_id="S2", idempotency_key="rival", people_count=60, age_band="x",
            theme="海洋馆", preferred_dates=[D3],
        )
        service.confirm(rival.request_id, "S2", rival.plans[0].plan_id)
        confirmed = service.confirm(req.request_id, "S1", alternative.plan_id)
        self.assertEqual(confirmed.status, STATUS_SCHEDULED)

    def test_alternative_plan_rejected_when_date_full(self) -> None:
        service = self.service
        req = service.apply(
            school_id="S1", idempotency_key="alt3", people_count=60, age_band="x",
            theme="海洋馆", preferred_dates=[D1, D2, D3],
        )
        alternative = next(p for p in req.plans if p.state == PLAN_ALTERNATIVE and p.date == D3)
        # 占满 D3 两个场次
        for key in ("rival1", "rival2"):
            rival = service.apply(
                school_id="S2", idempotency_key=key, people_count=60, age_band="x",
                theme="海洋馆", preferred_dates=[D3],
            )
            service.confirm(rival.request_id, "S2", rival.plans[0].plan_id)
        with self.assertRaises(BookingError) as ctx:
            service.confirm(req.request_id, "S1", alternative.plan_id)
        self.assertEqual(ctx.exception.code, "ALTERNATIVE_TAKEN")
        # 失败的确认没有破坏原有暂占
        self.assertEqual(req.plans[0].state, PLAN_HELD)
        self.assertEqual(req.plans[1].state, PLAN_HELD)


class CapacityAndWaitlistTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service(max_held_plans=1)

    def test_capacity_shortage_creates_waitlist_with_reason(self) -> None:
        # S1 占满 D1（A+B 共 120）
        self.service.apply(
            school_id="S1", idempotency_key="full", people_count=120, age_band="x",
            theme="海洋馆", preferred_dates=[D1], split_sizes=[60, 60],
        )
        req = self.service.apply(
            school_id="S2", idempotency_key="wl", people_count=30, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        self.assertEqual(req.status, STATUS_WAITLISTED)
        explanation = self.service.explanation(req.request_id)
        codes = [d["code"] for d in explanation["decisions"]]
        self.assertIn("WAITLISTED", codes)
        self.assertTrue(explanation["waitlist"])

    def test_reduction_frees_seats_and_promotes_waitlist(self) -> None:
        s1 = self.service.apply(
            school_id="S1", idempotency_key="big", people_count=60, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        self.service.confirm(s1.request_id, "S1", s1.plans[0].plan_id)
        # 此时 D1 剩余 A:0 B:60；30 人候补可直接进 B，故制造占用 B 的情况：
        s1b = self.service.apply(
            school_id="S1", idempotency_key="big2", people_count=60, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        # 第二个 60 会落到 B 并暂占
        self.service.confirm(s1b.request_id, "S1", s1b.plans[0].plan_id)

        waiter = self.service.apply(
            school_id="S2", idempotency_key="wait", people_count=30, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        self.assertEqual(waiter.status, STATUS_WAITLISTED)
        split_id = s1.splits[0].split_id
        # S1 缩减 60 -> 30，释放 30 席，触发递补
        self.service.reduce_split(s1.request_id, "S1", split_id, new_size=30)
        promoted = self.service.get_request(waiter.request_id, "S2")
        self.assertEqual(promoted.status, STATUS_PENDING_CONFIRMATION)
        new_plan = [p for p in promoted.plans if p.state == PLAN_HELD]
        self.assertEqual(len(new_plan), 1)
        explanation = self.service.explanation(waiter.request_id)
        self.assertIn("PROMOTED", [d["code"] for d in explanation["decisions"]])

    def test_full_class_cancellation_releases_all(self) -> None:
        req = self.service.apply(
            school_id="S1", idempotency_key="cc", people_count=30, age_band="x",
            theme="海洋馆", preferred_dates=[D1], split_sizes=[15, 15],
        )
        self.service.confirm(req.request_id, "S1", req.plans[0].plan_id)
        g1, g2 = [s.split_id for s in req.splits]
        self.service.reduce_split(req.request_id, "S1", g1, new_size=0)
        partial = self.service.get_request(req.request_id, "S1")
        self.assertEqual(partial.people_count, 15)
        self.service.reduce_split(req.request_id, "S1", g2)  # 整团取消
        self.assertEqual(self.service.session_availability("A")["confirmed"], 0)


class ExpiryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service(max_held_plans=1)

    def test_expired_hold_is_released_with_injected_clock(self) -> None:
        req = self.service.apply(
            school_id="S1", idempotency_key="ttl", people_count=60, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        self.assertEqual(self.service.session_availability("A")["held"], 60)
        # 未到期限不释放
        self.clock.advance(hours=23)
        self.assertEqual(self.service.sweep_expired(), [])
        self.assertEqual(self.service.session_availability("A")["held"], 60)
        # 超过期限
        self.clock.advance(hours=2)
        expired = self.service.sweep_expired()
        self.assertEqual(expired, [req.request_id])
        self.assertEqual(self.service.session_availability("A")["held"], 0)
        self.assertEqual(req.plans[0].state, PLAN_EXPIRED)

    def test_expiry_promotes_waitlisted_school(self) -> None:
        s1 = self.service.apply(
            school_id="S1", idempotency_key="e1", people_count=60, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        s2 = self.service.apply(
            school_id="S2", idempotency_key="e2", people_count=60, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        # S1 暂占 A，B 还剩 60，S2 其实可进 B；占掉 B：
        self.assertEqual([p.state for p in s2.plans], [PLAN_HELD])
        s3 = self.service.apply(
            school_id="S1", idempotency_key="e3", people_count=60, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        self.assertEqual(s3.status, STATUS_WAITLISTED)
        # S1 的 e1 超时 → s3（同校更高优先级同刻）队列验证递补触发
        self.clock.advance(hours=25)
        self.service.sweep_expired()
        self.assertEqual(self.service.session_availability("A")["held"], 60)
        refreshed = self.service.ops_get_request(s3.request_id)
        self.assertEqual(refreshed.status, STATUS_PENDING_CONFIRMATION)


class RescheduleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service(max_held_plans=1)

    def test_failed_reschedule_keeps_original(self) -> None:
        req = self.service.apply(
            school_id="S1", idempotency_key="r1", people_count=60, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        plan_id = req.plans[0].plan_id
        # D2 两个场次均被占满（120 人拆分）
        other = self.service.apply(
            school_id="S2", idempotency_key="r2", people_count=120, age_band="x",
            theme="海洋馆", preferred_dates=[D2], split_sizes=[60, 60],
        )
        self.service.confirm(other.request_id, "S2", other.plans[0].plan_id)
        with self.assertRaises(BookingError) as ctx:
            self.service.reschedule(req.request_id, "S1", [D2])
        self.assertEqual(ctx.exception.code, "RESCHEDULE_IMPOSSIBLE")
        unchanged = self.service.get_request(req.request_id, "S1")
        self.assertEqual(unchanged.plans[0].plan_id, plan_id)
        self.assertEqual(unchanged.plans[0].state, PLAN_HELD)

    def test_successful_reschedule_is_atomic(self) -> None:
        req = self.service.apply(
            school_id="S1", idempotency_key="r3", people_count=60, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        self.service.confirm(req.request_id, "S1", req.plans[0].plan_id)
        moved = self.service.reschedule(req.request_id, "S1", [D3])
        self.assertEqual(moved.status, STATUS_SCHEDULED)
        self.assertEqual(self.service.session_availability("A")["confirmed"], 0)
        self.assertEqual(self.service.session_availability("D")["confirmed"], 60)


class AccessibilityAndIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service(max_held_plans=1)

    def test_accessibility_shortage_is_explained(self) -> None:
        req = self.service.apply(
            school_id="S1", idempotency_key="acc", people_count=30, age_band="x",
            theme="海洋馆", preferred_dates=[D1], accessibility_required=True,
        )
        explanation = self.service.explanation(req.request_id)
        # 无障碍场 E 只有 20 座，30 人无法容纳
        self.assertTrue(
            any("无障碍" in d["message"] or "无场次可容纳" in d["message"] for d in explanation["decisions"])
        )

    def test_accessibility_request_uses_accessible_session(self) -> None:
        req = self.service.apply(
            school_id="S1", idempotency_key="acc-ok", people_count=20, age_band="x",
            theme="海洋馆", preferred_dates=[D1], accessibility_required=True,
        )
        self.assertEqual(req.status, STATUS_PENDING_CONFIRMATION)
        self.assertEqual(self.service.session_availability("E")["held"], 20)

    def test_school_cannot_read_others(self) -> None:
        req = self.service.apply(
            school_id="S1", idempotency_key="iso", people_count=10, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        with self.assertRaises(BookingError) as ctx:
            self.service.get_request(req.request_id, "S2")
        self.assertEqual(ctx.exception.code, "FORBIDDEN")
        self.assertEqual(self.service.list_requests("S2"), [])

    def test_ops_can_see_all_and_explain(self) -> None:
        self.service.apply(
            school_id="S1", idempotency_key="o1", people_count=10, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        self.service.apply(
            school_id="S2", idempotency_key="o2", people_count=10, age_band="x",
            theme="海洋馆", preferred_dates=[D1],
        )
        ids = {r.school_id for r in self.service.ops_list_requests()}
        self.assertEqual(ids, {"S1", "S2"})


class PriorityAndConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service(max_held_plans=1)

    def test_higher_priority_waitlist_jumps_queue(self) -> None:
        # 占满 D1 两个普通场次（40 人团连无障碍小场 E 也无法容纳，必然候补）
        full = self.service.apply(
            school_id="S1", idempotency_key="full", people_count=120, age_band="x",
            theme="海洋馆", preferred_dates=[D1], split_sizes=[60, 60],
        )
        low = self.service.apply(
            school_id="S2", idempotency_key="low", people_count=40, age_band="x",
            theme="海洋馆", preferred_dates=[D1], priority=1,
        )
        high = self.service.apply(
            school_id="S2", idempotency_key="high", people_count=40, age_band="x",
            theme="海洋馆", preferred_dates=[D1], priority=10,
        )
        self.assertEqual(low.status, STATUS_WAITLISTED)
        self.assertEqual(high.status, STATUS_WAITLISTED)
        # 普通团体满员时未占用无障碍专享小场
        self.assertEqual(self.service.session_availability("E")["held"], 0)
        waitlist = self.service.ops_waitlist()
        # 优先级高的排在队列前面
        for rows in waitlist.values():
            high_rows = [r for r in rows if r["request_id"] == high.request_id]
            low_rows = [r for r in rows if r["request_id"] == low.request_id]
            if high_rows and low_rows:
                self.assertLess(high_rows[0]["rank"], low_rows[0]["rank"])

        # 释放 40 席：高优先级先递补，低优先级继续候补
        self.service.reduce_split(full.request_id, "S1", full.splits[0].split_id, new_size=20)
        self.assertEqual(
            self.service.ops_get_request(high.request_id).status, STATUS_PENDING_CONFIRMATION
        )
        self.assertEqual(
            self.service.ops_get_request(low.request_id).status, STATUS_WAITLISTED
        )

    def test_concurrent_applications_never_oversell(self) -> None:
        import threading

        errors: list[Exception] = []

        def apply_many(school: str, start: int) -> None:
            try:
                for i in range(start, start + 10):
                    self.service.apply(
                        school_id=school, idempotency_key=f"k{i}", people_count=20,
                        age_band="x", theme="海洋馆", preferred_dates=[D1],
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=apply_many, args=("S1", 0)),
            threading.Thread(target=apply_many, args=("S2", 100)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        # D1 总容量 120（A/B；E 为无障碍专享不参与普通团）；暂占+确认绝不超过容量
        for row in self.service.ops_sessions():
            self.assertLessEqual(row["confirmed"] + row["held"], row["capacity"])


if __name__ == "__main__":
    unittest.main()
