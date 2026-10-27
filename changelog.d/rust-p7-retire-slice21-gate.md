---
type: remove
scope: rust-p7
---
P7 Task 3 補完（08 §0.6 退役清單）：移除 Slice 2.1 profile 能力閘 `slice21_unsupported_feature` 與其呼叫點（attach、既有 bridge 再探、reboot recovery），`post_login_cmd`（bcm）與 `console_unlock_env` profile 改走全平台 Login FSM 自動登入；`slice21_scope.yaml` 的 `unsupported_profile_features` 改為 `[]`；動態偵測（`SessionManager` detect）只略過 passthrough 與 `console_unlock_env`，`post_login_cmd` 模板恢復為候選（Python `detect_template`）。06 T13 測試 `unsupported_profile_no_auto_tx` 以 `post_login_cmd_profile_logs_in`、06 T16 `detect_skips_passthrough_console_unlock_and_unsupported` 以 `detect_skips_passthrough_and_console_unlock` 取代（L1 鎖以 `--allow-removal` 逐名核准移除）。
