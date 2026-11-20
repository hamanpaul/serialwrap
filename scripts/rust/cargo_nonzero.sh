#!/usr/bin/env bash
# P6 Task 1（07-slice22-console.md）：`cargo test` 包裝，實際執行的測試數（passed＋failed）為 0 時 exit 1。
#
# 用途：06 測試檔以 `#![cfg(feature = "test-fakes")]` 開頭，未帶 feature 會整檔被編掉、0 個測試也算通過；
# 本腳本只是開發迴圈防呆，切片出口仍以 Task 13 `s22_gate_check.py` 對凍結 nodeid 清單逐案判定（R5-F3）。
#
# 用法：scripts/rust/cargo_nonzero.sh <cargo test 參數…>
#   例：scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test telnet_filter
# 退出碼：cargo test 失敗時沿用其退出碼；cargo 成功但執行數為 0 時 exit 1。
set -u -o pipefail

log="$(mktemp "${TMPDIR:-/tmp}/cargo_nonzero.XXXXXX")"
trap 'rm -f "$log"' EXIT

cargo test "$@" 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}

# 加總每個 `test result:` 行的 passed 與 failed。
total=$(awk '/^test result:/ {
    for (i = 1; i <= NF; i++) {
        if ($(i+1) ~ /^passed;?$/ || $(i+1) ~ /^failed;?$/) { n += $i }
    }
} END { print n + 0 }' "$log")

echo "[cargo_nonzero] 執行測試數（passed+failed）＝${total}，cargo rc=${rc}" >&2
if [ "$rc" -ne 0 ]; then
    exit "$rc"
fi
if [ "$total" -eq 0 ]; then
    echo "[cargo_nonzero] FAIL：實際執行 0 個測試（是否漏了 --features test-fakes？）" >&2
    exit 1
fi
exit 0
