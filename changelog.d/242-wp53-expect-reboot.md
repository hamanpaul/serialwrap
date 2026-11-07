---
type: fix
scope: rust-refactor
---
Rust `OpRunner::run_command` 的 reboot 分流改為 `expect_reboot || guessed_reboot`，對齊 Python `session_manager.py` 的 `if expect_reboot or guessed_reboot`：帶 `expect_reboot:true` 的命令不論命令文字（如 `echo a && reboot`）、也不排除 passthrough，一律交給 reboot 分支；命令文字猜法仍只對非 passthrough 生效、未擴大。分流位於 completion pattern 與 `command_completion` META 之前，故旗標優先於 `completion_regex`：prompt 未在等待期限內回來時結算為 `EXPECT_REBOOT`（partial），不送 Ctrl-C／Ctrl-D、不依 completion regex 正常完成。無旗標的串接命令維持一般命令路徑。串接命令的 prompt 等待時間、actor 端 reboot quiet arming 與 `command_expect_reboot` WAL META 尚未接上。
