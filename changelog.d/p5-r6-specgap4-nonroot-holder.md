---
type: change
scope: rust-s21
---
P6 SPEC-GAP #4：新增非 root daemon 級回歸 `daemon_process::non_root_holder_scan_permission_does_not_block_open`——一般使用者執行時鎖後 holder 掃描讀不到他人行程的 `/proc/<pid>/fd`（`Permission`）須與 Python `serial_port.probe_holders` 相同：警告並照常開埠、`health.status.foreign_holders` 回報 `holder_unknown`、session READY；fail-closed 判定只保留在 CLI 升級／回滾安全檢查。
