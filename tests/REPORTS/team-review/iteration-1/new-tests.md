# Test Engineer 新增测试 — iteration 1

基线已 100% 行+分支覆盖，本轮新增测试为「回归锁定 + 方言相关运行时行为」补强。

| 测试 | 关联 finding | 维度 | 层 |
|---|---|---|---|
| `test_callbacks.py::test_replay_with_drifted_body_redrives_with_stored_payload` | A-C-001 | 幂等/重放 | 单测(SQLite) |
| `test_recon_ingest_mysql.py::test_dirty_amount_no_self_deadlock_marks_failed` | QA-M-001 | 并发/锁/异常 | 集成(MySQL 8.0 容器) |
| `test_deps.py::test_parse_amount_rejects_non_finite_400` (NaN/sNaN/Infinity/-Infinity/inf/nan 参数化) | CR-M-001 | 精度/边界 | 单测 |
| `test_deps.py::test_parse_amount_accepts_positive_finite` | CR-M-001 回归 | 正路径 | 单测 |
| `test_deps.py::test_parse_amount_rejects_non_positive_400` (0/-1/-0.0001) | CR-M-001 回归 | 边界 | 单测 |
| `test_deps.py::test_parse_amount_rejects_unparseable_400` | CR-M-001 回归 | 异常 | 单测 |

## 为什么需要 MySQL 集成测试

QA-M-001 是**方言相关**缺陷：SQLite 忽略 `FOR UPDATE`，自死锁不复现；100% 覆盖率覆盖的是
代码行/分支，不覆盖 InnoDB 行锁运行时语义。必须在真 MySQL 容器实跑才能暴露并锁定。该测试
把 `innodb_lock_wait_timeout` 调到 5s，使（若回归）自死锁能在 ~5s 内以 `OperationalError(1205)`
快速暴露，而非默认 50s 阻塞。

## 测试计数

- 单测：379 → 391（+12，含参数化展开）
- 集成（`-m integration`，默认 deselect，需 docker）：+1
- 全量单测：392 passed, 12 deselected, 0 failed。
