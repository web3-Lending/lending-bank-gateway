# lending-bank-gateway v1 范围与口径设计（spec）

> 定稿: 2026-06-11 | 状态: 用户已确认设计 + codex 两轮评审（方案对比 VERDICT:C / 全量评审 NEEDS-CHANGES 12 P1 全吸收）
> 上游决策: [ADR-0031](../../../../lending-workspace/04-decisions/0031-新建lending-bank-gateway统一对接wedap-baffle冻结现状.md)（新建 gateway + baffle 冻结）· [ADR-0029](../../../../lending-workspace/04-decisions/0029-biz_seq_no格式与生成算法.md)（biz_seq_no 格式）
> 推导过程: [02-proposals/20260611-bank-recon对接wedap对账结果与gateway方案分析.md](../../../../lending-workspace/02-proposals/20260611-bank-recon对接wedap对账结果与gateway方案分析.md)

## 0. 一句话定位

lending-bank-gateway 是 lending 项目群对接 wedap（银行资金通道平台）的**唯一出入口**：北向以 lending 工程规范封装的稳定契约服务项目群，南向对接 wedap，统一承担防腐、幂等、leg 落盘、回调分发与对账结果摄取。生产组件，资金链路单点，按 lending-core 级质量门禁建设。

## 1. 目标（4 项，用户原始口径）

1. 打通 lending ↔ wedap 接口，两边接口协调到一致
2. 协调 `biz_seq_no` / `external_ref` / `txn_date` 三键，解决组合交易多对一问题
3. 支持 lending-recon 完成与银行流水的对账
4. 天然支持 `05-reference/engineering-standards` 全部 16 份工程规范

## 2. 契约姿态（已拍板：方案 C）

**业务字段一致 + 封装层本地化**（codex 方案对比 VERDICT: C）：

- **业务字段两边同值、禁 normalize**：`biz_seq_no`、`external_ref`、`txn_date`、金额（DECIMAL(21,4) 字符串序列化，规范 03）、状态枚举，北向南向原样透传，单号全链路同值
- **HTTP 封装层各按其规范**：北向 envelope 按规范 02（`success/data/error/trace_id` 顶层 + `X-Request-Id` / `Idempotency-Key` / `X-Trace-Id` / `X-Biz-Seq-No` 四标识符 header）；南向按 wedap 现契约
- **异步收妥语义镜像 wedap**：同步响应只代表受理（含 `txnStatus`），最终结果经回调链路（wedap → gateway → 调用方）+ `RESULT_UNKNOWN` 查询收敛（规范 04）；**HTTP 200 ≠ 资金完成**
- **护栏**：OpenAPI 契约（规范 10 AI-first JSON）+ contract tests 钉死字段语义；任何北向字段改名/重解释 = 契约违规（防"半本地化滑坡"退化成无治理的方案 B）
- **biz_seq_no 格式前提**：使用 ADR-0029 格式（`<biz_type>-<YYYYMMDD>-<snowflake>`，由 lifecycel hub 生成）。需 wedap **书面确认**放宽校验至「唯一 + ≤32 字符 + 字符集合法」。被拒绝时的降级路径（二选一，codex 裁定）：①转方案 B，映射表升级为一级账务基础设施治理；②要求 wedap 支持 `clientBizSeqNo` 双字段且 journal/recon/steps 全部回吐 lending 原单号

## 3. v1 范围（双轨制，codex P1-1）

> 基线来源：三路并行扫描——baffle(8021) 暴露 72 条路由，lending 实际在用 22 条；
> 调用方 = lending-lifecycel(15) / liquidation-backend(4) / lending-customers(7)，经 BFF `/internal/proxy/baffle` 反代或直连。

### 3.1 v1-cutover 轨（wedap 对应物已确认，可进切换）

