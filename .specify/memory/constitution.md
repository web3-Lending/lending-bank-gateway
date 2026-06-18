# lending-bank-gateway Constitution

## Core Principles

### I. 资金字段定点十进制

所有金额/利率/汇率/LTV 字段 MUST 用定点十进制（DECIMAL/NUMERIC 或语言层 Decimal），MUST NOT 用 float/double，也 MUST NOT 在计算链路中途做 Decimal→float→Decimal 转换。

浮点会引入精度漂移，资金字段一旦失真即为不可逆资损隐患且无法平账。这是金额承载金融事实的底线，CI 中以 AST/Semgrep guard + 精度测试机器校验。

> 出处：03-data-persistence.md / 01-rule-packs.yaml

### II. 资金写操作幂等 + 事务 Outbox

所有产生资金/状态副作用的写操作 MUST 幂等：以 UNIQUE(tenant_id, business_scope, idempotency_key) 的 DB 约束去重，幂等记录与业务写入 MUST 同事务；跨服务写副作用 MUST 经事务 Outbox（与业务写同事务）承接。MUST NOT 先执行副作用再补写幂等记录，MUST NOT 仅靠内存/缓存保存幂等状态，MUST NOT 用一次同步 HTTP/RPC 承担最终一致性，MUST NOT 在业务事务提交前直接调下游再补偿写 Outbox。

重试、重复点击、进程重启、并发都会重复触发副作用；同事务 + DB 唯一约束是唯一能扛进程重启与并发的去重保证。先调下游后写记录会在崩溃/超时时双写不一致，Outbox 把副作用意图与业务数据绑定提交，保证可靠投递与可恢复。

> 出处：04-write-reliability.md / 01-service-architecture.md / 01-rule-packs.yaml

### III. result_unknown 不得当失败处理

下游超时/连接中断/5xx 无确认结果 MUST 标记为 result_unknown，走查询→以原 idempotency_key 重投→dead_letter→对账差异→人工的恢复闭环。MUST NOT 直接判定为确定失败并触发补偿，MUST NOT 换新幂等键盲目重投同一副作用。

把未知结果当失败会触发重复补偿扩大损失；换新幂等键重投会绕过下游去重造成重复划转。结果未知必须先查证再处理。

> 出处：04-write-reliability.md / 01-rule-packs.yaml

### IV. 安全身份事实只来自服务端可信上下文 + fail-closed

tenant_id / operator / role / permission / screening_status 等安全身份事实 MUST 来自服务端已验证的认证上下文（JWT claim / server session / S2S token 解析）；认证失败 / 租户或权限不可确定 / S2S token 或 mTLS 不可信 / 入参无效 / AML 非 PASSED 或已过期时 MUST 拒绝（fail-closed）。MUST NOT 从 request body / query / path / form / cookie / 客户端可伪造 header 读取这些字段，MUST NOT 降级为匿名内部调用、默认租户或继续执行业务副作用。

客户端可伪造字段直接信任会导致越权与审计操作人伪造；任一安全边界 fail-open 都可能造成资金越权流出或跨租户泄露。可信来源 + 默认拒绝是租户隔离、RBAC、审计不可抵赖的共同地基，正则/AST checker 直接 BLOCK。

> 出处：16-multi-tenancy-isolation.md / 06-security-compliance.md / 01-rule-packs.yaml

### V. 多租户隔离由默认 scope 强制注入

多租户业务表 MUST 含 tenant_id NOT NULL，所有 SELECT/UPDATE/DELETE 及缓存/锁/幂等/消息 key MUST 经 ORM 默认 scope 或 Repository 统一入口强制注入 tenant 过滤。MUST NOT 依赖业务代码记得加过滤、先全表查再应用层过滤、或裸 SQL 跳过 tenant scope。

靠人记得加过滤必然漏，默认 scope + 强制隔离键是防跨租户读写越权和缓存串租户的工程底线，由越权测试在 merge 前机器校验。

> 出处：16-multi-tenancy-isolation.md / 01-rule-packs.yaml

### VI. 审计经统一 helper、append-only、防篡改链 + 敏感脱敏

