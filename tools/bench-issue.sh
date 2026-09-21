#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)

usage() {
  cat <<'EOF'
Usage / 用法：
  tools/bench-issue.sh [選項] <code>

用途：
  在管理端建立 Cloudflare Named Tunnel、產出 handoff/<code>/ bundle，
  並更新 benches.yaml 對應條目。

選項：
  --domain DOMAIN         指定 bench 網域（也可用環境變數 BENCH_ISSUE_DOMAIN）
  --target-user USER      benches.yaml 的 target user；預設為目前登入使用者
  --target TARGET         直接覆蓋 benches.yaml 的 target（user@host）
  --remote-socket PATH    benches.yaml 的 remote_socket（預設 /run/serialwrap/serialwrapd.sock）
  --no-autossh            benches.yaml 寫 autossh: false
  --dry-run               跳過 cloudflared provider 呼叫，但仍產生本地 bundle 與 benches.yaml
  -h, --help              顯示此說明

重要環境變數：
  BENCH_ISSUE_DOMAIN              預設 domain；也接受 SERIALWRAP_BENCH_DOMAIN
  SERIALWRAP_BENCHES_FILE         benches.yaml 位置
  BENCH_ISSUE_HANDOFF_ROOT        bundle 輸出根目錄（預設 <repo>/handoff）
  BENCH_ISSUE_CLOUDFLARED_HOME    cloudflared 憑證目錄（預設 ~/.cloudflared）
  BENCH_ISSUE_CLOUDFLARED_BIN     cloudflared 執行檔（預設 cloudflared）
  BENCH_ISSUE_IDENTITY_FILE       benches.yaml ssh_opts 使用的私鑰路徑
  BENCH_ISSUE_HOST_PUBLIC_KEY     要打包進 bundle 的 host 公鑰路徑
  BENCH_ISSUE_PROXY_COMMAND       ProxyCommand 主體
  BENCH_ISSUE_BASE_LOCAL_PORT     新 bench 起始 local_port（預設 7777）
  BENCH_ISSUE_DRY_RUN_UUID        dry-run 模式指定 tunnel UUID

說明：
  - benches.yaml 會寫入 provider-neutral 欄位：target / remote_socket /
    local_port / ssh_opts / autossh。
  - 預設 ssh_opts 會包含：
      -o "ProxyCommand=cloudflared access ssh --hostname %h"
      -i <identity-file>
EOF
}

die() {
  printf 'bench-issue.sh: %s\n' "$*" >&2
  exit 1
}

info() {
  printf '%s\n' "$*"
}

expand_path() {
  python3 - "$1" <<'PY'
import os
import sys

print(os.path.expanduser(sys.argv[1]))
PY
}

require_file() {
  local path
  path=$(expand_path "$1")
  local label=$2
  [[ -f "$path" ]] || die "$label 不存在：$path"
}

