# lending-bank-gateway

lending 体系与 wedap 资金系统之间的**统一资金网关**服务（ADR-0031）。

## 定位

- **北向（inbound）**：接收 lending-lifecycel / lending-core 的资金指令，通过标准化 envelope（消息信封 v0.2）转发至 wedap
- **南向（outbound）**：wedap 回调经 inbox-outbox 幂等入库，再通过对账摄取接口供 lending-recon 消费
- **order-leg 供数**：`GET /api/v1/fiat-vault/transactions` 提供自有资金流水（BankTxnLeg），lending-recon 通过跨库 collector 直接读 gateway 库（Plan 3），无需穿越 wedap
- **对账数据消费**：lending-recon 消费 gateway 数据的方式为 `GET /api/v1/fiat-vault/transactions`；不存在 `/recon/inflows` 或 `/recon/outflows` 端点

## 端口

| 环境 | 地址 |
|------|------|
| 本地开发 | `http://localhost:8050` |
| 健康检查 | `GET /healthz` |

## 本地开发

```bash
# 创建 venv 并安装依赖
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# 运行单元测试（含覆盖率门禁 100%）
pytest -q

# 运行 integration 测试（需 docker，会自动起 MySQL testcontainer）
pytest -m integration --no-cov -q

# 代码风格检查
ruff check .

# 类型检查
mypy app

# 一次性跑全量门禁（与 CI 等价）
ruff check . && mypy app && pytest -q && pytest -m integration --no-cov -q
```

## 部署

### Docker Compose（推荐本地 / dev-hw）

```bash
# 1. 复制并填写环境变量
cp deploy/env.example deploy/env.local
# 编辑 env.local 填入真实值

# 2. 构建镜像
docker build -f deploy/Dockerfile -t lending-bank-gateway:latest .

# 3. 运行数据库迁移
docker run --rm --env-file deploy/env.local \
  -e GW_DB_URL=mysql+asyncmy://<user>:<pass>@<host>:3306/lending_bank_gateway \
  lending-bank-gateway:latest \
  python -m alembic upgrade head

# 4. 启动服务
docker compose -f deploy/docker-compose.yml up -d

# 确认健康
curl http://localhost:8050/healthz
```

> **注意**：必须先跑 `alembic upgrade head` 再启动服务；服务不会自动执行迁移。

### Worker（后台任务）

**outbox dispatcher** 与 **recon worker** 随 API 进程 lifespan 一并启动，无需独立部署：

- `GW_WORKERS_ENABLED=true`（默认）：进程启动时自动以 `asyncio.create_task` 起两个后台任务
- 多副本部署时：outbox 可能被多个副本同时投递，由下游幂等机制容忍双投递；recon worker 通过原子 claim（`UPDATE ... WHERE status='NOTIFIED'`）防止并发重复摄取
- 单副本或调试时设 `GW_WORKERS_ENABLED=false` 可完全关闭后台任务

### 环境变量

见 `deploy/env.example`，所有变量以 `GW_` 为前缀（除 DB 连接子变量）。

## API 契约

- OpenAPI 快照：`contracts/openapi.json`——CI 自动校验，schema 改动需先更新快照（`pytest -k test_openapi_snapshot --snapshot-update`）
- wedap 契约 replay：`tests/compat/` 目录，含直连 wedap dev 环境的兼容性测试套件（标记 `compat`，nightly/里程碑手动触发，不进常规 CI 必跑集）

## 项目文档

| 文档 | 路径 |
|------|------|
| 功能规格（spec） | `docs/specs/` |
| 实施计划（plan） | `docs/plans/` |
| Superpowers 计划 | `docs/superpowers/plans/` |
| ADR-0031（网关决策） | lending-workspace `04-decisions/` |