所有审计记录 MUST 经统一 audit helper 写入、append-only（修正只能追加 correction_of 记录）、并具备 prev_hash / row_hash / canonical_payload_hash 防篡改链，资金类审计 MUST 含 biz_seq_no；token / password / 私钥 / 助记词 / 完整身份证 / 完整钱包地址 / 完整银行卡号等敏感值 MUST 在落库、日志、响应、审计 before/after、异常、trace 前经 redaction filter 脱敏。MUST NOT 在业务代码直接 INSERT 审计表或 UPDATE/DELETE 已写审计原记录，MUST NOT 让敏感值明文进入任何出口。

绕过 helper 或可改写记录会让 operator/hash/脱敏失效、链式校验无法证明不可抵赖；敏感凭证明文一旦落库/落日志即构成不可逆泄露。脱敏与 hash chain 校验必须在后端各出口统一完成，由 hash chain 校验 + 脱敏测试机器把关。

> 出处：13-audit-engineering.md / 06-security-compliance.md / 01-rule-packs.yaml

### VII. 服务间调用双层鉴权 + 透传四标识符 + 结构化日志

服务间调用 MUST 同时使用 S2S token 与 mTLS 双层身份校验、设显式超时（普通查询≤10s / 金融写≤30s），接收方 MUST 校验 token、服务白名单、有效期、作用域与调用方身份；应用日志 MUST 为字段名稳定的结构化 JSON，所有请求/业务命令/下游调用/异常路径 MUST 含 trace_id + request_id（资金类额外含 biz_seq_no），且服务间 MUST 透传四标识符。MUST NOT 仅凭内网 IP/网段/自报 X-Service-Name 完成认证，MUST NOT 在下游重生成 X-Biz-Seq-No，凭据 MUST NOT 入代码仓/日志/响应/异常上下文。

仅信内网或自报服务名等于无认证，token+mTLS 双层叠加才能防内网横向越权与服务身份伪造；四标识符 + 结构化日志是跨服务追溯、对账与审计关联的唯一机器可校验抓手，下游重生成流水号会断链。

> 出处：06-security-compliance.md / 01-service-architecture.md / 05-observability-audit.md

### VIII. 约束沉淀为 merge 前机器门禁 + AI PR 强制 REQ 锚点与测试

安全/审计/可观测/隔离约束 MUST 沉淀为权限测试、越权测试、脱敏测试、secret scan、SQL 探针、hash chain 校验与 Semgrep/AST guard，并在 merge 前作为 CI gate 执行；任何 AI 提交的 PR MUST 关联 REQ-N 锚点、MUST 包含对应 test 文件变更、改动 MUST 落在已声明的 bounded context 内，违反任一 P0 规则或越界 MUST 触发 Policy Engine BLOCK 进入人工裁决。MUST NOT 用人工说明替代证据、把缺口留到上线后观察、由 AI 自行放行 BLOCK，也 MUST NOT 缺锚点/缺测试/越界仍进入后续审查。

留给人工评审记忆或上线后巡检等于无约束；Policy-as-Code 跑在 pre-commit + PR gate + merge gate，是补 AI 记忆不可靠的最后机器防线，CI 失败必须阻断合并、BLOCK 必须升级 Human Gate。

> 出处：06-security-compliance.md / 03-agent-review-pipeline.md / 02-policy-engine.md

### IX. 外呼必设显式超时，写类操作超时转 RESULT_UNKNOWN 而非失败

所有对银行侧（wedap）的外呼 MUST 显式设置 timeout（`GW_WEDAP_TIMEOUT_SECONDS` 默认 10.0s，`app/core/config.py:11` 定义、`app/main.py` 注入 WedapClient，`app/clients/wedap.py:18` 为构造参数）；仅幂等操作可重试，资金写类外呼（disbursement / repayment / collection / distribution）超时/网络异常 MUST 转 `RESULT_UNKNOWN` 态并走查状态确认后再定最终态。MUST NOT 把超时/异常直接标记 order status = FAILED，MUST NOT 对非幂等写盲目重试。

无 timeout 的外呼会挂死、占满连接池；把超时当失败会触发上层重复发送造成重复划转。超时是"未知"而非"失败"，必须查证。

