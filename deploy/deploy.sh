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

SERVICE_NAME="Lending Bank Gateway"
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

# Git commit SHA baked into the image for GET /build-info anchoring.
GIT_SHA="$(git -C "$PROJECT_ROOT" describe --always --dirty 2>/dev/null || echo unknown)"
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
echo "  ${SERVICE_NAME} - Deployment"
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

    # 注意：dev-hw 容器内 build 偶发 pip→files.pythonhosted.org read timeout（2026-06-18 实测，
    # 甚至把 SSH 会话拖断），故 remote 不在 dev-hw 上 build，而是「本机 build → docker save →
    # scp → dev-hw docker load → docker run」：本机网络可靠、镜像一次性传输，绕开 dev-hw 出网坑。
    # （与 recon 的"远程 build"差异在此——dev-hw gateway 出网比 recon 当时更不稳。）
    IMAGE_TAG="lending-bank-gateway:${ENV}"
    # mktemp 唯一路径 + trap 清理，避免并发/重试碰撞与失败残留（codex review LOW）。
    TARBALL="$(mktemp "/tmp/${COMPOSE_PROJECT}-${ENV}-XXXXXX.tar.gz")"
    GW_RUN_ENV="$(mktemp "/tmp/${COMPOSE_PROJECT}-runenv-XXXXXX")"
    REMOTE_TARBALL="/tmp/$(basename "$TARBALL")"
    REMOTE_RUN_ENV="/tmp/$(basename "$GW_RUN_ENV")"
    trap 'rm -f "$TARBALL" "$GW_RUN_ENV"' EXIT

    print_info "本机 build 镜像 ${IMAGE_TAG}（GIT_SHA=${GIT_SHA}）..."
    cd "$PROJECT_ROOT"
    DOCKER_BUILDKIT=1 docker build -f deploy/Dockerfile -t "${IMAGE_TAG}" \
        --build-arg GIT_SHA="${GIT_SHA}" \
        --build-arg APP_VERSION="${APP_VERSION}" \
        --build-arg RELEASE_ID="${COLLAB_RELEASE_ID:-not-reported}" \
        --build-arg SCHEMA_REVISION="${APP_SCHEMA_REVISION:-schema_unknown}" .

    print_info "docker save + gzip + scp 到 dev-hw + load ..."
    docker save "${IMAGE_TAG}" | gzip > "${TARBALL}"
    scp_cmd "${TARBALL}" "${REMOTE_TARBALL}"
    ssh_cmd "gunzip -c ${REMOTE_TARBALL} | docker load && rm -f ${REMOTE_TARBALL}"

    print_info "确保 wedap-network 存在 ..."
    ssh_cmd "docker network create wedap-network 2>/dev/null || true"

    # 容器 env 写临时文件 → scp(600) → docker run --env-file，避免把 secret 插进远程 shell
    # 字符串（含 ' 会破坏引用/注入，codex review HIGH）。注：env 仍会出现在 docker inspect，
    # 与其它服务一致，属 dev-hw 可接受口径。GW_S2S_SECRET 非 local/test 必填（资金网关 fail-fast）。
    print_info "dev-hw 起容器（${APP_PORT} / wedap-network；entrypoint alembic→uvicorn）..."
    {
        printf 'GW_DB_URL=mysql+asyncmy://%s:%s@%s:3306/lending_bank_gateway\n' "$DB_USER" "$DB_PASS" "$DB_HOST"
        printf 'GW_WEDAP_BASE_URL=%s\n' "$WEDAP_BASE_URL"
        printf 'GW_S2S_SECRET=%s\n' "$GW_S2S_SECRET"
        printf 'GW_ENV=%s\n' "$GW_ENV"
        # 运行态发布身份 env（/api/version 回显源；规范0707 §Docker/OCI Label 与环境变量）
        printf 'APP_VERSION=%s\n' "$APP_VERSION"
        printf 'GIT_SHA=%s\n' "$GIT_SHA"
        printf 'BUILD_TIME_HKT=%s\n' "$BUILD_TIME_HKT"
        printf 'COLLAB_RELEASE_ENV=%s\n' "$GW_ENV"
        [ -n "${COLLAB_RELEASE_ID:-}" ] && printf 'COLLAB_RELEASE_ID=%s\n' "$COLLAB_RELEASE_ID"
        [ -n "${COLLAB_RELEASE_RUN_ID:-}" ] && printf 'COLLAB_RELEASE_RUN_ID=%s\n' "$COLLAB_RELEASE_RUN_ID"
        [ -n "${APP_SCHEMA_REVISION:-}" ] && printf 'APP_SCHEMA_REVISION=%s\n' "$APP_SCHEMA_REVISION"
        # flow-import + 银行 API + S3 可选透传：env.<ENV> 定义即注入容器；未定义留空=直连 baffle
        # （codex HIGH：原先只透传 4 项，切流 APISIX 时容器凭证仍空→_bank_request 退回 baffle→400/401）。
        # AWS_* 走 boto3 标准 env（非 GW_ 前缀）；其余为 GW_ 前缀的 Settings 字段。
        for _kv in \
            "GW_WEDAP_IMPORT_API_KEY=${WEDAP_IMPORT_API_KEY:-}" \
            "GW_WEDAP_IMPORT_SIGNING_SECRET=${WEDAP_IMPORT_SIGNING_SECRET:-}" \
            "GW_WEDAP_IMPORT_BUCKET=${WEDAP_IMPORT_BUCKET:-}" \
            "GW_WEDAP_STAGING_BUCKET=${WEDAP_STAGING_BUCKET:-}" \
            "GW_WEDAP_PRESIGNED_ENABLED=${WEDAP_PRESIGNED_ENABLED:-}" \
            "GW_WEDAP_DELIVERY_ENABLED=${WEDAP_DELIVERY_ENABLED:-}" \
            "GW_WEDAP_BANK_API_KEY=${WEDAP_BANK_API_KEY:-}" \
            "GW_WEDAP_BANK_SIGNING_SECRET=${WEDAP_BANK_SIGNING_SECRET:-}" \
            "GW_S3_ENDPOINT_URL=${S3_ENDPOINT_URL:-}" \
            "GW_RECON_BASE_URL=${RECON_BASE_URL:-}" \
            "GW_RECON_CALLBACK_HMAC_SECRET=${RECON_CALLBACK_HMAC_SECRET:-}" \
            "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-}" \
            "AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-}" \
            "AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-}"; do
            [ -n "${_kv#*=}" ] && printf '%s\n' "$_kv"
        done
    } > "$GW_RUN_ENV"
    chmod 600 "$GW_RUN_ENV"
    scp_cmd "$GW_RUN_ENV" "$REMOTE_RUN_ENV"
    ssh_cmd "chmod 600 ${REMOTE_RUN_ENV}"
    ssh_cmd "docker rm -f ${CONTAINER_NAME} 2>/dev/null || true"
    # remote 走 docker run 不经 compose，compose 的 json-file logging 块在 dev-hw 不生效；
    # dev-hw daemon.json 也无全局 log-opts（2026-07-13 实测 LogConfig=json-file map[]），
    # 轮转上限必须在 run 命令显式带上，与 compose 口径一致（3 × 100MB）。
    ssh_cmd "docker run -d --name ${CONTAINER_NAME} --network wedap-network --restart unless-stopped --log-driver json-file --log-opt max-size=100m --log-opt max-file=3 -p ${APP_PORT}:${APP_PORT} --env-file ${REMOTE_RUN_ENV} ${IMAGE_TAG}"
    ssh_cmd "rm -f ${REMOTE_RUN_ENV}"

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
compose_cmd up -d --force-recreate

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
