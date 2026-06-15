# Team Review Final Report — lending-bank-gateway

- Scope: full repo（app/ 45 源文件，统一资金网关 ADR-0031）
- Branch: `fix/gateway-northbound-passthrough`
- Iterations: 1
- Date: 2026-06-15
- Exit status: **PARTIAL-EXIT**（机械门禁全绿 + 可当场修复 finding 全闭环；8 项架构债/v2/跨仓 finding 已登记 followups，待用户拍板）

## Coverage

- Line: 100.00%（1350/1350）
- Branch: 100.00%（196/196，0 partial）
- Exemptions: 见 `iteration-1/coverage.md`，均合法（uvicorn/lifespan guard、真 MySQL 路径、并发不可达分支）

## Findings (lifecycle)

- Critical: 2 found（A-C-001 修复 / A-C-002 deferred-followup）
- Major: 5 found（QA-M-001 / CR-M-001 / TEST-001 修复；A-M-001/A-M-003/A-M-004/A-M-002 deferred-followup）
- Minor: 3 found（A-m-002/A-m-003/A-m-004 deferred-followup）+ 若干观察项（CR-m-001/002）

## Real Bugs Surfaced（本轮已修，根因 + 回归）

- **QA-M-001 [Major]**：recon `parse_and_land` 脏金额置 FAILED 时 MySQL FOR UPDATE 自死锁
  - Root cause：主事务持 task 行 `FOR UPDATE` 未释放时，另一连接 UPDATE 同行置 FAILED → 互锁锁超时(1205)
  - Fix commit：`3e38078`；回归：`tests/integration/test_recon_ingest_mysql.py`（MySQL 8.0 容器 red-green 实证）
  - 为何漏：SQLite 忽略 FOR UPDATE，单测/100% 覆盖结构性覆盖不到方言锁行为
- **CR-M-001 [Major]**：`parse_amount` NaN→500(InvalidOperation 未捕获) / Infinity→受理后落库 500
  - Root cause：`value<=0` 比较在 try 外；非有限 Decimal 是合法值
  - Fix commit：`089da76`；回归：`tests/api/test_deps.py::test_parse_amount_rejects_non_finite_400`
- **A-C-001 [Critical-架构/实际防御性]**：inbox 重放用本次 body 而非首次落库 payload
  - Root cause：南向 inbox 幂等只做请求去重，未从权威记录收敛
  - Fix commit：`37d3993`；回归：`test_callbacks.py::test_replay_with_drifted_body_redrives_with_stored_payload`
- **TEST-001 [Major-CI]**：集成测试写死已删 worktree 路径 → 集成套件红 + 级联
  - Fix commit：`f2ff4cf`；集成套件复绿 11 passed

## Deferred Items（已登记 followups，需用户确认是否 iteration 2）

见 `iteration-1/summary.md` §Deferred，8 项均有 `FU-GW-*-20260615-001`。覆盖：inbox-outbox 单事务原子性、outbox 原子 claim、worker 连接池隔离、settings DI、跨库直读契约、S2S v2、audit chain 锁、CLT 状态接口。多为代码已自承认的 v1→v2 取舍或跨仓事项，不宜在 code review 内强行改完（带回归风险且需独立设计/验证周期）。

## Quality Gates

- [✅] pytest 单测：392 passed, 12 deselected, 0 failed
- [✅] pytest 集成（docker MySQL 8.0）：11 passed
- [✅] coverage：line 100% / branch 100%（fail_under=100 门禁过）
- [✅] ruff check .：clean
- [✅] mypy app（strict）：0 issues / 45 files
- [✅] bandit -r app：0 issues
- [⚠️] gitleaks / pip-audit / trivy：SKIPPED — tool not installed（本地复现命令见 security-audit.md，人工复核无真问题）
- [✅] TODO/FIXME/XXX：0
- [✅] Git：4 atomic fix commits（reports 单独 commit）

## 说明

本服务评审开始即满足全部「机械出口条件」（覆盖率/lint/类型/测试）。本轮真正价值是 6 角色实质评审找出 4 个 lint/覆盖率结构性漏掉的真实缺陷（尤以 MySQL FOR UPDATE 自死锁为代表）并根因修复。**未宣称 zero-finding 终局 DONE**：8 项架构债按 skill 规则 deferred-with-followup，待用户拍板。