> 出处：app/core/config.py:11（wedap_timeout_seconds）/ app/clients/wedap.py:18 / app/services/outbox.py:93-96（claim_timeout 回收）/ app/domain/states.py:RESULT_UNKNOWN

### X. 银行回调入站三元组幂等去重 + payload-drift 保护

回调入站（`POST /api/v1/callbacks/wedap/transactions`）MUST 经 `require_headers` 校验 `X-Tenant-Id` + `X-Request-Id`，并以三元组 `(tenant_id, source="WEDAP_TXN", request_id)` 唯一约束（`uq_inbox_tenant_src_req`）防重落库；命中 IntegrityError MUST 走幂等 dedup 路径而非报错，补偿重放 MUST 使用首份落库的 payload（payload-drift 保护）而非本次请求体。MUST NOT 让同一回调重复驱动业务副作用。

银行回调天然无序重发，三元组幂等 + 首份 payload 重放把"银行自动重试"转化为可靠补偿驱动；用本次请求体重放会因 payload 漂移产生不一致。

> 出处：app/api/v1/callbacks.py:89-193 / app/models/callback.py（uq_inbox_tenant_src_req）

### XI. external_ref 全局唯一 + (biz_seq_no, step_seq) 分腿 + 复合键租户隔离

银行侧边记录 BankTxnLeg MUST 同时满足两个唯一约束：`(tenant_id, external_system, external_ref)`（`uq_leg_tenant_ext`，每个银行交易号只入一条）与 `(tenant_id, biz_seq_no, step_seq)`（`uq_leg_tenant_biz_step`）——故一个 biz_seq_no MAY 对应多条 leg/多个 external_ref（组合交易按 step_seq 分腿）；leg 指向 order 的外键 MUST 为复合键 `(order_id, tenant_id)` 在 DB 层强制租户边界。MUST NOT 跨租户引用 order，MUST NOT 复用同一 external_ref 关联多笔银行流水。

external_ref 是对账的银行侧外键、须全局唯一；biz_seq_no↔external_ref 是 1:N（按 step_seq 分腿），把它当 1:1 会在组合交易下错配。复用 external_ref 或跨租户引用会直接错关资金、破坏对账。

> 出处：app/models/txn.py:47-56（uq_leg_tenant_ext + uq_leg_tenant_biz_step + 复合 FK fk_leg_order_tenant）

### XII. Outbox 至少投递一次，最终态由下游按 dedup_key 幂等保证

出站投递 MUST 经 outbox dispatcher 做至少一次（at-least-once）投递：指数退避重试（间隔 2^(attempts-1) 秒，上限 8 次），多副本部署在超 `claim_timeout`（300s）后允许另一副本 reclaim，故同一消息 MAY 被双投——下游消费方 MUST 按 dedup_key 幂等吸收。MUST NOT 假设 outbox 是 exactly-once，MUST NOT 在未确认结果前推进为最终态。

at-least-once + 下游幂等是分布式可靠投递的标准组合；把 outbox 当 exactly-once 会在 reclaim 双投时造成重复处理。

> 出处：app/services/outbox.py:114-160（dispatcher / claim / reclaim / backoff）/ app/domain/states.py（OrderStatus 状态机）

### XIII. S2S 鉴权优先 per-service token 密码学绑定，凭据禁入日志

内部调用方身份 MUST 从 `X-Caller-Service` + `X-S2S-Token` 头认证，优先启用 per-service token 模式（`GW_S2S_CALLER_TOKENS=caller:token,...`）按 caller 单独校验（caller↔token 密码学绑定，单凭据泄露只能冒充该 caller）；回退共享 secret 模式（`GW_S2S_SECRET`）时，仅当配置了调用方白名单（`GW_S2S_CALLERS` 非空）才校验白名单——白名单缺省为空即不启用（当前默认 `s2s_callers=''`）。MUST NOT 把 S2S token / 银行 API key 打进日志。

per-service 绑定限制凭据泄露的爆炸半径；共享 secret 模式仅靠白名单做兜底归因，而白名单缺省不启用是已知弱点（fallback 下任一持密钥方可冒充任意 caller），代码注释标记 v2 将以 per-service token/签名绑定取代——生产 SHOULD 显式配置白名单。

