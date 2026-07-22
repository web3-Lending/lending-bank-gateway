#!/bin/bash
# ============================================
# Lending Bank Gateway - Deployment Script
# ============================================
# Unified deployment: local or remote (dev-hw), modeled on lending-recon.
#   - env.<ENV> 含 REMOTE_SERVER → remote 部署（SSH+SCP，在 dev-hw 上 build+up）
#   - env.<ENV> 无 REMOTE_SERVER → 本地 Docker 部署
# 容器自迁移：deploy/entrypoint.sh 启动时跑 `alembic upgrade head` 再起 uvicorn，
# 故首次部署到全新 DB 也无需手动 alembic。
#
# git commit SHA 烘进镜像（--build-arg GIT_SHA），GET /build-info 可证镜像绑定哪个 commit。
#
# Usage: ./deploy/deploy.sh [local|dev-hw] [options]
#   local   本地 Docker（默认）
#   dev-hw  dev-hw（HW 内网，139.159.161.9，远程 SSH+SCP）
#   --no-build      Skip build, use the existing image（仅 local）
#   --build-only    Only build the image, don't start（仅 local）
#   --logs          Tail container logs（仅 local）
#   --stop          Stop + remove the container（仅 local）
#   --restart       Restart the container（仅 local）
#   --status        Show container status（仅 local）
#   -h, --help      Show this help
# ============================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_DIR="$SCRIPT_DIR"

DISPLAY_SERVICE_NAME="Lending Bank Gateway"
CONTAINER_NAME="lending-bank-gateway"
APP_PORT="8022"
HEALTH_ENDPOINT="/healthz"
# Dedicated compose project name. WITHOUT this, docker compose defaults the
# project to the compose file's directory name ("deploy"), colliding with every
# other repo's deploy/ — and a down/up --remove-orphans then wipes their
# containers (2026-06-16 incident: 14 containers gone). Isolate to our project.
COMPOSE_PROJECT="lending-bank-gateway"
# 远程主机上放代码的目录名（${REMOTE_PATH}/${REMOTE_APP_DIRNAME}）。
REMOTE_APP_DIRNAME="lending-bank-gateway"

# ── 第一个位置参数 = 环境（local|dev-hw），其余是 flags ──
ENV="local"
if [[ "${1:-}" == "local" || "${1:-}" == "dev-hw" ]]; then
    ENV="$1"; shift
fi
ENV_FILE="$DEPLOY_DIR/env.${ENV}"

# 发布身份 GIT_SHA：buildops promote 经 release_payload env 传入既定 sha（部署既有制品，
# 不重算）；直接部署时回落 git describe。烘进镜像供 GET /build-info 与 /api/version anchoring。
# 版本升版已移交发布链（scripts/ds-build.sh local-verify），deploy.sh 不再自动 bump——
# dev-hw 禁止 deploy.sh 直接 build（见 remote 段治理护栏）。
GIT_SHA="${GIT_SHA:-$(git -C "$PROJECT_ROOT" describe --always --dirty 2>/dev/null || echo unknown)}"
# Harden against command injection: git describe can emit tag names with shell
# metacharacters. Reject anything outside a safe charset and fall back to the
# bare short hash (+ -dirty). Defensive parity with lending-recon remote path.
if ! [[ "$GIT_SHA" =~ ^[A-Za-z0-9._/-]+$ ]]; then
    GIT_SHA="$(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    git -C "$PROJECT_ROOT" diff --quiet 2>/dev/null || GIT_SHA="${GIT_SHA}-dirty"
fi
export GIT_SHA

