# Refactoring Engineer 修复清单 — iteration 1

按 Critical→Major 顺序，根因修复，每个原子 commit + 回归测试锁定。

| finding | 严重度 | 根因 | commit | 回归测试 |
|---|---|---|---|---|
| A-C-001 inbox 重放用本次 body 而非首份 payload | Critical(架构) / 实际防御性 | 南向 inbox 幂等只做请求去重，重放再驱动未从权威记录(inbox.payload)收敛 | `37d3993` | `test_callbacks.py::test_replay_with_drifted_body_redrives_with_stored_payload` |
| QA-M-001 recon 脏金额置 FAILED 的 MySQL FOR UPDATE 自死锁 | Major | 主事务持 task 行 FOR UPDATE 未释放时，另一连接 UPDATE 同行 → 互锁锁超时 | `3e38078` | `test_recon_ingest_mysql.py::test_dirty_amount_no_self_deadlock_marks_failed`（red-green 实证） |
| CR-M-001 parse_amount 非有限金额穿透 500 | Major | `value<=0` 比较在 try 外；NaN 比较抛 InvalidOperation、Infinity 通过比较被放行 | `089da76` | `test_deps.py::test_parse_amount_rejects_non_finite_400`（NaN/sNaN/Inf 参数化） |

## 修复纪律核对

- ✅ 均为根因修复，无覆盖式修补 / 无 try/except: pass / 无放宽类型 / 无删断言迁就。
- ✅ QA-M-001 先红后绿：stash 修复跑 MySQL 集成测得 `OperationalError(1205)` 证伪，恢复后绿。
- ✅ 每个修复后跑相关测试，再跑全量；100% 行+分支覆盖维持。
- ✅ 未引入新依赖。

## 本轮未当场修、转 followup 的 finding

见 `summary.md` §Deferred 与 Followups Registry（架构债 / 跨仓 / 已文档化 v2 项）。
