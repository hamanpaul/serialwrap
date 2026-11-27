---
type: fix
scope: rust-command-completion
---
- Rust 一般 agent line／background 命令完成與 stdout 共用同一 snapshot；完整 command echo 仍是輸出下界，BusyBox／readline 單一局部折行與狹義重複 echo 相容規則保留。READY evidence 只取 probe 的獨立 nonce 輸出＋相同既知 prompt；evidence 清除、被拒或 PS1 改變時，echo 所在行前綴須從真實行首完整匹配目前 profile、至少含字母或數字，且 echo 後真實行首到 tail 重現相同字面，才能完成並原子重建 evidence。只作命令輸出的 `Path => ` 不得學成 evidence。
- 已找到本命令 echo 時，已知且至少含字母或數字的完整 prompt 可裁掉無換行 stdout 尾端 suffix，精確保留 `printf abc`、`router => `、`router=> ` 與 NUL；純符號 `=> `、`> `、`# ` 只在行首接受，不成為 evidence。沒有 human 訊號的無 echo 命令不要求 evidence，只由 profile regex 從真實最後一行行首完整匹配到 snapshot 尾端；同行 suffix 仍不完成，必要 post-TX RX 修剪時回 `ok:true`、`partial=true`。
- evidence 寫回與完成線性化共同驗證 snapshot revision、profile revision、RX watermark／epoch；clear、換 epoch、profile rebind 與接管後舊 evidence 不可復活。tail candidate 為最後一行完整 profile match、已知 prompt suffix，或 profile 可辨識的純符號短 suffix。無 human 且完全沒有候選時保留 9af187ca timeout／recovery 與救援 TX；有候選但身分不足時回 `PROMPT_TIMEOUT`、`partial=true`、`NONE`、保留輸出、零救援。
- human 交錯且無 echo 時維持既有 pending 線性化與一次性消耗規則；未改 `completion_regex`／producer、passthrough、登入、reboot／recover、檔案傳輸、Python、錯誤碼集合或案例分母。
