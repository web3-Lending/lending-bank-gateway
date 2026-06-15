# Senior Code Review — iteration 1

> 评审对象：`lending-bank-gateway`（分支 `fix/gateway-northbound-passthrough`）。
> 基线：ruff(E/F/I/UP/B/ASYNC/S) / mypy --strict / bandit 全过，故本报告聚焦 **lint 抓不到的代码质量与安全逻辑**。
> 方式：主控（main loop）执行代码评审角色（原并行 subagent 因长 SSE 连接 RST 掉线，改主控逐文件读源码评审）。

## Critical (0)

无新增 Critical（架构层 Critical 见 `architect.md` A-C-001/A-C-002）。

## Major (1)

### [CR-M-001] `parse_amount` 非有限 Decimal（NaN/Infinity）穿透成 500 ✅ 已修复
- **Location**: `app/api/deps.py:44-60`（`parse_amount`）
- **What**: `value = Decimal(str(raw))` 的 try 只包了构造，未包后续 `value <= 0` 比较。`Decimal("NaN")`/`Decimal("sNaN")` 是合法 Decimal（不抛），但 `NaN <= 0` 抛 `InvalidOperation`（未捕获）→ 走 `_generic_exception_handler` → **500 GW_500_INTERNAL**；`Decimal("Infinity")` 通过 `<= 0`（结果 False）被当正数放行 → 返回无穷金额 → 下游 `Numeric(21,4)` 落库炸 → 500。
- **实测证据**: `Decimal('NaN') <= 0` → InvalidOperation；`Decimal('Infinity') <= 0` → False（放行）。见 Step 实测输出。
- **影响**: 北向 collect/distribute 的 totalAmount / distributeAmount 传 `"NaN"`/`"Infinity"` → 500 而非干净 400；Infinity 一度被当合法金额受理。金融入口对脏金额的拒绝语义破损。
- **Fix**: `parse_amount` 在 `<= 0` 前加 `if not value.is_finite(): raise 400`。回归测试 `tests/api/test_deps.py::test_parse_amount_rejects_non_finite_400`（NaN/sNaN/Infinity/-Infinity/inf/nan 参数化）。

## Minor (观察项，未改 / 转 followup)

- [CR-m-001] `app/services/submit.py:136` `assert_transition(ACCEPTED, new_status)` 在事务2外执行；事务2 的 UPDATE 不重读当前 status 直接覆盖。in-flight 幂等护栏已防同 key 并发提交，回调推进父单发生在 wedap 处理之后，实际窗口极窄。**评估：当前安全，不改**，作为状态机健壮性观察记录。
- [CR-m-002] `app/services/outbox.py:130` dispatcher 每条外呼 `httpx.AsyncClient(timeout=10.0)` 新建（与 architect A-M-003 重叠）；高频投递握手开销。转 followup（与 A-M-001 outbox 改造一并）。
- [CR-m-003] `app/clients/wedap.py` 写操作不自动重试，超时靠上层 RESULT_UNKNOWN 收敛（已文档化、设计如此）。无 finding。

## 安全反模式专项结论（lint 抓不到的逻辑层）

- **生产 mock/fallback**：未发现生产代码含 mock/假数据/fallback 桩；`_noop_after_ingest` 仅作默认接线点，`create_app` 已用真实 `_after_ingest` 覆盖。✅
- **密钥/JWT/凭证泄露**：S2S token 比较用 `hmac.compare_digest`（常量时间，防时序侧信道），失败日志显式不写 token 值（s2s.py:50 注释 + 实现）。✅ 无密钥落日志/落响应。
- **SQL 拼接**：全量走 SQLAlchemy ORM / `select()` / `update()` 参数化，无字符串拼 SQL。✅
- **日志泄露**：错误响应统一 envelope（`err()`），不回传内部栈；trace_id 贯通。✅（A-m-001 trace 在 outbox 跳断属可观测性缺口，非泄露）
- **输入校验**：envelope/回调 body 有最小校验；金额校验经 CR-M-001 修复后闭合 NaN/Infinity 缺口。

## 整体代码质量评估

代码风格统一、类型注解充分（mypy strict 过）、错误处理模式一致（envelope + 分级 HTTPException + 幂等三态）、注释对已知 v1 局限诚实标注。仅 1 个 Major（脏金额穿透 500，已修），其余为与架构债重叠的观察项。
