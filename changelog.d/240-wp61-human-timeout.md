---
type: fix
scope: rust-refactor
---
S2-PAR WP6-1（G7b，OD-74）：session actor 於前景命令開始時保存請求的 `trusted_human` 與正規化 `source` 快照，結算時與 line editor 的 `HumanConsole` 取 OR 判定 human 分支。RPC bare `human`（及 `human:`）命令逾時或 reboot 改升格 interactive lease（`PROMOTE_HUMAN_INTERACTIVE`），lease owner 為正規化後的 source `human:cli`（Python `owner=source`，`origin/main:sw_core/session_manager.py:6476-6504`、`:6988-7012`）；reboot 轉 ATTACHED、`last_error=REBOOTING`，不進 agent 自動登入恢復。明寫 `human:<x>`（含 `human:cli`）的 RPC 仍比照 agent；可信判定不看 source 字首或 `source_class`。快照於命令結算與 drain 時清除，不讓下一筆命令承接。逾時救援鍵（CTRL-C／D）的抑制屬 OpRunner，不在本項。
