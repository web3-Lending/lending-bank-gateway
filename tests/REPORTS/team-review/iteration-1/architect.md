# Architect Review — iteration 1

> 评审对象：`lending-bank-gateway`（分支 `fix/gateway-northbound-passthrough`），统一资金网关（ADR-0031）。
> 维度：服务边界/耦合/依赖方向、幂等贯通、inbox-outbox 事务与可靠投递、事务边界与业务边界一致性、跨服务调用韧性、S2S 信任边界、跨库直读契约、全局状态、worker 生命周期与并发。
> 范围：只读架构评审，未改任何源码。

## Critical (2)

### [A-C-001] inbox 重放再驱动时漏校验 payload，dedup 路径用「最新 body」覆盖执行
- **Tags**: [IDEMPOTENCY][TXN]
- **Location**: `app/api/v1/callbacks.py:122-178`（dedup 分支）、配合 `app/api/v1/callbacks.py:109-121`（首落库只存首次 body）
- **What**: 南向回调首次落库时只写 `payload=body`（首次 body），三元组 `(tenant_id, WEDAP_TXN, request_id)` 唯一去重。重发命中 dedup（line 122-126）后，若既有行 `status=="RECEIVED"`，会用**本次请求的 body**（line 163-168 把当前 `body` 透传给 `after_ingest`）再次驱动 `sync_legs_for`/`enqueue_forward`，**完全不比对本次 body 与首次落库 payload 是否一致**。inbox 这条幂等链没有 payload-hash 校验（北向 `idempotency.py` 有 `payload_hash`，南向 inbox 没有对等机制）。
- **失败场景**: wedap 用同一 `X-Request-Id` 重发，但 body 不同（如金额/状态字段被中途改写、或上游 bug 复用 request_id 发不同交易）。首次 body 已落 inbox 且执行了 leg 同步；重发被判 dedup→RECEIVED 再驱动，此时用**第二份 body** 跑 `sync_legs_for`，可能推进 leg 状态/聚合父单到与首份 body 不一致的结果，且 outbox 转发给 lifecycle 的仍是首次 `fwd-{request_id}`（被 outbox dedup 拦），导致 gateway 内部 leg 状态与下游收到的版本不一致。
- **影响**: 同一幂等键下产生「执行内容漂移」——这是资金回调链幂等的核心防线缺口。资金状态机可能被非首份 body 污染，对账时 gateway 自有 leg 与 wedap/下游不一致。
- **Root cause**: 南向 inbox 幂等只做了「请求去重（唯一约束）」，未做「请求内容一致性校验（payload hash）」，与北向 `check_or_register` 的设计不对称。
- **建议修复方向**: inbox 行增加 payload_hash 列；dedup 命中后先比对本次 body 的 hash 与既有行 hash，不一致直接拒绝（4xx/告警）而非用新 body 再驱动；RECEIVED 再驱动必须使用**既有行已落库的 payload**（line 158 已经查回了 `existing`，应改用 `existing.payload` 而非当前 `body` 驱动 after_ingest），保证重放严格幂等。

### [A-C-002] `_after_ingest` 两阶段写 + 转发原始 body，崩溃窗口下漏发或转发陈旧状态
- **Tags**: [TXN][IDEMPOTENCY][ARCH]
- **Location**: `app/main.py:93-119`（`_after_ingest`：先 `sync_legs_for` 独立事务 commit，再独立事务 `enqueue_forward`）；转发内容见 line 113-119 `payload=body`
- **What**: `_after_ingest` 分两个独立事务：事务A `sync_legs_for`（落 leg + 聚合父单状态，提交）；事务B `enqueue_forward`（写 outbox，提交）。两者非同一事务。且 outbox 转发的 `payload` 是**原始 wedap 回调 body**，不是 gateway 聚合后的父单/leg 状态。
- **失败场景**:
  1. 事务A 提交成功（leg 已落库、父单状态已推进），进程在事务B 之前崩溃 → leg 已更新但 outbox 没入队。inbox 行此时仍是 RECEIVED（`_set_inbox_status(PROCESSED)` 在 `_after_ingest` 返回后才执行，见 callbacks.py:137-140），靠 wedap 重发再驱动收敛——但若 wedap 不再重发（超出其重试次数），这条回调**永久不会转发给 lifecycle**，下游丢失该笔状态更新。
  2. 即便正常，转发给 lifecycle 的是原始 body，下游拿到的是「wedap 视角的单步通知」，而 gateway 已经做了 leg 聚合/父单状态推进——下游与 gateway 的状态视图不一致，对账口径分叉。
