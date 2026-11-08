#!/bin/sh
# 假 cargo（測試會複製為 bin/cargo）：只回應 `cargo test --no-run --message-format=json …`，
# 輸出同目錄 cargo_no_run.jsonl 並以 cargo_exit 結束。
dir=$(dirname "$0")
cat "$dir/cargo_no_run.jsonl"
exit "$(cat "$dir/cargo_exit")"
