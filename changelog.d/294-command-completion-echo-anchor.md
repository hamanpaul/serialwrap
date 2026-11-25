---
type: fix
scope: rust-command-completion
---
- Rust 一般 agent line／background 命令只接受清理後行尾錨定的完整 echo；折行最多接受一個帶局部串流證據的 BusyBox／readline 插入點，多次、混用、無局部證據及其他形態均按無 echo 規則處理。`completion_regex`／marker producer 維持既有結果。
- operation deadline 到時仍未判定完成，一律回 `PROMPT_TIMEOUT`、`partial=true` 並保留可用輸出，不以截止 snapshot 事後改判完成。無 human TX 時保留基線 Ctrl-C／Ctrl-D recovery TX 與 session 收斂；基線救援成功原回 `status=done`、`PROMPT_TIMEOUT_RECOVERED`、`partial=true`，R8 deadline 結算改回 `status=error`、`PROMPT_TIMEOUT`、`partial=true`；有 human TX 或 settlement 時 `tx_submit_pending` 為 true 時，回 `recovery_action=NONE` 且不送救援鍵。
- 保留既有狹義重複 echo 相容規則：echo 後第一行完整等於命令且後面尚有輸出時刪除該行；需要保留時使用 `completion_regex` 或 marker。i294 第一階段不涵蓋 issue 262 prompt 身分與 issue 271 同行 stdout 裁切。
