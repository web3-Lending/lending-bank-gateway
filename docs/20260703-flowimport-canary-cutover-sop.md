# flow-import 切流 canary SOP（§6.1 护栏①·运维手册）

> 口径（2026-07-03 用户拍板）：canary 小流量**不写代码**，靠部署配置人工控制；机器强制的是
> 护栏②③④⑤（batch 受理证据+截止、PENDING age 监控、result 超时告警、报告分离）。
> **护栏②③（受理落证 + PENDING 监控）未部署的环境不得放量**（§6.1 原约束）。

## 切流对象

lending-bank-gateway 的 wedap flow-import 投递路径从「不签名直连 baffle mock」切到
「HMAC 签名 → APISIX（LENDING consumer）→ wedap-adapter → web2-core 真链路」。

切流开关（全部 `GW_` 环境变量，见 `app/core/config.py`）：

| 变量 | 作用 | canary 用法 |
|---|---|---|
| `GW_WEDAP_DELIVERY_ENABLED` | 投递 worker 总开关 | 关 = 任务只排队不投（安全阀） |
| `GW_WEDAP_IMPORT_SIGNING_SECRET` | HMAC 密钥 | **空=直连 baffle；非空=切 APISIX**（事实切流开关） |
| `GW_WEDAP_BASE_URL` | 南向地址 | 指 baffle(8021) 或 APISIX 入口 |
| `GW_WEDAP_IMPORT_API_KEY` | notify apikey | 对齐 wedap FLOW_IMPORT_API_KEYS |

## canary 步骤（人工小流量）

1. **单环境先行**：只在 dev 配 secret + APISIX 地址，其它环境保持直连。上游（recon 导出）
   只放一种 data_type 的批次（recon 侧按导出配置控制），观察 ≥1 个完整 result 周期
   （wedap BatchScanScheduler 每日 02:00 UTC + 30min 宽限）。
2. **观察面**（护栏⑤ 报告，按 `import_date` 逐日核对）：
   ```bash
   curl -sS -H 'X-Caller-Service: <svc>' -H 'X-Tenant-Id: <t>' -H 'X-Request-Id: <r>' \
     'http://<gw>:8022/api/v1/admin/wedap-import/delivery-report?import_date=YYYYMMDD'
   ```
   放量判定（全部满足才扩面）：
   - `acceptance.by_status` 无 FAILED 增量、`accepted == total`（排队中除外）；
   - `result_closure.overdue == 0` 且 `outstanding` 随 scanner 周期归零；
   - `alerts` 无新增 `PENDING_STUCK` / `RESULT_OVERDUE`。
3. **扩面**：dev 全 data_type → dev-hw → 生产（每级重复第 2 步观察一个 result 周期）。

## 回滚（RESULT_OVERDUE 告警触发时评估）

`wedap_delivery_alert` 出现 `RESULT_OVERDUE`（受理后超过 scanner 窗口+30min 无 _result.json）：

1. 摘 `GW_WEDAP_IMPORT_SIGNING_SECRET` + 回指 baffle（或直接关 `GW_WEDAP_DELIVERY_ENABLED`）
   → 重启容器（settings 为启动时快照）。
2. 未闭环批次在 `delivery-report` 的 `result_closure.outstanding` 里逐一核对：wedap 侧确认
   是否已入库（DUPLICATE_BATCH 幂等语义保证恢复后重投安全）。
3. 告警行处置后人工清理（表无状态机，删除行即可重新监控）。

## 关联

- 护栏②③④ 实现：`app/services/wedap_delivery.py`（`compute_result_deadline` /
  `alert_stuck_deliveries`）+ `wedap_import_delivery_task.accepted_at/result_file_path/
  result_deadline_at` + `wedap_delivery_alert` 表（迁移 0018）。
- 护栏⑤：`GET /api/v1/admin/wedap-import/delivery-report`。
- 阈值配置：`GW_WEDAP_RESULT_SCAN_ANCHOR_HOUR`（默认 2，UTC）、`GW_WEDAP_RESULT_GRACE_MINUTES`
  （默认 30）、`GW_WEDAP_DELIVERY_PENDING_MAX_AGE_SECONDS`（默认 1800）。
- 背景：FU-FLOWIMPORT-APISIX-20260701-004（§6.1 定口径）/ -009（P1b HMAC 签名）。
