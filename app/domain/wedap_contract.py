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