validate_target() {
  local value=$1
  [[ "$value" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*@([A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)(\.([A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?))*$ ]] || die "target 必須是安全且乾淨的 user@host：$value"
}

extract_uuid() {
  local raw=$1
  local uuid
  uuid=$(printf '%s\n' "$raw" | sed -nE 's/.*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}).*/\1/p' | head -n 1)
  [[ -n "$uuid" ]] || return 1
  printf '%s\n' "$uuid"
}

write_bundle_config() {
  local path=$1
  local tunnel_uuid=$2
  local fqdn=$3
  cat >"$path" <<EOF
tunnel: $tunnel_uuid
credentials-file: /etc/cloudflared/$tunnel_uuid.json
ingress:
  - hostname: $fqdn
    service: ssh://localhost:22
  - service: http_status:404
EOF
}

domain=${BENCH_ISSUE_DOMAIN:-${SERIALWRAP_BENCH_DOMAIN:-}}
target_user=${BENCH_ISSUE_TARGET_USER:-${USER:-}}
target_override=${BENCH_ISSUE_TARGET:-}
remote_socket=${BENCH_ISSUE_REMOTE_SOCKET:-/run/serialwrap/serialwrapd.sock}
autossh=1
dry_run=0

while (($# > 0)); do
  case "$1" in
    --domain)
      (($# >= 2)) || die "--domain 需要參數"
      domain=$2
      shift 2
      ;;
    --target-user)
      (($# >= 2)) || die "--target-user 需要參數"
      target_user=$2
      shift 2
      ;;
    --target)
      (($# >= 2)) || die "--target 需要參數"
      target_override=$2
      shift 2
      ;;
    --remote-socket)
      (($# >= 2)) || die "--remote-socket 需要參數"
      remote_socket=$2
      shift 2
      ;;
    --no-autossh)
      autossh=0
      shift
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      die "不支援的參數：$1"
      ;;
    *)
      break
      ;;
  esac
done

(($# == 1)) || {
  usage >&2
  die "需要且只能提供一個 bench 代號"
}

code=$1
[[ "$code" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "bench 代號格式不合法：$code"
[[ -n "$domain" ]] || die "需要提供 domain（--domain 或 BENCH_ISSUE_DOMAIN / SERIALWRAP_BENCH_DOMAIN）"

handoff_root=${BENCH_ISSUE_HANDOFF_ROOT:-"$REPO_ROOT/handoff"}
benches_file=${SERIALWRAP_BENCHES_FILE:-"${XDG_CONFIG_HOME:-$HOME/.config}/serialwrap/benches.yaml"}
cloudflared_home=${BENCH_ISSUE_CLOUDFLARED_HOME:-"$HOME/.cloudflared"}
cloudflared_bin=${BENCH_ISSUE_CLOUDFLARED_BIN:-cloudflared}
proxy_command=${BENCH_ISSUE_PROXY_COMMAND:-cloudflared access ssh --hostname %h}
base_local_port=${BENCH_ISSUE_BASE_LOCAL_PORT:-7777}
identity_file=${BENCH_ISSUE_IDENTITY_FILE:-}
host_public_key=${BENCH_ISSUE_HOST_PUBLIC_KEY:-}

if [[ -z "$host_public_key" && -n "$identity_file" ]]; then
  host_public_key="${identity_file}.pub"
fi
if [[ -z "$identity_file" && -n "$host_public_key" ]]; then
  identity_file=${host_public_key%.pub}
fi
if [[ -z "$host_public_key" ]]; then
  host_public_key="$HOME/.ssh/id_ed25519.pub"
fi
if [[ -z "$identity_file" ]]; then
  identity_file=${host_public_key%.pub}
fi

handoff_root=$(expand_path "$handoff_root")
benches_file=$(expand_path "$benches_file")
cloudflared_home=$(expand_path "$cloudflared_home")
identity_file=$(expand_path "$identity_file")
host_public_key=$(expand_path "$host_public_key")

require_file "$host_public_key" "host 公鑰"
require_file "$identity_file" "ssh 私鑰"

fqdn="$code.$domain"
if [[ -n "$target_override" ]]; then
  target=$target_override
else
  [[ -n "$target_user" ]] || die "需要 target user（--target-user 或 BENCH_ISSUE_TARGET_USER）"
  target="$target_user@$fqdn"
fi
validate_target "$target"

benches_candidate=$(mktemp "${TMPDIR:-/tmp}/bench-issue-benches.XXXXXX")
bundle_stage=
bundle_backup=
bundle_installed=0
benches_stage=
created_tunnel_ref=
operation_complete=0

cleanup_created_tunnel() {
  if ((dry_run)); then
    return 0
  fi
  [[ -n "$created_tunnel_ref" ]] || return 0
  "$cloudflared_bin" tunnel delete "$created_tunnel_ref" >/dev/null 2>&1 || true
}

cleanup() {
  local status=$?
  rm -f -- "$benches_candidate"
  if [[ -n "$benches_stage" && -e "$benches_stage" ]]; then
    rm -f -- "$benches_stage"
  fi
  if [[ -n "$bundle_stage" && -d "$bundle_stage" ]]; then
    rm -rf -- "$bundle_stage"
  fi
  if ((operation_complete == 0)); then
    cleanup_created_tunnel
    if [[ -n "$bundle_backup" && -d "$bundle_backup" ]]; then
      if [[ -n "${bundle_dir:-}" && -e "$bundle_dir" ]]; then
        rm -rf -- "$bundle_dir"
      fi
      mv -- "$bundle_backup" "$bundle_dir"
    elif ((bundle_installed == 1)) && [[ -n "${bundle_dir:-}" && -e "$bundle_dir" ]]; then
      rm -rf -- "$bundle_dir"
    fi
  fi
  return "$status"
}
trap cleanup EXIT

local_port=$(
  BENCH_ISSUE_CODE=$code \
  BENCH_ISSUE_TARGET_VALUE=$target \
  BENCH_ISSUE_REMOTE_SOCKET_VALUE=$remote_socket \
  BENCH_ISSUE_IDENTITY_FILE_VALUE=$identity_file \
  BENCH_ISSUE_PROXY_COMMAND_VALUE=$proxy_command \
  BENCH_ISSUE_AUTOSSH_VALUE=$autossh \
  BENCH_ISSUE_BASE_LOCAL_PORT_VALUE=$base_local_port \
  BENCH_ISSUE_BENCHES_FILE_VALUE=$benches_file \
  BENCH_ISSUE_BENCHES_OUTPUT_PATH=$benches_candidate \
  python3 - <<'PY'
import os
from pathlib import Path

import yaml

from sw_core import bench_registry as registry

path = Path(os.environ["BENCH_ISSUE_BENCHES_FILE_VALUE"])
output_path = Path(os.environ["BENCH_ISSUE_BENCHES_OUTPUT_PATH"])
code = os.environ["BENCH_ISSUE_CODE"]
target = os.environ["BENCH_ISSUE_TARGET_VALUE"]
remote_socket = os.environ["BENCH_ISSUE_REMOTE_SOCKET_VALUE"]
identity_file = os.environ["BENCH_ISSUE_IDENTITY_FILE_VALUE"]
proxy_command = os.environ["BENCH_ISSUE_PROXY_COMMAND_VALUE"]
autossh = os.environ["BENCH_ISSUE_AUTOSSH_VALUE"] == "1"
base_local_port = int(os.environ["BENCH_ISSUE_BASE_LOCAL_PORT_VALUE"])

try:
    existing = registry.load_benches(str(path))
except Exception as exc:
    raise SystemExit(f"既有 benches.yaml 無法通過 provider-neutral schema 驗證：{exc}") from exc

existing_port = existing.get(code).local_port if code in existing else None
ports = [base_local_port - 1]
ports.extend(entry.local_port for entry in existing.values())
local_port = existing_port if existing_port is not None else max(ports) + 1

updated = dict(existing)
updated[code] = registry.BenchEntry(
    target=target,
    remote_socket=remote_socket,
    local_port=local_port,
    ssh_opts=("-o", f"ProxyCommand={proxy_command}", "-i", identity_file),
    autossh=autossh,
)

payload = {
    "benches": {
        name: {
            "target": entry.target,
            "remote_socket": entry.remote_socket,
            "local_port": entry.local_port,
            "ssh_opts": list(entry.ssh_opts),
            "autossh": entry.autossh,
        }
        for name, entry in updated.items()
    }
}

output_path.write_text(
    yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
    encoding="utf-8",
)
registry.load_benches(str(output_path))
print(local_port)
PY
)

bundle_dir="$handoff_root/$code"
mkdir -p -- "$handoff_root"
bundle_stage=$(mktemp -d "$handoff_root/.${code}.stage.XXXXXX")

if ((dry_run)); then
  tunnel_uuid=${BENCH_ISSUE_DRY_RUN_UUID:-00000000-0000-4000-8000-000000000000}
  info "DRY-RUN: $cloudflared_bin tunnel create $code"
  info "DRY-RUN: $cloudflared_bin tunnel route dns $code $fqdn"
else
  if ! create_output=$("$cloudflared_bin" tunnel create "$code" 2>&1); then
    printf '%s\n' "$create_output" >&2
    die "cloudflared tunnel create 失敗"
  fi
  printf '%s\n' "$create_output"
  created_tunnel_ref=$code
  if ! tunnel_uuid=$(extract_uuid "$create_output"); then
    die "無法從 cloudflared 輸出解析 tunnel UUID"
  fi
  created_tunnel_ref=$tunnel_uuid
  if ! route_output=$("$cloudflared_bin" tunnel route dns "$code" "$fqdn" 2>&1); then
    printf '%s\n' "$route_output" >&2
    die "cloudflared tunnel route dns 失敗"
  fi
  if [[ -n "$route_output" ]]; then
    printf '%s\n' "$route_output"
  fi
fi

credentials_src="$cloudflared_home/$tunnel_uuid.json"
credentials_dst="$bundle_stage/$tunnel_uuid.json"
if ((dry_run)); then
  if [[ -f "$credentials_src" ]]; then
    cp -- "$credentials_src" "$credentials_dst"
  else
    cat >"$credentials_dst" <<EOF
{"TunnelID":"$tunnel_uuid","TunnelName":"$code","dry_run":true}
EOF
  fi
else
  if [[ ! -f "$credentials_src" ]]; then
    die "cloudflared tunnel credentials 不存在：$credentials_src"
  fi
  cp -- "$credentials_src" "$credentials_dst"
fi

cp -- "$host_public_key" "$bundle_stage/host_authorized_key.pub"
write_bundle_config "$bundle_stage/config.yml" "$tunnel_uuid" "$fqdn"
if [[ -e "$bundle_dir" ]]; then
  bundle_backup=$(mktemp -d "$handoff_root/.${code}.backup.XXXXXX")
  rmdir -- "$bundle_backup"
  mv -- "$bundle_dir" "$bundle_backup"
fi
mv -- "$bundle_stage" "$bundle_dir"
bundle_stage=
bundle_installed=1
benches_dir=$(dirname -- "$benches_file")
mkdir -p -- "$benches_dir"
benches_stage=$(mktemp "$benches_dir/.benches.stage.XXXXXX")
cp -- "$benches_candidate" "$benches_stage"
mv -- "$benches_stage" "$benches_file"
benches_stage=
if [[ -n "$bundle_backup" && -d "$bundle_backup" ]]; then
  rm -rf -- "$bundle_backup"
  bundle_backup=
fi
created_tunnel_ref=
operation_complete=1

info "已產出 bundle：$bundle_dir"
info "已更新 benches.yaml：$benches_file"
info "bench=$code fqdn=$fqdn local_port=$local_port tunnel_uuid=$tunnel_uuid"
