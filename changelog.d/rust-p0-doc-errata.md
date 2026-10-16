---
type: docs
scope: docs
---
修正 Rust 重構 P0 階段盤點出的文件與註解勘誤，行為不變：`SKILL.md` 的 `file push` 預設 chunk 改為 512、`file pull` 省略 `--local` 時寫入 daemon 工作目錄並附 `local_path`、event counter 與 event log 改述為 `STATE_DIR/events`（非 tmpfs）；`README.md` 的事件規則僅讀 `*.json`、stdin payload 欄位更正為 `selector`／`matched_at`；`docs/design-event-trigger.md` 改述飽和時丟棄新 fire、`match_skipped` 的 4 種 reason、`matched_line` 與 `bridge_generation` 的實際值；`CLAUDE.md` 補上 CLI `sort_keys=True`、動態 session 不套用 `defaults.log_dir`、passthrough 的 probe 由 `ready_probe` 決定，並補充隔離實例需同時設 `SERIALWRAP_BY_ID_DIR` 與 `SERIALWRAP_BY_PATH_DIR`；`docs/serialwrap-spec.md` 更正 Windows 走 TCP loopback。同時新增 `contracts/p0_row_coverage.yaml` 與規格勘誤節，並以 `tests/test_contracts_doc_errata.py` 防止勘誤回退與 P0 列漏登。