| # | 能力 | 北向接口（gateway 契约） | wedap 对应物 | 现调用方 |
|---|---|---|---|---|
| 1 | P2P 放款 | POST /api/v1/loans/p2p-disbursements | ✅ WeDAPAPI-Lending 3.1.1 | 9000 bank_p2p |
| 2 | P2P 还款 | POST /api/v1/loans/p2p-repayments | ✅ 3.1.2 | 9000 bank_p2p |
| 3 | 资金归集 | POST /api/v1/bank-funds/collect-from-users | ✅ user-collections | 9000 + liquidation |
| 4 | 资金分发 | POST /api/v1/bank-funds/distribute-to-users | ✅ user-distributions | 9000 |
| 5 | 资金状态查询 | GET /api/v1/bank-funds/status | ✅ 状态查询 | 9000（RESULT_UNKNOWN 收敛） |
| 6 | 组合交易 leg 查询 | GET /api/v1/composite-transactions/{bizSeqNo}/steps | ✅ wedap-adapter 同名 | gateway 内用 + 排查 |
| 7 | 存款总余额 | GET /api/v1/deposit/balances/total | ✅ WeDAPAPI-Public 4.2.x | 9000 |
| 8 | 存款账户列表 | GET /api/v1/deposit/accounts | ✅ Public 4.2.2（20260331 调查核对对齐） | 9000 + customers |
| 9 | 用户信息查询 | GET /api/v1/users/info | ✅ Public 3.3.1 | customers |
| 10 | 对账结果摄取 | POST /api/v1/recon/notify（wedap → gateway） | ✅ 契约已定稿（2026-06-08 v1.0.0） | wedap 主动调 |
| 11 | 交易结果回调接收 | POST /api/v1/callbacks/wedap/transactions（wedap → gateway） | ✅ LendingTransactionNotifyService | wedap 主动调 |
| 12 | 回调转发（cutover 仅 9000） | gateway → 9000（outbox 投递）；**customers webhook 兼容转发随 C4/迁移阶段 5 放行**，不在 cutover | — | 9000 transaction-callback |
| 13 | recon 对账供数 | GET /api/v1/fiat-vault/transactions 平移 + gateway 库跨库读 | 不依赖 wedap（gateway 自有库供数） | recon (8040) |

### 3.2 v1-coordination 轨（wedap 对应物未确认/不存在，逐项协调，确认一项放行一项，不阻塞 cutover）

| # | 能力 | 现状证据 | 协调内容 |
|---|---|---|---|
| C1 | 冻结/解冻（清算待上线需求） | wedap 仅有 holdAmount/FROZEN 概念，无操作接口 | wedap 提供 freeze/unfreeze 或等价能力 |
| C2 | 法币汇率查询（用户要求保留） | wedap 仅有 fx-rate 数据导入方向（Public-4.5.1 §4.5.4），无查询接口 | wedap 提供汇率查询，或 gateway 从 fx-rate 导入数据反向供查 |
| C3 | Exchange 锁汇兑换（quotes/trades/status） | 疑似 wedap-counter 能力，未核实 | 确认对应接口与两阶段语义 |
| C4 | 钱包/法币转账 | `/admin/v1/app/wallet/*` 不在 WeDAP 对外契约；客户间转账无发起端接口（20260331 调查实锤，`CUSTOMER_TRANSFER` 仅作流水枚举） | wedap 开放客户间转账发起端，或该能力降级 |
| C5 | 账户划扣 deduct（自营追保） | baffle 自建路径，wedap 对应未核 | 确认对应接口 |
| C6 | 退款 refund（liquidation） | baffle 自建路径 | 确认对应接口 |
| C7 | 银行用户创建 POST /users/ | WeDAP 开户能力归属（疑在 BankCore） | 确认 |
| C8 | 账户流水查询 | WeDAP Public §4.2.4 `/api/v1/deposit/transactions` 语义近似路径不同 | 契约映射核实 |
| C9 | 余额直查两条（accounts/{id}/balance 等） | baffle 自建路径，语义对应 deposit 系列 | 契约映射核实 |

### 3.3 排除项（已核实，不进 gateway）

custody 系列（liquidation 走 `CUSTODY_SERVICE_URL`→8100 mock / lending-custody 体系）· admin dashboard 16 条 · 借据计息 mock 内部能力 · fiat-wallet v2 路径（零调用方；北向新契约取代新旧两套 wallet 路径）· fxRates 手动管理（refresh/PUT，仅查询保留=C2）

### 3.4 调用方改造矩阵（codex P1-2：不是"只改 base URL"）

每个调用方逐接口需要三类改动，实施计划逐条展开：