# 运行态发布身份（版本号与发布身份规范0707）：appVersion 真源是 pyproject.toml，
# 部署时注入容器 env 供 /api/version 回显；未提供的批次/schema 字段由端点显式占位。
APP_VERSION="${APP_VERSION:-$(sed -n 's/^version = "\(.*\)"/\1/p' "$PROJECT_ROOT/pyproject.toml" | head -1)}"
APP_VERSION="${APP_VERSION:-0.1.0}"
export APP_VERSION
# buildops promote 经 release_payload env 覆盖以下发布身份字段（digest / release id 等）；
# 直接部署时用下方默认值，/api/version 显式占位而非猜测。
export PROJECT_ID="${PROJECT_ID:-lending-bank-gateway}"
export SERVICE_NAME="${SERVICE_NAME:-lending-bank-gateway-api}"
export APP_SCHEMA_REVISION="${APP_SCHEMA_REVISION:-d3e4f5a6b7c8_0021_platform_bank_account}"
export DATA_ACTION="${DATA_ACTION:-none}"
export COLLAB_RELEASE_ID="${COLLAB_RELEASE_ID:-not-reported}"
export COLLAB_RELEASE_RUN_ID="${COLLAB_RELEASE_RUN_ID:-not-reported}"
export COLLAB_RELEASE_ENV="${COLLAB_RELEASE_ENV:-$ENV}"
export IMAGE_DIGEST="${IMAGE_DIGEST:-digest_missing}"
export SOURCE_CONFIG_DIGEST="${SOURCE_CONFIG_DIGEST:-digest_missing}"
export BUILD_TIME_HKT="${BUILD_TIME_HKT:-$(TZ=Asia/Hong_Kong date '+%Y-%m-%d %H:%M:%S HKT')}"

# ── Colors & logging ─────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
print_info()    { echo -e "${CYAN}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[OK]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

# ── Args (flags) ─────────────────────────────────────────────
ACTION="deploy"
NO_BUILD=false
BUILD_ONLY=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-build)    NO_BUILD=true ;;
        --build-only)  BUILD_ONLY=true ;;
        --logs)        ACTION="logs" ;;
        --stop)        ACTION="stop" ;;
        --restart)     ACTION="restart" ;;
        --status)      ACTION="status" ;;
        -h|--help)     grep '^#' "$0" | sed 's/^# \?//'; exit 0 ;;
        *)             print_error "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

if [ ! -f "$ENV_FILE" ]; then
    print_error "env file not found: $ENV_FILE"
    echo "  可用: $(ls "$DEPLOY_DIR"/env.* 2>/dev/null | xargs -n1 basename | sed 's/env\.//' | tr '\n' ' ')"
    echo "  （从 deploy/env.example 复制；dev-hw 还需补 REMOTE_* 段）"
    exit 1
fi

# ── 载入 env（REMOTE_* + 变量替换用的 DB_*/WEDAP/GW_*）──
# 要求 env 文件是合法 shell（KEY=VALUE）；source 失败直接 fail（set -e），不静默吞——
# 否则坏的 dotenv 会带着错凭证继续跑（codex review MED）。
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

DEPLOY_MODE="local"
[ -n "${REMOTE_SERVER:-}" ] && DEPLOY_MODE="remote"

# ── docker compose v2（fallback v1）──────────────────────────
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    print_error "Neither 'docker compose' nor 'docker-compose' is available"
    exit 1
fi

compose_cmd() {
    $DC -p "$COMPOSE_PROJECT" -f "$DEPLOY_DIR/docker-compose.yml" --env-file "$ENV_FILE" "$@"
}

ensure_networks_local() {
    docker network create wedap-network 2>/dev/null || true
}

# ── Banner ───────────────────────────────────────────────────
echo "=========================================="
echo "  ${DISPLAY_SERVICE_NAME} - Deployment"
echo "=========================================="
echo "  Env:       ${ENV}"
echo "  Mode:      ${DEPLOY_MODE}"
echo "  Commit:    ${GIT_SHA}"
echo "  Container: ${CONTAINER_NAME}"
echo "  Port:      ${APP_PORT}"
if [ "$DEPLOY_MODE" = "remote" ]; then
    echo "  Server:    ${REMOTE_USER:-?}@${REMOTE_SERVER}"
    echo "  Path:      ${REMOTE_PATH:-?}/${REMOTE_APP_DIRNAME}"
fi
echo ""

