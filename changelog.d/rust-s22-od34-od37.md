---
type: change
scope: rust-console
---
依擁有者裁決 OD-37 登記 DF-20：`device.release` 後再 `device.attach`，以 release 前取得的 `interactive_id`（agent lease 或 human console lease）呼叫 `session.interactive_send`，Rust daemon 回 `INTERACTIVE_NOT_FOUND`（帶 `interactive_id`）且不送任何 bytes；Python 現行照送，維持不改。
依擁有者裁決 OD-34：func-test `cowork-01-agent-bad-cmd` 自 Slice 2.2 gate 逐名移出、改列 Slice 2.3；`tests/acceptance/regen_lock.py --add-only` 新增 `--authorize-deferral <case_id>:OD-<n>`，只放行擁有者逐名授權案例的 `daemon_slice` 往後移，其餘屬性改動、其他案例與未對應實際延後的授權仍拒絕。