| 调用方 | 接口数 | envelope 解析改造 | 异步语义改造 | RESULT_UNKNOWN 收敛 |
|---|---|---|---|---|
| 9000 bank_p2p/bank_fund/account_8021/exchange_8021 | 15 | code/msg/data → success/data/error/trace_id | 同步 txnStatus 解析改为「受理 + 回调/查询终态」，借鉴现有 PROCESSING 轮询 | 现有 result_unknown_query_worker 对接 gateway 状态查询 |
| liquidation-backend wedap_bank_client | 4 | 同上 | collect/refund 异步化 | 新增收敛路径 |
| lending-customers wedap_client | 7→收口后减少 | 同上 + 新旧 wallet 路径合一 | fallback 链（404/502→None）重写 | 新增 |
| BFF `/internal/proxy/baffle` | 反代路径 | 切换 audience 指向 gateway（走 ssot-cutover SOP） | — | — |

## 4. 三键口径

| 键 | 口径 | 权威 |
|---|---|---|
| `biz_seq_no` | lifecycel hub 生成（ADR-0029 格式），全链路透传同值；gateway 不生成业务单号 | ADR-0029 |
| `external_ref` | = wedap leg `sysRefNo` = 银行 `bank_seq_no`；**leg 级 1:1**：`external_ref ↔ (tenant_id, biz_seq_no, step_seq)`；父 biz_seq_no ↔ N leg 合法；禁止父单单列存 external_ref | 规范 07 §2（本设计配套修订澄清 leg 粒度） |
| `txn_date` | 沿用 [20260605-txnDate 方案](../../../../lending-workspace/02-proposals/20260605-txnDate-recon-date-跨系统对账日期设计方案.md) 定稿口径：txnDate=BANK_TIMEZONE YYYYMMDD 透传不重算；txnTimestamp=UTC ms；recon_date=T-1 BANK_TIMEZONE（WeDAP M7 recon_date 固定 HKT）。**gateway 不二次推算业务日期**（codex P2） | 20260605 方案 §14.3/14.4 + 规范 11 |

## 5. 数据模型（库 `lending_bank_gateway`）

> 全表带 `tenant_id`（规范 16）；审计 append-only + hash chain（规范 13）；金额 DECIMAL(21,4)（规范 03）；expand/contract 迁移（规范 14）。

| 表 | 关键约束 | 说明 |
|---|---|---|
| `bank_txn_order` | `UNIQUE(tenant_id, biz_seq_no)`；FK 无（顶层） | 业务单：biz_type、amount、currency、caller_service、business_action、状态机（§6）、submitted_at/acked_at/finalized_at |
| `bank_txn_leg` | `UNIQUE(tenant_id, external_system, external_ref)`；`UNIQUE(tenant_id, biz_seq_no, step_seq)`；FK→order_id | leg：step_type、step_seq、external_ref、amount、currency、payer/payee、status、posted_at；REVERSAL 冲正 leg 事后幂等 upsert 追加 |
| `idempotency_record` | `UNIQUE(tenant_id, business_scope, idempotency_key)` | codex P1-4：存 method/path/payload_hash/first_response/final_effect_id；同 key 不同 payload_hash → 409 |
| `callback_inbox` | `UNIQUE(tenant_id, source, request_id)` | wedap 入站回调/notify 幂等登记，原始 payload 落档（不依赖 request_id 全局唯一的假设） |
| `callback_outbox` | 状态机 PENDING/SENT/FAILED/DEAD | gateway→9000/customers 转发可靠投递（规范 04 Outbox），dead letter 重放入口 |
| `exchange_quote` / `exchange_trade_order` | quote 含汇率快照 + 有效期 | codex P1-8：独立于 order/leg；幂等重放不得重取实时汇率（规范 12）；随 C3 协调放行 |
| `query_audit` | — | 查询类接口落 request metadata + payload hash + trace_id，不落业务响应全量（codex P1-7） |
| `balance_snapshot` | — | 余额类最小快照（preflight 争议复盘用） |
| `recon_result_task` | `UNIQUE(request_id)`；taskNo+version supersede | 含 s3_bucket/key/md5、**原始文件存档引用、parser_version、schema_version、列校验结果**（codex P2） |
| `recon_result_diff` / `recon_result_source_wedap` / `recon_result_source_bank` | 按 task_no+version 归属 | 3-sheet 解析落库；source_wedap 带 biz_seq_no + bank_biz_seq_no 双索引 |

## 6. 状态机（codex P1-5）

**order**（父单）：

