---
type: feat
scope: rust-s21
---
Rust SessionActor：sans-IO 狀態機、control／safety／data 有界 mailbox、conn_epoch／fsm_gen／owner_gen 世代隔離，Release 與 WorkerStopped 不受依賴 RX backlog 阻塞；RECOVERING／FLASHING 下的提交改回 SESSION_RECOVERING／FLASHING_BUSY（SC-01、SC-02）。passthrough 不登入、ATTACHED 連續 30 s 無 RX 由 tick 轉 READY，未滿時 submit 回 SESSION_NOT_READY＋passthrough_ready_in_s（SC-35）。帳密 gate、console unlock、WARNING／WAL META 去重與 env 檔 export／註解／引號解析對齊 Python；empty value 不再誤判為已解析。Removed during attach 設 DEVICE_REMOVED_DURING_ATTACH；AccessDenied 記為 ATTACH_FAILED:PermissionError。
