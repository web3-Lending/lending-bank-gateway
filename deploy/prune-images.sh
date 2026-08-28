#!/usr/bin/env bash
# ============================================================
# 部署后清理：同一镜像仓库只保留最近 N 个 tag
# ============================================================
# 每次部署都会打一个新 tag（<repo>:<service>-<ts>-<sha>），而部署链此前没有
# 任何回收逻辑 —— tag 只增不减。2026-08-28 实测 dev-hw：lending 四仓共 44 个
# 历史 tag、7.3GB，最老的一个已存活 6 周。本脚本补上这一步。
#
# 四仓（lending-core / lending-recon / lending-bank-gateway / baffle）各留一份
# 相同副本，改动请四份同步。
#
# 用法:
#   prune-images.sh <repository> [keep]
#     repository  镜像仓库名（不含 tag），如 lending-core-app
#     keep        保留最近几个 tag，默认 3（含当前正在跑的那个）
#
# 环境变量:
#   PRUNE_DRY_RUN=true  只打印将要删除的 tag，不真删
#   DOCKER_BIN          docker 可执行文件路径（默认 docker；测试用它注入桩）
#
# 安全边界（这几条是本脚本敢在部署链里自动跑的全部理由）:
#   1. 只碰 <repository>:* 这一个仓库。dev-hw 上同时跑着其它团队的镜像，
#      因此绝不使用 `docker image prune -a` 这类全局回收。repository 走严格
#      的 Docker 仓库名语法校验 —— 放行 `*` / `?` / `[` 会让 reference filter
#      的匹配面扩大到别人的仓库（codex 复核 2026-08-28 指出）。
#   2. 显式排除「任何容器（含已停止）正在引用的 image ID」的全部 tag。
#      不能只依赖 dockerd 拒绝删除：当多个 tag 指向同一 image ID 时，
#      `docker rmi repo:tag` 只做 untag 就会成功 —— 容器按 image ID 继续跑，
#      而你的当前版本 / 回滚素材的 tag 已经没了（同上，codex 复核实锤）。
#   3. 单个 tag 删不掉只记一行、继续处理下一个；只有前置条件不满足
#      （docker 不可用 / 参数非法）才非零退出，交给调用方决定是否阻断部署。
#
# 已知取舍：rmi 不带 --no-prune，被顺带回收的是已经没人引用的 untagged
# parent 层。dev-hw 上的镜像是 docker load 进来的、没有本地 parent 链，
# 影响仅限本机构建缓存，换来的是清理真的能释放磁盘。
set -euo pipefail

print_info()  { echo "[INFO] $1"; }
print_warn()  { echo "[WARN] $1"; }
print_error() { echo "[ERROR] $1" >&2; }

usage() {
    echo "Usage: $0 <repository> [keep]"
    echo "  repository  image repository without tag, e.g. lending-core-app"
    echo "  keep        how many newest tags to keep (default 3)"
    echo "Env: PRUNE_DRY_RUN=true (print only), DOCKER_BIN=<path to docker>"
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

REPO="${1:-}"
KEEP="${2:-3}"
DOCKER_BIN="${DOCKER_BIN:-docker}"

if [ -z "$REPO" ]; then
    print_error "repository is required"
    usage >&2
    exit 2
fi
# Docker 仓库名语法（小写字母数字，段间 . _ - 分隔，可带 / 路径）。冒号、
# 通配符、shell 元字符一律不放行 —— 见上方安全边界 1。
if ! [[ "$REPO" =~ ^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$ ]]; then
    print_error "repository is not a valid docker repository name: $REPO"
    exit 2
fi
case "$KEEP" in
    ''|*[!0-9]*) print_error "keep must be a non-negative integer, got: $KEEP"; exit 2 ;;
esac
if [ "$KEEP" -lt 1 ]; then
    # keep=0 会把正在跑的那个 tag 也列进删除名单。dockerd 未必拦得住（多 tag
    # 同 ID 时 rmi 只是 untag），所以这里直接拦掉，不依赖下游兜底。
    print_error "keep must be >= 1, got: $KEEP"
    exit 2
fi
if ! command -v "$DOCKER_BIN" > /dev/null 2>&1; then
    print_error "docker binary not found: $DOCKER_BIN"
    exit 2
fi

# 任何容器（含已停止）引用的 image ID —— 这些 ID 的每一个 tag 都不许动。
# 用 inspect 拿完整 sha256 ID：docker ps 的 .Image 列可能是名字也可能是 ID，
# 不能直接比。
in_use_ids="$("$DOCKER_BIN" ps -aq 2>/dev/null \
    | xargs -r "$DOCKER_BIN" inspect -f '{{.Image}}' 2>/dev/null \
    | sort -u || true)"

# CreatedAt 形如 `2026-08-28 10:08:08 +0800 CST`：同仓库的 tag 全由同一台
# docker 引擎打出，时区一致，因此字典序倒排 == 时间倒排。
# 注意这是**镜像创建时间**，不是打 tag / 部署时间：给旧 image ID 打一个新
# tag（回滚过的版本）时它排在后面，可能被算进「旧的」—— 但这种镜像必然正
# 被容器引用，已由上面的 in_use_ids 兜住。
candidates="$("$DOCKER_BIN" images --filter "reference=${REPO}:*" --no-trunc \
    --format '{{.CreatedAt}}|{{.ID}}|{{.Repository}}:{{.Tag}}' | sort -r)"

if [ -z "$candidates" ]; then
    print_info "${REPO}: no tag found, nothing to prune"
    exit 0
fi

stale="$(printf '%s\n' "$candidates" | tail -n +"$((KEEP + 1))")"

if [ -z "$stale" ]; then
    print_info "${REPO}: nothing beyond the newest ${KEEP} tag(s), nothing to prune"
    exit 0
fi

print_info "${REPO}: keeping newest ${KEEP}, examining $(printf '%s\n' "$stale" | wc -l | tr -d ' ') older tag(s)"

removed=0
kept=0
while IFS='|' read -r _created image_id image; do
    [ -n "${image:-}" ] || continue
    if [ -n "$in_use_ids" ] && printf '%s\n' "$in_use_ids" | grep -qxF "$image_id"; then
        # 同一 image ID 上还挂着容器：删这个 tag 会 untag 掉一个仍在服役
        # （或可回滚）的镜像，不碰。
        print_warn "  kept ${image} (image is referenced by a container)"
        kept=$((kept + 1))
    elif [ "${PRUNE_DRY_RUN:-false}" = "true" ]; then
        print_info "  would remove ${image}"
        removed=$((removed + 1))
    elif "$DOCKER_BIN" rmi "$image" > /dev/null 2>&1; then
        print_info "  removed ${image}"
        removed=$((removed + 1))
    else
        print_warn "  kept ${image} (removal refused by docker)"
        kept=$((kept + 1))
    fi
done <<< "$stale"

print_info "${REPO}: pruned ${removed}, kept ${kept}"
exit 0
