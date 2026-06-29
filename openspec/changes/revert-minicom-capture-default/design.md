## Context

`minicom_router.sh` 的 `_resolve_capture_mode()`（`sw_core/assets/tools/minicom_router.sh:53-77`）以三層 precedence 決定 auto-capture 模式：

1. 顯式 `MINICOM_CAPTURE_MODE`（`:54-65`，僅接受 `script|minicom|off`，否則報錯 exit 2）。
2. legacy `MINICOM_CAPTURE_WRAPPER` 曾被設定（`:67-74`，`=1`→`script`、其餘如 `=0`→`minicom`）。
3. 兩者皆未設的最終 fallback（`:76`，目前為 `printf 'script'`——即 `6df17a5` 翻成 script 的位置）。

解析結果在 `_exec_minicom()` 消費：`script` 分支（`:221-229`，`script -qef -c "<cmdline>" logfile`，script 不存在則印 warning 降級為不帶 `-C` 跑 minicom）、`minicom` 分支（`:230-232`，`cmd+=("-C" logfile)`）、`off`（logfile 保持空、完全不注入）。`has_capture` 偵測（`:189-198`）涵蓋使用者自帶 `-C`/`--capturefile`，命中即跳過 auto 注入避免重複。

歷史脈絡：`b883506`(script transcript) → `a4633bb`(改 `-C`，理由「降低 broker 延遲」) → `6df17a5`(又改回 `script`，理由查無實證的「-C native crash」)。本 change 把第 (3) 層預設復原為 `minicom`。

## Goals / Non-Goals

**Goals:**
- 預設 auto-transcript 產生**乾淨序列 RX**（不含 minicom 自身 UI/全螢幕重繪/狀態列/Leave 對話框）。
- 完全保留 `MINICOM_CAPTURE_MODE` 與 legacy `MINICOM_CAPTURE_WRAPPER` 既有語意與 precedence。
- docs（README/spec）與實作一致，並移除查無實證的 crash 斷言措辭。

**Non-Goals:**
- **不**追求「100% 無 ANSI」：target 自身輸出的 ANSI（如 `ls --color`、著色 prompt）仍會被 `-C` 如實記錄；要完全 strip 屬另案（daemon 端擷取或事後 strip），本 change 不做。
- **不**碰 daemon／RPC／序列埠 I/O，**不**碰 console 生命週期（孤兒回收/raw ownership 由 PR-B 處理）。
- **不**為 `-C` 加 `--capturefile-buffer-mode`：minicom 2.9 預設 N(Unbuffered) 逐字 flush，115200 無需 buffer；極高 baud 才考慮，列為未來。

## Decisions

- **單行變更**：`minicom_router.sh:76` 的 `printf 'script'` → `printf 'minicom'`。`:69`（WRAPPER=1→script，6 空格縮排）與 `:71`（WRAPPER=0→minicom）**不得動**；Edit 必須以上下文（含 `:74-77` 的 `return`/縮排）鎖定，避免誤改到同字串的 `:69`。
- **precedence 自洽**：翻轉後唯一行為變動是「`MODE` 與 `WRAPPER` 皆未設」時 `script`→`minicom`；顯式 `MODE`、legacy `WRAPPER=1/0`、`off`、使用者自帶 `-C` 路徑皆不變。
- **CHANGELOG 處置**：`[Unreleased]` 既有「改為 script…」一條尚未隨任何 release 出貨，直接**修正/取代**該條為「預設維持 minicom 原生 `-C`」，避免同一未出貨段落自相矛盾。
- **docs 軟化**：README 對 `minicom` 模式的「native crash 風險」警語改為中性描述（如「全終端 transcript 請用 `MODE=script`」），不再宣稱 `-C` 會 crash（查無實證）。

## Risks / Trade-offs

- **是否引回「-C native crash」？** 評估為低：查證無 repro/test/Issue；minicom 2.9 `-C` 預設逐字 flush 無丟尾；115200 不會背壓。此為 PR 說明須點明的最大風險點。
- **行為可見差異**：倚賴完整終端畫面錄製者，預設 log 內容會改變；補救路徑為 `MINICOM_CAPTURE_MODE=script`，需在 CHANGELOG/README 明示。
- **Edit 誤改風險**：`:69` 與 `:76` 文字皆為 `printf 'script'`；誤改 `:69` 會破壞 legacy `WRAPPER=1==script` 契約並使 `test_wrapper_generates_transcript_log` 失敗——列為實作必檢項。
- **測試耦合**：`test_default_capture_uses_script_...` 以 fake-minicom 見到 `-C` 即 exit 44 來鎖死「預設=script」，必須重寫；`test_script_unavailable_...` 依賴舊預設=script 才走 warning 降級，必須補 `MODE=script`。漏改任一會造成新預設下測試失敗。
