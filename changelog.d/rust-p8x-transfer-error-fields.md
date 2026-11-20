---
type: fix
scope: rust-core
---
P8X-t4fix：修正 `file.push` 的 prompt 逾時後清理 TX 遭拒時，不再回傳 Python 基線未提供的 chunk 進度欄位；正常 chunk 逾時仍保留進度。
