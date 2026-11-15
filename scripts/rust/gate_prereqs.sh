#!/usr/bin/env bash
# Slice 2 gate 共用前置：候選版 no-fakes CLI、Stage 2 handoff bundles 與主機安全預檢。

sw_gate_prereqs_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"

sw_gate_prereqs_fail() {
  printf 'gate_prereqs.sh: %s\n' "$*" >&2
  return 1
}

sw_gate_prereqs_has_wsl() {
  [[ -n "${WSL_DISTRO_NAME:-}" || -e /proc/sys/fs/binfmt_misc/WSLInterop ]] && return 0
  command -v wslpath >/dev/null 2>&1 && command -v cmd.exe >/dev/null 2>&1 \
    && [[ -e /mnt/c/Windows/System32/cmd.exe ]]
}

sw_gate_prereqs_prepare_windows_path() {
  local dir
  for dir in /mnt/c/Windows/System32 /mnt/c/Windows /mnt/c/Windows/System32/WindowsPowerShell/v1.0; do
    [[ -d "$dir" ]] || continue
    case ":$PATH:" in
      *":$dir:"*) ;;
      *) PATH="$dir:$PATH" ;;
    esac
  done
  export PATH
  command -v cmd.exe >/dev/null 2>&1 || { sw_gate_prereqs_fail "PATH 缺少 Windows System32（cmd.exe）"; return 1; }
  command -v wslpath >/dev/null 2>&1 || { sw_gate_prereqs_fail "PATH 缺少 wslpath"; return 1; }
  cmd.exe /c exit 0 >/dev/null 2>&1 || { sw_gate_prereqs_fail "WSL interop 無法啟動 cmd.exe"; return 1; }
}

sw_gate_prereqs_check_d_drive() {
  local available
  [[ -d /mnt/d ]] || { sw_gate_prereqs_fail "D 槽掛載點 /mnt/d 不存在"; return 1; }
  available="$(df -BG --output=avail /mnt/d 2>/dev/null | tail -n 1 | tr -cd '0-9')"
  [[ "$available" =~ ^[0-9]+$ ]] || { sw_gate_prereqs_fail "無法讀取 /mnt/d 可用空間"; return 1; }
  (( available >= 100 )) || { sw_gate_prereqs_fail "D 槽可用空間 ${available}G，小於 100G"; return 1; }
  printf 'D 槽可用空間：%sG\n' "$available"
}

sw_gate_prereqs_check_loadavg() {
  local load cores
  read -r load _ </proc/loadavg || { sw_gate_prereqs_fail "無法讀取 /proc/loadavg"; return 1; }
  cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null)"
  [[ "$cores" =~ ^[1-9][0-9]*$ ]] || { sw_gate_prereqs_fail "無法取得線上 CPU 數"; return 1; }
  awk -v avg="$load" -v cores="$cores" 'BEGIN { exit !(avg < cores / 2) }' \
    || { sw_gate_prereqs_fail "loadavg ${load} 不低於核心數一半（${cores}/2）"; return 1; }
  printf 'loadavg：%s；核心數：%s\n' "$load" "$cores"
}

