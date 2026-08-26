"""wedap 资金写原语的字段契约事实（单一真源）。

**来源是实测，不是文档**：2026-08-11 用「同一基线报文单变量逐个删/加字段」对 wedap dev
递进探测 23 笔得出，每一项都有 wedap 自身 400 文案自证（详见
`lending-workspace/01-investigations/20260811-wedap资金写原语真契约实测与dev全面500故障.md`）。
之所以不引对接文档：文档 v0.4.0 未随 wedap 侧「未定义字段严格拒绝」同步，实测才是权威。

为什么 gateway 要在入口挡而不是让报文直达 wedap：
- wedap 拒收会让 gateway 先落一条 FAILED `bank_txn_order`——纯字段错误污染资金台账
  （dev 上 33 条 FAILED 里近 20 条是这类），对账/告警要反复排除噪声；
- 上游 lending-lifecycel 明确要求 gateway 挡住（LFC-GW-CUTOVER 2026-08-06：
  「gateway `extra=allow` 不构成保护，缺字段会直达 wedap」）；
- 缺字段是**切换当天全线被拒**级风险，fail-fast 比事后查 wedap 报文便宜得多。

契约变更时**只改本模块**：端点与测试都从这里取值，不写死字面量。
"""

# ── collect（/api/v1/bank-funds/user-collections） ─────────────────────────────
# gateway 已由 pydantic/端点强校验 bizSeqNo / currencyCode / transType / txnAmount，
# 故此处只列「靠 extra=allow 透传、gateway 原先完全不看」的那部分 wedap 必填字段。
COLLECT_REQUIRED: tuple[str, ...] = (
    "channelId",
    "userId",
    "custAccountNo",
    "bankAccountNo",
    "bankAccountName",
)

# wedap 归集只认扁平字段；嵌套 user{} 直接 `Unknown field 'user'` 整包拒收（实测 E2）。
# 不能像 totalAmount 那样静默剪掉——userId/custAccountNo 等必填值就在 user{} 里，
# 剪掉等于把「结构不对」变成「字段缺失」，上游拿到的报错会指错方向。
# 文案用美式英语：与本仓既有错误消息（"missing txnAmount" 等）一致，且 message 会原样
# 返给调用方——按本仓语言规范（禁简体）用户可见文案只能是香港繁体或美式英语。
COLLECT_REJECTED: dict[str, str] = {
    "user": "wedap collection contract is a flat single-user shape and rejects the "
    "nested user{} object; lift userId/custAccountNo to the top level",
}

# collect transType 的封闭值域（wedap 自报：实测 E3 传旧值 USER_COLLECTION 时
# wedap 回「transType must be BANK_FUND_COLLECT_LOAN or BANK_FUND_COLLECT_CLEARING」）。
# 仅作契约记录，不在入口强校验——上游 lifecycel 的方言开关正处切换期，
# 值由 transType 定稿工单（FU-GW-TRANSTYPE-CUTOVER）收口，此处硬拒会拦住过渡期回滚。
COLLECT_TRANS_TYPES: tuple[str, ...] = (
    "BANK_FUND_COLLECT_LOAN",
    "BANK_FUND_COLLECT_CLEARING",
)

# ── distribute（/api/v1/bank-funds/user-distributions） ────────────────────────
# 分发与归集的账户分层**相反**：付款方（平台户）在顶层，收款人在 recipients[]。
DISTRIBUTE_REQUIRED: tuple[str, ...] = (
    "channelId",
    "bankAccountNo",
    "bankAccountName",
)

# recipients[] 内的必填集（distributeAmount 由端点单独校验并参与金额汇总，不在此列）。
DISTRIBUTE_RECIPIENT_REQUIRED: tuple[str, ...] = (
    "userId",
    "userName",
    "currencyCode",
    "custAccountNo",
)

# recipients[] 内被 wedap 明确拒收的字段（实测 D1~D8）：账户身份只能出现在顶层。
DISTRIBUTE_RECIPIENT_REJECTED: dict[str, str] = {
    "bankAccountNo": "bankAccountNo belongs to the top-level paying platform account in "
    "the wedap distribution contract and is rejected inside recipients[]",
    "bankAccountName": "bankAccountName belongs to the top-level paying platform account "
    "in the wedap distribution contract and is rejected inside recipients[]",
}

