# 安全审计流水线复核 — iteration 1

对照 `references/security-audit-pipeline.md` 的 7 工具 category 复核。

| category | 工具 | 状态 | 结论 |
|---|---|---|---|
| Python lint / 反模式 | ruff (E/F/I/UP/B/ASYNC/**S**) | ✅ 已跑 | clean。S(bandit-in-ruff) 规则全过 |
| Python 安全 AST | bandit -r app | ✅ 已跑 | 0 issues (High/Medium/Low 均 0) |
| 类型安全 | mypy --strict | ✅ 已跑 | 0 issues / 45 files |
| 前端 lint | eslint | N/A | 本仓无前端 |
| 密钥扫描 | gitleaks | ⚠️ SKIPPED — tool not installed | 人工复核：无硬编码密钥；`deploy/env.local`/`.env` 已 .gitignore；S2S secret 经 env 注入；`tc_root_pw="test"` 为 testcontainer 凭证已标 `# noqa: S105`。本地复现：`gitleaks detect --source .` |
| Python 依赖 CVE | pip-audit | ⚠️ SKIPPED — tool not installed | 本地复现：`pip-audit -r <(uv export)` 或 `uv pip compile` 后审 |
| 容器 CVE | trivy | ⚠️ SKIPPED — tool not installed | 本地复现：`trivy image lending-bank-gateway:latest` |

## 安全逻辑层人工复核（lint 抓不到的）

- **生产 mock/fallback**：无。`_noop_after_ingest` 仅默认接线点，运行时被真实 `_after_ingest` 覆盖。✅
- **密钥/凭证泄露**：S2S token 用 `hmac.compare_digest`（常量时间），失败日志显式不写 token（s2s.py:50）。无凭证落日志/响应。✅
- **SQL 注入面**：全 ORM 参数化，无字符串拼 SQL。✅
- **认证绕过**：S2SMiddleware 先校验 caller 头存在 → token 常量时间比对 → caller 白名单。非 local/test 环境 `create_app` fail-fast 强制 secret（禁 fail-open）。✅
  - 已知 v1 局限（architect A-m-002）：共享 token 无法密码学绑定 caller，单 secret 泄露=全线沦陷 → 转 followup 跟踪 v2 per-service token / mTLS。
- **输入校验**：金额非有限值穿透 500（CR-M-001）已修；envelope/回调最小校验在位。✅

## Critical / Major 真实安全问题

**0 个**（已修 CR-M-001 后）。SKIPPED 三工具为环境缺失，非真问题，已列本地复现命令。