> 出处：app/core/s2s.py:55-83（两层 token 校验 + 白名单可选）/ app/core/config.py:17-32（s2s_caller_tokens / s2s_callers 默认空）

## 技术栈与附加约束

**运行时与框架**：服务 MUST 用 Python `3.12`，Web 层 FastAPI `0.115+` + uvicorn，ORM 用 async SQLAlchemy `2.0+` + asyncmy（MySQL），SQLite 仅用于测试。监听端口 8022（dev；待 baffle 退役后接管 8021，见 §待确认）。金额列 MUST 用 `Numeric(21,4)`（总精度 21、小数 4 位，即整数部分最多 17 位；`app/models/txn.py:34`），SQLite 测试态在 alembic 0001 降级为 Integer。

**依赖锁（单轨 uv）**：依赖权威源 MUST 是 `pyproject.toml`，锁文件 `uv.lock` 由 uv 维护，本仓**无 requirements.txt**；部署 MUST 在启动前置 `alembic upgrade head`。

**时区**：银行时间用 `GW_BANK_TIMEZONE`（缺省 `Asia/Hong_Kong`），银行 txn_date（YYYYMMDD）MUST 透传不重算（避免跨时区改写银行业务日）；应用层内部时间仍遵循 UTC-aware 写库。文档/会话回复一律北京时间（UTC+8），引用系统/日志原文时戳保留 UTC 并加 `(UTC)`。

**审计 append-only + per-tenant hash chain**：`audit_log` 表为 append-only（MySQL 触发器禁止 UPDATE/DELETE，migration 0005），per-tenant hash chain 以 `audit_chain_head` 锚点 `FOR UPDATE` 串行化防分叉（A-M-003）；migration 0005 依赖 MySQL 全局 `log_bin_trust_function_creators=1`（触发器创建权限）。

**worker 连接池隔离**：API 连接池 pool_size=5 + max_overflow=10，outbox/recon worker 用独立连接池 pool_size=3 + max_overflow=5，MUST NOT 让慢外呼/长事务与在线请求争抢连接（A-M-003）。

**安全扫描**：S2S token / 银行 API key MUST NOT 入日志（s2s.py 注释）；密钥 MUST NOT 入代码仓/响应/异常上下文。

## 开发工作流与质量门禁

**本地自检（四步等价 CI）**：合并/推送 main 前 MUST 跑通 `pytest -q`（覆盖率门禁）、`pytest -m integration --no-cov -q`（testcontainers MySQL，与单测分离）、`ruff check .`、`mypy app`（strict）。任一失败 MUST NOT merge。

**覆盖率底线（本仓最硬约束）**：`fail_under=100%`（全 pytest suite，金融网关零容忍漏测）；每个改动 MUST 同步对应测试，测试结果 MUST 0 fail / 0 unexpected skip。

**CI 门禁（.github/workflows/ci.yml）**：MUST 跑 `ruff check .` + `mypy app` + `pytest -q` + `pytest -m integration --no-cov -q`。OpenAPI 契约 `contracts/openapi.json` 由 CI 快照校验，改 API MUST 先更新快照（`pytest -k test_openapi_snapshot --snapshot-update`）。`tests/compat/`（直连 wedap dev 兼容套件，标记 compat）为里程碑/nightly 手动触发，不进 CI 必跑集。

**禁刷假绿**：MUST NOT 写无断言测试、静默 skip testcontainer、只测 happy path、或用 mock 掩盖真实 DB 约束来凑覆盖率/门禁。

**DB 迁移 expand/contract**：不兼容 schema 变更 MUST 走 expand/contract；当前 alembic 8 个版本（0001 order/leg、0005 audit append-only 触发器、0007 audit_chain_head 锚点、0008 outbox trace 字段），空库 `alembic upgrade head` MUST 通过。部署前置踩坑：asyncmy 账号 MUST 用 `mysql_native_password`（caching_sha2 会连接挂起）。