# ============================================================
# REMOTE 部署（dev-hw）
# ============================================================
if [ "$DEPLOY_MODE" = "remote" ]; then
    # 部署前一次性校验所有必需变量（REMOTE_* + 容器运行时 GW/DB），缺失/为空立即 fail，
    # 而不是后面 set -u 报模糊错或容器 crash-loop（codex review LOW）。GW_S2S_SECRET 非
    # local/test 必填（资金网关 fail-fast），空值在此即拦下。
    for var in REMOTE_USER REMOTE_SERVER REMOTE_PASSWORD REMOTE_PATH \
               DB_USER DB_PASS DB_HOST WEDAP_BASE_URL GW_S2S_SECRET GW_ENV; do
        if [ -z "${!var:-}" ]; then
            print_error "remote 部署缺少/为空 env 变量: $var（见 deploy/env.dev-hw）"
            exit 1
        fi
    done
    if ! command -v sshpass >/dev/null 2>&1; then
        print_error "sshpass 未安装。安装: sudo apt-get install -y sshpass"
        exit 1
    fi

    # 用 SSHPASS env + sshpass -e，密码不进 argv（比 -p 少一处进程列表泄露面，codex review MED）。
    ssh_cmd() {
        SSHPASS="${REMOTE_PASSWORD}" sshpass -e ssh -o StrictHostKeyChecking=no \
            "${REMOTE_USER}@${REMOTE_SERVER}" "$@"
    }
    scp_cmd() {
        SSHPASS="${REMOTE_PASSWORD}" sshpass -e scp -o StrictHostKeyChecking=no -r \
            "$1" "${REMOTE_USER}@${REMOTE_SERVER}:$2"
    }

    remote_compose_cmd() {
        ssh_cmd "cd ${REMOTE_PATH}/${REMOTE_APP_DIRNAME} && docker compose -p ${COMPOSE_PROJECT} -f deploy/docker-compose.yml --env-file deploy/.env $*"
    }

    # 发布治理（buildops 不可变发布链）：dev-hw 禁止远端 build，只允许 promote 一个已在本地
    # local-verify 通过、带 digest 的既有 APP_IMAGE 制品。正规入口 scripts/ds-build.sh
    # dev-promote --confirm——它先 scp + docker load 镜像到 dev-hw、再以 --no-build 调本脚本
    # 走 docker compose up（不上传源码、不远端 build，绕开 dev-hw 出网坑同时保证制品不可变）。
    # 日志轮转由 docker-compose.yml 的 logging 块承载（compose up 生效，与旧 docker run 口径一致）。
    if [ "$NO_BUILD" = false ]; then
        print_error "dev/UAT remote build is forbidden by Lending group governance"
        print_error "Run scripts/ds-build.sh local-verify first, then promote with scripts/ds-build.sh dev-promote --confirm."
        exit 1
    fi
    if [ -z "${APP_IMAGE:-}" ]; then
        print_error "APP_IMAGE is required for remote --no-build promotion"
        exit 1
    fi
    if [ "${IMAGE_DIGEST}" = "digest_missing" ] || [ "${SOURCE_CONFIG_DIGEST}" = "digest_missing" ]; then
        print_error "IMAGE_DIGEST and SOURCE_CONFIG_DIGEST are required for remote --no-build promotion"
        exit 1
    fi

    # mktemp 唯一路径 + trap 清理，避免并发/重试碰撞与失败残留（codex review LOW）。
    GW_RUN_ENV="$(mktemp "/tmp/${COMPOSE_PROJECT}-runenv-XXXXXX")"
    trap 'rm -f "$GW_RUN_ENV"' EXIT

    print_info "确保 wedap-network 存在 + 远端 deploy 目录 ..."
    ssh_cmd "docker network create wedap-network 2>/dev/null || true"
    ssh_cmd "mkdir -p ${REMOTE_PATH}/${REMOTE_APP_DIRNAME}/deploy"

    # 容器 env 写临时文件 → scp(600) → docker compose --env-file，避免把 secret 插进远程 shell
    # 字符串（含 ' 会破坏引用/注入，codex review HIGH）。注：env 仍会出现在 docker inspect，
    # 与其它服务一致，属 dev-hw 可接受口径。GW_S2S_SECRET 非 local/test 必填（资金网关 fail-fast）。
    print_info "dev-hw 起容器（${APP_PORT} / wedap-network；entrypoint alembic→uvicorn；--no-build promote 既有制品）..."
    {
        # compose 变量替换 + 容器 env 同源：image 与发布身份 build-arg/env 都从这份 .env 取。
        printf 'APP_IMAGE=%s\n' "$APP_IMAGE"
        printf 'GW_DB_URL=mysql+asyncmy://%s:%s@%s:3306/lending_bank_gateway\n' "$DB_USER" "$DB_PASS" "$DB_HOST"
        printf 'GW_WEDAP_BASE_URL=%s\n' "$WEDAP_BASE_URL"
        printf 'GW_S2S_SECRET=%s\n' "$GW_S2S_SECRET"
        printf 'GW_ENV=%s\n' "$GW_ENV"
        # 运行态发布身份 env（/api/version 回显源；规范0707 §Docker/OCI Label 与环境变量）
        printf 'PROJECT_ID=%s\n' "$PROJECT_ID"
        printf 'SERVICE_NAME=%s\n' "$SERVICE_NAME"
        printf 'APP_VERSION=%s\n' "$APP_VERSION"
        printf 'GIT_SHA=%s\n' "$GIT_SHA"
        printf 'BUILD_TIME_HKT=%s\n' "$BUILD_TIME_HKT"
        printf 'COLLAB_RELEASE_ENV=%s\n' "$COLLAB_RELEASE_ENV"
        printf 'COLLAB_RELEASE_ID=%s\n' "$COLLAB_RELEASE_ID"
        printf 'COLLAB_RELEASE_RUN_ID=%s\n' "$COLLAB_RELEASE_RUN_ID"
        printf 'APP_SCHEMA_REVISION=%s\n' "$APP_SCHEMA_REVISION"
        printf 'IMAGE_DIGEST=%s\n' "$IMAGE_DIGEST"
        printf 'SOURCE_CONFIG_DIGEST=%s\n' "$SOURCE_CONFIG_DIGEST"
        printf 'DATA_ACTION=%s\n' "$DATA_ACTION"
        # flow-import + S3 可选透传：env.<ENV> 定义即注入容器；未定义留空。
        # 银行南向走 gw-internal（Phase 1 无鉴权），前缀由 GW_WEDAP_BASE_URL 承载（/lending-gw），
        # 无凭证可透传；flow-import 独立 base 走 GW_WEDAP_IMPORT_BASE_URL（/external/web2-core）。
        # AWS_* 走 boto3 标准 env（非 GW_ 前缀）；其余为 GW_ 前缀的 Settings 字段。
        for _kv in \
            "GW_WEDAP_CALLBACK_API_KEY=${GW_WEDAP_CALLBACK_API_KEY:-}" \
            "GW_WEDAP_IMPORT_API_KEY=${WEDAP_IMPORT_API_KEY:-}" \
            "GW_WEDAP_IMPORT_BASE_URL=${WEDAP_IMPORT_BASE_URL:-}" \
            "GW_WEDAP_IMPORT_BUCKET=${WEDAP_IMPORT_BUCKET:-}" \
            "GW_WEDAP_STAGING_BUCKET=${WEDAP_STAGING_BUCKET:-}" \
            "GW_WEDAP_PRESIGNED_ENABLED=${WEDAP_PRESIGNED_ENABLED:-}" \
            "GW_WEDAP_DELIVERY_ENABLED=${WEDAP_DELIVERY_ENABLED:-}" \
            "GW_WEDAP_RESULT_WATCHDOG_HOURS=${GW_WEDAP_RESULT_WATCHDOG_HOURS:-}" \
            "GW_WEDAP_RESULT_BUFFER_MINUTES=${GW_WEDAP_RESULT_BUFFER_MINUTES:-}" \
            "GW_S3_ENDPOINT_URL=${S3_ENDPOINT_URL:-}" \
            "GW_CALLBACK_TARGET_LIFECYCLE_URL=${CALLBACK_TARGET_LIFECYCLE_URL:-}" \
            "GW_ACCOUNT_GUARD_MODE=${ACCOUNT_GUARD_MODE:-}" \
            "GW_ADMIN_CALLERS=${ADMIN_CALLERS:-}" \
            "GW_S2S_CALLER_TOKENS=${S2S_CALLER_TOKENS:-}" \
            "GW_RECON_BASE_URL=${RECON_BASE_URL:-}" \
            "GW_RECON_CALLBACK_HMAC_SECRET=${RECON_CALLBACK_HMAC_SECRET:-}" \
            "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-}" \
            "AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-}" \
            "AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-}"; do
            [ -n "${_kv#*=}" ] && printf '%s\n' "$_kv"
        done
        printf 'GW_ENV_FILE=.env\n'
    } > "$GW_RUN_ENV"
    chmod 600 "$GW_RUN_ENV"
    print_info "上传 release runtime compose/env（不上传源码，不远端 build）..."
    scp_cmd "$DEPLOY_DIR/docker-compose.yml" "${REMOTE_PATH}/${REMOTE_APP_DIRNAME}/deploy/docker-compose.yml"
    scp_cmd "$GW_RUN_ENV" "${REMOTE_PATH}/${REMOTE_APP_DIRNAME}/deploy/.env"
    ssh_cmd "chmod 600 ${REMOTE_PATH}/${REMOTE_APP_DIRNAME}/deploy/.env"
    ssh_cmd "docker rm -f ${CONTAINER_NAME} 2>/dev/null || true"
    remote_compose_cmd "up -d --force-recreate --remove-orphans --no-build"

    print_info "远程健康检查（容器内先跑 alembic 再起服务，首次可能稍久）..."
    backend_up=false
    for i in $(seq 1 45); do
        if ssh_cmd "curl -sf http://localhost:${APP_PORT}${HEALTH_ENDPOINT} >/dev/null 2>&1"; then
            backend_up=true; break
        fi
        echo "  等待 backend ... ($i/45)"
        sleep 4
    done

    echo ""
    if [ "$backend_up" = true ]; then
        print_success "dev-hw 部署完成，backend 健康！"
        echo "  Health:     http://${REMOTE_SERVER}:${APP_PORT}${HEALTH_ENDPOINT}"
        echo "  Build-info: http://${REMOTE_SERVER}:${APP_PORT}/build-info"
        echo "  Logs:       ssh ${REMOTE_USER}@${REMOTE_SERVER} 'docker logs -f ${CONTAINER_NAME}'"
        exit 0
    fi
    print_warning "backend 未在超时内就绪，查容器日志："
    echo "  ssh ${REMOTE_USER}@${REMOTE_SERVER} 'docker logs --tail 60 ${CONTAINER_NAME}'"
    exit 1
