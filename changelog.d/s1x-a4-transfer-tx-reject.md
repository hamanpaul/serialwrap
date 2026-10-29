---
type: fix
scope: file-transfer
---

修正 `file.push`／`file.pull` 傳輸中目標重開機（boot quiet）或 bridge drain 時 `TxIncompleteError` 穿越 RPC 邊界、回 `EXCEPTION` 的回歸：quiet 拒絕改回 `AUTOBOOT_QUIET`＋`retry_after_s`（SC-03），其餘 TX 拒絕回傳輸自身的 `TRANSFER_TIMEOUT`（0.3.2 結束碼），push 附 `chunks_sent`／`chunks_total`；被拒後 best-effort 清半行與遠端暫存檔，再被拒即停，不送 bytes 進 autoboot 倒數（S1X-a-4）。
