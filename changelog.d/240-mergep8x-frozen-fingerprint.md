---
type: fix
scope: rust-daemon
---
S2-PAR mergep8x：P8 線併入整合線後，admission 凍結的早退回應（`FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`）與一般回應共用 Python `_versioned_response` 的補欄位邏輯，啟動指紋可用時在 `version` 之後補 `code_fingerprint`（`origin/main:sw_core/service.py:870-874`、`:894-901`）。
