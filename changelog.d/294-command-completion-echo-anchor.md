---
type: fix
scope: rust-command-completion
---
- Rust 一般 agent line／background 命令只接受清理後行尾錨定的完整 echo；折行最多接受一個帶局部串流證據的 BusyBox／readline 插入點，其餘折行形態按無 echo 處理。完成與 stdout 使用同一 RX snapshot；echo 後恰好等於命令的第一行仍依相容規則視為重複 echo。`completion_regex`／marker producer 維持既有結果。
- 同一 session 的 bridge epoch 共用 human gate 與計數域。human writer 的 OS write 成功返回後才以 permit epoch 和 active RX watermark 登錄邊界；同 epoch 使用 write 後絕對 RX 位置，舊 epoch 晚登錄映射至 `(active_epoch, 0)`，接管將 pending 推至 `(new_epoch, 0)`。邊界 tuple `(epoch, offset)` 單調遞增。正式 close fence 依 writer／reader join、舊 handle close、WorkerStopped 回條後才開啟新 handle。只對 Accepted／Pending 登錄，WouldBlock、NotSubmitted、拒絕不建界。
- human prompt 結案須在完整清理視窗中由可證明的真實行首開始，整行符合目前 profile regex 並終止於 snapshot 尾端，且該行全部原始來源位元組都在最新 TX 邊界後。最後一行若從已修剪視窗的開頭起算（trimmed > 0），行首無法證明，不結案；只有 trimmed == 0 且窗口起點是 epoch 串流起點時，視窗開頭才算行首。分包未完整不結案，CR／重繪依 clean_text 語意處理。
- 一般 echo evaluator 命令開始時已有 pending_human，且靜默 chunk 或絕對 deadline 選擇 human timeout partial 時，在同一 decision reservation 臨界區檢查 submit 開始計數、human TX generation、pending boundary tuple、human_in_flight 與 tx_submit_pending。均無新動作且無在途／排隊提交時，原子清除 pending 並記錄「timeout 消耗」事件；其他情況保留。該筆 partial、TX、零救援 TX 與既有 timeout 行為不變；人類 submit 在線性化點前到達則保留，點後到達則建立新 pending。舊 epoch 接管後的 pending 同樣適用。
- human 停止後，穩定 pending 最多使一筆無 echo agent 命令回 partial，後續命令恢復與 9af187ca 基線逐位元組相同。若 timeout 消耗時 human 命令實際仍在裝置執行，下一筆回到基線原有行為；基線本來沒有 human 保護，因此不會比基線差。
- 保守代價各自最多一筆無 echo partial：prompt 先於 write return 完整抵達；Ctrl-L、方向鍵、Tab 等不送換行按鍵；無 echo 終端的無輸出 human 命令使新 prompt 接在同一行；修剪後行首無法證明。有 echo 的 human 命令通常以 echo 換行讓 prompt 落在真實行首。
- issue 262 的 prompt 身分辨識與 issue 271 的同行 stdout 裁切仍由 i294-i2 處理；`completion_regex`、passthrough、Python 基線、錯誤碼集合與案例分母不變。
