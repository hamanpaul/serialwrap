---
type: fix
scope: rust-refactor
---

- `device.release` 撤銷尚未由正式 bridge 接手的預開 UART handle 後，撤銷標記不再隨 release 逾時回 `DRAIN_STALLED` 而移除，改由 session 確定被取消的開埠不會再取用時才清除：遲到的開埠 worker 不會在 release 之後另開新 handle，遲到的關閉完成後 session 直接轉 RELEASED 並落盤，不需再次 release。
- release 進行中（session 已是 RECOVERING）的開埠不論成功或失敗都視為已結束，遲到的 release 收尾不再等待不會回報的開埠而停在 RECOVERING。
