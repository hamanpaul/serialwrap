---
type: fix
scope: rust-refactor
---

- release、bind、shutdown 或裝置改指認領尚未由正式 bridge 接手的預開 UART handle 後，關閉期間 handle 仍登錄為在途作業，確認關閉才移出：重複的 `device.release` 會等同一個作業、沿用第一次的期限，不再於 handle 仍開啟時回 ok；關閉卡住超過期限時兩個 release 都回 `DRAIN_STALLED`，關閉遲到完成後 handle 已關閉、不重新開埠。
- release 認領交接 handle 時照 Python 立即把 session 設為 RECOVERING；開埠 worker 先撞到撤銷標記回報時，之後的 release 不再把已結束的開埠當成在途而等待到逾時。
