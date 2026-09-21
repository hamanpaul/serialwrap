#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage / 用法：
  tools/bench-enroll.sh --bundle <dir> [--dry-run]

用途：
  在 remote 端安裝並設定 cloudflared、強制 sshd 走金鑰登入，
  追加 host 公鑰，最後執行兩個檢查點：
    CP-1 journal 看到 "Registered tunnel connection"
    CP-2 本地 self-ssh 成功

選項：
  --bundle DIR    handoff bundle 目錄（需含 config.yml、<UUID>.json、host_authorized_key.pub）
  --dry-run       不碰真實系統服務；若未覆寫目的路徑，會自動寫到暫存 sandbox
  -h, --help      顯示此說明

重要環境變數：
  BENCH_ENROLL_CLOUDFLARED_DIR     覆寫 cloudflared 目的目錄
  BENCH_ENROLL_SSHD_CONFIG_PATH    覆寫 sshd_config 路徑
  BENCH_ENROLL_SSHD_DROPIN_PATH    覆寫 key-only sshd drop-in 路徑
  BENCH_ENROLL_AUTHORIZED_KEYS_PATH 覆寫 authorized_keys 路徑
  BENCH_ENROLL_DRY_RUN_ROOT        dry-run sandbox 根目錄
  BENCH_ENROLL_CP1_LOG             dry-run 模式模擬 journal 內容
  BENCH_ENROLL_CP1_TIMEOUT_SEC     CP-1 最長等待秒數（預設 30）
  BENCH_ENROLL_CP1_POLL_INTERVAL_SEC CP-1 輪詢間隔秒數（預設 1）
  BENCH_ENROLL_CP2_RESULT          dry-run 模式指定 cp2 結果（pass 或 fail）
  BENCH_ENROLL_LOGIN_USER          self-ssh / authorized_keys 使用者
                                   （預設 SUDO_USER，否則目前登入使用者）
  BENCH_ENROLL_LOGIN_HOME          覆寫 BENCH_ENROLL_LOGIN_USER 的 home
  BENCH_ENROLL_APT_GET_BIN         apt-get 執行檔（預設 apt-get）
  BENCH_ENROLL_CLOUDFLARED_BIN     cloudflared 執行檔（預設 cloudflared）
  BENCH_ENROLL_JOURNALCTL_BIN      journalctl 執行檔（預設 journalctl）
  BENCH_ENROLL_SYSTEMCTL_BIN       systemctl 執行檔（預設 systemctl）
  BENCH_ENROLL_SSH_BIN             ssh 執行檔（預設 ssh）
  BENCH_ENROLL_SSH_KEYGEN_BIN      ssh-keygen 執行檔（預設 ssh-keygen）
  BENCH_ENROLL_SUDO_BIN            sudo 執行檔（預設 sudo）
EOF
}

die() {
  printf 'bench-enroll.sh: %s\n' "$*" >&2
  exit 1
}

info() {
  printf '%s\n' "$*"
}

run_as_root() {
  if ((EUID == 0)); then
    "$@"
  else
    "$sudo_bin" "$@"
  fi
}

render_cloudflared_config() {
  local src=$1
  local dest=$2
  local credentials_path=$3
  CLOUD_FLARED_SRC=$src CLOUD_FLARED_DEST=$dest CLOUD_FLARED_CREDENTIALS=$credentials_path python3 - <<'PY'
import os
from pathlib import Path

src = Path(os.environ["CLOUD_FLARED_SRC"])
dest = Path(os.environ["CLOUD_FLARED_DEST"])
credentials_path = os.environ["CLOUD_FLARED_CREDENTIALS"]

lines = src.read_text(encoding="utf-8").splitlines()
updated = False
out_lines: list[str] = []
for line in lines:
    if line.startswith("credentials-file:"):
        out_lines.append(f"credentials-file: {credentials_path}")
        updated = True
    else:
        out_lines.append(line)
if not updated:
    raise SystemExit(f"config 缺少 credentials-file: {src}")
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
PY
}