**部署不等于完成（三步真机验证）**：dev-hw 部署后 MUST 按 (a) 即时证据（健康 + git_sha 比对）+ (b) 等 1 个调度 tick（outbox dispatcher 真跑）+ (c) 双重 tick + active 对照三步全 PASS 才标 COMPLETE，未闭环 MUST NOT 宣告完成。

**Agent Review Pipeline 闸门**：Stage 0 Pre-flight MUST 校 REQ 锚点 + test 文件变更 + bounded context；同一改动单元自检自修复 ≤3 次，第 3 次仍 FAIL MUST 升级人工；COMPLIANCE_FAIL / ARCH_FAIL / ADR_CONFLICT MUST NOT 降级为 WARN 放行，豁免 MUST 创建 ADR。

## Governance

**本宪法高于其它一切实践、约定与口头习惯**：当文档、注释、历史代码或个人偏好与本宪法冲突时，以本宪法为准；冲突项 MUST 在发现时修正或登记整改，MUST NOT 沿用旧习默认放行。

**修订流程**：任何对 Core Principles（共享核心 I–VIII 与网关级专属 IX–XIII）的增删改，以及对外呼超时转 RESULT_UNKNOWN、回调三元组幂等、external_ref 全局唯一 +(biz_seq_no, step_seq) 分腿、audit append-only hash chain、多租户复合键隔离等宪法级不变量的变更，MUST 经 ADR 记录（含动机、影响面、回滚方案）+ Architecture Committee 审批后方可生效，MUST NOT 由 Agent 在普通 PR 中擅自修改。Section 2/3 的工具链/门禁参数调整经常规 PR + 评审即可，但降低覆盖率底线（100%）或放宽 S2S/回调鉴权视同宪法级变更，须走 ADR。

**违反处理**：违反 P0 规则或本宪法任一 MUST 条款的改动 MUST 触发 Policy Engine BLOCK 并进入 Human Gate 人工裁决，CI 失败 MUST 阻断合并；MUST NOT 用人工说明替代机器证据、由 AI 自行放行 BLOCK、或把缺口留到上线后观察。豁免 MUST 以 ADR 形式留痕，注明范围与到期复核时点。

**版本递增规则**：本宪法采用语义化版本。MAJOR 用于不向后兼容的治理重定义或删除/重写某条 Principle；MINOR 用于新增 Principle 或实质性扩充某节约束；PATCH 用于措辞澄清、出处订正、不改变约束语义的修订。每次修订 MUST 更新下方版本行与 Last Amended 日期，并在 ADR 中记录变更摘要。

## 待确认项

- **回调入站签名验证缺失**：当前回调仅做三元组幂等去重（`callbacks.py`），未见 HMAC/签名校验——shared 原则 IV/VII 期望验签，应确认 wedap 是否提供签名头并补入站验签 (待确认)。
- **payer/payee 账户脱敏 vs 共享原则 VI**：本仓 payer_account / payee_account 当前 `String(64)` 直存不脱敏，查询 API 透传给 recon 消费（`app/models/txn.py`）——与共享原则 VI「完整银行卡号 MUST 脱敏」存在已知偏差，需定夺：查询层按权限脱敏 / 字段级加密 / 仅靠访问控制兜底 (待确认)。
- **audit payload 敏感字段**：audit 日志 payload 直存 JSON，无字段级脱敏，是否需对 card/account 显式脱敏抑或靠访问控制 (待确认)。
- **mTLS / 签名落地状态（已知 gap，非仅悬挂）**：共享原则 VII 要求 S2S token + mTLS 双层，但 `app/core/config.py` 注释明确"mTLS/签名绑定为后续增强"，本仓代码仅见 S2S token 层——应作为明确整改项跟踪，确认 mTLS 是否在网关/网络层（如 service mesh）另行实施 (待确认)。
- **端口 8021 接管时点**：dev 现用 8022，"待 baffle 退役接管 8021"的切换时点与配置变更未定 (待确认)。
- **S2S 白名单去留**：per-service token 优先后，共享 secret 白名单（`GW_S2S_CALLERS`）在 v2 是直接删除还是保留为兼容层 (待确认)。

**Version**: 1.0.0 | **Ratified**: 2026-06-17 | **Last Amended**: 2026-06-17
