# Team Review iteration 1 — 汇总

- 对象：`lending-bank-gateway`（统一资金网关 ADR-0031）
- 分支：`fix/gateway-northbound-passthrough`
- 范围：全仓（app/ 45 源文件）
- 角色：Architect / Senior Code Review / QA Bug Finder / Refactoring / Test Engineer / Coverage Auditor（架构角色由 subagent 产出；code-review/QA 因长 SSE 连接 RST 掉线改由主控逐文件执行）

## 机械门禁基线（评审开始即全绿）

ruff clean · mypy --strict clean(45) · bandit 0 · pytest 379 passed · 行/分支覆盖 100% · 无 TODO/FIXME。
**增量价值集中在 6 角色实质评审找 lint/覆盖率抓不到的逻辑/方言/可靠性缺陷。**

## 本轮已修复（4，根因修复 + 回归测试 + 原子 commit）

| finding | 级别 | commit | 说明 |
|---|---|---|---|
| A-C-001 inbox 重放用本次 body | Critical(架构)/实际防御性 | `37d3993` | 改为从首次落库 payload 收敛 |
| QA-M-001 recon 脏金额 MySQL FOR UPDATE 自死锁 | Major（red-green 实证） | `3e38078` | FAILED 留痕移到主事务回滚后 |
| CR-M-001 parse_amount 非有限金额穿透 500 | Major | `089da76` | is_finite() 护栏拒 NaN/Infinity 为 400 |
| TEST-001 集成测试写死已删 worktree 路径 | Major(CI) | `f2ff4cf` | 改仓库根推导，集成套件复绿 |

**最高价值**：QA-M-001 是单测/100%覆盖结构性漏掉的方言相关锁 bug（SQLite 忽略 FOR UPDATE），真 MySQL 容器 red-green 实证。

## 本轮 Deferred（8，已登记 followups，待用户拍板是否进 iteration 2）

| finding | 级别 | followup_id | 为何 defer |
|---|---|---|---|
| A-C-002 inbox-outbox 非单事务 | Critical(架构) | `FU-GW-INBOX-OUTBOX-ATOMICITY-20260615-001` | 已文档化 at-least-once 取舍；需事务重构+设计评审 |
| A-M-001 outbox 无原子 claim(+A-m-001/CR-m-002) | Major | `FU-GW-OUTBOX-CLAIM-20260615-001` | 需 schema 迁移(SENDING/locked_at)+设计 |
| A-M-003 worker 与 API 共享连接池 | Major | `FU-GW-WORKER-POOL-ISOLATION-20260615-001` | ops/perf 设计决策 |
| A-M-002 settings lru_cache 单例 | Major | `FU-GW-SETTINGS-DI-20260615-001` | 低实际风险，conftest 已 workaround |
| A-M-004 跨库直读契约无版本化 | Major | `FU-GW-RECON-CROSSDB-CONTRACT-20260615-001` | 跨仓(需 lending-recon 协作) |
| A-m-002 S2S v1 共享 token | Minor(security) | `FU-GW-S2S-V2-20260615-001` | 代码已标 v1，v2 安全工作 |
| A-m-003 audit chain 并发分叉 | Minor | `FU-GW-AUDIT-CHAIN-LOCK-20260615-001` | 代码已标 v2，需 MySQL gap-lock 验证 |
| A-m-004 CLT 无 wedap 状态查询 | Minor | `FU-GW-CLT-STATUS-API-20260615-001` | 需确认 wedap 归集状态接口存在性 |

## 出口状态

- ✅ 机械门禁全绿（pytest 392 单测 + 11 集成 / 100% 覆盖 / ruff / mypy / bandit / 无 TODO）
- ✅ 本轮所有「可当场根因修复」的 finding 已修 + 回归锁定
- ⏳ 8 项架构债/v2/跨仓 finding 已登记 followups，**按 skill 规则需用户确认 defer 决策**（非静默丢弃）
- 结论：**partial-exit**——不宣称 zero-finding DONE；待用户拍板是否对任一 deferred 项开 iteration 2。
