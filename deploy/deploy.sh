#!/bin/bash
# ============================================
# Lending Bank Gateway - Deployment Script
# ============================================
# Local Docker deployment: build + up + health check.
# The git commit SHA is baked into the image (--build-arg GIT_SHA) so that
# GET /build-info can prove which commit the running container was built from
# (deploy-verify step (a)). `git describe --always --dirty` flags an
# uncommitted working tree so a stamped sha can never lie about the code.
#
# Usage: ./deploy/deploy.sh [options]
#   --no-build      Skip build, use the existing image
#   --build-only    Only build the image, don't start the container
#   --logs          Tail container logs
#   --stop          Stop + remove the container
#   --restart       Restart the container (no rebuild)
#   --status        Show container status
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
ENV_FILE="$DEPLOY_DIR/env.local"
# Dedicated compose project name. WITHOUT this, docker compose defaults the
# project to the compose file's directory name ("deploy"), which COLLIDES with
# every other repo's deploy/ dir sharing the same default — and a `down`/`up`
# with --remove-orphans then deletes those repos' containers as cross-project
# "orphans". 2026-06-16 incident: 14 unrelated containers (lending-console-bff,
# lending-lifecycel, wedap-*) were wiped this way. Isolate to our own project.
COMPOSE_PROJECT="lending-bank-gateway"

# Git commit SHA baked into the image for GET /build-info anchoring.
# Exported so docker compose build args ${GIT_SHA} pick it up.
GIT_SHA="$(git -C "$PROJECT_ROOT" describe --always --dirty 2>/dev/null || echo unknown)"
# Harden against command injection: git describe can emit tag names, and git
# refs permit shell metacharacters. Reject anything outside a safe charset and
# fall back to the bare commit hash (+ -dirty). Defensive parity with the other
# repos' remote deploy path. (codex review NEEDS-ATTENTION 2026-06-16)
if ! [[ "$GIT_SHA" =~ ^[A-Za-z0-9._/-]+$ ]]; then
    GIT_SHA="$(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    git -C "$PROJECT_ROOT" diff --quiet 2>/dev/null || GIT_SHA="${GIT_SHA}-dirty"
fi
export GIT_SHA

# ── Colors & logging ─────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
print_info()    { echo -e "${CYAN}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[OK]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

# ── Args ─────────────────────────────────────────────────────
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

# ── docker compose v2 (fallback v1) ──────────────────────────
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

ensure_networks() {
    docker network create wedap-network 2>/dev/null || true
}

# ── Banner ───────────────────────────────────────────────────
echo "=========================================="
echo "  ${SERVICE_NAME} - Deployment"
echo "=========================================="
echo "  Commit:    ${GIT_SHA}"
echo "  Container: ${CONTAINER_NAME}"
echo "  Port:      ${APP_PORT}"
echo ""

if [ ! -f "$ENV_FILE" ]; then
    print_error "env file not found: $ENV_FILE (copy from deploy/env.example first)"
    exit 1
fi

cd "$PROJECT_ROOT"

# ── Non-deploy actions ───────────────────────────────────────
case "$ACTION" in
    logs)
        compose_cmd logs -f
        exit 0 ;;
    status)
        compose_cmd ps || docker ps --filter "name=${CONTAINER_NAME}"
        exit 0 ;;
    stop)
        print_info "Stopping..."
        compose_cmd down 2>/dev/null || true
        print_success "Stopped"
        exit 0 ;;
    restart)
        print_info "Restarting..."
        ensure_networks
        compose_cmd down 2>/dev/null || true
        compose_cmd up -d --force-recreate
        print_success "Restarted"
        exit 0 ;;
esac

# ── Full deploy ──────────────────────────────────────────────
ensure_networks

if [ "$NO_BUILD" = false ]; then
    print_info "Building image (GIT_SHA=${GIT_SHA})..."
    compose_cmd build
fi

if [ "$BUILD_ONLY" = true ]; then
    print_success "Build complete (--build-only)"
    exit 0
fi

print_info "Starting service..."
compose_cmd up -d --force-recreate

print_info "Waiting for service..."
sleep 3
max_retries=30
retry_count=0
backend_up=false
while [ $retry_count -lt $max_retries ]; do
    if curl -sf "http://localhost:${APP_PORT}${HEALTH_ENDPOINT}" >/dev/null 2>&1; then
        backend_up=true
        break
    fi
    retry_count=$((retry_count + 1))
    echo "  Waiting for backend... ($retry_count/$max_retries)"
    sleep 2
done

if [ "$backend_up" = true ]; then
    print_success "Backend is up!"
    echo ""
    echo "=========================================="
    echo "  Deployment complete!"
    echo "=========================================="
    echo "  Health:     http://localhost:${APP_PORT}${HEALTH_ENDPOINT}"
    echo "  Build-info: http://localhost:${APP_PORT}/build-info"
    echo "  Logs:       $0 --logs"
    echo "  Stop:       $0 --stop"
    echo "=========================================="
    exit 0
fi

print_warning "Service may not be fully started, check logs:"
echo "  $0 --logs"
exit 1
