# Team Review iteration 2 — 汇总（架构债全修，用户拍板「全部修」）

iter1 的 8 项 deferred 架构债，用户选择全部进 iteration 2 现场修。本轮逐项根因修复 + 测试 +
门禁 + 原子 commit；MySQL 相关项真机 red-green 实证；跨仓/wedap 外部依赖部分如实留 followup。

## 已修复（8 项 finding → 8 commit）

| finding | commit | 关键 | 验证 |
|---|---|---|---|
| A-M-002 settings DI | `0b80a4d` | create_app(settings=None) 注入 app.state.settings，去 lru_cache | SQLite 单测 |
| A-m-003 audit 链分叉 | `abe1f7c` (+0007) | naive 链尾 FOR UPDATE **实测 1213 死锁** → 改 per-tenant 锚点 audit_chain_head 主键精确锁 | **MySQL 5 并发无分叉无死锁** |
| A-C-002 inbox-outbox 非原子 | `e53633e` | leg 同步 + outbox enqueue 同事务原子提交，事务外预取 steps | SQLite 原子回滚回归 |
| A-M-001 outbox 重复投递 | `958699d` (+0008) | 原子 claim(SENDING+locked_at) + stale reclaim；含 A-m-001 trace 透传 + CR-m-002 httpx 复用 | **MySQL 2 dispatcher 无重复投递 attempts==1** |
| A-M-003 worker 池争用 | `089035d` | worker 专用 engine(pool=3+overflow5) 与 API 隔离 + shutdown dispose | 单测断言独立 factory |
| A-m-002 S2S 共享 token | `33209c9` | per-service token 密码学绑定 caller↔token，回退共享 secret | 5 例含 A 拿 B token→401 |
| A-M-004 跨库契约(gateway 侧) | `a29dcc7` | bank_txn_leg 列名/类型/可空 + 表名契约冻结测试，漂移即 CI red | 契约快照测试 |
| A-m-004 CLT 终态 | `db641e6` | 验证 CLT 单经回调 sync_legs(biz_type 无关)驱动至终态 SUCCEEDED | 回归测试 |

## 关键工程发现（本轮最有价值）

**A-m-003 naive FOR UPDATE 会死锁**：直接对 `audit_log` 链尾 `SELECT ... ORDER BY id DESC LIMIT 1
FOR UPDATE` 经 MySQL 8.0 实测触发 **1213 Deadlock**（二级索引 next-key/间隙锁）——印证原作者
「v2 需谨慎设计」的判断。改用 per-tenant 锚点行 `audit_chain_head(tenant_id PK)` 主键精确锁
（无间隙锁）+ 列级 select 取标量绕过 ORM identity-map 快照陈旧，才真正串行化、零死锁零分叉。
若不在真 MySQL 跑并发测试，naive 改法会把死锁带上金融审计热路径。

## followups 收口

- **6 项 complete**（带 commit + 测试 evidence）：A-M-002 / A-m-003 / A-C-002 / A-M-001 / A-M-003 / A-m-002
- **2 项 in_progress（跨仓/外部，如实留 open）**：
  - `FU-GW-RECON-CROSSDB-CONTRACT`：gateway 侧契约测试已落；recon 仓侧对账契约 + DB 视图需跨仓协作
  - `FU-GW-CLT-STATUS-API`：CLT 回调终态已验证；wedap 归集状态接口存在性需 wedap 侧确认；status 端点 note 增强因 bank_funds.py 被并发会话编辑暂缓

## ⚠️ 并发会话告警（重要）

评审期间检测到**另一活跃 Claude 会话**（session 62d63d84，pid 613258，锁 2026-06-16 10:29）在
**同一 worktree** 编辑 `app/api/v1/bank_funds.py` + `tests/api/test_bank_funds.py` + `contracts/openapi.json`
（txnAmount 扁平契约，与本评审无关）。本评审全程用显式 `git add` 仅提交自己的文件，**未污染也未
提交对方 WIP**；但共享工作树被对方未提交改动污染，故 `ruff check .` 会报对方文件的行长问题（非本
评审引入）。建议后续遵循 worktree 隔离纪律，避免多会话同 worktree。

## 出口状态（iter2）

- ✅ 单测 410 passed / 行+分支覆盖 100%
- ✅ 集成 13 passed（MySQL 8.0，含 0007/0008 alembic upgrade head + 4 并发/锁/原子真机测）
- ✅ ruff（本评审文件）clean / mypy strict clean(45) / 单一 alembic head 0008
- ✅ 8 finding 全闭环（6 complete + 2 跨仓/外部 followup 保持 open 并附进度）