- **影响**: at-least-once 的「投递保证」在 leg 同步与 outbox 入队之间存在缺口；崩溃窗口 + wedap 停止重试 = 静默漏发（数据丢失类）。转发陈旧 body 造成跨服务状态语义不一致。
- **Root cause**: inbox-outbox 模式的核心要义是「业务状态变更与 outbox 写入同事务」，从而由 outbox 派发保证 at-least-once。这里把 leg 同步与 outbox 拆成两事务，破坏了该原子性；inbox 状态推进又是第三个事务，三段非原子。
- **建议修复方向**: 将「leg 同步/父单聚合 + outbox enqueue」纳入**同一事务**（外呼 `get_composite_steps` 已在 `sync_legs_for` 内事务外预取，可在进事务前先取 steps，事务内只做 DB 写 + enqueue），使 outbox 写与状态变更原子提交；inbox→PROCESSED 也应纳入同一事务或由 outbox 行存在性推导。转发 payload 改为 gateway 聚合后的规范化状态（或显式约定下游只认 gateway 派生视图），消除「转发原始 body」的语义分叉。

## Major (4)

### [A-M-001] outbox dispatcher 无 claim/租约，多副本必然重复投递且与 backoff 冲突
- **Tags**: [CONCURRENCY][UPSTREAM][TXN]
- **Location**: `app/services/outbox.py:99-166`（`dispatch_once`：snapshot 读无 `FOR UPDATE`、无中间态 `SENDING`、无 claim）；多副本说明见 `README.md:84`
- **What**: `dispatch_once` 先无事务快照读所有到期 PENDING/FAILED 行（line 100-115），逐条外呼后再开独立事务回写状态。整个过程**没有把行原子 claim 成 SENDING/锁定**。recon worker 用了原子 claim（`recon_worker.py:50-68` `UPDATE ... WHERE status='NOTIFIED'`），但 outbox 没有对等机制。
- **失败场景**: `GW_WORKERS_ENABLED=true` 多副本部署（README 明确支持），两副本同一轮都读到同一 PENDING 行，各自向 lifecycle POST 一次 → 下游同一 `X-Request-Id`（=dedup_key）收到双投递。README 说「由下游幂等容忍」，但：(a) 这把可靠性正确性外包给下游，gateway 自身不保证恰好/有界投递；(b) 两副本回写 status 时会各 `attempts += 1`，backoff/DEAD 计数被双倍消耗，可能提前进 DEAD；(c) 单副本内同一行若上一轮外呼慢、本轮 interval 到了又被读（无 in-flight 标记），同样重复。
- **影响**: 多副本下投递量不可控、attempts 计数失真导致提前死信、可靠投递语义实际退化为「尽力而为 + 全靠下游兜底」。
- **Root cause**: outbox 派发缺少与 recon worker 同级的原子 claim（中间态 + 条件 UPDATE 租约），仅依赖下游幂等。
- **建议修复方向**: 引入 `SENDING` 中间态 + 原子 claim（`UPDATE ... SET status='SENDING', locked_at=now WHERE id=:id AND status IN('PENDING','FAILED') AND (next_retry_at IS NULL OR next_retry_at<=now)`，rowcount==1 才外呼），外呼后再置 SENT/FAILED；加 `SENDING` 超时回收（防外呼后崩溃卡死）。或单副本 dispatcher 选主（仅一个副本跑），与 README 多副本声明二选一并写清。