sw_gate_prereqs_disposable_daemons() {
  local link pid exe base
  local found=()
  for link in /proc/[0-9]*/exe; do
    [[ -L "$link" ]] || continue
    exe="$(readlink "$link" 2>/dev/null || true)"
    exe="${exe% (deleted)}"
    base="${exe##*/}"
    [[ "$base" == "serialwrapd" || "$base" == "serialwrapd.exe" ]] || continue
    case "$exe" in
      "$sw_gate_prereqs_root"/target/*)
        pid="${link#/proc/}"
        pid="${pid%/exe}"
        found+=("$pid:$exe")
        ;;
    esac
  done
  if ((${#found[@]})); then
    printf '殘留的 worktree 拋棄式 serialwrapd：\n' >&2
    printf '  %s\n' "${found[@]}" >&2
    sw_gate_prereqs_fail "先依 /proc/<pid>/exe 核對並清理殘留測試 daemon"
    return 1
  fi
  printf '拋棄式 daemon：無殘留\n'
}

sw_gate_prereqs_host_preflight() {
  sw_gate_prereqs_has_wsl || { sw_gate_prereqs_fail "需要 WSL interop 環境"; return 1; }
  sw_gate_prereqs_prepare_windows_path || return 1
  sw_gate_prereqs_check_d_drive || return 1
  sw_gate_prereqs_check_loadavg || return 1
  sw_gate_prereqs_disposable_daemons || return 1
}

sw_gate_prereqs_check_env() {
  local path="${SW_TEST_SERIALWRAP_NO_FAKES:-}"
  if [[ -z "$path" || ! -f "$path" ]]; then
    printf 'EVIDENCE_MISSING: SW_TEST_SERIALWRAP_NO_FAKES 未設定或檔案不存在\n'
    return 3
  fi
  printf 'PASS: SW_TEST_SERIALWRAP_NO_FAKES=%s\n' "$path"
}

sw_gate_prereqs_build() {
  local root="$sw_gate_prereqs_root"
  local CARGO_TARGET_DIR="$root/target"
  local prereqs="$root/target/s2-par/gpz/prereqs"
  local no_fakes_target="$prereqs/no-fakes-target"
  local base_bundle="$prereqs/base-bundle"
  local feature_bundle="$prereqs/bundle"
  local linux_target="x86_64-unknown-linux-gnu"

  export CARGO_TARGET_DIR
  mkdir -p "$prereqs"
  CARGO_TARGET_DIR="$no_fakes_target" cargo build -p sw-cli --no-default-features --bin serialwrap --target "$linux_target" \
    || { sw_gate_prereqs_fail "no-fakes CLI 建置失敗"; return 1; }
  "$root/scripts/rust/mkbundle.sh" --dev --stage 2 --profile debug --version-override 0.0.1 --out "$base_bundle" \
    || { sw_gate_prereqs_fail "handoff base-bundle 建置失敗"; return 1; }
  "$root/scripts/rust/mkbundle.sh" --dev --stage 2 --profile debug --out "$feature_bundle" \
    || { sw_gate_prereqs_fail "候選版 Stage 2 bundle 建置失敗"; return 1; }

  export SW_TEST_SERIALWRAP_NO_FAKES="$no_fakes_target/$linux_target/debug/serialwrap"
  export SW_TEST_FEATURE_BUNDLE="$feature_bundle"
  export SW_TEST_BASE_BUNDLE="$base_bundle"
  export SW_TEST_SERIALWRAP="$feature_bundle/bin/serialwrap"
  [[ -f "$SW_TEST_SERIALWRAP_NO_FAKES" ]] || { sw_gate_prereqs_fail "no-fakes CLI 建置輸出不存在"; return 1; }
  [[ -f "$SW_TEST_SERIALWRAP" ]] || { sw_gate_prereqs_fail "候選 bundle CLI 不存在"; return 1; }
  printf 'SW_TEST_SERIALWRAP_NO_FAKES=%s\n' "$SW_TEST_SERIALWRAP_NO_FAKES"
  printf 'SW_TEST_SERIALWRAP=%s\n' "$SW_TEST_SERIALWRAP"
  printf 'SW_TEST_BASE_BUNDLE=%s\n' "$SW_TEST_BASE_BUNDLE"
}

sw_gate_prereqs_main() {
  local mode="gate"
  while (($#)); do
    case "$1" in
      --gate) mode="gate"; shift ;;
      --ci) mode="ci"; shift ;;
      --dry-run) mode="dry-run"; shift ;;
      --check-env) mode="check-env"; shift ;;
      -h|--help)
        printf '%s\n' '用法：source scripts/rust/gate_prereqs.sh [--gate|--ci|--dry-run|--check-env]'
        return 0
        ;;
      *) sw_gate_prereqs_fail "未知參數：$1"; return 2 ;;
    esac
  done

  case "$mode" in
    dry-run)
      printf 'dry-run：略過 host 預檢與建置；正式執行會準備 no-fakes CLI、base-bundle 與候選 bundle。\n'
      ;;
    check-env)
      sw_gate_prereqs_check_env
      ;;
    gate)
      sw_gate_prereqs_host_preflight || return 3
      sw_gate_prereqs_build || return 2
      ;;
    ci)
      sw_gate_prereqs_build || return 2
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  sw_gate_prereqs_main "$@"
  exit $?
else
  sw_gate_prereqs_main "$@"
fi
