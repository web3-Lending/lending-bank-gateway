# QA / Bug Finder 对抗扫描 — iteration 1

> 对象：`lending-bank-gateway`。方式：主控执行 QA 角色（对抗输入 + 真 MySQL 容器实跑验证关键路径）。

## Critical (0)

无。

## Major (2)

### [QA-M-001] recon `parse_and_land` 脏金额触发 MySQL FOR UPDATE 自死锁 ✅ 已修复（red-green 实证）
- **Location**: `app/services/recon_ingest.py`（旧实现：主事务 `with_for_update` 锁 task 行 + except 内另一连接 UPDATE 同行置 FAILED）
- **攻击输入**: 一个三 sheet 列头合法、但 `WeDAP Source` 的 `Amount` 列含 `"not-a-number"` 的 xlsx；task 状态 DOWNLOADED。
- **预期 vs 实际（旧实现）**: 预期抛 DataQualityError + task 置 FAILED + 三表 0 行；实际在 MySQL 下 **第二连接 `UPDATE recon_result_task SET status='FAILED'` 卡 task 行排他锁（主事务仍持 FOR UPDATE）→ `(1205, 'Lock wait timeout exceeded')`**，task 未被置 FAILED。
- **实证**: `tests/integration/test_recon_ingest_mysql.py` 在 MySQL 8.0 容器 + `innodb_lock_wait_timeout=5` 下：
  - 旧代码（git stash 掉修复后运行）→ FAILED：`asyncmy.errors.OperationalError (1205, Lock wait timeout exceeded)`，16.09s。
  - 新代码 → PASS：抛 DataQualityError、task=FAILED、三表 0 行，11.30s。
- **为何门禁/单测漏掉**: 单测跑 SQLite，SQLite **忽略** `FOR UPDATE`，自死锁不复现；100% 覆盖率也覆盖不到方言相关的运行时锁行为。
- **影响**: 生产 recon 摄取遇脏金额对账文件 → worker 卡 `innodb_lock_wait_timeout`（默认 50s）后报锁超时异常（非 DataQualityError），task 滞留非 FAILED，脏数据告警链断。
- **Fix**: 把 DataQualityError → FAILED 留痕移到主事务回滚（FOR UPDATE 释放）之后的 `except` 执行。根因消除。

### [QA-M-002] 北向金额入口 NaN/Infinity → 500（同 CR-M-001）✅ 已修复
- 见 `code-review.md` CR-M-001。QA 侧以精度攻击清单命中：`totalAmount="NaN"` → 500；`"Infinity"` → 受理后落库 500。已在 `parse_amount` 加 `is_finite()` 护栏 + 参数化回归测试。

## Minor / 已被现有代码防住（不报 finding，记录对抗结论）

- **并发同 key 提交**：`idempotency.check_or_register` 用 `begin_nested` + IntegrityError 兜底 + `FOR UPDATE` 穿透 RR 快照，三态（重放/in-flight/conflict）完整。✅ 防住。
- **outbox dedup 并发**：`enqueue_forward` 乐观查 + `begin_nested` + IntegrityError 查回，`fwd-{request_id}` 跨重放稳定。✅（多副本重复投递属 architect A-M-001，已知）
- **状态机非法迁移**：`legs.sync_legs_for` 终态防倒退（HARD_TERMINAL + SUCCESS→REVERSED 白名单），聚合/迁移失败抛 LegsSyncIncomplete 让 inbox 留 RECEIVED 重放。✅ 防住。
- **上游降级**：`submit_order` 对 Timeout/TransportError→RESULT_UNKNOWN、5xx→RESULT_UNKNOWN、4xx→FAILED、WedapError→FAILED 分类正确，不会把上游失败误判成功。✅
- **回调重放 body 漂移**：旧实现用本次 body 再驱动（architect A-C-001），已改为用首次落库 payload 收敛。✅ 已修。
- **审计链并发分叉**：`audit.write_audit` 取链尾无锁（architect A-m-003，文档化 v1），并发同 tenant 会分叉。转 followup。
- **负金额 / 0**：`parse_amount` `<= 0` 拒；`-Infinity` 经 `<=0` 拒（finite 检查也拒）。✅
- **detail 金额 NaN/Infinity**：`validate_detail_consistency` 的 `sum(amounts) != total` 对 NaN/Inf 返回 True → 400（非 500），可接受（消息略泛，未改）。

## 对抗扫描覆盖声明

并发竞态 / 幂等重放 / 金额精度（NaN/Inf/负/0）/ 状态机倒退 / 上游降级（timeout/5xx/4xx/WedapError）/ 数据畸形 / inbox-outbox 崩溃窗口 均已逐项过；真 MySQL 容器实跑验证了 QA-M-001 的方言相关锁行为（红→绿）。