```
ACCEPTED → SUBMITTED → PROCESSING → SUCCEEDED | FAILED | EXPIRED | CANCELLED
                  ↘ RESULT_UNKNOWN（查询收敛后回到 PROCESSING/终态）
终态后追加：SUCCEEDED → PARTIALLY_REVERSED → REVERSED（仅由冲正 leg 聚合驱动，原终态记录不可改，规范 12 调账冲正模型）
```

**leg**（镜像 wedap 枚举）：`PENDING → SUCCESS | FAILED | UNKNOWN`；`REVERSED`（原 leg 被冲正标记）；`REVERSAL`（冲正 leg 本身）。

**父子聚合规则**：

| leg 集合状态 | order 状态 |
|---|---|
| 全部 SUCCESS | SUCCEEDED |
| 任一 FAILED 且无在途 | FAILED（saga 已补偿则伴随 REVERSAL leg，净额为零） |
| 存在 UNKNOWN/PENDING | PROCESSING / RESULT_UNKNOWN |
| SUCCESS 单 + 事后 REVERSAL 部分覆盖 | PARTIALLY_REVERSED |
| SUCCESS 单 + REVERSAL 全覆盖 | REVERSED |

非法转移显式建表测试；终态（SUCCEEDED/FAILED/EXPIRED/CANCELLED）不可逆写（只允许冲正追加路径）。

## 7. 对账支撑（四段模型，codex P1-3 补第 0 段）

```
第0段 发起意图对账: wbt_admin.admin_bank_intent ↔ bank_txn_order
       （tenant_id + biz_seq_no + business_action 对齐；缺 order = lending 发了但 gateway 没收到 → finding）
第1段 受理对账:     bank_txn_order/leg ↔ WeDAP Source（GROUP BY biz_seq_no 聚合 + external_ref 集合相等串单校验）
第2段 执行对账:     wedap ↔ 银行（wedap 已对完，Differences 透传成 finding，version supersede 关旧 finding）
第3段 三角抽查(可选): lending leg external_ref 直接 join Bank Source（存在性+金额，独立验证 wedap 无系统性漏记）
```

**第 0 段权威数据源（定稿，不留调查项）**：9000 新增 `admin_bank_intent` 表（属 9000 整改范围新增项，与 biz_seq_no 统一整改同批；复用 9000 既有 outbox 基建先例 `admin_event_outbox`，与 W2 `admin_external_call_result_unknown` 机制衔接）：

- 约束/列：`UNIQUE(tenant_id, biz_seq_no)`；`business_action`（DISBURSE/REPAY/COLLECT/DISTRIBUTE/…）、`amount`、`currency`、`caller_module`、`status`、`created_at/submitted_at`
- 写入时机：与业务状态变更**同库同事务**先落 `INTENT_CREATED`（事务内不发外部 HTTP，规范 14），事务提交后发起 gateway 调用，按结果推进 `SUBMITTED / SUBMIT_FAILED / ABORTED`；RESULT_UNKNOWN 单在 intent 标记并由现有 W2 worker 收敛
- 对账规则：`status≥SUBMITTED` 超宽限窗口而 gateway 无对应 order → finding；`INTENT_CREATED` 卡住超窗（发起前夭折）→ finding；`ABORTED`（前置校验失败/草稿）不参与对账
- liquidation / customers 调用方在各自迁移阶段（§10）接入同模式

- recon 侧沿用**跨库只读 collector** 模式读 `lending_bank_gateway` 库（先例 BaffleCollector）
- 新规则供给：就绪性（每 tenant×对账日 T+1 截止前必须收到 notify）/ 差异透传 / 第 0+1 段对账
- **BANK-RECON-005 余额对账 v1 降级口径**（codex P2）：降级为诊断（DIAG），以 `balance_snapshot` 作弱数据源；恢复 ERROR 级以 wedap 提供余额对账数据源为前提（协调项）

## 8. 可靠性与安全

