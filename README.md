# 学校团体预约受理

本项目维护学校团体预约受理的领域约定、角色边界与样例数据，并提供一个**零第三方依赖**的
Python 服务端：维护预约意向、团体拆分、场次容量、优先级与确认期限，支撑电话/线上受理的
多方案暂占、确认、缩减、候补、改期、超时释放等流程。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/booking_service/`：预约受理服务端。
  - `models.py`：学校、场次、预约意向、分团、方案、候补、审计事件。
  - `service.py`：核心领域服务（单锁原子调整名额、候补递补、决策留痕）。
  - `clock.py`：可注入时钟（`SystemClock` / `FixedClock`），超时任务不依赖墙钟。
  - `http_app.py`：标准库实现的 HTTP 接口（学校自助 + 运营解释）。
  - `serializers.py`：领域对象 JSON 序列化。
  - `__main__.py`：服务启动入口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约、领域服务、HTTP 端到端回归测试。

## 领域规则（对应契约四项不变量）

1. **团体拆分容量**：一次意向按 `split_sizes` 拆为多个分团；装箱时校验每个场次
   `容量 − 已确认 − 待确认暂占 ≥ 分团人数`，无障碍为硬约束，且普通团优先使用普通场，
   为刚需团体保留无障碍席位。
2. **多方案有限暂占**：多个备选日期各生成一个方案，默认仅前 2 个（可配置）暂占名额并设
   确认期限；其余方案为 `alternative`（不占名额、先到先得，确认时原子复核容量）。
3. **候补递补一致性**：容量不足自动进入按 **(优先级降序, 登记时间升序)** 排序的场次候补
   队列；缩减、取消、超时、改期释放名额时原子触发递补；队头不满足时不跳过，保证公平。
4. **租户预约隔离**：学校接口凭 `X-School-ID` 只能查看/操作本校意向，跨校访问返回 403
   且不泄露意向存在性；运营接口凭 `X-Admin-Key` 可查看全部并获取拒绝/递补原因链。

其他保证：

- **确认/缩减/候补/改期/重复请求全部原子**：所有名额调整在同一把可重入锁内完成；
  改期采用“先校验新方案、可行后才释放旧名额”，失败时原排定不变。
- **幂等**：申请按 `(school_id, idempotency_key)` 去重；对已确认方案的重复确认直接返回。
- **超时**：暂占到期由可注入时钟判定；`sweep_expired()` 可手动/定时调用，测试用
  `FixedClock` 确定性推进。
- **可解释**：每次拒绝、候补、递补受阻/成功都写入 `decisions` 原因链和全局审计事件流。

## HTTP 接口

学校接口（请求头 `X-School-ID: <学校ID>`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/schools/applications` | 提交意向（人数、年龄段、主题、无障碍、备选日期、拆分、优先级、幂等键） |
| GET  | `/api/schools/requests` | 列出本校全部意向 |
| GET  | `/api/schools/requests/{id}` | 查看本校单个意向 |
| POST | `/api/schools/requests/{id}/confirm` | 确认某方案（其余暂占原子释放，触发递补） |
| POST | `/api/schools/requests/{id}/reduce` | 缩减分团（`new_size=0`/缺省表示整班取消） |
| POST | `/api/schools/requests/{id}/cancel` | 取消整个意向 |
| POST | `/api/schools/requests/{id}/reschedule` | 原子改期（新日期不可行则原排定不变） |

运营接口（请求头 `X-Admin-Key: <密钥>`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/ops/schools` | 登记学校 |
| POST | `/api/ops/sessions` | 开放场次（容量、无障碍标记） |
| GET  | `/api/ops/sessions` | 场次容量/已确认/暂占/可售 |
| GET  | `/api/ops/requests` | 全部意向 |
| GET  | `/api/ops/requests/{id}/explanation` | 拒绝、候补、递补原因链 |
| GET  | `/api/ops/waitlist` | 各场次候补队列与排名 |
| GET  | `/api/ops/audit` | 审计事件流 |
| POST | `/api/ops/sweep` | 执行一次超时扫描 |

## 使用示例

```bash
python3 -m booking_service --host 127.0.0.1 --port 8080 --admin-key ops-key
```

```python
from datetime import timedelta
from booking_service import BookingService, FixedClock
from datetime import datetime, timezone

service = BookingService(clock=FixedClock(datetime(2026, 10, 4, tzinfo=timezone.utc)),
                         confirm_ttl=timedelta(hours=24), max_held_plans=2)
service.register_school("S1", "第一中学")
service.create_session("A", "2026-10-20", "上午", "海洋馆", 60, accessible=True)
service.create_session("B", "2026-10-20", "下午", "海洋馆", 60)

req = service.apply(
    school_id="S1", idempotency_key="call-001",
    people_count=100, age_band="12-14", theme="海洋馆",
    preferred_dates=["2026-10-20", "2026-10-21"],
    split_sizes=[60, 40], accessibility_required=True,
)
service.confirm(req.request_id, "S1", req.plans[0].plan_id)
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
