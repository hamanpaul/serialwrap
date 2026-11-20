---
type: fix
scope: rust-s21
---

Slice 2.1 gate 的 WAL 寫入效能判定改以數值重算延遲門檻後，補回兩個非門檻條件：寫入失敗（`write_failures > 0`）與任一流未跨越 rotate 仍判 FAIL，缺欄判證據不足（避免全數寫入失敗時延遲數字反而更好而誤判 PASS）。匯入證據的可追溯性路徑另納入 sw-pal 的時鐘、`native()` 組裝點與 WalFs／Clock trait。
