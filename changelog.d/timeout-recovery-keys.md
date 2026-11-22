---
type: feat
scope: command
---

profile 新增選配欄位 `timeout_recovery_keys`（template 或 target），決定命令逾時後（以及 `READY` 時的 `session recover`）依序送哪些恢復按鍵。預設 `[CTRL_C, CTRL_D]`，與既有行為相同。設 `[CTRL_C]` 就不再送 CTRL_D——CTRL_D 打在已登入 shell 的空提示符上等於登出，prplOS 之類停在 askconsole 的板子之後要有人按 Enter 才回得來；設 `[]` 則完全不送按鍵。清單內的鍵都救不回 prompt 時照既有收尾：回 `PROMPT_TIMEOUT`、session 轉 `ATTACHED`。未知鍵名、重複或非 list 的值在載入 profile 時就拒絕，不會悄悄退回預設。Rust daemon 與 Rust CLI 的 profile 載入同樣支援這個欄位（解析、驗證、template 繼承與 target 覆寫與 Python 一致），命令逾時與 `READY` 時的 `session recover` 都依清單送鍵。

出貨的 `prpl-template` 設為 `timeout_recovery_keys: [CTRL_C]`：這個 template 的命令逾時後只送 CTRL_C，不再送 CTRL_D，避免把已登入的 ash 登出。CTRL_C 救不回 prompt 時回 `PROMPT_TIMEOUT`、session 轉 `ATTACHED`。`serialwrap setup` 不覆寫既有 profile，舊安裝要自行在線上 profile 的 template 加這個欄位才會生效。其餘出貨 template 維持預設。
