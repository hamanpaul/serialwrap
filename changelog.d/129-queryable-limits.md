---
type: feat
issue: 129
scope: daemon
---
暴露可查詢的命令長度上限，讓 client 執行期查詢而非硬編碼：`health.status`（CLI `serialwrap daemon status`）回應新增 `limits` 欄位——`max_submit_cmd_bytes`（16384 硬上限，超過拒 `CMD_TOO_LONG`）、`warn_submit_cmd_bytes`（4096 軟上限，超過附 `CMD_LENGTH_WARNING`）、`reject_error_code`、`newline_forbidden`——值直接引用 `sw_core/arbiter.py` 常數（單一事實來源）。`serialwrap cmd submit --help` 補上限說明：`--cmd` 以 UTF-8 位元組計、broker 不截斷、含 `\n` 直接拒（`CMD_CONTAINS_NEWLINE`），並區分 broker 參數上限與 target 端 tty line buffer（常見 4096）的物理單行限制；README／spec／SKILL.md 同步。
