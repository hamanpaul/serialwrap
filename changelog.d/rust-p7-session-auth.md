---
type: feat
scope: rust-p7
---
P7 Task 2：`session_auth.rs` 以 Python 基線 golden（`env_file`、`auth_resolution`）鎖定帳密解析，並修正與 `auth.py` 的偏差：env 檔改以 universal newlines＋`\n` 切行與 Python `str.strip` 語意、`env_file` 先 `os.path.expanduser`（`SysEnv::path_flavor` 選 posixpath／ntpath）、`local or environ` 保留環境變數空字串、補 `env_file 不存在`／`無法解析 env_file` WARNING（`resolve_session_auth_with_warnings`）；`SessionAuth` 除錯輸出遮蔽密碼與 console unlock 值。