# ── refund（/api/v1/transactions/refund） ──────────────────────────────────────
# channelId 是 2026-08-11 实测新发现的必填项（K2 未带 → wedap 400
# `channelId: must not be blank`）；7/24 成功样本 RFDCLEAN3 未见该要求 → wedap 侧近期加严。
# bankAccountNo / custAccountNo / subaccountSerialNo 三者齐为 2026-07-24 实测所得。
REFUND_REQUIRED: tuple[str, ...] = (
    "channelId",
    "bankAccountNo",
    "custAccountNo",
    "subaccountSerialNo",
)


# ── loans 域（组合交易：放款 / 还款） ──────────────────────────────────────────
# 2026-08-11 实测补齐（codex 复核指出 loans 域缺门禁 → 报文被 wedap 拒前已落 ACCEPTED 单
# 再翻 FAILED，白污染 bank_txn_order 台账）。必填集取自 wedap 400 原文，非文档推断：
#   放款 traceId 71d801a40b19460d8992838dc17fbbf8
#   还款 traceId f1117af91cef4fe8b24de21a59ee320b
#
# 注：本机 wedap 源码（wedap-adapter-core 的 @NotBlank）与 dev 运行版本**存在差异**——
# dev 实测要求 collect 的 custAccountNo，本机 DTO 只有 @Size。故一律以 dev 实测为准，
# 源码仅作交叉参考；wedap 升级后需用 20260811-wedap契约实测-evidence/ 下脚本复测。
DISBURSEMENT_REQUIRED: tuple[str, ...] = ("channelId",)
DISBURSEMENT_INFO_REQUIRED: tuple[str, ...] = ("userId", "userName")
# lendAmount 由 validate_detail_consistency 单独校验（参与金额汇总），不在此列。
DISBURSEMENT_LENDER_REQUIRED: tuple[str, ...] = ("currencyCode",)

# loanNo：wedap 侧 2026-07-23 起**无条件必填**——迁移
# V20260722160000__sevan_repayment_txn_table.sql 建的新主表 loan_repayment_txn 声明
# `loan_no VARCHAR(64) NOT NULL COMMENT '借据单号；新表无灰度期，受理即必填'`，
# 同一迁移把灰度开关 `migration.loan-no.enforce` 从 dtc_params DELETE 掉（"新主表
# loan_no NOT NULL，受理侧改为无条件必填，开关失去意义"）。缺失时 wedap 受理即拒，
# 返 13 位业务码 6605B00900209（见 states.WEDAP_TERMINAL_REJECT_CODES）。
#
# 放在网关必填集而非只靠上游自觉：缺字段本可在本层 400 挡下并直说缺哪个字段，
# 放行则要等 wedap 回一个需翻文档才看得懂的业务码，且已先落 ACCEPTED 单再翻 FAILED，
# 白污染 bank_txn_order 台账（与 2026-08-11 补齐 loans 域门禁同一成因）。
# 仅撮合（agency）还款走本端点；自营（principal）还款走 collect-from-users 归集，
# 归集契约无 loanNo 字段，不受本项影响。
REPAYMENT_REQUIRED: tuple[str, ...] = ("channelId", "loanNo")
# principalAmount / interestAmount 是 @NotNull 的 BigDecimal——0 是合法值（无息期还款），
# 故判据是「键存在且非 None」而不是「非零」。
REPAYMENT_INFO_REQUIRED: tuple[str, ...] = (
    "interestAmount",
    "principalAmount",
    "repaymentType",
    "userId",
    "userName",
)
REPAYMENT_LENDER_REQUIRED: tuple[str, ...] = ("interestAmount", "principalAmount")

# ── reversal（/api/v1/transactions/reversal） ─────────────────────────────────
# **wedap 侧该端点当前不存在**：2026-08-11 实测 404 `No endpoint POST
# /api/v1/transactions/reversal`（与 wedap 2026-07-24 自述一致——4.8 冲正归阶段 3、
# 单数路径无路由）。故不设必填集：任何冲正请求都注定 FAILED，加字段校验也拦不住，
# 真因是上游未实现（跟踪 FU-GW-FULL-REFUND-INTERIM）。
# codex 复核 2026-08-11 曾判为「缺 oriReqDate 被拒」——实测证伪，是端点不存在。
