---
type: fix
scope: rust-refactor
---

Stage 1A gate 依 OD-30 (2) 把 Windows `l15-transport-disconnect` 因 com0com 無斷線能力（`HARNESS_CAPABILITY_UNAVAILABLE`）未執行的列判為 BLOCKED-OWNER（不計 PASS），Linux 列仍須 PASS，其他失敗照判 FAIL；延後只適用登記的 verdict 與完整符合的訊息型態，列帶 harness 關閉失敗診斷時不延後。
