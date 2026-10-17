#!/usr/bin/env bash
# P4-T1：黑箱測試用 bundle 的最小形狀（`--dev`、`--delegate-sentinel`、`--print-dir`）。
#
# 完整的自含式 dev bundle（launcher、Python runtime、release manifest 全欄位）由 P4-T17 承接；本腳本產生的形狀必須
# 與 P3 解析端對齊，供 tests/contract_blackbox/mgmt_fixtures.py 的 `mgmt_bundle` 使用：
#   - `release-manifest.json`（schema_version 1；sw-proto::parse_manifest 的唯一接受形狀）
#   - `components.cli` = {implementation:"rust", version, path, sha256}（黑箱 _artifact_identity 檢查）
#   - `components.python_cli_delegate.entry` 首項 `py/bin/python3` 必存在於 bundle 內且可執行（指向系統 python3）
#
# 用法：
#   scripts/rust/mkbundle.sh --dev --delegate-sentinel [--out DIR] [--print-dir]
#
#   --dev                 每次都以 `cargo build -p sw-cli` 重建 target/debug/serialwrap 後組成 bundle（不沿用舊 binary）
#   --delegate-sentinel   entry = [py/bin/python3, -I, tests/contract_blackbox/delegate_sentinel.py]；
#                         被呼叫即寫 marker 並 exit 97，任何漏網委派都會留下證據
#   --out DIR             輸出目錄（預設 target/mkbundle-dev）
#   --print-dir           只把 bundle 絕對路徑印到 stdout
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEV=0
SENTINEL=0
PRINT_DIR=0
OUT="$REPO_ROOT/target/mkbundle-dev"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) DEV=1; shift ;;
    --delegate-sentinel) SENTINEL=1; shift ;;
    --print-dir) PRINT_DIR=1; shift ;;
    --out) OUT="$2"; shift 2 ;;
    *) echo "mkbundle.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done

if [[ "$DEV" -ne 1 ]]; then
  echo "mkbundle.sh: 目前只支援 --dev（完整 bundle 由 P4-T17 承接）" >&2
  exit 2
fi
if [[ "$SENTINEL" -ne 1 ]]; then
  echo "mkbundle.sh: 目前只支援 --delegate-sentinel（manifest entry 不得為空，P3 解析端會拒絕）" >&2
  exit 2
fi

# 每次都重建：不得把舊 binary 打包進 bundle（審查修正：原本只在缺檔時 build）。
(cd "$REPO_ROOT" && cargo build -p sw-cli --quiet)
BIN_SRC="$REPO_ROOT/target/debug/serialwrap"
if [[ ! -x "$BIN_SRC" ]]; then
  echo "mkbundle.sh: cargo build 後仍找不到 $BIN_SRC" >&2
  exit 1
fi

# 不清空既有目錄（避免對呼叫端給的路徑做遞迴刪除）；bundle 檔案每次覆寫。
mkdir -p "$OUT/bin" "$OUT/py/bin"
cp "$BIN_SRC" "$OUT/bin/serialwrap"
chmod +x "$OUT/bin/serialwrap"

# 委派 entry 首項必須是 bundle 內存在的可執行檔：指向系統 python3（哨兵以它執行）。
PY_SRC="$(command -v python3)"
ln -sf "$(readlink -f "$PY_SRC")" "$OUT/py/bin/python3"

SENTINEL_PATH="$REPO_ROOT/tests/contract_blackbox/delegate_sentinel.py"
CLI_SHA="$(sha256sum "$OUT/bin/serialwrap" | awk '{print $1}')"
BUNDLE_VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")"
REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
TARGET_TRIPLE="$(rustc -vV 2>/dev/null | awk '/^host:/ {print $2}')"
TARGET_TRIPLE="${TARGET_TRIPLE:-x86_64-unknown-linux-gnu}"

python3 - "$OUT/release-manifest.json" "$BUNDLE_VERSION" "$REVISION" "$TARGET_TRIPLE" "$CLI_SHA" "$SENTINEL_PATH" <<'PY'
import json, sys
path, version, revision, target, cli_sha, sentinel = sys.argv[1:7]
manifest = {
    "schema_version": 1,
    "bundle_version": version,
    "stage": "rust-1b-dev",
    "target": target,
    "git_revision": revision,
    "components": {
        "cli": {
            "implementation": "rust",
            "version": version,
            "path": "bin/serialwrap",
            "sha256": cli_sha,
        },
        "python_cli_delegate": {
            "entry": ["py/bin/python3", "-I", sentinel],
        },
    },
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY

if [[ "$PRINT_DIR" -eq 1 ]]; then
  (cd "$OUT" && pwd -P)
else
  echo "$OUT"
fi
