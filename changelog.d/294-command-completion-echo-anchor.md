---
type: fix
scope: rust-command-completion
---
- Rust 一般 agent line／background 命令只接受清理後行尾錨定的完整 echo；折行最多接受一個帶局部串流證據的 BusyBox／readline 插入點，多次、混用、無局部證據及其他形態均按無 echo 規則處理。`completion_regex`／marker producer 維持既有結果。
- 本 evaluator 涵蓋的一般 agent line／background 命令 timeout 入口（包括 30 秒靜默 chunk）都依結算 snapshot 的 human TX／在途 submit 狀態決定救援，不以 evaluator 的 `Uncertain` 結果代替。有人類 TX 或 `tx_submit_pending=true` 時回 `PROMPT_TIMEOUT`、`partial=true`、`recovery_action=NONE`、零救援 TX，stdout 取自 snapshot partial。沒有 human TX 時 timeout 結果與 `9af187ca` 基線逐位元組相同，直接採用完整 `recover_after_failure` 結果與 stdout；救援成功回 `status=done`、`ok=true`、`error_code=PROMPT_TIMEOUT_RECOVERED`、`partial=true` 及實際 `recovery_action`，失敗回 `PROMPT_TIMEOUT`。operation deadline 到時尚未判定完成時，不以截止 snapshot 事後改判正常完成。
- 保留既有狹義重複 echo 相容規則：echo 後第一行完整等於命令且後面尚有輸出時刪除該行；需要保留時使用 `completion_regex` 或 marker。i294 第一階段不涵蓋 issue 262 prompt 身分與 issue 271 同行 stdout 裁切。
