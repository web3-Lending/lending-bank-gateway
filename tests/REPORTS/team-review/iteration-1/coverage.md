# Coverage Auditor — iteration 1

## 总览

| 指标 | 值 |
|---|---|
| 行覆盖 | 100.00%（1350/1350） |
| 分支覆盖 | 100.00%（196/196，0 partial） |
| 门禁 | pyproject `[tool.coverage.report] fail_under = 100` —— 通过 |
| 未覆盖行 | 0 |

## 豁免（`# pragma: no cover` / `no branch`）核对

现有豁免均属可接受类型（`references/exemption-policy.md`），本轮未新增：

- `app/main.py:169` `if tasks:` lifespan shutdown 分支 — `pragma: no cover`（仅 worker 启用 + 真关停路径，TestClient 不触发）。合法。
- `app/core/db.py:29` mysql `_pin_utc` 事件 — `pragma: no cover`（仅真 MySQL 连接路径；已由集成测 `test_utc_pin_async` 实际验证）。合法。
- `app/services/outbox.py:67,149` 并发 IntegrityError / 理论不可达分支 — `pragma: no cover`，注释说明单线程测试不可达。合法。
- `app/services/idempotency.py:100` 并发竞态 race re-raise — `pragma: no cover`。合法。
- `app/services/recon_ingest.py` `if _t is not None:  # pragma: no branch`（FAILED 留痕，move 到 except 后仍保留）。合法。

## 本轮变更对覆盖的影响

- 新增代码（callbacks 重放分支 / recon except 重构 / parse_amount is_finite 分支）均由新回归测试覆盖到 100%。
- recon_ingest.py 重构后 121 stmts / 34 branch 仍 100%（DataQualityError 路径由 SQLite 单测 `test_dirty_amount_fails_task_no_rows` + MySQL 集成测共同覆盖）。

## 判定

✅ 行 100% / 分支 100% / 无非法未覆盖 / 豁免均合法。无需回到 Step 4 补测。
