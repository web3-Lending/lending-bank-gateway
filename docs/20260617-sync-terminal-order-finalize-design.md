# lending-bank-gateway · 同步优先终态收口改造设计（V2）

> 2026-06-17 · 分支 `fix/gateway-sync-terminal` · 经 codex 对抗评审（NEEDS-ATTENTION → V2 收口）后定稿

## 1. 背景

上游 wedap-adapter 写接口（放款 DSB / 还款 RPY / 归集 CLT / 分发 DST）是**同步优先**：包在 `LoanAsyncExecutor.executeWithTimeout(5s)`——

- **≤5s 完成** → 同步 HTTP 返终态 `txnStatus=SUCCESS/FAILED`；
- **>5s** → 同步 HTTP 返 `PROCESSING`，后台继续；
- **关键**：`onComplete`（`notifyTransactionCompleted` 回调）在 routeCall 完成即触发——**≤5s 同步成功也发回调、>5s 后台完成也发回调**。即 ≤5s 时「HTTP 同步终态 + 异步回调」**双出口都到**。

### 当前 gateway 缺陷

`app/services/submit.py:109` 外呼成功后 `new_status` **硬编 `SUBMITTED`**，不读 wedap 同步终态 `txnStatus`（只塞进响应体 `:111`）；order 终态只靠异步回调 `_after_ingest` 推进。后果：

- ≤5s 已 `SUCCESS`、钱已动、9000 已据透传核销，但 gateway order 仍 `SUBMITTED`；
- **回调一丢 → order 永卡 `SUBMITTED`**，BANK-RECON / status 查询读它 → 误判在途、SLA 假超时。

## 2. codex 评审暴露的核心问题（V1 → V2）

V1（仅「同步终态推进 order」）被 codex 判 NEEDS-ATTENTION，根因：**同步 / 回调 / 兜底 worker 三条写路径没有形成同一套「状态推进 + 明细完整 + lifecycle 转发」的原子/幂等模型**。8 条 finding（已对照真代码核验全部成立）：

| 级别 | Finding | 证据 |
|---|---|---|
| HIGH-1 | `submit.py` tx2 是**无锁 blind UPDATE**，开放 `ACCEPTED→SUCCEEDED` 后回调先聚合 SUCCEEDED、tx2 再盲写覆盖 → **终态倒退** | `submit.py:140-147` 无 status 守卫/无 FOR UPDATE；`legs.py:61` 回调侧 FOR UPDATE |
| HIGH-2 | lifecycle 转发**只在回调 `_after_ingest` enqueue**；同步 SUCCESS / reconcile 都不入 outbox → **回调真丢，lifecycle 永远收不到终态** | `main.py:117` 仅此处 `enqueue_forward`；`submit.py:148` tx2 无；`legs.py:171` `sync_legs_for` 无 |
| HIGH-3 | 同步置 SUCCEEDED 但 legs 空 → `apply_legs:143 if not all_legs: return` 静默不推进/不告警；worker 只扫非终态 → **终态空明细单被永久排除** | `legs.py:143`；`fiat_vault.py` 查不到流水 |
| MED-1 | `sync_legs_for` 只调 `/composite-transactions/{biz}/steps`，未证明单 leg DSB/RPY/CLT/DST 都拿得到明细；CLT 无 status 端点 → worker 兜底依赖未验证契约 | `wedap.py:160/127`；`bank_fund.py` 无 collect status |
| MED-2 | 全局开 `ACCEPTED→终态/PROCESSING` 放大并发覆盖，须配 CAS/锁；`SUCCEEDED→FAILED` 保持非法正确（兜住同步成功但 leg 失败的分歧） | `states.py:27`；`callbacks.py` |
| MED-3 | 只同步写 `finalized_at`；callback/reconcile 聚合到终态**不写 finalized_at/audit** → 来源/时间线不可追 | `txn.py:39`；`legs.py:149`；`submit.py:156` |
| LOW-1 | 已有 `recon_worker.py`（对账文件摄取），新 worker 再叫 recon 混淆；3 worker 共享池 | `recon_worker.py`；`config.py:46` |
| LOW-2 | 现有测试断言 `ACCEPTED→SUCCEEDED` 非法 + submit→SUBMITTED + 100% gate；`finalized_via` 需 migration | `tests/domain/test_states.py`；`tests/services/test_submit.py` |

## 3. V2 设计：三路径统一收口模型

核心：抽**统一终态收口** + **CAS 防倒退** + **转发不分叉** + **明细与父单终态解耦**。

### 3.1 状态机（`app/domain/states.py`）

