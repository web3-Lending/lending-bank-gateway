# 测试结果 — iteration 1

## 单元测试 + 覆盖率（默认套件，`pytest -q`）

```
392 passed, 12 deselected, 39 warnings in ~39s
TOTAL  1350 stmts  0 miss  196 branch  0 partial  100%
Required test coverage of 100.0% reached. Total coverage: 100.00%
```
- 行覆盖 100% / 分支覆盖 100%（pyproject `fail_under=100` 门禁通过）
- warnings 为既有 aiosqlite teardown「Event loop is closed」+ starlette TestClient 弃用提示，非本轮引入。

## 集成测试（`pytest -m integration --no-cov -q`，需 docker · MySQL 8.0 容器）

```
11 passed, 393 deselected, 1 warning in ~37s
```
- 修 TEST-001（alembic 路径写死已删 worktree）前：`3 failed, 8 passed`（alembic upgrade 失败 + 2 级联 table-missing）。
- 修后：11 passed（含本轮新增 `test_recon_ingest_mysql.py`）。

## red-green 实证（QA-M-001 recon 自死锁）

| 代码状态 | 结果 | 耗时 |
|---|---|---|
| 旧（git stash 修复） | FAILED：`asyncmy OperationalError (1205, 'Lock wait timeout exceeded')` on `UPDATE recon_result_task SET status='FAILED'` | 16.09s |
| 新（修复） | PASS：抛 DataQualityError + task=FAILED + 三表 0 行 | 11.30s |

## 静态门禁

| 门禁 | 结果 |
|---|---|
| `ruff check .` | clean |
| `mypy app`（strict） | Success: no issues in 45 files |
| `bandit -r app` | 0 issues（exit 0） |
| TODO/FIXME/XXX in app/ | 0 |

## 失败处理记录

- 集成套件首跑 3 failed → 定位为 TEST-001（pre-existing 写死路径，非本轮代码引入）→ 当场根因修复 → 复跑全绿。无回归。
