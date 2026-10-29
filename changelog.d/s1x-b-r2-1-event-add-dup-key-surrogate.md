---
type: fix
scope: rust-cli
---

修正 Rust CLI `event add --file`：規則 JSON 以重複鍵覆寫掉含孤立 surrogate 的值（如 `{"lone":"\ud800","lone":"ok"}`，或被覆寫的巢狀物件）時，原本仍以 `INTERNAL_ERROR` 拒送；現比照 Python `json.load`（後值覆寫前值）只依最終物件判定，最終值不含孤立 surrogate 即正常送出（S1-2a65438-1）。最終值仍含孤立 surrogate 時的拒送行為不變。