fi

# ============================================================
# LOCAL 部署
# ============================================================
cd "$PROJECT_ROOT"

case "$ACTION" in
    logs)
        compose_cmd logs -f; exit 0 ;;
    status)
        compose_cmd ps || docker ps --filter "name=${CONTAINER_NAME}"; exit 0 ;;
    stop)
        print_info "Stopping..."; compose_cmd down 2>/dev/null || true
        print_success "Stopped"; exit 0 ;;
    restart)
        print_info "Restarting..."; ensure_networks_local
        compose_cmd down 2>/dev/null || true
        compose_cmd up -d --force-recreate
        print_success "Restarted"; exit 0 ;;
esac

ensure_networks_local

if [ "$NO_BUILD" = false ]; then
    print_info "Building image (GIT_SHA=${GIT_SHA})..."
    compose_cmd build
fi

if [ "$BUILD_ONLY" = true ]; then
    print_success "Build complete (--build-only)"; exit 0
fi

print_info "Starting service..."
if [ "$NO_BUILD" = true ]; then
    # local --no-build promote：起一个已 local-verify 通过、带 digest 的既有 APP_IMAGE 制品
    if [ -z "${APP_IMAGE:-}" ]; then
        print_error "APP_IMAGE is required for local --no-build promotion"
        exit 1
    fi
    if [ "${IMAGE_DIGEST}" = "digest_missing" ] || [ "${SOURCE_CONFIG_DIGEST}" = "digest_missing" ]; then
        print_error "IMAGE_DIGEST and SOURCE_CONFIG_DIGEST are required for local --no-build promotion"
        exit 1
    fi
    compose_cmd up -d --force-recreate --remove-orphans --no-build
else
    compose_cmd up -d --force-recreate --remove-orphans
fi

print_info "Waiting for service..."
sleep 3
backend_up=false
for i in $(seq 1 30); do
    if curl -sf "http://localhost:${APP_PORT}${HEALTH_ENDPOINT}" >/dev/null 2>&1; then
        backend_up=true; break
    fi
    echo "  Waiting for backend... ($i/30)"; sleep 2
done

if [ "$backend_up" = true ]; then
    print_success "Backend is up!"
    echo "  Health:     http://localhost:${APP_PORT}${HEALTH_ENDPOINT}"
    echo "  Build-info: http://localhost:${APP_PORT}/build-info"
    exit 0
fi
print_warning "Service may not be fully started, check logs:"
echo "  $0 --logs"
exit 1
