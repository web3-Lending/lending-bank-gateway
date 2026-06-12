# compat — 兼容性套件（直连 wedap dev 环境）

## 用途

本目录存放**直连 wedap dev 环境**的兼容性测试，验证 WedapClient 与真实 wedap 服务的协议兼容性。

- **不计覆盖率**，不进 CI 必跑集
- **nightly / 里程碑节点手动执行**，或在接入新 wedap 版本前回归

## 运行方式

```bash
GW_WEDAP_BASE_URL=<wedap-dev-url> pytest tests/compat -m compat --no-cov
```

例：

```bash
GW_WEDAP_BASE_URL=http://wedap-dev.internal:8021 pytest tests/compat -m compat --no-cov -v
```

## 标记

测试均标记 `@pytest.mark.compat`，默认 pytest addopts（`-m 'not integration and not compat'`）会自动排除，CI 不会误触发。

## 环境变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `GW_WEDAP_BASE_URL` | wedap dev 端点（必填，无则 skip） | `http://wedap-dev.internal:8021` |
| `GW_WEDAP_TIMEOUT` | 请求超时秒数（可选，默认 10.0） | `30.0` |

## 注意事项

- 本套件**不 mock**，直接发起真实 HTTP 请求，需要网络可达 wedap dev 环境
- 执行前确认 wedap dev 可用（建议先跑 `test_connectivity.py` 验证连通性）
- 测试数据尽量使用测试租户 / 测试账号，避免污染生产数据