render_sshd_main_with_include() {
  local src=$1
  local dest=$2
  local include_path=$3
  SSHD_MAIN_SRC=$src SSHD_MAIN_DEST=$dest SSHD_INCLUDE_PATH=$include_path python3 - <<'PY'
import os
from pathlib import Path

src = Path(os.environ["SSHD_MAIN_SRC"])
dest = Path(os.environ["SSHD_MAIN_DEST"])
include_line = f"Include {os.environ['SSHD_INCLUDE_PATH']}"
dest.parent.mkdir(parents=True, exist_ok=True)
if src.exists():
    lines = src.read_text(encoding="utf-8").splitlines()
else:
    lines = []
filtered = [line for line in lines if line.strip() != include_line]
out_lines = [include_line]
if filtered:
    out_lines.append("")
    out_lines.extend(filtered)
dest.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
PY
}

render_sshd_key_only_dropin() {
  local path=$1
  cat >"$path" <<'EOF'
# Managed by tools/bench-enroll.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PubkeyAuthentication yes
EOF
}

resolve_login_home() {
  local user=$1
  local dry_run_flag=$2
  local sandbox=${3:-}
  BENCH_ENROLL_LOGIN_USER_VALUE=$user \
  BENCH_ENROLL_DRY_RUN_FLAG=$dry_run_flag \
  BENCH_ENROLL_DRY_RUN_SANDBOX=$sandbox \
  python3 - <<'PY'
import os
import pwd

override = os.environ.get("BENCH_ENROLL_LOGIN_HOME")
if override:
    print(os.path.expanduser(override))
    raise SystemExit(0)

user = os.environ["BENCH_ENROLL_LOGIN_USER_VALUE"]
if os.environ["BENCH_ENROLL_DRY_RUN_FLAG"] == "1":
    sandbox = os.environ["BENCH_ENROLL_DRY_RUN_SANDBOX"]
    print(os.path.join(sandbox, "home", user))
    raise SystemExit(0)

try:
    print(pwd.getpwnam(user).pw_dir)
except KeyError as exc:
    raise SystemExit(f"找不到登入使用者 {user} 的 home 目錄") from exc
PY
}

current_utc_timestamp() {
  python3 - <<'PY'
from datetime import datetime, timezone

print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f UTC"))
PY
}

capture_cloudflared_cursor() {
  local cursor_output
  cursor_output=$(run_as_root "$journalctl_bin" -u cloudflared -n 0 --show-cursor -o cat 2>/dev/null || true)
  printf '%s\n' "$cursor_output" | sed -n 's/^-- cursor: //p' | tail -n 1
}

fetch_cloudflared_logs() {
  local cursor=$1
  local since_marker=$2
  if [[ -n "$cursor" ]]; then
    run_as_root "$journalctl_bin" -u cloudflared --after-cursor "$cursor" -o cat 2>&1
  else
    run_as_root "$journalctl_bin" -u cloudflared --since "$since_marker" -o cat 2>&1
  fi
}

wait_for_cloudflared_registration() {
  local cursor=$1
  local since_marker=$2
  local timeout_s=$3
  local interval_s=$4
  local deadline
  deadline=$(python3 - "$timeout_s" <<'PY'
import sys
import time

print(time.monotonic() + float(sys.argv[1]))
PY
)

  while :; do
    cp1_log=$(fetch_cloudflared_logs "$cursor" "$since_marker" || true)
    if grep -Fq "Registered tunnel connection" <<<"$cp1_log"; then
      return 0
    fi
    if python3 - "$deadline" <<'PY'
import sys
import time

raise SystemExit(0 if time.monotonic() >= float(sys.argv[1]) else 1)
PY
    then
      return 1
    fi
    sleep "$interval_s"
  done
}

append_unique_line() {
  local file=$1
  local line=$2
  mkdir -p -- "$(dirname -- "$file")"
  touch -- "$file"
  if ! grep -Fqx -- "$line" "$file"; then
    printf '%s\n' "$line" >>"$file"
  fi
}

