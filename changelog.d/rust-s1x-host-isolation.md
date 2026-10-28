---
type: fix
scope: rust-cli
---

Stage 1 黑箱 rig 改於獨立 user+pid+mount namespace 內執行：自啟 daemon 並呼叫 CLI `daemon start／restart`、`setup` 更新／回滾、`supervision-mode set` 的 Linux rig（`pidns_rig` 標記）整段重跑於隔離 namespace，不再因宿主生產 `serialwrapd` 或容器 daemon 被多開偵測而誤判 `DEVICE_STILL_HELD`／`DAEMON_ALREADY_RUNNING_ELSEWHERE`；namespace 不可用時 skip 並註明能力原因，不以 PASS 放行。`ab_blackbox.sh` 整段（A 輪、釋放確認、B 輪）於同一 namespace 內執行，並以 preflight（exit 4）判定非隔離或可見外部 `serialwrapd` 的 run 為 INVALID，身分檔記錄 `pid_namespace_isolated`（僅記錄，守門為 preflight）。`ab_blackbox.sh` 改於 `target/ab-runs/<run>/bundle` 建置本次 bundle，不再共用 `target/mkbundle-dev`。

修正 Rust CLI 回滾時 `start_and_verify` 誤啟動新版 daemon 的缺陷：POSIX 穩定入口為 symlink，PAL `is_file` 不跟隨連結，改以 `CLI 自身 bundle` 的 launcher 啟動，回滾必回報 `pairing_mismatch`；改為跟隨連結判定穩定入口。交接停止（`daemon.handoff_stop`）成功後，Python daemon 的 RPC 監聽保留有界排空窗口（`HANDOFF_DRAIN_GRACE_S`，3 秒）再停止，窗口內有副作用請求取得 `FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`，而非 socket 錯誤（此為 SC-11 字面「回覆後立即停止」的偏離，待擁有者或規格協調確認）。交接停止成功後的 `handoff_stop` 記入 setup／supervision 回報的 `ran`（與 `daemon stop` 對稱）。黑箱 chunk 延遲改以實際送出的 chunk 指令前綴觸發；busy push 測試以 ACK-gated 傳輸（`ack_mode=auto`）、16 KiB 檔案、20 秒首個 ACK 延遲，並以 `--chunk-timeout`（`BUSY_CHUNK_TIMEOUT_S`＝60 秒）避免 CLI 在 ACK 前逾時送出清理指令而破壞零 TX 斷言；背景執行緒例外於 `join()` 重新拋出；supervision 切換測試等待 holder 行程以 `serialwrapd` 身分出現於 `/proc`，消除競態。