- **幂等**：北向写操作按 `(tenant_id, business_scope, idempotency_key)` 三元组（§5）；入站回调按 `(tenant_id, source, request_id)` 去重（与 §5 `callback_inbox` 约束一致）
- **入站回调安全（codex P1-12，如实声明）**：wedap recon notify **无报文签名**（仅 X-Tenant-Id）。v1 补偿控制：IP allowlist + 内网 S2S token + 时间戳重放窗口 + body/S3 双 md5 校验 + taskNo+version 幂等；报文签名列为 wedap 协调项
- **出站**：全部外呼强制 timeout（07 §1）+ 断路器（沿用 9000 模式）+ 连接池舱壁；事务内禁外部 HTTP（规范 14）
- **鉴权**：北向沿用项目群 S2S（HMAC/svc-JWT）；南向按 wedap 要求
- **健康（codex P2）**：/healthz、/readyz（含 DB migration version、wedap connectivity、S3 可达）

## 9. 测试与 dev 策略

- **CI 门禁（codex P1-9）**：checked-in OpenAPI/schema fixture + HTTP client boundary mock + contract replay；覆盖率与异常路径全部本地闭环，按 lending-core 级门禁（覆盖率底线 + ruff/mypy + 金融 9 维）
- **联调（用户拍板 B）**：直连 wedap dev 环境，定位为 nightly / 里程碑 compatibility suite，**不作为覆盖率来源**
- 技术栈：FastAPI + MySQL（ADR-0006/0007 惯例）

## 10. 迁移与回滚（五阶段灰度，codex P1-10）

| 阶段 | 切换内容 | 回滚 |
|---|---|---|
| 1 | read-only 查询（deposit 余额/账户/用户信息） | per-caller feature flag 回切 8021 |
| 2 | 非资金写（用户创建等，待 C7 放行） | 同上 |
| 3 | 单一资金写（collect/distribute——cutover 轨已确认项；**refund 不在本阶段**，待 C6 放行后并入，或经协调证实 liquidation refund 复用已确认的 user-distributions 后提前并入） | flag 回切 + 在途单收敛后切换 |
| 4 | 组合交易（p2p-disbursements/repayments） | 同上 + 对账双跑核对 |
| 5 | customers wallet（待 C4 放行；新旧路径收口；customers webhook 兼容转发随本阶段，见 §3.1 #12 拆分） | 保留旧 fallback 链直至验收 |

每阶段独立 feature flag；BFF 反代 audience 切换走既有 ssot-cutover SOP；回切目标 8021 在 baffle 冻结期内始终可用。

## 11. wedap 协调清单（汇总，挂 FU-BANK-GATEWAY-ALIGN）

1. biz_seq_no 格式放宽书面确认（§2 前提，被拒走降级路径）
2. 回调/notify 报文签名（§8）
3. freeze/unfreeze 操作接口（C1）
4. 法币汇率查询接口（C2）
5. Exchange 对应物（C3）
6. 客户间转账发起端（C4）
7. deduct/refund/users-create/流水查询/余额直查契约映射（C5-C9）
8. 余额对账数据源（§7 BANK-RECON-005 恢复前提）
9. 在途单回传 SLA（第 1 段对账宽限窗口数值）

## 12. 配套文档动作

- [x] ADR-0031 §2.2 表述修订：北向「WEDAP 风格契约」→「lending envelope（规范 02）+ WEDAP 业务字段」
- [x] 规范 07 §2 修订：组合交易 leg 级 1:1 澄清
- [x] FU-BANK-GATEWAY-ALIGN 更新为本 spec §11 清单
- [ ] 实施计划（writing-plans 产出，spec 批准后）

## 13. 评审记录

| 轮次 | 评审 | 结论 |
|---|---|---|
| 1 | codex consult：契约姿态 A/B/C 对比（130k tokens） | VERDICT: C；揪出 ADR-0031 自相矛盾 + 07 §2 leg 粒度阻塞项 |
| 2 | codex 全量设计评审（363k tokens，resume session） | NEEDS-CHANGES：12 P1 + 5 P2，本 spec 已全量吸收（§3 双轨 / §3.4 改造矩阵 / §5 幂等三元组+tenant 约束+exchange 独立模型+查询落库策略 / §6 状态机 / §7 第 0 段 / §8 签名如实声明 / §9 CI 口径 / §10 迁移顺序） |
| 3 | codex spec 验收评审（593k tokens，resume session） | GATE: FAIL→修复：旧 P1 10/12 RESOLVED；剩余 3 P1 已修——§7 第 0 段定稿 `admin_bank_intent` 权威数据源（9000 整改新增项）/ §10 阶段 3 剔除 refund + §3.1 #12 拆分 customers 转发随 C4 / `callback_inbox` 唯一约束补 tenant_id |