remove_exact_line() {
  local file=$1
  local line=$2
  REMOVE_LINE_FILE=$file REMOVE_LINE_VALUE=$line python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["REMOVE_LINE_FILE"])
target = os.environ["REMOVE_LINE_VALUE"]
if not path.exists():
    raise SystemExit(0)
lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line != target]
path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
PY
}

print_checkpoint_pass() {
  printf '%s PASS: %s\n' "$1" "$2"
}

print_checkpoint_fail() {
  printf '%s FAIL: %s\n' "$1" "$2" >&2
}

bundle_dir=
dry_run=0

while (($# > 0)); do
  case "$1" in
    --bundle)
      (($# >= 2)) || die "--bundle 需要參數"
      bundle_dir=$2
      shift 2
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
      die "不支援的位置參數：$1"
      ;;
  esac
done

(($# == 0)) || die "不支援額外位置參數"
[[ -n "$bundle_dir" ]] || {
  usage >&2
  die "需要 --bundle <dir>"
}

bundle_dir=$(
  python3 - "$bundle_dir" <<'PY'
import os
import sys

print(os.path.expanduser(sys.argv[1]))
PY
)
[[ -d "$bundle_dir" ]] || die "bundle 目錄不存在：$bundle_dir"
bundle_dir=$(CDPATH='' cd -- "$bundle_dir" && pwd)

config_src=$bundle_dir/config.yml
if [[ ! -f "$config_src" && -f "$bundle_dir/config.yaml" ]]; then
  config_src=$bundle_dir/config.yaml
fi
[[ -f "$config_src" ]] || die "bundle 缺少 config.yml"

host_key_src=$bundle_dir/host_authorized_key.pub
[[ -f "$host_key_src" ]] || die "bundle 缺少 host_authorized_key.pub"

shopt -s nullglob
credential_files=("$bundle_dir"/*.json)
shopt -u nullglob
(( ${#credential_files[@]} == 1 )) || die "bundle 必須且只能包含一個 tunnel credentials json"
credentials_src=${credential_files[0]}
credentials_name=$(basename -- "$credentials_src")

login_user=${BENCH_ENROLL_LOGIN_USER:-${SUDO_USER:-$(id -un)}}
apt_get_bin=${BENCH_ENROLL_APT_GET_BIN:-apt-get}
cloudflared_bin=${BENCH_ENROLL_CLOUDFLARED_BIN:-cloudflared}
journalctl_bin=${BENCH_ENROLL_JOURNALCTL_BIN:-journalctl}
systemctl_bin=${BENCH_ENROLL_SYSTEMCTL_BIN:-systemctl}
ssh_bin=${BENCH_ENROLL_SSH_BIN:-ssh}
ssh_keygen_bin=${BENCH_ENROLL_SSH_KEYGEN_BIN:-ssh-keygen}
sudo_bin=${BENCH_ENROLL_SUDO_BIN:-sudo}
cp1_timeout_s=${BENCH_ENROLL_CP1_TIMEOUT_SEC:-30}
cp1_poll_interval_s=${BENCH_ENROLL_CP1_POLL_INTERVAL_SEC:-1}

cleanup_tmpdir=
cp2_tmpdir=
cp2_temp_key_line=
auto_sandbox_root=
cloudflared_tmp=
sshd_main_tmp=
sshd_dropin_tmp=
cp1_log=

cleanup() {
  if [[ -n "$cp2_temp_key_line" && -n "${authorized_keys_path:-}" ]]; then
    remove_exact_line "$authorized_keys_path" "$cp2_temp_key_line"
  fi
  [[ -n "$cp2_tmpdir" ]] && rm -rf -- "$cp2_tmpdir"
  [[ -n "$cloudflared_tmp" ]] && rm -f -- "$cloudflared_tmp"
  [[ -n "$sshd_main_tmp" ]] && rm -f -- "$sshd_main_tmp"
  [[ -n "$sshd_dropin_tmp" ]] && rm -f -- "$sshd_dropin_tmp"
  if [[ -n "$cleanup_tmpdir" ]]; then
    rm -rf -- "$cleanup_tmpdir"
  fi
}
trap cleanup EXIT

if ((dry_run)); then
  if [[ -n ${BENCH_ENROLL_DRY_RUN_ROOT:-} ]]; then
    sandbox_root=${BENCH_ENROLL_DRY_RUN_ROOT}
  else
    sandbox_root=$(mktemp -d "${TMPDIR:-/tmp}/bench-enroll.XXXXXX")
    cleanup_tmpdir=$sandbox_root
  fi
  auto_sandbox_root=$sandbox_root
  cloudflared_dir_default="$sandbox_root/etc/cloudflared"
  sshd_config_default="$sandbox_root/etc/ssh/sshd_config"
else
  cloudflared_dir_default=/etc/cloudflared
  sshd_config_default=/etc/ssh/sshd_config
fi

login_home=$(resolve_login_home "$login_user" "$dry_run" "${sandbox_root:-}")
authorized_keys_default="$login_home/.ssh/authorized_keys"

cloudflared_dir=${BENCH_ENROLL_CLOUDFLARED_DIR:-$cloudflared_dir_default}
sshd_config_path=${BENCH_ENROLL_SSHD_CONFIG_PATH:-$sshd_config_default}
sshd_dropin_default="$(dirname -- "$sshd_config_path")/sshd_config.d/serialwrap-bench-key-only.conf"
sshd_dropin_path=${BENCH_ENROLL_SSHD_DROPIN_PATH:-$sshd_dropin_default}
authorized_keys_path=${BENCH_ENROLL_AUTHORIZED_KEYS_PATH:-$authorized_keys_default}
credentials_dst="$cloudflared_dir/$credentials_name"
config_dst="$cloudflared_dir/config.yml"

if ((dry_run)); then
  [[ -n "$auto_sandbox_root" ]] && info "DRY-RUN sandbox: $auto_sandbox_root"
  info "DRY-RUN: NEEDRESTART_SUSPEND=1 $apt_get_bin install -y cloudflared"
else
  run_as_root env DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 "$apt_get_bin" install -y cloudflared
fi

cloudflared_tmp=$(mktemp "${TMPDIR:-/tmp}/bench-enroll-cloudflared.XXXXXX")
render_cloudflared_config "$config_src" "$cloudflared_tmp" "$credentials_dst"
if ((dry_run)); then
  mkdir -p -- "$cloudflared_dir"
  install -m 600 -- "$credentials_src" "$credentials_dst"
  install -m 600 -- "$cloudflared_tmp" "$config_dst"
else
  run_as_root mkdir -p -- "$cloudflared_dir"
  run_as_root install -m 600 -- "$credentials_src" "$credentials_dst"
  run_as_root install -m 600 -- "$cloudflared_tmp" "$config_dst"
fi
rm -f -- "$cloudflared_tmp"
cloudflared_tmp=

if ((dry_run)); then
  info "DRY-RUN: $cloudflared_bin --config $config_dst service install"
else
  cp1_since_marker=$(current_utc_timestamp)
  cp1_cursor=$(capture_cloudflared_cursor)
  run_as_root "$cloudflared_bin" --config "$config_dst" service install
fi

sshd_main_tmp=$(mktemp "${TMPDIR:-/tmp}/bench-enroll-sshd-main.XXXXXX")
sshd_dropin_tmp=$(mktemp "${TMPDIR:-/tmp}/bench-enroll-sshd-dropin.XXXXXX")
render_sshd_main_with_include "$sshd_config_path" "$sshd_main_tmp" "$sshd_dropin_path"
render_sshd_key_only_dropin "$sshd_dropin_tmp"
if ((dry_run)); then
  mkdir -p -- "$(dirname -- "$sshd_config_path")"
  mkdir -p -- "$(dirname -- "$sshd_dropin_path")"
  install -m 600 -- "$sshd_main_tmp" "$sshd_config_path"
  install -m 600 -- "$sshd_dropin_tmp" "$sshd_dropin_path"
else
  run_as_root mkdir -p -- "$(dirname -- "$sshd_config_path")"
  run_as_root mkdir -p -- "$(dirname -- "$sshd_dropin_path")"
  run_as_root install -m 600 -- "$sshd_main_tmp" "$sshd_config_path"
  run_as_root install -m 600 -- "$sshd_dropin_tmp" "$sshd_dropin_path"
fi
rm -f -- "$sshd_main_tmp" "$sshd_dropin_tmp"
sshd_main_tmp=
sshd_dropin_tmp=
if ((dry_run)); then
  info "DRY-RUN: $systemctl_bin reload ssh || $systemctl_bin reload sshd"
else
  if run_as_root "$systemctl_bin" reload ssh; then
    :
  else
    run_as_root "$systemctl_bin" reload sshd
  fi
fi

append_unique_line "$authorized_keys_path" "$(cat -- "$host_key_src")"
chmod 600 -- "$authorized_keys_path"

if ((dry_run)); then
  cp1_log=${BENCH_ENROLL_CP1_LOG:-Registered tunnel connection}
  cp1_ok=0
  if grep -Fq "Registered tunnel connection" <<<"$cp1_log"; then
    cp1_ok=1
  fi
else
  cp1_ok=0
  if wait_for_cloudflared_registration "${cp1_cursor:-}" "${cp1_since_marker:-}" "$cp1_timeout_s" "$cp1_poll_interval_s"; then
    cp1_ok=1
  fi
fi
if ((cp1_ok)); then
  print_checkpoint_pass "CP-1" "journal 已看到 Registered tunnel connection"
else
  print_checkpoint_fail "CP-1" "本次 enroll 後仍未看到新的 Registered tunnel connection"
  exit 1
fi

if ((dry_run)); then
  cp2_result=${BENCH_ENROLL_CP2_RESULT:-pass}
  case "$cp2_result" in
    pass)
      print_checkpoint_pass "CP-2" "dry-run 模擬本地 self-ssh 成功"
      ;;
    fail)
      print_checkpoint_fail "CP-2" "dry-run 模擬本地 self-ssh 失敗"
      exit 1
      ;;
    *)
      die "BENCH_ENROLL_CP2_RESULT 只接受 pass 或 fail"
      ;;
  esac
else
  cp2_tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/bench-enroll-cp2.XXXXXX")
  "$ssh_keygen_bin" -q -t ed25519 -N "" -f "$cp2_tmpdir/selfcheck" >/dev/null
  cp2_temp_key_line=$(cat -- "$cp2_tmpdir/selfcheck.pub")
  append_unique_line "$authorized_keys_path" "$cp2_temp_key_line"
  if SSH_AUTH_SOCK='' "$ssh_bin" \
      -F /dev/null \
      -i "$cp2_tmpdir/selfcheck" \
      -o IdentitiesOnly=yes \
      -o PreferredAuthentications=publickey \
      -o PubkeyAuthentication=yes \
      -o PasswordAuthentication=no \
      -o KbdInteractiveAuthentication=no \
      -o BatchMode=yes \
      -o StrictHostKeyChecking=accept-new \
      -o UserKnownHostsFile="$cp2_tmpdir/known_hosts" \
      "$login_user@localhost" true >/dev/null 2>&1; then
    print_checkpoint_pass "CP-2" "本地 self-ssh 成功"
  else
    print_checkpoint_fail "CP-2" "本地 self-ssh 失敗"
    exit 1
  fi
fi

info "已完成 enroll：bundle=$bundle_dir"
info "login_user=$login_user"
info "cloudflared_dir=$cloudflared_dir"
info "sshd_config=$sshd_config_path"
info "sshd_dropin=$sshd_dropin_path"
info "authorized_keys=$authorized_keys_path"