`_ALLOWED[ACCEPTED]` 增加 `SUCCEEDED`、`PROCESSING`（保留 `SUBMITTED/RESULT_UNKNOWN/FAILED/CANCELLED`）。**`SUCCEEDED→FAILED` 保持非法**（兜住「同步 SUCCESS 但 leg 实为 FAILED」的分歧——回调聚合 FAILED 时 `assert_transition` 抛 `IllegalTransition` → `LegsSyncIncomplete` → 升级告警）。

### 3.2 submit tx2 改 CAS（HIGH-1 / MED-2）

`submit.py` 事务2不再 blind UPDATE。改为条件 UPDATE + rowcount：

```
UPDATE bank_txn_order SET status=:new, submitted_at=:now [+ 终态字段]
WHERE tenant_id=:t AND biz_seq_no=:b AND status='ACCEPTED'
```

- `rowcount == 1`：order 仍 ACCEPTED，正常按 wedap txnStatus 映射推进。
- `rowcount == 0`：order 已被回调/worker 推进到更强态 → **只补 `record_response` + audit，不覆盖**。

wedap txnStatus 映射（仅 HTTP 200 成功分支；大小写归一；保留原始 payload 供审计）：

| wedap txnStatus | order 目标态 | 终态? |
|---|---|---|
| `SUCCESS` | `SUCCEEDED` | 是 |
| `FAILED` | `FAILED` | 是 |
| `PROCESSING` | `PROCESSING` | 否 |
| 缺省 / 未知 | `SUBMITTED` | 否（保守，等回调/worker） |

> 4xx/WedapError→`FAILED`、timeout/5xx→`RESULT_UNKNOWN` 分类**不变**（`submit.py:114-133`），同步映射只动 HTTP 200 成功分支。

### 3.3 统一 `finalize_order` 收口 helper（HIGH-2 / MED-3）

新增一个在**调用方事务内**执行的收口函数，三路径共用：

```
finalize_order_in_session(session, *, tenant_id, biz_seq_no, target_status, source, forward_payload):
    # 1. CAS/守卫推进 status（只前进不回退；已达更强终态则跳过）
    # 2. 终态时写 finalized_at + finalized_via=source(SYNC|CALLBACK|RECONCILE)
    # 3. write_audit(action=ORDER_<status>, source)
    # 4. 终态时 enqueue_forward(target=lifecycle, dedup_key=稳定业务键)
```

- **同步终态**（submit tx2 SUCCESS/FAILED）、**回调聚合到终态**（`_after_ingest`）、**reconcile 推进到终态** 全部走它 → 同步/兜底终态也转发 lifecycle，不再回调独占。
- **转发 dedup_key 改业务稳定键** `fwd-{tenant_id}-{biz_seq_no}-{terminal_status}`（替代 `fwd-{request_id}`）：同一 biz 无论哪条路径转发，dedup_key 一致 → outbox 去重 → lifecycle 只收一次；且该 key 作下游 X-Request-Id（`outbox.py:128`）保证下游幂等。**非终态不转发**（PROCESSING/SUBMITTED 不 enqueue）。
- 现有回调路径 `_after_ingest` 的 `apply_legs_in_session` 聚合后，终态收口改调本 helper（含 finalized_at/audit/forward），替代当前「只 enqueue 原始 body」。

### 3.4 明细完整性与父单终态解耦（HIGH-3）

- `apply_legs_in_session` 空 steps **不再 silent `return`**：记录可重试/告警标记（不静默吞）。
- **order_reconcile worker 扫描集**加入「**终态（SUCCEEDED/FAILED）但 legs 缺失**」的单 → 补拉 steps 落明细（父单态已终，仅补 ledger）。
- 同步置终态后，依赖 reconcile worker 异步补 legs（不阻塞同步响应）。

### 3.5 order_reconcile worker（HIGH-2 / HIGH-3 / MED-1 / LOW-1）

新增 `app/workers/order_reconcile_worker.py`（**命名区别于现有对账摄取 `recon_worker.py`**），配置前缀 `order_reconcile_*`：

- 扫描两类（实现：均按 `created_at`/`finalized_at` 时间窗，非 `submitted_at`）：① 非终态 `{ACCEPTED, SUBMITTED, PROCESSING, RESULT_UNKNOWN}` 且 `created_at` 在 `max_age`~`stale_after` 窗内；② 终态但 legs 缺失，**且 `finalized_at` 在 `leg_backfill` 短窗内**（默认 1h，超窗放弃补拉，防 CLT/无 composite 明细的终态单每轮空 steps 热重试，codex MED）。
- **拉取（composite-only）**（MED-1）：`sync_legs_for` 统一调 `/composite-transactions/{biz}/steps`（未做 biz_type 分流）；CLT 无 status 端点、若非 composite 则明细兜不到 → **「CLT 无法 worker 兜底明细」为运维风险**（见 §6），后续可扩展 biz_type-aware 分流。
- 拉到后走 `finalize_order_in_session`（统一收口，含转发）。
- 单单 `LegsSyncIncomplete` 隔离不中断批；`max_age` 下界避免无限扫古单。
- `main.py` lifespan 照 outbox/recon worker 同款 `supervised()` 注册；3 worker 共享 worker 连接池 → **重评 `worker_db_pool_size`**。

