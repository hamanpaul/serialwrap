---
type: fix
scope: rust-refactor
---

OD-29：`cap_supervisor_runtime__systemd_hold_blocks_restart_on_failure` 的清理一併移除 release 後留下的空 `<unit>.service.d` 目錄，確保本 host 實跑後 `~/.config/systemd/user` 不留暫時 unit 痕跡；新增擁有者 runbook `docs/rust-refactor/owner-runbook-od29.md`（`cap_interactive_desktop__exec_or_wait_ctrl_c` 需互動 console）。
