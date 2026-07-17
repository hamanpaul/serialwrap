---
type: fix
issue: 140
scope: session
---
帳密宣告但解析為空時不再靜默送空帳密狂 probe，改回明確 `CREDENTIALS_UNRESOLVED` 終態：`resolve_session_auth()` 除 `SessionAuth` 外新增回傳 `AuthResolution`（`reason` 為 `ok` / `env_file_missing` / `env_file_unreadable` / `key_absent` / `not_configured`，並帶實際解析到的 env_file 絕對路徑；只要帳密實際解析成功即為 `ok`，即使 env_file 缺失、只要 `os.environ` 補齊亦不誤擋）。當 profile **宣告了帳密來源**（`user_env`／`pass_env`／`env_file` 任一）但解析為空時，login 流程 **不對** `Login:`／`Password:` 送空字串、不進無限 `Login incorrect` 迴圈，session 標 `last_error=CREDENTIALS_UNRESOLVED`（與「板子尚未到 login prompt」的 `LOGIN_REQUIRED` 明確區分）並停止自動 reprobe（明確終態）；四條 attach/recover 路徑一致套用。進入此態時 daemon 輸出一次性（去重）log + WAL 警告，含 env_file 實際解析絕對路徑與原因，**絕不含帳密值**。手動 `session attach`／`recover` 視為重新介入、重置去重旗標並重解析（補齊帳密即恢復；daemon 重啟亦重讀 env_file）。profile **未宣告**帳密來源（`not_configured`）者行為完全不變（passwordless/auto-login 不受影響）。新增 error_code 為 additive，不改既有欄位；純 POSIX/共用邏輯，Windows daemon 走同路徑同步受益。

**排查重點（易踩）**：profile YAML 內的**相對** `env_file` 是**相對 daemon 的 profile-dir** 解析（systemd-system＝`/etc/serialwrap/profiles/`、pipx/XDG＝`~/.config/serialwrap/profiles/`），**不是** shell CWD 或 XDG config——帳密檔要放在 daemon profile-dir，警告訊息印出的絕對路徑即 daemon 實際查找位置。README（中英）／`docs/serialwrap-spec.md`／`SKILL.md` 同步。
