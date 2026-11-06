#!/usr/bin/env bash
# serialwrap 安裝程式（OD-50）：安裝 Rust bundle 並執行 `<bundle>/bin/serialwrap setup`。
# 預設從 GitHub release 下載 bundle tarball 與 SHA256SUMS 並驗證；`--bundle` 供離線／開發安裝。
# 安裝前檢查系統需求（x86_64、glibc ≥ 2.39、/usr/bin/python3.12），不符時 rc=2 且不改動任何東西。
set -euo pipefail

DEFAULT_RELEASE_BASE_URL="https://github.com/hamanpaul/serialwrap/releases"
MIN_GLIBC="2.39"
PYTHON312_DEFAULT="/usr/bin/python3.12"

usage() {
  cat <<'EOF'
用法（Usage）：
  install.sh [--version X.Y.Z] [--] [setup 參數...]
  install.sh --bundle <目錄或 .tar.gz> [--] [setup 參數...]
  install.sh -h | --help

安裝 serialwrap Rust bundle，然後執行 `<bundle>/bin/serialwrap setup [setup 參數...]`，
回傳 setup 的 exit code。

選項（Options）：
  --version X.Y.Z   安裝指定 release 版本；未指定時取最新 release（解析 releases/latest 的 redirect）。
  --bundle <路徑>   離線／開發安裝：<路徑> 為已解開的 bundle 目錄或 bundle .tar.gz，不下載。
                    開發者可先建 bundle：
                      scripts/rust/mkbundle.sh --release-bundle --profile release --out <dir>
  --                之後的參數原樣轉交給 `serialwrap setup`（未知參數亦會轉交，例如 --user、--on-demand）。
  -h, --help        顯示本說明。

下載（Download）：
  從 https://github.com/hamanpaul/serialwrap/releases 下載
  serialwrap-<版本>-linux-x86_64.tar.gz 與 SHA256SUMS（curl；沒有 curl 時用 wget），
  以 SHA256SUMS 中該 tarball 的列驗證，不符立即失敗、不執行 setup。
  基底 URL 可由環境變數 SERIALWRAP_RELEASE_BASE_URL 覆寫。

系統需求（Requirements，安裝前檢查，不符時 exit 2 且不改動任何東西）：
  - 架構 x86_64
  - glibc >= 2.39（Ubuntu 24.04 以上）
  - /usr/bin/python3.12（bundle 內附的 Python daemon runtime 綁定此直譯器）

舊版 pipx 安裝（0.3.x）：直接執行本程式即可升級，入口改由 bundle 接管；
完成後若偵測到舊 pipx 安裝，會提示可執行 `pipx uninstall serialwrap`（不會自動移除）。
EOF
}

log() { printf '[serialwrap] %s\n' "$*"; }
err() { printf '[serialwrap] %s\n' "$*" >&2; }
die() { local rc="$1"; shift; err "$*"; exit "$rc"; }

