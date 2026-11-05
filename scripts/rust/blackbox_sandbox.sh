#!/usr/bin/env bash
# 黑箱管理面測試的 Linux 入口：真實根目錄唯讀，只允許本 worktree 寫入。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
socket_args=()
if [[ "${1:-}" == "--with-docker" ]]; then
    # 只供 CI 的 L1.5 測試明確啟用；預設不暴露宿主 Docker 或 systemd socket。
    if ! docker_socket="$(readlink -e /var/run/docker.sock)" || [[ ! -S "$docker_socket" ]]; then
        echo '缺少 Docker Unix socket，證據不足' >&2
        exit 3
    fi
    socket_args=(--ro-bind "$docker_socket" /run/docker.sock)
    shift
fi
if [[ "$#" -eq 0 ]]; then
    echo '用法：blackbox_sandbox.sh [--with-docker] <命令> [參數...]' >&2
    exit 2
fi

# 記錄真正的外層 namespace，供 pidns_rig 與巢狀 A/B 核對；不得以旗標冒充已隔離。
# DNS 錯誤碼 oracle 需沿用宿主解析器；獨立網路會把 EAI_NONAME 改成 EAI_AGAIN。
outer_pidns="$(readlink /proc/self/ns/pid)"
exec bwrap --ro-bind / / --bind "$REPO_ROOT" "$REPO_ROOT" \
    --dev /dev --proc /proc --tmpfs /tmp --tmpfs /run \
    --unshare-pid --die-with-parent \
    --setenv SW_PIDNS_OUTER "$outer_pidns" \
    "${socket_args[@]}" --chdir "$REPO_ROOT" "$@"
