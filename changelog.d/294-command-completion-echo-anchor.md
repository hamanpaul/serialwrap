---
type: fix
scope: rust-command-completion
---
- Rust 一般 agent line／background 命令完成與 stdout 共用同一 snapshot；完整 command echo 仍是輸出下界，BusyBox／readline 單一局部折行與狹義重複 echo 相容規則保留。READY evidence 只取 probe 的獨立 nonce 輸出＋相同既知 prompt；probe echo、舊 prompt 與非同步輸出三類拒絕字面不寫入 evidence，後續同字面缺互證前綴時也不提前完成。evidence 清除、被拒或 PS1 改變時，echo 前綴固定為該行真實行首到 echo 起點的完整字面，可跨 TX 延伸到同 epoch、未修剪且行首可證明的前一段，不再剝除舊 evidence 或改取 suffix。完整前綴與 echo 後真實行首 prompt 完全相等才完成並重建 evidence；否則只有既有 evidence 與 tail 完整相等可完成。只作命令輸出的 `Path => ` 不得學成 evidence。
- 已找到本命令 echo 時，已知且至少含字母或數字的完整 prompt 可裁掉無換行 stdout 尾端 suffix，精確保留 `printf abc`、`router => `、`router=> ` 與 NUL；純符號 `=> `、`> `、`# ` 只在行首接受，不成為 evidence。沒有 human 訊號的無 echo 命令不要求 evidence，只由 profile regex 從真實最後一行行首完整匹配到 snapshot 尾端；同行 suffix 仍不完成，必要 post-TX RX 修剪時回 `ok:true`、`partial=true`。
- evidence 寫回與完成線性化共同驗證 snapshot revision、profile revision、RX watermark／epoch；clear、換 epoch、profile rebind 與接管後舊 evidence 不可復活。跨 TX 前綴遇 human 訊號時走既有 human partial、不互證學習；同一 snapshot 的 `human_in_flight > 0` 或 `tx_submit_pending` 也算在途訊號，在途消失後才由 revision 變動重新評估。前綴所在行首被修剪、或 prompt 與 echo 不在同一行時，不由該前綴恢復；這類與同一行連續 prompt 仍回保守 partial，已列為已知限制。tail candidate 為最後一行完整 profile match、已知 prompt suffix，或 profile 可辨識的純符號短 suffix。無 human 且完全沒有候選時保留 9af187ca timeout／recovery 與救援 TX；有候選但身分不足時回 `PROMPT_TIMEOUT`、`partial=true`、`NONE`、保留輸出、零救援。
- human 交錯且無 echo 時維持既有 pending 線性化與一次性消耗規則；未改 `completion_regex`／producer、passthrough、登入、reboot／recover、檔案傳輸、Python、錯誤碼集合或案例分母。
