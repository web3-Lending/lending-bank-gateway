#!/bin/sh
# Gateway 容器启动入口：先跑 DB 迁移（alembic upgrade head），再起 uvicorn。
# 与 lending-recon 一致——容器自迁移，dev-hw / 首次部署无需手动跑 alembic。
# alembic 从 GW_DB_URL 读连接（app.core.config.Settings，见 alembic/env.py）。
set -e

echo "[gw-entrypoint] alembic upgrade head ..."
n=0
until alembic upgrade head; do
    n=$((n + 1))
    if [ "$n" -ge 12 ]; then
        echo "[gw-entrypoint] alembic 连续失败 $n 次（DB 不可达或迁移错误），放弃启动"
        exit 1
    fi
    echo "[gw-entrypoint] alembic 第 $n 次失败（可能 DB 未就绪），5s 后重试 ..."
    sleep 5
done

echo "[gw-entrypoint] 迁移完成，启动 uvicorn :8022"
exec uvicorn app.main:app --host 0.0.0.0 --port 8022
