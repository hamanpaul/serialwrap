---
type: fix
scope: rust-command-completion
---
- Rust 一般 agent line／background 命令只接受清理後行尾錨定的完整 echo；折行最多接受一個帶局部串流證據的 BusyBox／readline 插入點，多次、混用、無局部證據及其他形態均按無 echo 規則處理。`completion_regex`／marker producer 維持既有結果。無 echo、窗口內 human submit 開始計數未變且尾端 prompt 合規時，維持相容完成。
- 無 echo 且窗口內有 human submit 時，不採相容完成；等待上限到達後按統一 timeout 規則回 `PROMPT_TIMEOUT`／`partial=true`／`recovery_action=NONE`，stdout 取結算 snapshot partial，救援 TX 為零。
- 所有 timeout 入口（含 30 秒靜默 chunk）在鎖內比較目前單調遞增的 human submit 開始計數與命令 TX 起點計數，並檢查 `tx_submit_pending`；任一成立即走 human timeout 路徑。計數於 human submit 呼叫 OS submit 前遞增，分流不依事件時間或 snapshot 過濾，因此截止後、timeout 決策前才開始的 submit 也保守視為 human。Complete 線性化重驗時計數變化須重新判定。判定無 human 後，共享 submit gate 會保持到第一個救援鍵已提交至 OS，期間新的 human submit 等待；operation deadline 已到且尚未線性化 Complete 時，不得事後改判正常完成。無 human timeout 的完整 `recover_after_failure` 結果及 stdout 與 `9af187ca` 基線逐位元組相同。
- 保留既有狹義重複 echo 相容規則：echo 後第一行完整等於命令且後面尚有輸出時刪除該行；需要保留時使用 `completion_regex` 或 marker。i294 第一階段不涵蓋 issue 262 prompt 身分與 issue 271 同行 stdout 裁切。
