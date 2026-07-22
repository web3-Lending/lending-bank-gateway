# 通用冲正北向端点（reversal）设计

> 写于 2026-07-22 · 基准 gateway `main@3cefff3` · 作者 zhaoyangli + Claude
> 关联调研：`lending-workspace/01-investigations/20260722-lending-bank-gateway切换全系统改造排查.md` §4
> wedap 契约来源：`wedap-docs/接口文档/WeDAPAPI-Public.md` §4.4.2「通用冲正（一期）」

## 1. 目标与背景

liquidation abort 场景的**全额退款**按业务口径（用户 2026-07-22 拍板）走**交易冲正 reversal**，部分退款才走 refund。gateway 现状：有 `REVERSED` 终态 + 被动摄取（callback/reconcile 把 SUCCEEDED→REVERSED），但**没有主动发起冲正的北向端点**。本设计补齐该端点，并启用已有的全额退款护栏。

**wedap 侧已实现**（v1.0.10，2026-06-22 一期上线）：`POST /api/v1/transactions/reversal`（通用冲正），故本端点是"薄透传对接已实现的 wedap 能力"，不涉及 wedap 侧改造。

## 2. wedap 通用冲正契约（对接标尺，实测自 WeDAPAPI-Public.md §4.4.2）

- **路径**：`POST /api/v1/transactions/reversal`（gateway `base_url` 已含环境前缀，与 `refund()` 同款拼接）
- **同步接口**：同步返回 `txnStatus=REVERSED`（当日 DCN；跨日 wedap 内部转 BANK-104），**无异步回调**
- **transType = 原交易类型**：一期仅归集类 `BANK_FUND_COLLECT_LOAN` / `BANK_FUND_COLLECT_CLEARING`；资金到客户账不支持冲正
- **仅全额冲正**：部分退款走 refund（§4.4.3）
- **防冲错三方校验**：请求 `oriTxnAmount`/`currencyCode` == 本地原交易 == BANK-313，任一不一致 → **422 BUSINESS_RULE_VIOLATION**（不触发任何资金动作）。`oriTxnAmount` 是校验用，非可调冲正金额
- **幂等**：X-Request-Id 保证；原交易已 REVERSED（下游 BANK_06 已冲正）时幂等返回 `txnStatus=REVERSED`，不报错
- **错误码**：200 SUCCESS / 101 TIMEOUT / 104 PARAMS_NOT_VALID / 401 AUTH_TOKEN_EXPIRED / 422 BUSINESS_RULE_VIOLATION / 500 SYSTEM_ERROR

请求字段：`bizSeqNo`（本次冲正流水）、`channelId`、`transType`（原交易类型）、`oriReqDate`、`oriBizSeqNo`、`oriRequestId`(选)、`oriTxnAmount`（校验）、`currencyCode`、`reason`(选)。
响应字段：`oriBizSeqNo`、`transType`、`reversalMode`(DCN/…)、`txnStatus`(REVERSED)、`reversalBizSeqNo`、`reversalAmount`、`bizSeqNo`、`requestId`。

## 3. 架构与数据流

```
liquidation abort(全额) → POST /api/v1/bank-funds/reversals(oriBizSeqNo=原归集单, transType=原交易类型, oriTxnAmount, currencyCode)
  → gateway 落 RVSL 单(自己的 bizSeqNo, biz_type=RVSL) —— 复用 _submit(账户守门+幂等+落库)
  → wedap.reverse()【同步】→ 200 txnStatus=REVERSED
  → 同一请求内：
       · RVSL 单收口 SUCCEEDED（冲正指令成功）
       · 复用 order_finalize 升级 helper 把本地原单 SUCCEEDED→REVERSED（查得到才翻；查不到不拦）
  → 同步返回，无需等回调
```

**关键设计判定**：wedap 通用冲正是**同步权威返回**、无回调 → 原单翻转在端点内同步完成（复用既有 SUCCEEDED→REVERSED 升级路径，幂等）。这不同于既有的"counter 人工冲正回传"摄取链（callback 驱动，仍保留用于人工冲正场景）；两条路径共用同一升级 helper + 幂等键，即便未来 wedap 对该单也补回调，REVERSED 吸收态保证不重复翻。

## 4. 组件（全在 gateway 我方仓）

### 4.1 `ReversalRequest` schema（`app/api/v1/bank_funds.py`）

镜像 `RefundRequest` 的 `extra=allow` 薄透传风格，字段对齐 wedap 通用冲正：

| 字段 | 必填 | 说明 |
|---|---|---|
| `bizSeqNo` | Y | 本次冲正交易流水号（RVSL 单自己的 seq）|
| `transType` | Y | **原交易类型**，透传给 wedap；一期归集类 |
| `oriBizSeqNo` | Y | 被冲正的原交易流水号 |
| `oriReqDate` | Y | 原交易请求日期 YYYYMMDD（wedap 回查消歧需要）|
| `oriTxnAmount` | Y | 原交易金额（防冲错校验用，字符串透传）|
| `currencyCode` | Y | 币种，与原交易一致校验 |
| `reason` | N | 冲正原因 |

`channelId` / `oriRequestId` / `bankAccountNo` 等经 `extra=allow` 原样透传，gateway 不剪裁。

### 4.2 `reverse_transaction` 端点（`app/api/v1/bank_funds.py`）

