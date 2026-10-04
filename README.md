# 学校团体预约受理

研学季学校团体预约受理服务端。电话受理人员录入学校提交的**人数、年龄段、主题、无障碍需求**与多个备选日期，系统维护预约意向、团体拆分、场次容量、候补优先级与确认期限，支持确认、缩减、候补、改期与重复请求的原子名额调整，并向运营提供拒绝与递补原因解释。

## 设计要点（对应领域契约四条不变量）

| 不变量 | 实现 |
| --- | --- |
| 团体拆分容量 | 人数按单组上限（默认 20 人/组）拆分为若干讲解组；容量账本对每个场次分账记录「暂占」与「已确认」，所有调整在同一把锁内校验后原子提交。 |
| 多方案有限暂占 | 一次申请可提交多个备选场次，满足无障碍且有名额的方案才暂占，最多暂占 3 个（可配置）；落选方案不占位并保留拒绝原因。 |
| 候补递补一致性 | 名额释放时原子递补：无障碍刚需学校优先，同级按申请时间先到先得、再按预约编号；队首团体过大塞不下时跳过并继续考察较小团体，跳过/递补均留痕。 |
| 租户预约隔离 | 学校侧接口必须携带 `X-School-Id`，只能查看与操作本校预约；越权访问与资源不存在统一返回 404，避免存在性泄漏。 |

状态机：`筹备 → 待确认 → 已排定 → 执行中 → 已结算`，旁路状态 `候补中`、`已取消`。确认期限超时（默认 24 小时）自动释放暂占并递补；候补条目有 TTL（默认 7 天）。所有超时判断走**可注入时钟**（`SystemClock` / `FakeClock`），任何读写操作前都会自动处理到期事项。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/booking/`：预约服务端。
  - `models.py`：场次、预约意向、备选方案、候补条目、决策留痕。
  - `service.py`：核心领域服务（容量账本、优先级、原子流转、超时与递补）。
  - `clock.py`：可注入时钟（`SystemClock` / `FakeClock`）。
  - `http_app.py`：标准库 HTTP 接口（零第三方依赖）。
  - `errors.py`：稳定错误码与 HTTP 状态映射。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、领域服务（27 例）与 HTTP 端到端（含完整受理工作流）测试。

## HTTP 接口

学校侧（请求头 `X-School-Id: <学校标识>`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/sessions` | 查看场次与剩余名额 |
| POST | `/api/requests` | 提交预约意向（可带 `idempotency_key` 幂等键，重放返回 200 且不重复占位） |
| GET | `/api/requests` | 列出本校全部预约 |
| GET | `/api/requests/{id}` | 查看本校某预约（含方案、候补、决策链） |
| POST | `/api/requests/{id}/confirm` | 确认某暂占方案，释放同申请其他暂占 |
| POST | `/api/requests/{id}/reduce` | 部分班级取消，缩减人数并释放容量、触发递补 |
| POST | `/api/requests/{id}/cancel` | 取消预约，释放全部名额 |
| POST | `/api/requests/{id}/reschedule` | 改期（原子释放旧方案、暂占新方案，全满可转候补） |
| POST | `/api/requests/{id}/waitlist` | 加入某场次候补（body 带 `session_id`） |
| POST | `/api/requests/{id}/waitlist/{sessionId}` | 退出该场次候补 |

运营侧（请求头 `X-Operator-Token`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/sessions` | 新建场次（容量、无障碍设施） |
| GET | `/admin/sessions` | 全部场次用量 |
| GET | `/admin/requests` | 全部学校的预约 |
| GET | `/admin/requests/{id}/explain` | 决策解释：每方案接受/拒绝原因、候补去向、完整决策链 |
| GET | `/admin/waitlist?session_id=` | 候补队列（按优先级排序） |
| POST | `/admin/expire` | 手动驱动到期处理（生产可由定时任务调用） |
| POST | `/admin/requests/{id}/execute` | 标记到场：已排定 → 执行中 |
| POST | `/admin/requests/{id}/settle` | 结算：执行中 → 已结算 |

## 本地运行

```bash
PYTHONPATH=src BOOKING_OPS_TOKEN=change-me python3 -m booking
# 预约受理服务监听 http://127.0.0.1:8080
```

快速体验：

```bash
curl -s -XPOST localhost:8080/admin/sessions -H 'X-Operator-Token: change-me' \
  -H 'Content-Type: application/json' \
  -d '{"id":"S0501","date":"2026-05-01","label":"上午","capacity":40,"accessibility":[]}'

curl -s -XPOST localhost:8080/api/requests -H 'X-School-Id: SCH001' \
  -H 'Content-Type: application/json' \
  -d '{"school_name":"阳光小学","contact":"王老师","headcount":45,
       "grades":"3-4年级","theme":"自然科普","accessibility":[],
       "candidate_session_ids":["S0501"],
       "idempotency_key":"call-2026-10-04-01"}'
```

## 验证

```bash
python3 -m unittest discover -s tests -v     # 契约 + 服务 + HTTP，共 39 个用例
python3 -m compileall -q src tools tests     # 编译检查
python3 tools/check_contract.py domain/contract.json
```
