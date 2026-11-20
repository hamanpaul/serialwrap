---
type: fix
scope: rust-p7
---
P7 雲端審查修正（第 1 輪）：attach 只在 Python 會解析帳密處解析（boot quiet 時不讀 env_file、不多寫 `env_file 不存在` WARNING；既有 bridge 再探不以 `pending_auto_login` 決定 require_login）；login run 部分寫出／結果不明（`Aborted(TxIncomplete)`）改與 Python `TxIncompleteError` 穿出同收斂（`DETACHED`＋`ATTACH_FAILED:TxIncompleteError`），不再標成 `PROMPT_UNAVAILABLE`；`pyre::compile_capturing` 的零寬可選擷取群組（`()?`、`(\b)?`、`()??`…）不再被省略而使 `MatchResult.groups` 序號錯位；event matcher cooldown 以任意精度整數比較，不再因 i128 飽和誤判。