### 3.6 留痕列（MED-3）

`BankTxnOrder` 加 `finalized_via: String(12) nullable`（`SYNC|CALLBACK|RECONCILE`）；alembic `0009` 加列 + downgrade。`finalize_order` 统一写。

## 4. 双终态幂等结论（V2）

≤5s「同步 HTTP 终态 + 回调」双出口：

- **同步 tx2 CAS 先到**：order ACCEPTED→SUCCEEDED；回调 `apply_legs` 聚合 SUCCEEDED，`legs.py:149` `new_status==order.status` 跳过转移（仅补 legs）→ 幂等。转发 dedup_key 业务稳定键 → outbox 去重，lifecycle 只收一次。
- **回调先到**：order 经聚合→SUCCEEDED；同步 tx2 CAS `WHERE status='ACCEPTED'` rowcount==0 → 不覆盖 → 无倒退。
- **分歧**（同步 SUCCESS 但 leg FAILED）：回调聚合 FAILED，`SUCCEEDED→FAILED` 非法 → `LegsSyncIncomplete` → **告警 + 人工**（资金一致性裂纹，不静默覆盖）。

## 5. 测试（100% 行+分支 gate，`.venv/bin/pytest`）

- states：`ACCEPTED→SUCCEEDED/PROCESSING` 合法、`SUCCEEDED→FAILED` 仍非法。
- submit CAS：SUCCESS→SUCCEEDED+finalized_at+via=SYNC+转发入队；PROCESSING→PROCESSING（不转发）；FAILED→FAILED；缺省→SUBMITTED；**rowcount==0（回调先推进）→ 不覆盖**。
- finalize_order：三 source 都写 status+finalized_at+via+audit+forward；非终态不转发；转发 dedup 稳定键去重。
- 并发 tx2↔callback：两序都不倒退、lifecycle 单次转发。
- 空 legs：不再 silent return，留痕/告警。
- reconcile：非终态 stale 推进 + 终态缺 legs 补拉；biz_type 选源；CLT 兜底受限路径；单单失败隔离；max_age 边界。
- migration：`alembic upgrade head` / downgrade round-trip。
- 既有断言更新：`tests/domain/test_states.py`、`tests/services/test_submit.py`（原断言「ACCEPTED→SUCCEEDED 非法 / submit→SUBMITTED」需随新语义改）。

## 6. 部署 + 风险

- **local 先行**：`.venv/bin/pytest`(100%) + ruff + mypy → 本地起服(:8022) healthz/readyz + 3 worker 日志无异常 + 造一笔同步 SUCCESS 验 order=SUCCEEDED + lifecycle 转发入队 → dev → dev-hw（`/dev-hw-ssh`）+ (a)(b)(c) 真机验证。
- **codex 复审** diff（`codex exec -s read-only` + run_in_background）。
- **风险**：① 开放 ACCEPTED→终态 + CAS 必须成对（漏 CAS = 倒退，本设计已绑）；② **CLT 无法 worker 兜底明细**——若 wedap collect 既无 status 端点又非 composite，则 CLT 明细只能靠回调，回调丢失时 CLT 的 leg 永缺（父单终态可由同步 txnStatus 收口，但 ledger 缺 leg）→ 列为运维风险，推动 wedap 补 collect status / 把 collect 纳入 composite；③ 纯加性（新转移、新列、新 worker、CAS 替盲写），worker 由 `workers_enabled` 开关，可回滚。

## 7. 改动文件清单

| 文件 | 改动 |
|---|---|
| `app/domain/states.py` | `_ALLOWED[ACCEPTED]` 加 SUCCEEDED/PROCESSING |
| `app/services/submit.py` | tx2 改 CAS + txnStatus 映射 + 终态走 finalize_order |
| `app/services/legs.py` | 空 legs 不静默 return；终态聚合走 finalize_order；新增/复用收口 |
| `app/services/order_finalize.py`（新） | `finalize_order_in_session` 统一收口 helper |
| `app/workers/order_reconcile_worker.py`（新） | 兜底 worker（双扫描集 + biz_type 拉取） |
| `app/main.py` | lifespan 注册 order_reconcile worker；`_after_ingest` 终态走 finalize_order |
| `app/core/config.py` | `order_reconcile_*` 配置 + 重评 `worker_db_pool_size` |
| `app/models/txn.py` | `BankTxnOrder.finalized_via` |
| `alembic/versions/0009_*.py`（新） | 加 finalized_via 列 |
| `tests/**` | 全量补 + 既有断言更新（100% gate） |
