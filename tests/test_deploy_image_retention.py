"""deploy/prune-images.sh 的行为契约 + deploy.sh 接线。

这个脚本在部署链尾部自动跑、会真删镜像，所以每条断言都对着一个具体的
破坏场景：删错仓库、untag 掉一个容器还在用的镜像、按列表顺序而不是时间
顺序挑、把清理失败当成部署失败、把带分号的镜像名送到远端 shell 执行。

docker 本身用桩替代（DOCKER_BIN）。桩回放 `docker images` 的输出格式、
`ps`/`inspect` 的在用镜像、`rmi` 的成功/拒绝。**reference filter 的过滤
不在桩里假装实现** —— 「只碰本仓库」那条性质由 dockerd 保证，测试能验证
的是脚本确实把正确的 filter 传了出去，所以单独有一条断言盯 argv。
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "prune-images.sh"
DEPLOY_SH = ROOT / "deploy" / "deploy.sh"

REPO = "lending-bank-gateway"

# (CreatedAt, image id, repo:tag) —— 故意不按时间顺序排列：脚本必须按 CreatedAt
# 排序，而不是照单全收 docker 的输出顺序。
IMAGES = [
    (
        "2026-08-20 09:00:00 +0800 CST",
        "sha256:aaa",
        f"{REPO}:lending-bank-gateway-20260820-090000-aaaaaaa",
    ),
    (
        "2026-08-28 10:08:08 +0800 CST",
        "sha256:eee",
        f"{REPO}:lending-bank-gateway-20260828-100808-eeeeeee",
    ),
    (
        "2026-08-24 17:31:14 +0800 CST",
        "sha256:bbb",
        f"{REPO}:lending-bank-gateway-20260824-173114-bbbbbbb",
    ),
    (
        "2026-08-27 18:28:01 +0800 CST",
        "sha256:ddd",
        f"{REPO}:lending-bank-gateway-20260827-182801-ddddddd",
    ),
    (
        "2026-08-26 12:13:53 +0800 CST",
        "sha256:ccc",
        f"{REPO}:lending-bank-gateway-20260826-121353-ccccccc",
    ),
]
NEWEST_THREE = {IMAGES[1][2], IMAGES[3][2], IMAGES[4][2]}
OLDEST_TWO = {IMAGES[2][2], IMAGES[0][2]}


@pytest.fixture
def docker_stub(tmp_path):
    """返回 (stub_path, rmi_log, argv_log)。in_use 是被容器引用的 image ID。"""

    def _make(images=IMAGES, refuse=(), in_use=()):
        rmi_log = tmp_path / "rmi.log"
        argv_log = tmp_path / "argv.log"
        listing = "\n".join(f"{created}|{image_id}|{ref}" for created, image_id, ref in images)
        containers = "\n".join(f"container{i}" for i, _ in enumerate(in_use))
        stub = tmp_path / "docker-stub"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{argv_log}"\n'
            'case "$1" in\n'
            "  images)\n"
            f"    cat <<'LISTING'\n{listing}\nLISTING\n"
            "    ;;\n"
            "  ps)\n"
            f"    cat <<'CONTAINERS'\n{containers}\nCONTAINERS\n"
            "    ;;\n"
            "  inspect)\n"
            f"    cat <<'INUSE'\n{chr(10).join(in_use)}\nINUSE\n"
            "    ;;\n"
            "  rmi)\n"
            f'    printf "%s\\n" "$2" >> "{rmi_log}"\n'
            f'    case " {" ".join(refuse)} " in *" $2 "*) exit 1 ;; esac\n'
            "    ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        return stub, rmi_log, argv_log

    return _make


def _run(stub, *args, **env):
    environ = {**os.environ, "DOCKER_BIN": str(stub), **env}
    return subprocess.run(  # noqa: S603 — 参数全是本文件写死的路径，没有外部输入
        ["bash", str(SCRIPT), *args],  # noqa: S607 — 测试环境里 bash 走 PATH 是既定前提
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=environ,
    )


def _removed(rmi_log):
    if not rmi_log.exists():
        return set()
    return {line for line in rmi_log.read_text(encoding="utf-8").split("\n") if line}


def test_keeps_newest_three_and_removes_the_rest(docker_stub):
    stub, rmi_log, _ = docker_stub()

    result = _run(stub, REPO)

    assert result.returncode == 0, result.stderr
    assert _removed(rmi_log) == OLDEST_TWO
    # 正在跑的那个（最新）必须一根汗毛都不掉。
    assert _removed(rmi_log) & NEWEST_THREE == set()


def test_orders_by_created_at_not_by_listing_order(docker_stub):
    """docker 的输出顺序不可依赖；按列表顺序截断会删掉最新的镜像。"""
    shuffled = [IMAGES[1], IMAGES[0], IMAGES[3], IMAGES[2], IMAGES[4]]
    stub, rmi_log, _ = docker_stub(images=shuffled)

    _run(stub, REPO)

    assert _removed(rmi_log) == OLDEST_TWO


def test_never_untags_an_image_a_container_still_references(docker_stub):
    """多个 tag 指向同一 image ID 时，rmi 只 untag 就会成功。

    容器按 image ID 继续跑，看起来毫发无伤，但那个 tag（可能正是当前版本或
    回滚素材）已经没了。所以在用的 image ID 必须显式排除，不能指望 dockerd
    替我们拦。
    """
    # 最旧的那个 tag 与一个仍在跑的容器共用 image ID
    stub, rmi_log, _ = docker_stub(in_use=("sha256:aaa",))

    result = _run(stub, REPO)

    assert result.returncode == 0, result.stderr
    assert IMAGES[0][2] not in _removed(rmi_log), "在用的 image ID 上的 tag 不该被 rmi"
    assert "referenced by a container" in result.stdout
    # 另一个旧 tag 不受影响，照删
    assert IMAGES[2][2] in _removed(rmi_log)


def test_scopes_the_query_to_this_repository_only(docker_stub):
    """dev-hw 上跑着其它团队的镜像：全局 prune 会误伤，必须带 reference filter。"""
    stub, _, argv_log = docker_stub()

    _run(stub, REPO)

    argv = argv_log.read_text(encoding="utf-8")
    assert f"--filter reference={REPO}:*" in argv
    assert "image prune" not in argv


def test_keep_argument_is_honoured(docker_stub):
    stub, rmi_log, _ = docker_stub()

    _run(stub, REPO, "4")

    assert _removed(rmi_log) == {IMAGES[0][2]}


def test_dry_run_touches_nothing(docker_stub):
    stub, rmi_log, _ = docker_stub()

    result = _run(stub, REPO, PRUNE_DRY_RUN="true")

    assert result.returncode == 0
    assert _removed(rmi_log) == set()
    assert "would remove" in result.stdout


def test_removal_refused_by_docker_is_not_a_failure(docker_stub):
    """镜像还被容器引用时 dockerd 会拒绝 —— 这是设计内的保护，不是故障。"""
    refused = IMAGES[2][2]
    stub, rmi_log, _ = docker_stub(refuse=(refused,))

    result = _run(stub, REPO)

    assert result.returncode == 0, result.stderr
    assert refused in _removed(rmi_log)  # 尝试过
    assert "removal refused by docker" in result.stdout  # 但如实报告没删成
    assert f"removed {IMAGES[0][2]}" in result.stdout


def test_nothing_to_prune_when_within_retention(docker_stub):
    stub, rmi_log, _ = docker_stub(images=IMAGES[:2])

    result = _run(stub, REPO, "3")

    assert result.returncode == 0
    assert _removed(rmi_log) == set()
    assert "nothing to prune" in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        (),  # 缺 repository
        (f"{REPO}:latest",),  # 带 tag，会让 filter 只匹配单个 tag
        ("*",),  # 通配符：reference=*:* 会扩到别人的仓库
        ("lending-core-app?",),
        ("lending-core-app[ab]",),
        ("evil; rm -rf /tmp/x",),  # shell 元字符
        (REPO, "zero"),  # keep 非数字
        (REPO, "0"),  # keep=0 会把当前版本列进删除名单
    ],
)
def test_rejects_dangerous_or_malformed_arguments(docker_stub, args):
    stub, rmi_log, _ = docker_stub()

    result = _run(stub, *args)

    assert result.returncode == 2
    assert _removed(rmi_log) == set()


def test_deploy_script_prunes_on_both_success_paths():
    """加了脚本却没接线 = 白加。两条部署路径各自都要调。"""
    text = DEPLOY_SH.read_text(encoding="utf-8")

    assert text.count("prune_local_images") >= 2  # 定义 + 至少一处调用
    assert text.count("prune_remote_images") >= 2
    assert f'DEFAULT_IMAGE_REPO="{REPO}"' in text


def test_prune_failure_never_fails_the_deploy():
    """清理是尽力而为：把一次成功的部署改判成失败，比留几个旧 tag 糟得多。"""
    text = DEPLOY_SH.read_text(encoding="utf-8")

    for call in ('bash "$DEPLOY_DIR/prune-images.sh"', 'ssh_cmd "echo ${payload}'):
        idx = text.index(call)
        assert "print_warning" in text[idx : idx + 400]


# ── deploy.sh 的远端接线：真把函数体跑起来，而不是 grep 源码 ──────────


def _extract_bash_function(text, name):
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _run_prune_remote(tmp_path, app_image, keep="3", healthy=True):
    """跑 deploy.sh 的 prune_remote_images，ssh_cmd / print_* 全部打桩。

    没有任何东西真的连出去；我们只看这个函数最终打算把什么字符串交给远端
    shell 执行 —— 以及在什么情况下它选择什么都不发。
    """
    text = DEPLOY_SH.read_text(encoding="utf-8")
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -uo pipefail\n"
        f'DEPLOY_DIR="{ROOT / "deploy"}"\n'
        f'DEFAULT_IMAGE_REPO="{REPO}"\n'
        f'IMAGE_RETENTION_KEEP="{keep}"\n'
        f"APP_IMAGE='{app_image}'\n"
        'APP_PORT="8022"\nHEALTH_ENDPOINT="/healthz"\n'
        "print_info() { :; }\n"
        'print_warning() { echo "WARN:$1"; }\n'
        # 健康探测与真正的清理调用共用 ssh_cmd_soft：按参数区分，probe 返回
        # 可控的成功/失败，其余原样回显。
        'ssh_cmd() { case "$1" in *curl*) return '
        + ("0" if healthy else "1")
        + ';; *) echo "SSH:$1" ;; esac; }\n'
        + _extract_bash_function(text, "resolve_image_repo")
        + _extract_bash_function(text, "ssh_probe_healthy")
        + _extract_bash_function(text, "prune_remote_images")
        + "prune_remote_images\n",
        encoding="utf-8",
    )
    return subprocess.run(  # noqa: S603 — 全是本文件生成的内容，没有外部输入
        ["bash", str(harness)],  # noqa: S607 — 测试环境里 bash 走 PATH 是既定前提
        text=True,
        capture_output=True,
    )


def test_remote_prune_sends_the_expected_command(tmp_path):
    result = _run_prune_remote(tmp_path, f"{REPO}:lending-bank-gateway-20260828-100808-abc")

    assert f"base64 -d | bash -s -- {REPO} 3" in result.stdout
    assert "WARN:" not in result.stdout


def test_remote_prune_refuses_a_repository_name_that_would_inject(tmp_path):
    """repo 被拼进一条交给远端 shell 执行的串：带分号就是远端任意命令执行。

    脚本自己的参数校验发生在远端 shell 解析**之后**，挡不住这一层，所以闸门
    必须在拼串之前。
    """
    result = _run_prune_remote(tmp_path, "evil; touch /tmp/pwned:tag")

    assert "SSH:" not in result.stdout, "注入的镜像名不应该被送到远端执行"
    assert "WARN:" in result.stdout


def test_remote_prune_refuses_a_non_numeric_retention(tmp_path):
    result = _run_prune_remote(tmp_path, f"{REPO}:x", keep="3; rm -rf /tmp/nope")

    assert "SSH:" not in result.stdout
    assert "WARN:" in result.stdout


def test_remote_prune_waits_for_the_service_to_be_healthy(tmp_path):
    """compose up 返回 ≠ 服务就绪。没起来时旧 tag 是唯一的回滚素材，不能删。"""
    result = _run_prune_remote(tmp_path, f"{REPO}:x", healthy=False)

    assert "SSH:" not in result.stdout
    assert "not healthy" in result.stdout


def test_registry_qualified_image_is_skipped_not_mistaken_for_another_repo(tmp_path):
    """`${ref%%:*}` 会把 registry:5000/repo:tag 截成 `registry` —— 那是另一个仓库名，
    清理就清到别人头上去了。

    正确剥完 tag 之后，剩下的引用带 registry 端口的冒号，过不了字符集闸门，
    于是整次清理被跳过。四仓目前都用本地 tag、不走 registry，所以这只是
    fail-closed 的兜底 —— 关键是它跳过，而不是拿着 `registry.example` 去删。
    """
    result = _run_prune_remote(tmp_path, "registry.example:5000/lending-bank-gateway:v1")

    assert "SSH:" not in result.stdout
    assert "registry.example:5000/lending-bank-gateway" in result.stdout
    assert (
        "WARN:Image prune skipped (unsafe repository name: registry.example)" not in result.stdout
    )