### [A-M-002] 全局 `lru_cache` 单例 Settings 导致测试/运行期配置不可隔离，且 fail-fast 校验绕过运行期
- **Tags**: [ARCH][STATE]
- **Location**: `app/core/config.py:44-46`（`@lru_cache get_settings`）；消费点 `app/main.py:125,177` 与 `_lifespan` 内多处 `settings.*`
- **What**: `get_settings()` 用 `@lru_cache` 返回进程级单例。`create_app()`（line 177）和 `_lifespan`（line 125）各调一次，依赖同一缓存实例。
- **失败场景**: (a) 多环境/多租户配置无法在同进程切换；(b) 测试需改配置时必须 `get_settings.cache_clear()`，否则跨用例污染——这是隐性全局可变状态，违反「配置应可注入」的边界原则；(c) `create_app` 里的 S2S fail-fast 校验（main.py:178-181）只在建 app 时跑一次，若 lru_cache 在别处被预热成 dev 默认值，校验语义依赖调用顺序。
- **影响**: 全局状态耦合，配置变更/测试隔离困难；金融服务配置（S2S secret、wedap URL、callback target）的来源不透明、难审计。
- **Root cause**: 配置以模块级缓存单例供给，而非依赖注入到 `app.state`（其余依赖如 engine/wedap 都进了 `app.state`，唯独 settings 走全局缓存，依赖供给方式不一致）。
- **建议修复方向**: 把 settings 也放入 `app.state.settings`，由 `create_app` 一次性构造并向下传递；测试用 fixture 注入而非清缓存。保留 `get_settings` 仅作默认工厂。

### [A-M-003] worker 用进程级 `app.state.session_factory`，supervisor 重启不重建连接资源
- **Tags**: [ARCH][CONCURRENCY][OBS]
- **Location**: `app/main.py:128-160`（lifespan 起 worker，传 `app.state.session_factory`）；`app/workers/supervisor.py:8-19`（崩溃退避重启）；`app/main.py:152` recon worker 每次都 `S3FileClient(...)` 新建
- **What**: outbox/recon worker 与 API 共用同一 `app.state.engine`/`session_factory`（同一连接池）。supervisor 崩溃重启只是重调 `factory()`（worker 协程函数），不重建 engine/pool/S3 client。outbox dispatcher 每条外呼新建一个 `httpx.AsyncClient`（outbox.py:130），无连接复用。
- **失败场景**: (a) worker 长事务/慢外呼会占用与 API 共享的连接池（`db_pool_size=5` + overflow 10），高峰期 worker 与北向请求争抢连接 → 北向请求拿不到连接超时；(b) S3FileClient 在 lifespan 构造一次（main.py:152）传给 `run_forever`，崩溃重启后 supervisor 复用同一 boto3 client，若其内部连接已坏无自愈；(c) 每条 outbox 外呼新建 AsyncClient 在高频投递下 TCP/TLS 握手开销大。
- **影响**: worker 与在线请求资源耦合，故障隔离弱；连接池争用可放大为在线可用性问题。
- **Root cause**: worker 与 API 共享生命周期与连接资源，缺少独立连接池/资源边界；外呼客户端未复用。
- **建议修复方向**: worker 用独立 engine/连接池（与 API 隔离），或显式限制 worker 并发与单次取数批量；outbox dispatcher 复用一个长生命周期 `httpx.AsyncClient`（带连接池）而非每条新建；supervisor 重启时按需重建坏掉的资源。

