#!/usr/bin/env bash
# 以 WSL interop 在 Windows 本機磁碟執行 Windows GNU 測試程式。
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"

usage() {
  echo "用法：scripts/rust/win-runner.sh <exe> [args…]" >&2
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi
if ! command -v cmd.exe >/dev/null 2>&1 || ! command -v wslpath >/dev/null 2>&1; then
  echo "win-runner.sh: 需要可執行的 WSL interop（cmd.exe、wslpath）" >&2
  exit 2
fi
if ! cmd.exe /c exit 0 >/dev/null 2>&1; then
  echo "win-runner.sh: cmd.exe 存在但 WSL interop 無法啟動它；證據不足" >&2
  exit 2
fi

exe_source="$1"
shift
list_only=0
for arg in "$@"; do
  if [[ "$arg" == "--list" ]]; then
    list_only=1
    break
  fi
done
if [[ ! -f "$exe_source" ]]; then
  echo "win-runner.sh: Windows executable 不存在：$exe_source" >&2
  exit 2
fi

local_appdata="$(cmd.exe /c echo %LOCALAPPDATA% 2>/dev/null | tr -d '\r' | tail -n 1)"
if [[ -z "$local_appdata" || "$local_appdata" == '%LOCALAPPDATA%' ]]; then
  echo "win-runner.sh: 無法讀取 Windows LOCALAPPDATA" >&2
  exit 2
fi
local_appdata_unix="$(wslpath -u "$local_appdata" 2>/dev/null)" || {
  echo "win-runner.sh: 無法轉換 LOCALAPPDATA 路徑：$local_appdata" >&2
  exit 2
}

run_id="$(date +%y%m%d-%H%M%S)-$$"
staging="$local_appdata_unix/Temp/sw-rust-test/$run_id"
mkdir -p "$staging/tmp" "$staging/data"
if [[ "$list_only" == "0" ]]; then
  # drvfs 以 `case=dir` 掛載時，WSL 建立的目錄會被設為「逐目錄區分大小寫」，其下由 Windows
  # 程式新建的子目錄也會繼承；測試在 TMP 下建立的檔案因而失去 Windows 原生的不分大小寫語意
  # （例如 PATHEXT 的 `.EXE` 查不到 `.exe`）。staging/tmp 仍為空時即關閉該屬性，失敗則中止
  # （不得在非原生語意下產生 Windows 證據）。
  if ! fsutil.exe file setCaseSensitiveInfo "$(wslpath -w "$staging/tmp")" disable >/dev/null 2>&1; then
    echo "win-runner.sh: 無法關閉 staging TMP 的逐目錄大小寫區分（fsutil setCaseSensitiveInfo）；證據不足" >&2
    rm -rf -- "$staging"
    exit 2
  fi
fi
keep_staging="${SW_WIN_KEEP:-0}"
print_staging="${SW_WIN_PRINT_STAGING:-0}"
cleanup() {
  local rc=$?
  trap - EXIT
  if [[ "$keep_staging" != "1" ]]; then
    rm -rf -- "$staging"
  fi
  if [[ "$print_staging" == "1" ]]; then
    printf '%s\n' "$staging" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT
exe_name="$(basename "$exe_source")"
cp -- "$exe_source" "$staging/$exe_name"
chmod +x "$staging/$exe_name"

copy_data() {
  local rel="$1"
  local source="$ROOT/$rel"
  if [[ ! -e "$source" ]]; then
    echo "win-runner.sh: 缺少 Windows 測試資料：$rel" >&2
    rm -rf -- "$staging"
    exit 2
  fi
  mkdir -p "$staging/data/$(dirname "$rel")"
  cp -R -- "$source" "$staging/data/$rel"
}

if [[ "$list_only" == "0" ]]; then
  for rel in \
    contracts/golden \
    contracts/cli_help \
    contracts/error_codes.yaml \
    contracts/session_last_errors.yaml \
    contracts/cli_schema.json \
    contracts/rpc_schema.json \
    contracts/release_manifest.schema.json \
    tests/contract_blackbox/data \
    crates/sw-cli/tests/golden \
    crates/sw-cli/src \
    sw_core/constants.py \
    sw_core/assets/skill \
    sw_core/assets/profiles \
    sw_core/assets/tools \
    VERSION; do
    copy_data "$rel"
  done
fi

if [[ "$list_only" == "0" && -n "${SW_WIN_EXTRA_FILES:-}" ]]; then
  IFS=: read -r -a extra_files <<< "$SW_WIN_EXTRA_FILES"
  for source in "${extra_files[@]}"; do
    [[ -n "$source" ]] || continue
    if [[ ! -e "$source" ]]; then
      echo "win-runner.sh: SW_WIN_EXTRA_FILES 項目不存在：$source" >&2
      rm -rf -- "$staging"
      exit 2
    fi
    cp -R -- "$source" "$staging/"
  done
fi

if [[ -f "$staging/serialwrapd.exe" ]]; then
  SW_TEST_SERIALWRAPD="$(wslpath -w "$staging/serialwrapd.exe")"
  export SW_TEST_SERIALWRAPD
fi

export SW_TEST_DATA_DIR="$staging/data"
export TMP="$staging/tmp"
export TEMP="$staging/tmp"

# WSLENV 不支援萬用字元；逐一轉送目前 shell 中的 SERIALWRAP_* 變數。
# 一般變數不加方向旗標（雙向轉送）：擁有者 host 的 WSL 2.6.1 實測 `NAME/u` 不會轉送到
# WSL→Win32 啟動的行程（`NAME`／`NAME/w` 會），與文件語意相反；不帶旗標在任何版本都會轉送。
wsl_env="${WSLENV:-}"
append_wsl_env() {
  local entry="$1" name="${1%%/*}" existing
  IFS=: read -r -a wsl_entries <<< "$wsl_env"
  for existing in "${wsl_entries[@]}"; do
    [[ "${existing%%/*}" == "$name" ]] && return 0
  done
  if [[ -n "$wsl_env" ]]; then
    wsl_env+="::$entry"
  else
    wsl_env="$entry"
  fi
  wsl_env="${wsl_env//::/:}"
}

for name in SW_TEST_CAPS SW_TEST_COM_PAIR SW_TEST_SEED SW_TEST_SERIALWRAPD RUST_BACKTRACE RUST_TEST_THREADS; do
  [[ -v "$name" ]] && append_wsl_env "${name}"
done
while IFS= read -r name; do
  [[ "$name" == SERIALWRAP_* ]] && append_wsl_env "${name}"
done < <(compgen -e)
append_wsl_env "SW_TEST_DATA_DIR/p"
append_wsl_env "TMP/p"
append_wsl_env "TEMP/p"
export WSLENV="$wsl_env"

cd "$staging"
set +e
timeout "${SW_WIN_TIMEOUT:-900}" "./$exe_name" "$@"
exit_code=$?
set -e
exit "$exit_code"