VERSION=""
BUNDLE=""
SETUP_ARGS=()
while (($#)); do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --version)
      (($# >= 2)) || die 2 "--version 缺少版本參數（missing version）"
      VERSION="$2"; shift 2 ;;
    --version=*) VERSION="${1#--version=}"; shift ;;
    --bundle)
      (($# >= 2)) || die 2 "--bundle 缺少路徑參數（missing bundle path）"
      BUNDLE="$2"; shift 2 ;;
    --bundle=*) BUNDLE="${1#--bundle=}"; shift ;;
    --) shift; SETUP_ARGS+=("$@"); break ;;
    *) SETUP_ARGS+=("$1"); shift ;;
  esac
done

if [[ -n "$VERSION" && -n "$BUNDLE" ]]; then
  die 2 "--version 與 --bundle 不可並用（mutually exclusive）"
fi
if [[ -n "$VERSION" && ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  die 2 "--version 必須為 X.Y.Z（invalid version）：$VERSION"
fi

# ---------- 系統需求 ----------
# 探測值可由 SERIALWRAP_INSTALL_ARCH／SERIALWRAP_INSTALL_GLIBC／SERIALWRAP_INSTALL_PYTHON312 覆寫（測試用）；預設一律實際探測。
detect_glibc() {
  local line=""
  line="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
  if [[ "$line" =~ ([0-9]+\.[0-9]+) ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
    return
  fi
  line="$(ldd --version 2>/dev/null | head -n 1 || true)"
  if [[ "$line" =~ ([0-9]+\.[0-9]+)[[:space:]]*$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  fi
}

# version_ge A B：A >= B（MAJOR.MINOR 數值比較）
version_ge() {
  local a_major="${1%%.*}" a_minor="${1#*.}" b_major="${2%%.*}" b_minor="${2#*.}"
  a_minor="${a_minor%%.*}"
  b_minor="${b_minor%%.*}"
  ((10#$a_major > 10#$b_major || (10#$a_major == 10#$b_major && 10#$a_minor >= 10#$b_minor)))
}

check_requirements() {
  local failures=() arch glibc python312
  arch="${SERIALWRAP_INSTALL_ARCH:-$(uname -m)}"
  if [[ "$arch" != x86_64 ]]; then
    failures+=("架構不符（arch）：偵測到 ${arch:-unknown}，需要 x86_64")
  fi
  glibc="${SERIALWRAP_INSTALL_GLIBC:-$(detect_glibc)}"
  if [[ ! "$glibc" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
    failures+=("無法判定 glibc 版本（glibc version unknown）：偵測到 '${glibc}'，需要 glibc >= ${MIN_GLIBC}（Ubuntu 24.04 以上）")
  elif ! version_ge "$glibc" "$MIN_GLIBC"; then
    failures+=("glibc 版本過舊（glibc too old）：偵測到 ${glibc}，需要 >= ${MIN_GLIBC}（Ubuntu 24.04 以上）")
  fi
  python312="${SERIALWRAP_INSTALL_PYTHON312:-$PYTHON312_DEFAULT}"
  if [[ ! -x "$python312" ]]; then
    failures+=("缺少 Python 3.12（python3.12 not found）：${python312} 不存在或不可執行；bundle 內附的 daemon runtime 需要它（Ubuntu：sudo apt install python3.12）")
  fi
  if ((${#failures[@]})); then
    err "系統需求不符，未安裝、未改動任何東西（requirement not met）："
    local item
    for item in "${failures[@]}"; do
      err "  - $item"
    done
    exit 2
  fi
}

check_requirements

# ---------- 下載工具 ----------
DOWNLOADER=""
select_downloader() {
  local want="${SERIALWRAP_INSTALL_DOWNLOADER:-auto}"
  case "$want" in
    auto)
      if command -v curl >/dev/null 2>&1; then DOWNLOADER=curl
      elif command -v wget >/dev/null 2>&1; then DOWNLOADER=wget
      fi ;;
    curl|wget) command -v "$want" >/dev/null 2>&1 && DOWNLOADER="$want" ;;
    *) die 2 "SERIALWRAP_INSTALL_DOWNLOADER 僅支援 auto、curl、wget：$want" ;;
  esac
  [[ -n "$DOWNLOADER" ]] || die 2 "需要 curl 或 wget 才能下載 release（requirement not met: curl/wget）；或改用 --bundle 離線安裝"
}

# fetch URL DEST：下載到檔案，HTTP 錯誤即失敗
fetch() {
  if [[ "$DOWNLOADER" == curl ]]; then
    curl -fsSL --retry 2 -o "$2" "$1"
  else
    wget -q -O "$2" "$1"
  fi
}

# resolve_latest BASE：以 <BASE>/latest 的 redirect（.../tag/vX.Y.Z）解析最新版本
resolve_latest() {
  local effective="" tag=""
  if [[ "$DOWNLOADER" == curl ]]; then
    effective="$(curl -fsSL -o /dev/null -w '%{url_effective}' "$1/latest")" || return 1
  else
    effective="$(wget -O /dev/null --server-response "$1/latest" 2>&1 \
      | awk 'tolower($1) == "location:" { loc = $2 } END { print loc }' | tr -d '\r')"
  fi
  tag="${effective##*/tag/}"
  [[ "$effective" == */tag/* && "$tag" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  printf '%s\n' "${tag#v}"
}

# ---------- 暫存目錄 ----------
WORK=""
cleanup() {
  if [[ -n "$WORK" && -d "$WORK" ]]; then
    rm -rf -- "$WORK"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

make_work() {
  [[ -n "$WORK" ]] || WORK="$(mktemp -d "${TMPDIR:-/tmp}/serialwrap-install.XXXXXX")"
}

# locate_bundle DIR：DIR 本身或其唯一子目錄含 bin/serialwrap 者
locate_bundle() {
  local dir="$1" candidates=()
  if [[ -x "$dir/bin/serialwrap" ]]; then
    printf '%s\n' "$dir"
    return 0
  fi
  local sub
  for sub in "$dir"/*/; do
    [[ -x "${sub}bin/serialwrap" ]] && candidates+=("${sub%/}")
  done
  if ((${#candidates[@]} == 1)); then
    printf '%s\n' "${candidates[0]}"
    return 0
  fi
  return 1
}

# extract_tarball TARBALL：解到暫存目錄並設定 BUNDLE_ROOT
extract_tarball() {
  make_work
  local dest="$WORK/bundle"
  mkdir -p "$dest"
  tar -xzf "$1" -C "$dest" --no-same-owner || die 1 "解開 bundle 失敗（extract failed）：$1"
  if ! BUNDLE_ROOT="$(locate_bundle "$dest")"; then
    if compgen -G "$dest/bin/serialwrap" >/dev/null || compgen -G "$dest/*/bin/serialwrap" >/dev/null; then
      die 2 "暫存目錄無法執行 bin/serialwrap（noexec?）：${WORK}；請以 TMPDIR=<可執行的目錄> 重新執行"
    fi
    die 2 "bundle 內找不到 bin/serialwrap（invalid bundle）：$1"
  fi
}

verify_sha256() {
  local file="$1" sums="$2" name="$3" expected="" actual=""
  expected="$(awk -v n="$name" '{ f = $2; sub(/^\*/, "", f) } f == n && NF == 2 { print tolower($1); exit }' "$sums")"
  if [[ ! "$expected" =~ ^[0-9a-f]{64}$ ]]; then
    die 1 "SHA256SUMS 沒有 ${name} 的列（checksum entry missing），停止安裝、未執行 setup"
  fi
  actual="$(sha256sum -- "$file" | awk '{ print $1 }')"
  if [[ "$actual" != "$expected" ]]; then
    die 1 "SHA256 驗證失敗（checksum mismatch）：${name} 預期 ${expected}，實際 ${actual}；停止安裝、未執行 setup"
  fi
  log "SHA256 驗證通過（checksum OK）：${name}"
}

download_bundle() {
  select_downloader
  local base="${SERIALWRAP_RELEASE_BASE_URL:-$DEFAULT_RELEASE_BASE_URL}"
  base="${base%/}"
  if [[ -z "$VERSION" ]]; then
    VERSION="$(resolve_latest "$base")" \
      || die 1 "無法解析最新 release 版本（cannot resolve latest release）：${base}/latest；可改用 --version X.Y.Z"
    log "最新 release（latest）：${VERSION}"
  fi
  local name="serialwrap-${VERSION}-linux-x86_64.tar.gz"
  local url="${base}/download/v${VERSION}"
  make_work
  log "下載（download）：${url}/${name}"
  fetch "${url}/${name}" "$WORK/$name" \
    || die 1 "下載 bundle 失敗（download failed）：${url}/${name}（Linux bundle 自 0.4.0 起提供）"
  fetch "${url}/SHA256SUMS" "$WORK/SHA256SUMS" \
    || die 1 "下載 SHA256SUMS 失敗（download failed）：${url}/SHA256SUMS；未驗證不安裝"
  verify_sha256 "$WORK/$name" "$WORK/SHA256SUMS" "$name"
  extract_tarball "$WORK/$name"
}

# ---------- 取得 bundle ----------
BUNDLE_ROOT=""
if [[ -n "$BUNDLE" ]]; then
  if [[ -d "$BUNDLE" ]]; then
    BUNDLE_ROOT="$(locate_bundle "$BUNDLE")" || die 2 "bundle 目錄內找不到可執行的 bin/serialwrap（invalid bundle）：$BUNDLE"
  elif [[ -f "$BUNDLE" ]]; then
    extract_tarball "$BUNDLE"
  else
    die 2 "--bundle 路徑不存在（not found）：$BUNDLE"
  fi
else
  download_bundle
fi

# ---------- setup ----------
detect_legacy_pipx() {
  if [[ -d "${PIPX_HOME:-$HOME/.local/pipx}/venvs/serialwrap" || -d "$HOME/.local/share/pipx/venvs/serialwrap" ]]; then
    return 0
  fi
  command -v pipx >/dev/null 2>&1 || return 1
  pipx list --short 2>/dev/null | awk '$1 == "serialwrap" { found = 1 } END { exit !found }'
}

log "執行（run）：${BUNDLE_ROOT}/bin/serialwrap setup ${SETUP_ARGS[*]-}"
rc=0
"$BUNDLE_ROOT/bin/serialwrap" setup ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"} || rc=$?
if ((rc == 0)) && detect_legacy_pipx; then
  log "偵測到舊版 pipx 安裝（legacy pipx install）；入口已由 bundle 接管。"
  log "確認新版運作正常後，可執行 \`pipx uninstall serialwrap\` 移除舊版（本程式不會自動移除）。"
fi
exit "$rc"