### [A-M-004] 跨库直读（recon 读 gateway 库）契约靠口头约定，无 schema 版本/视图隔离，漂移风险高
- **Tags**: [ARCH][RECON]
- **Location**: `app/models/txn.py:44-75`（`BankTxnLeg` 表结构即跨库读契约）；`app/api/v1/fiat_vault.py:15-94`（同表的 HTTP 供数口径）；`README.md:9` 声明 recon 跨库直读 gateway 库
- **What**: ADR-0031/README 说 lending-recon 通过跨库 collector 直读 gateway 库的 `bank_txn_leg`（Plan 3）。同时 gateway 又用 `GET /api/v1/fiat-vault/transactions` 暴露同一份数据，且该 HTTP 口径对金额做了 4 位格式化 + inflow/outflow 聚合（fiat_vault.py:71-92）。直读路径绕过这套 HTTP 派生逻辑，**直接消费物理表列**（status 字符串、amount Numeric、txn_date String(8) 等）。
- **失败场景**: gateway 改 `bank_txn_leg` 列（改名/拆字段/status 枚举值变化/`txn_date` 语义调整）时，HTTP 契约有 OpenAPI 快照（`contracts/openapi.json` CI 校验）兜底，但**跨库直读没有任何契约校验**——recon 侧静默读到旧假设的列，对账数据错位却无 CI 信号。两条消费路径（直读 vs HTTP）对同一数据的派生口径也可能分叉（如金额精度、双向流水聚合）。
- **影响**: 跨服务隐性强耦合：gateway 的内部物理 schema 成了 recon 的外部契约，但无版本化、无 CI gate。schema 漂移→对账错账且难发现。
- **Root cause**: 把「内部表」当「跨服务契约」用，却没给它契约级保护（视图/物化契约层/schema 版本/契约测试）。
- **建议修复方向**: 为跨库直读提供专用稳定契约层（DB 视图或契约化只读表，固定列名/语义/版本号），gateway 内部表演进与该视图解耦；或统一收口到 HTTP 供数一条路径，废弃直读；无论哪条，补一个跨仓契约测试（recon 仓断言 gateway 契约形态）进 CI。
- **✅ RESOLVED（2026-06-23 · FU-GW-LEG-CONTRACT-PREMISE-20260623-001）**: 本 finding 已闭环，但纠正了原前提——实测 recon **不下钻 leg**（C5 约束），跨库直读的是 order 级 `bank_txn_order` + `recon_result_*`，**非 `bank_txn_leg`**。故采用「废直读 HTTP + 跨仓契约测试」组合：废弃 `GET /api/v1/fiat-vault/transactions`（commit e3af8d6，原 fiat_vault.py 已删）；删指错表的 leg 跨库契约、改建 `bank_txn_order` 契约（commit 7d28261：gateway `tests/contract/test_bank_txn_order_contract.py` + recon vendored 契约 + 双向 CI gate）。leg 表降级为 gateway 内部聚合实现细节，无跨库消费方。

## Minor (4)

### [A-m-001] outbox 转发丢失原始 trace_id，跨服务链路追踪断链
- **Tags**: [OBS]
- **Location**: `app/services/outbox.py:124,138`（`X-Trace-Id=f"outbox-{oid}"`，`X-Request-Id=dedup_key`）
- **What**: dispatcher 转发给 lifecycle 时 `X-Trace-Id` 用 `outbox-{oid}`，没有携带触发该回调的原始 trace_id。inbox 落库时 body 里也未持久化原始 trace_id 供后续转发复用。
- **影响**: 从北向请求→wedap→回调→outbox→lifecycle 的全链路 trace 在 outbox 这一跳断开，线上排障需手工跨 `outbox-{oid}`↔原 trace 拼接。
- **建议修复方向**: inbox/outbox 行持久化原始 trace_id，转发时透传；`outbox-{oid}` 可作为附加 span 标识而非替换 trace。

### [A-m-002] S2S v1 共享 token 信任边界粗，caller 白名单非密码学绑定（已知但需登记）
- **Tags**: [SECURITY][ARCH]
- **Location**: `app/core/s2s.py:30-68`；`app/core/config.py:17-25`（注释自承认 v1 局限）
- **What**: S2S 用单一共享 secret（`hmac.compare_digest` 常量时间比较，已正确避免时序侧信道）+ 可选 caller 白名单。但白名单只是「自报 `X-Caller-Service` 是否在集合内」，任何持有共享 token 的调用方都能伪造任意 caller 名。代码注释已明确这是 v1 兜底、v2 才 per-service 绑定。
- **影响**: 资金网关的服务间信任边界在 v1 是「单一 secret 泄露=全线沦陷」，caller 归因不可信（审计 actor=`svc:{caller}` 可被伪造，见 submit.py:159 写 audit 的 actor）。属已知架构债，非新缺陷，但应作为风险项登记跟踪 v2。
- **建议修复方向**: v2 per-service token 或 mTLS/签名绑定 caller；在此之前确保 secret 轮换机制与泄露应急预案就位。

