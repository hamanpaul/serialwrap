---
type: fix
scope: bridge-tool
---

Windows COM 寫入在 setter 或重複 DSR 取樣耗時時，會將實際量到的成本預留於呼叫期限內，再以最後一次 peer 樣本和剩餘期限決定是否寫入。新增延遲 DSR 路徑回歸，確認穩定 peer 可在期限內完成寫入，且不會設定超過剩餘預算的 pySerial timeout。
