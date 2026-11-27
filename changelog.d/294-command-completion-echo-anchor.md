---
type: fix
scope: rust-command-completion
---
- Rust 一般 agent line／background 命令只以行尾錨定的完整 echo 建立輸出下界，完成與 stdout 共用同一 snapshot；既有 BusyBox／readline 單一局部折行與狹義重複 echo 相容規則保留。prompt evidence 只來自 READY probe 的獨立 nonce 輸出＋相同既知 prompt，或已確認完整 echo 前綴；probe echo、舊 prompt、非同步輸出與 `Path => ` 不得只因寬鬆 regex 成為 evidence。
- 已找到本命令 echo 時，已知、完整且至少含字母或數字的 prompt 若黏在無換行 stdout 尾端，只裁掉該 suffix，`printf abc`、`router=> ` 與檔案尾端 NUL 可精確保留；短 prompt 只能在最後一行行首。沒有 echo 時 prompt 也必須在真正最後一行行首，同行 `abcroot@t:/# ` 只是候選。evidence 的寫回與完成線性化共同驗證 snapshot revision、profile revision、RX watermark／epoch；clear、換 epoch、profile rebind 與接管後舊 evidence 不可復活。
- tail completion candidate 定義為：最後一行完整符合 prompt regex，或 snapshot 尾端完整等於已知 prompt 字面。無 human 且完全沒有候選時，silence／deadline 入口仍採 9af187ca timeout／recovery 與相同救援 TX；有候選但身分不足時回 `PROMPT_TIMEOUT`、`partial=true`、`recovery_action=NONE`，保留輸出且不送救援鍵。human 路徑維持既有 pending 線性化與一次性消耗規則。
- 一般無 echo 命令在無 pending_human／human 訊號且 prompt 位於最後一行行首時可正常完成；未修剪資料回 `partial=false`，必要 post-TX RX 已確定被窗口修剪時回 `ok:true`、`partial=true` 且無 error_code。只有 human 交錯且無 echo 時才套 human partial；不得改 `completion_regex`／producer、passthrough、登入、reboot／recover、檔案傳輸、Python、錯誤碼集合或案例分母。
