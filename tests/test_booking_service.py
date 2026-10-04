"""核心预约服务测试：容量、暂占、候补、超时、租户隔离、决策解释。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from booking import BookingService, FakeClock
from booking.errors import DomainError

T0 = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)


def make_service(capacity: int = 40, **kwargs) -> tuple[BookingService, FakeClock]:
    clock = FakeClock(T0)
    service = BookingService(
        clock=clock,
        confirm_window=timedelta(hours=24),
        waitlist_ttl=timedelta(days=7),
        max_group_size=20,
        max_holds=3,
        **kwargs,
    )
    # 三个备选日期 + 一个无障碍场次
    service.add_session("S0501", "2026-05-01", "上午", capacity)
    service.add_session("S0502", "2026-05-02", "上午", capacity)
    service.add_session("S0503", "2026-05-03", "上午", capacity)
    service.add_session("S0504", "2026-05-04", "上午", capacity, {"电梯"})
    return service, clock


def apply(service, school="SCH1", candidates=("S0501", "S0502", "S0503"),
          headcount=30, accessibility=None, key=None, waitlist=False):
    return service.apply(
        school_id=school,
        school_name=f"{school}小学",
        contact="王老师 13800000000",
        headcount=headcount,
        grades="3-4年级",
        theme="自然科普",
        accessibility=accessibility,
        candidate_session_ids=list(candidates),
        idempotency_key=key,
        waitlist_if_full=waitlist,
    )


class ApplyAndHoldTest(unittest.TestCase):
    def test_多方案暂占_团体拆分_容量账本(self) -> None:
        service, _ = make_service(capacity=60)
        req, replayed = apply(service, headcount=45)  # 45人 → 3个讲解组
        self.assertFalse(replayed)
        self.assertEqual(req.group_count, 3)
        held = req.held_options()
        self.assertEqual(len(held), 3)
        self.assertEqual(req.status, "待确认")
        # 三个场次各占 45
        for sid in ("S0501", "S0502", "S0503"):
            self.assertEqual(service._used(sid), 45)
        # 申请决策留痕
        self.assertEqual(req.decisions[0].trigger, "申请")

    def test_拒绝方案不占位且给出原因(self) -> None:
        service, _ = make_service()
        # S0501 只剩 10：先占 30
        apply(service, school="A", candidates=("S0501",), headcount=30)
        req, _ = apply(service, school="B",
                       candidates=("S0501", "S0502"), headcount=30)
        s1 = req.options["S0501"]
        s2 = req.options["S0502"]
        self.assertEqual(s1.state, "已拒绝")
        self.assertIn("剩余名额10不足30人", s1.reason)
        self.assertEqual(s2.state, "已暂占")
        # 被拒方案没有占用容量
        self.assertEqual(service._used("S0501"), 30)

    def test_无障碍需求不匹配拒绝(self) -> None:
        service, _ = make_service()
        req, _ = apply(service, candidates=("S0501", "S0504"),
                       accessibility=["电梯"])
        self.assertEqual(req.options["S0501"].state, "已拒绝")
        self.assertIn("电梯", req.options["S0501"].reason)
        self.assertEqual(req.options["S0504"].state, "已暂占")

    def test_暂存方案数量上限(self) -> None:
        service, _ = make_service()
        req, _ = apply(service,
                       candidates=("S0501", "S0502", "S0503", "S0504"),
                       headcount=10)
        self.assertEqual(len(req.held_options()), 3)
        self.assertEqual(req.options["S0504"].state, "已拒绝")
        self.assertIn("暂占方案已达上限3个", req.options["S0504"].reason)

    def test_全部落选且不候补时为筹备态(self) -> None:
        service, _ = make_service()
        apply(service, school="A", candidates=("S0501",), headcount=40)
        req, _ = apply(service, school="B", candidates=("S0501",), headcount=10)
        self.assertEqual(req.status, "筹备")
        self.assertEqual(req.held_options(), [])

    def test_参数校验(self) -> None:
        service, _ = make_service()
        with self.assertRaises(DomainError) as ctx:
            apply(service, headcount=0)
        self.assertEqual(ctx.exception.http_status, 400)
        with self.assertRaises(DomainError):
            apply(service, candidates=())


class IdempotencyTest(unittest.TestCase):
    def test_重复请求返回同一预约且不重复占位(self) -> None:
        service, _ = make_service()
        r1, replay1 = apply(service, key="phone-call-001", headcount=30)
        r2, replay2 = apply(service, key="phone-call-001", headcount=30)
        self.assertFalse(replay1)
        self.assertTrue(replay2)
        self.assertEqual(r1.id, r2.id)
        self.assertEqual(service._used("S0501"), 30)  # 而非 60
        self.assertEqual(service._used("S0502"), 30)
        # 重复请求留痕
        self.assertTrue(any(d.trigger == "重复请求" for d in r2.decisions))

    def test_幂等键按学校隔离(self) -> None:
        service, _ = make_service()
        r1, _ = apply(service, school="SCH1", key="K", candidates=("S0502",))
        r2, replay = apply(service, school="SCH2", key="K", candidates=("S0502",))
        self.assertFalse(replay)
        self.assertNotEqual(r1.id, r2.id)


class ConfirmReduceCancelTest(unittest.TestCase):
    def test_确认释放其他暂占并触发递补(self) -> None:
        service, _ = make_service()
        # A 在三个场次暂占
        a, _ = apply(service, school="A", headcount=20)
        # B 候补 S0501（A 占 20，剩 20，B 要 30 → 候补）
        b, _ = apply(service, school="B", candidates=("S0501",), headcount=30)
        service.join_waitlist(b.id, "S0501", "B")
        self.assertEqual(b.status, "候补中")

        service.confirm(a.id, "S0502", "A")
        self.assertEqual(a.status, "已排定")
        self.assertEqual(service._used("S0501"), 30)  # 释放后由 B 递补暂占
        self.assertEqual(service._used("S0502"), 20)  # A 确认占用
        self.assertEqual(service._used("S0503"), 0)
        # B 被递补为 S0501 暂占
        b2 = service.get_request(b.id, "B")
        self.assertEqual(b2.status, "待确认")
        self.assertEqual(b2.options["S0501"].state, "已暂占")
        wl = next(iter(b2.waitlist.values()))
        self.assertEqual(wl.state, "已递补")
        # 递补原因写入决策链
        self.assertTrue(any("候补递补" in d.message for d in b2.decisions))

    def test_确认后候补全部取消(self) -> None:
        service, _ = make_service()
        # S0503 先占满，A 才能对其候补
        x, _ = apply(service, school="X", candidates=("S0503",), headcount=40)
        service.confirm(x.id, "S0503", "X")
        a, _ = apply(service, school="A", candidates=("S0501", "S0502"), headcount=20)
        service.join_waitlist(a.id, "S0503", "A")
        service.confirm(a.id, "S0501", "A")
        self.assertEqual(a.active_waitlist(), [])

    def test_不能确认非暂占方案(self) -> None:
        service, _ = make_service()
        a, _ = apply(service, headcount=10)
        with self.assertRaises(DomainError):
            service.confirm(a.id, "S0504", "SCH1")  # 不在方案里
        service.confirm(a.id, "S0501", "SCH1")
        with self.assertRaises(DomainError):
            service.confirm(a.id, "S0502", "SCH1")  # 已排定
        # 越权学校同样不能操作
        with self.assertRaises(DomainError):
            service.confirm(a.id, "S0502", "SCH-X")

    def test_待确认缩减同步缩减所有暂占并递补(self) -> None:
        service, _ = make_service()
        a, _ = apply(service, school="A", candidates=("S0501", "S0502"), headcount=40)
        # S0501 已满（40/40），B 候补 10 人
        b, _ = apply(service, school="B", candidates=("S0501",), headcount=10)
        service.join_waitlist(b.id, "S0501", "B")
        service.reduce(a.id, 30, "A")
        # A 暂占 30 + B 递补暂占 10
        self.assertEqual(service._used("S0501"), 40)
        self.assertEqual(service._used("S0502"), 30)
        b2 = service.get_request(b.id, "B")
        self.assertEqual(b2.options["S0501"].state, "已暂占")
        self.assertEqual(a.group_count, 2)  # 30人/20 → 2组

    def test_已排定缩减释放确认容量并递补(self) -> None:
        service, _ = make_service()
        a, _ = apply(service, school="A", candidates=("S0501",), headcount=40)
        service.confirm(a.id, "S0501", "A")
        b, _ = apply(service, school="B", candidates=("S0501",), headcount=15)
        service.join_waitlist(b.id, "S0501", "B")
        service.reduce(a.id, 20, "A")
        # A 确认 20 + B 递补暂占 15
        self.assertEqual(service._used("S0501"), 35)
        b2 = service.get_request(b.id, "B")
        self.assertEqual(b2.status, "待确认")
        self.assertEqual(b2.options["S0501"].seats, 15)

    def test_缩减不能增员(self) -> None:
        service, _ = make_service()
        a, _ = apply(service, headcount=30)
        with self.assertRaises(DomainError):
            service.reduce(a.id, 35, "SCH1")

    def test_取消释放全部名额并递补(self) -> None:
        service, _ = make_service()
        a, _ = apply(service, school="A", candidates=("S0501",), headcount=40)
        service.confirm(a.id, "S0501", "A")
        b, _ = apply(service, school="B", candidates=("S0501",), headcount=40)
        service.join_waitlist(b.id, "S0501", "B")
        service.cancel(a.id, "A", "部分班级取消")
        self.assertEqual(a.status, "已取消")
        self.assertEqual(service._used("S0501"), 40)  # B 递补占位
        b2 = service.get_request(b.id, "B")
        self.assertEqual(b2.status, "待确认")


class WaitlistPriorityTest(unittest.TestCase):
    def test_无障碍刚需优先递补(self) -> None:
        service, _ = make_service()
        # A 占满有电梯设施的 S0504
        a, _ = apply(service, school="A", candidates=("S0504",), headcount=40)
        service.confirm(a.id, "S0504", "A")
        # 先到的无刚需学校 B
        b, _ = apply(service, school="B", candidates=("S0504",), headcount=10)
        service.join_waitlist(b.id, "S0504", "B")
        # 后到但有电梯刚需的学校 C（S0504 满足其需求）
        c, _ = apply(service, school="C", candidates=("S0504",), headcount=10,
                     accessibility=["电梯"])
        service.join_waitlist(c.id, "S0504", "C")
        service.reduce(a.id, 20, "A")  # 释放 20 个名额
        b2 = service.get_request(b.id, "B")
        c2 = service.get_request(c.id, "C")
        # C 刚需优先递补，B 随后递补（20 个名额够两者）
        self.assertEqual(c2.options["S0504"].state, "已暂占")
        self.assertEqual(b2.options["S0504"].state, "已暂占")
        overview = service.waitlist_overview("S0504")
        c_entry = next(e for e in overview if e["request_id"] == c.id)
        self.assertEqual(c_entry["base_priority"], 0)

    def test_候补不满足无障碍需求时拒绝(self) -> None:
        service, _ = make_service()
        a, _ = apply(service, school="A", candidates=("S0501",), headcount=40,
                     accessibility=["电梯"])
        # S0501 无电梯：申请时方案已被拒；尝试候补同样被拒
        with self.assertRaises(DomainError) as ctx:
            service.join_waitlist(a.id, "S0501", "A")
        self.assertEqual(ctx.exception.http_status, 400)

    def test_队首团体过大被跳过_较小团体递补并留痕(self) -> None:
        service, _ = make_service()
        a, _ = apply(service, school="A", candidates=("S0501",), headcount=40)
        service.confirm(a.id, "S0501", "A")
        big, _ = apply(service, school="BIG", candidates=("S0501",), headcount=30)
        service.join_waitlist(big.id, "S0501", "BIG")
        small, _ = apply(service, school="SML", candidates=("S0501",), headcount=10)
        service.join_waitlist(small.id, "S0501", "SML")
        service.reduce(a.id, 30, "A")  # 只释放 10 个名额
        big2 = service.get_request(big.id, "BIG")
        small2 = service.get_request(small.id, "SML")
        self.assertEqual(small2.options["S0501"].state, "已暂占")
        # 大队仍在候补，且有跳过原因留痕
        self.assertTrue(any(e.state == "候补中" for e in big2.waitlist.values()))
        self.assertTrue(any("跳过" in d.message for d in big2.decisions))

    def test_退出候补(self) -> None:
        service, _ = make_service()
        # S0502 占满，A 申请落空后进候补
        x, _ = apply(service, school="X", candidates=("S0502",), headcount=40)
        service.confirm(x.id, "S0502", "X")
        a, _ = apply(service, school="A", candidates=("S0502",), headcount=20)
        self.assertEqual(a.status, "筹备")
        service.join_waitlist(a.id, "S0502", "A")
        service.leave_waitlist(a.id, "S0502", "A")
        self.assertEqual(a.status, "筹备")
        self.assertEqual(service.waitlist_overview("S0502")[0]["state"], "已取消")


class RescheduleTest(unittest.TestCase):
    def test_已排定改到空位直接重新确认(self) -> None:
        service, _ = make_service()
        a, _ = apply(service, school="A", candidates=("S0501",), headcount=20)
        service.confirm(a.id, "S0501", "A")
        service.reschedule(a.id, ["S0502"], "A")
        self.assertEqual(a.status, "已排定")
        self.assertEqual(a.confirmed_option().session_id, "S0502")
        self.assertEqual(service._used("S0501"), 0)
        self.assertEqual(service._used("S0502"), 20)

    def test_改期释放名额触发他人递补(self) -> None:
        service, _ = make_service()
        a, _ = apply(service, school="A", candidates=("S0501",), headcount=40)
        service.confirm(a.id, "S0501", "A")
        b, _ = apply(service, school="B", candidates=("S0501",), headcount=40)
        service.join_waitlist(b.id, "S0501", "B")
        service.reschedule(a.id, ["S0502"], "A")
        b2 = service.get_request(b.id, "B")
        self.assertEqual(b2.status, "待确认")
        self.assertEqual(service._used("S0501"), 40)  # B 递补

    def test_改期新场次全满自动候补(self) -> None:
        service, _ = make_service()
        x, _ = apply(service, school="X", candidates=("S0502",), headcount=40)
        service.confirm(x.id, "S0502", "X")
        a, _ = apply(service, school="A", candidates=("S0501",), headcount=20)
        service.confirm(a.id, "S0501", "A")
        service.reschedule(a.id, ["S0502"], "A")
        self.assertEqual(a.status, "候补中")
        self.assertEqual(a.active_waitlist()[0].session_id, "S0502")
        # 原确认已释放
        self.assertEqual(service._used("S0501"), 0)

    def test_改期清理旧候补(self) -> None:
        service, _ = make_service()
        x, _ = apply(service, school="X", candidates=("S0503",), headcount=40)
        service.confirm(x.id, "S0503", "X")
        a, _ = apply(service, school="A", candidates=("S0501",), headcount=20)
        service.join_waitlist(a.id, "S0503", "A")
        service.reschedule(a.id, ["S0502"], "A")
        self.assertTrue(all(e.state == "已取消" for e in a.waitlist.values()))


class TimeoutTest(unittest.TestCase):
    def test_确认期限超时释放暂占并递补(self) -> None:
        service, clock = make_service()
        a, _ = apply(service, school="A", candidates=("S0501",), headcount=40)
        b, _ = apply(service, school="B", candidates=("S0501",), headcount=40)
        service.join_waitlist(b.id, "S0501", "B")
        # 未到期限：无动作
        clock.advance(hours=23, minutes=59)
        self.assertEqual(service.expire_due(), [])
        self.assertEqual(a.status, "待确认")
        # 到点：A 取消释放，B 递补
        clock.advance(minutes=2)
        affected = service.expire_due()
        self.assertIn(a.id, affected)
        a2 = service.get_request(a.id, "A")
        self.assertEqual(a2.status, "已取消")
        b2 = service.get_request(b.id, "B")
        self.assertEqual(b2.status, "待确认")
        self.assertEqual(b2.options["S0501"].state, "已暂占")
        self.assertEqual(service._used("S0501"), 40)

    def test_递补获得新的确认期限_再超时继续传递(self) -> None:
        service, clock = make_service()
        a, _ = apply(service, school="A", candidates=("S0501",), headcount=40)
        b, _ = apply(service, school="B", candidates=("S0501",), headcount=40)
        service.join_waitlist(b.id, "S0501", "B")
        clock.advance(hours=24)
        service.expire_due()  # A 释放，B 递补，新期限 = T0+48h
        self.assertEqual(service.get_request(b.id, "B").status, "待确认")
        clock.advance(hours=23)
        service.expire_due()
        self.assertEqual(service.get_request(b.id, "B").status, "待确认")
        clock.advance(hours=2)
        service.expire_due()
        self.assertEqual(service.get_request(b.id, "B").status, "已取消")
        self.assertEqual(service._used("S0501"), 0)

    def test_候补TTL超时失效(self) -> None:
        service, clock = make_service()
        # S0501 占满，A 申请落选后进入候补（无暂占、无确认期限）
        x, _ = apply(service, school="X", candidates=("S0501",), headcount=40)
        service.confirm(x.id, "S0501", "X")
        a, _ = apply(service, school="A", candidates=("S0501",), headcount=20)
        service.join_waitlist(a.id, "S0501", "A")
        self.assertEqual(a.status, "候补中")
        clock.advance(days=7)
        service.expire_due()
        self.assertEqual(a.status, "已取消")
        self.assertEqual(next(iter(a.waitlist.values())).state, "已失效")

    def test_任何操作前自动处理到期(self) -> None:
        service, clock = make_service()
        a, _ = apply(service, candidates=("S0501",), headcount=10)
        clock.advance(hours=25)
        # 不显式调用 expire_due，查询也会触发超时处理
        self.assertEqual(service.list_my_requests("SCH1")[0]["status"], "已取消")


class TenantIsolationTest(unittest.TestCase):
    def test_学校只能看自己的预约(self) -> None:
        service, _ = make_service()
        a, _ = apply(service, school="SCH1")
        with self.assertRaises(DomainError) as ctx:
            service.get_request(a.id, "SCH2")
        self.assertEqual(ctx.exception.http_status, 404)
        ids = {r["id"] for r in service.list_my_requests("SCH1")}
        self.assertEqual(ids, {a.id})
        self.assertEqual(service.list_my_requests("SCH2"), [])

    def test_越权操作返回404且不改变名额(self) -> None:
        service, _ = make_service()
        a, _ = apply(service, school="SCH1", headcount=40)
        with self.assertRaises(DomainError):
            service.confirm(a.id, "S0501", "SCH2")
        self.assertEqual(service._used("S0501"), 40)


class LifecycleTest(unittest.TestCase):
    def test_已排定执行中已结算流转(self) -> None:
        service, _ = make_service()
        a, _ = apply(service, candidates=("S0501",), headcount=10)
        service.confirm(a.id, "S0501", "SCH1")
        service.mark_in_session(a.id)
        self.assertEqual(a.status, "执行中")
        service.mark_settled(a.id)
        self.assertEqual(a.status, "已结算")
        with self.assertRaises(DomainError):
            service.cancel(a.id, "SCH1")  # 已结算不可取消
        # 状态跳跃被拒
        b, _ = apply(service, school="B", candidates=("S0502",), headcount=10)
        with self.assertRaises(DomainError):
            service.mark_settled(b.id)

    def test_运营接口不受租户隔离限制(self) -> None:
        service, _ = make_service()
        a, _ = apply(service, school="SCH1")
        # 运营无需学校身份即可查看
        self.assertTrue(service.explain(a.id)["request_id"] == a.id)


class ExplainTest(unittest.TestCase):
    def test_运营解释包含拒绝与递补原因(self) -> None:
        service, _ = make_service()
        apply(service, school="X", candidates=("S0501",), headcount=30)
        req, _ = apply(service, school="B",
                       candidates=("S0501", "S0502"), headcount=30)
        explanation = service.explain(req.id)
        rejected = next(o for o in explanation["options"] if o["session_id"] == "S0501")
        self.assertIn("剩余名额10不足30人", rejected["reason"])
        self.assertIn("used", rejected["session"])
        triggers = {d["trigger"] for d in explanation["decision_chain"]}
        self.assertIn("申请", triggers)


if __name__ == "__main__":
    unittest.main()