### [A-m-003] audit hash-chain 并发下会分叉，「append-only 完整性」保证弱于声称
- **Tags**: [OBS][CONCURRENCY]
- **Location**: `app/services/audit.py:13-61`（取 last `row_hash` 无锁，line 28-35）；自承认见 line 24-26 注释
- **What**: per-tenant hash chain 取「该 tenant 最后一行 row_hash」作为 prev 时无 `FOR UPDATE`/无串行化。并发两笔同 tenant audit 会读到同一 prev → 链分叉。`uq_audit_tenant_rowhash` 只防完全相同行重复，不防分叉。代码注释已承认「高并发可能分叉，v2 可加 SELECT FOR UPDATE」。
- **影响**: 审计链「不可篡改+连续」的完整性在并发下退化为「多条叶子的森林」，验证工具需容忍分叉反向追溯，削弱了 hash-chain 的防篡改强度（金融审计敏感）。
- **建议修复方向**: 写 audit 时对该 tenant 链尾加锁（`SELECT ... FOR UPDATE`）串行化，或用单调序列号 + 链校验；权衡写吞吐。

### [A-m-004] `query_funds_status` 对 `CLT`（归集）无状态查询路径，status 端点对该 biz_type 永远降级
- **Tags**: [STATE][ARCH]
- **Location**: `app/clients/wedap.py:128-158`（`_STATUS_PATH_TMPL` 无 `CLT` 键）；消费 `app/api/v1/bank_funds.py:186-199`（UNSUPPORTED→`unavailable:no_status_api`）；`CLT` 来源 `bank_funds.py:120`
- **What**: 北向 `collect-from-users` 落单 `biz_type="CLT"`，但 wedap client 的状态查询映射表只有 DSB/RPY/DST，无 CLT。`GET /bank-funds/status` 查归集单时 wedap 实时状态恒为 `unavailable: no_status_api`。
- **影响**: 归集类资金单无法经 gateway 拿到 wedap 实时状态，只能看本地 order 状态；若本地状态卡在中间态（ACCEPTED/RESULT_UNKNOWN），运营无在线收敛手段，需走人工/对账。属能力缺口而非错误（降级路径明确），但对资金单状态可观测性是短板。
- **建议修复方向**: 确认 wedap 归集是否有状态查询接口；有则补映射，无则在文档/状态端点显式标注 CLT 仅依赖回调/对账收敛，并确保回调链能驱动 CLT 单终态。

## 架构整体评估（2-4 句）

整体架构方向正确且工程质量较高：北向「事务1 落单(禁外呼)→外呼→事务2 推进+幂等回写」的三段式、RESULT_UNKNOWN 可收敛态、状态机防倒退、复合 FK 防跨租户、recon worker 原子 claim、审计/快照留痕都体现了金融网关的成熟设计意识，代码注释对多数已知局限也有诚实标注。核心风险集中在**幂等贯通的对称性与 inbox-outbox 的事务原子性**：南向 inbox 缺 payload 一致性校验（C-001）、leg 同步与 outbox 入队非同事务且转发原始 body（C-002）、outbox 派发缺原子 claim（M-001），这三点共同削弱了「至少一次可靠投递 + 严格幂等」的端到端保证，是 v1 上线前最该收口的架构债。其次是配置全局单例（M-002）、worker 与 API 资源共享（M-003）、跨库直读契约无版本化（M-004）三处边界/耦合问题，建议按 Critical→Major 顺序处理。