`POST /api/v1/bank-funds/reversals`，复用 `_submit` helper（自动带账户守门 + 幂等 + 落库），参数：
`business_action="REVERSE"`、`biz_type="RVSL"`、`business_scope="bank_reversal"`、`wedap_method="reverse"`、`amount=parse_amount(oriTxnAmount)`、`currency=currencyCode`。

**原单同步翻转**：`_submit` 成功（wedap 返 REVERSED）后，在同一 session/请求内查本地 `oriBizSeqNo` 原单：查得到 → 复用升级 helper 翻 SUCCEEDED→REVERSED（含 audit/outbox 转发，幂等键按 REVERSED 状态细分，不与首次 SUCCEEDED 收口互吞）；查不到 → 仅记 RVSL 单，不翻（原单可能非本 gateway 出，同 refund「查不到不拦」口径）。

> 实现细节（`_submit` 当前只返回 collect/refund 的收口结果，需扩展一个「拿到 wedap 响应后回调本地原单升级」的挂钩点，或在端点内独立取原单 + 调升级 helper）——落到 plan 阶段决定最小侵入写法。

### 4.3 `WedapClient.reverse()`（`app/clients/wedap.py`）

镜像 `refund()`：`POST /api/v1/transactions/reversal`，`tenant_id` / `request_id` / `payload` 透传。docstring 标注契约来源 §4.4.2 + 一期归集类限制。

### 4.4 全额退款护栏（硬规则 · 不可配置，用户拍板 2026-07-22）

护栏为**无条件强制**，无 flag（原 `refund_full_amount_guard` 配置已移除）：凡 gateway 本地能查到原单、且 refund 金额 == 原单金额的**全额退款请求一律返回 422 `GW_422_FULL_REFUND_USE_REVERSAL`**，引导调用方改走 reversal。部分退款不受影响（`amount != ori.amount`）；原单不在本地台账 → 不拦（交 wedap 校验）。

## 5. 错误处理与幂等

- `_submit` 已覆盖：幂等冲突 → 409、参数校验 → 400、账户守门 fail-closed。
- wedap 422（金额/币种不一致）→ gateway 透传为 422，携带 wedap 错误码/消息（不吞成 500）。
- wedap 已 REVERSED 幂等返回 REVERSED → gateway 端点幂等成功（RVSL 单幂等 + 原单已 REVERSED 吸收态不重复翻）。
- 一期只支持归集类 transType：gateway thin-passthrough，非归集类由 wedap 返 422/104 拒绝（gateway 不预置白名单，避免与 wedap 枚举漂移双维护）。

## 6. 测试（镜像 `tests/api/test_refund.py`）

| 用例 | 断言 |
|---|---|
| 冲正成功 | 落 RVSL 单 SUCCEEDED；本地原单被翻 REVERSED；返回 wedap `reversalBizSeqNo` |
| 原单本地查不到 | RVSL 单落库成功，不因缺原单报错（查不到不拦）|
| 幂等重放 | 同 bizSeqNo 二次请求 → 幂等，不重复调 wedap、不重复翻原单 |
| 账户守门 | `bankAccountNo` 未注册 platform_accounts + enforce → fail-closed 拒绝，不落单不调 wedap |
| wedap 422 | 金额/币种不一致 → gateway 返 422，原单不翻 |
| wedap 失败降级 | wedap 500/超时 → 端点不吞成静默成功，原单保持原态 |
| 护栏·全额 refund | 全额 refund（能查到原单）→ 422 GW_422_FULL_REFUND_USE_REVERSAL（无条件硬规则，无 flag） |
| 护栏开·部分 refund | 部分金额 → 正常放行 |

覆盖率遵循 gateway 质量门禁（后端行+分支，见 `08-quality-gate.md`）。

## 7. 范围外（本设计不含）

- 法币 deduct / 换汇 / 用户间划转 / EarnVault 建户原语 —— §7 决策未定，独立设计。
- 费用冲正（Wallet.md 3.1.3 `FEE_REVERSAL`，异步+回调）—— 非本次场景。
- `PARTIALLY_REVERSED` 契约 —— 口径部分走 refund，不需要。
- liquidation `abort_solution.py` 的全额路由改造 —— liquidation owner 执行（只读仓），本端点提供其目标能力。

## 8. 依赖与协作项

- **wedap 契约已坐实**（§4.4.2 一期已实现），无待确认前置，联调直接对接 dev。
- **调用方须传原交易类型 + oriTxnAmount**：liquidation/lifecycel 发起冲正时须带 `transType`(原交易类型) 与 `oriTxnAmount`(原单金额)，否则 wedap 422。此为调用方改造项，本端点在文档/openapi 明确必填。
- **护栏默认 True 是行为变更**：启用后全额 refund（本地可查原单）一律 422。调用方若仍走全额 refund 会立刻挂 → 必须与 liquidation owner 同步"全额走 reversal"的路由改造，否则联调红。此风险在 §4.4 与本节双点明。

## 9. 落地顺序

1. `reverse()` wedap client + `ReversalRequest` schema + 端点（先写失败测试）。
2. 原单同步翻转接线（复用升级 helper）。
3. 护栏默认翻 True + 护栏测试。
4. openapi 契约再生成（`contracts/openapi.json` 加 `/bank-funds/reversals`）。
5. 本地起服务冒烟 → dev-hw 部署 → wedap dev 联调验收（真发一笔归集 → 冲正 → 查单 REVERSED 闭环）。
