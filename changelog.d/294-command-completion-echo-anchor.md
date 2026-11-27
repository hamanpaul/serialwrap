---
type: fix
scope: rust-command-completion
---
- Rust 一般 agent line／background 命令只以行尾錨定的完整 echo 建立輸出下界，完成與 stdout 共用同一 snapshot；既有 BusyBox／readline 單一局部折行與狹義重複 echo 相容規則保留。prompt 必須另有 epoch 內字面證據：只採可辨識 READY 結果或完整 echo 前綴，clear、換 epoch、profile rebind 後失效；`Path => ` 等任意 regex 命中不再提前完成。
- 已知、完整且至少含字母或數字的 prompt 若黏在無換行 stdout 尾端，只裁掉該 suffix，`printf abc` 與檔案尾端 NUL 均精確保留；短 prompt（例如 `> `／`# `）只能在最後一行行首使用。已知短 suffix 或未知／變更的尾端 regex 候選若無法辨識身分，保守回 `PROMPT_TIMEOUT`、`partial=true`、`recovery_action=NONE`，保留可用輸出，不重送或另送 rescue key；完全沒有候選時仍走既有 no-prompt timeout。
- 一般無 echo 命令在命令起點沒有 pending_human、窗口內沒有 human 訊號時仍可由可信尾端 prompt 正常完成：未修剪資料回 `partial=false`；若必要 post-TX RX 已確定被窗口修剪，回 `ok:true`、`partial=true` 且無 error_code。只有 human 交錯而且無 echo 時才改走 human partial；不得把此 human 判準誤套到一般無 echo 終端。
- Human pending 結案合併為單一規則：完整清理視窗最後一行須由可證明的真實行首起、完整符合目前 profile regex、終止於 snapshot 尾端，且所有原始來源位元組在最新 OS write 返回後 watermark 之後；trimmed > 0 的窗口開頭不能冒充行首，CR／分包依既有 clean_text 與完整行判斷。跨 epoch pending tuple 單調、舊 writer 晚到映射與 close fence 不變；穩定 pending 只在原子 timeout decision 符合既有四項不變量時消耗一次。
- timeout 仍共用 session dispatch／human enqueue／pending 線性化與既有 rescue reservation。無 human 訊號且沒有 prompt 身分歧義的 timeout／recover_after_failure 結果與救援 TX 維持 9af187ca 基線；human 交錯或 prompt 身分歧義則保留 stdout partial，且不改 `completion_regex`／producer、passthrough、登入、reboot／recover、檔案傳輸、Python、錯誤碼集合或案例分母。
