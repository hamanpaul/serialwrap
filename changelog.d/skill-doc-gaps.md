---
type: docs
scope: agent-skill
---

補上游 agent skill（`sw_core/assets/skill/SKILL.md`）三處過時或缺漏，並同步 README／`docs/serialwrap-spec.md` 中同樣不準的句子；不改程式行為，每個數字與行為皆對照 `session_manager.py`、`config.py`、`setup_cmd.py` 核實。

- `cmd submit` 的期限語意：總期限是 `--cmd-timeout`（命令一直有輸出也一樣會到點進恢復）、靜默上限是 `min(--cmd-timeout, 30s)`；`--expected-duration` 只延長總期限、不延長靜默上限，舊文「暫停 prompt timeout」不成立，已更正。逾時後非 human 來源依序送 Ctrl-C、Ctrl-D（各在 `min(--cmd-timeout, 2s)` 內比對 prompt），Ctrl-D 打在已登入的 shell 等於登出，開機靜默窗內不送。建議長時間或會靜默的命令在板上以 `setsid` 丟背景再輪詢；`--mode background` 與 `line` 共用同一段等待邏輯，不是長命令出口。
- boot quiet window 補 `boot_done_regex`：命中 `login_regex`、`prompt_regex`、`boot_done_regex` 任一即解除（尾行為 `bootloader_prompts` 例外），三者都不中才等滿 180s；prplOS askconsole 要靠此欄位。`serialwrap setup` 不覆寫既有 profile，舊安裝需手動補欄位並重啟 service。
- `--completion-regex`：指向 `docs/command-completion.md`，補要點（行首錨定、數字 RC 加獨立 END 行、每次換 nonce、上限 1024 bytes、不匹配空串）；它只換掉「等什麼」，總期限、靜默上限、逾時恢復語意不變，reboot 仍走既有 recovery。
- README 長命令範例原把全域 `--timeout 300`（RPC 等待秒數）寫在 `cmd submit` 之後，argparse 回 `unrecognized arguments` 直接拒絕，改為 `--cmd-timeout 30 --expected-duration 300`；`docs/serialwrap-spec.md` §6.5／§14 同步更正。
