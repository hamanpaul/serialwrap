---
type: fix
scope: holder-probe
---

修正 holder 探測以 readlink 字串或 `st_rdev` 判定同一 tty 的缺陷：docker 容器內獨立 devpts instance 的 `/dev/pts/N` 與主機同名、同 rdev（136:N），卻非同一裝置，會被誤判為外部 holder（可能使 host namespace 的 daemon attach 回 `DeviceHeldError`，並使黑箱 helper 的等待條件假成功）。

現行判準（`sw_core/serial_port.py::same_tty`）：`(st_dev, st_ino)` 相同即同一 tty；另允許非 devpts 字元裝置的 mknod 別名（兩邊皆 char device、rdev 相等且非零、major 不在 136–143），以涵蓋容器以 mknod 掛入同一實體 UART 節點之情境。devpts 不以 rdev 判定。`probe_holders`、`UARTBridge._enumerate_all_held_paths`（先以字串篩除非 `/dev/` link，降低 reaper 的 syscall 成本）與 `detect_multi_open` 皆採此判準；Rust `crates/sw-pal/src/proc_scan/posix.rs::serialwrapd_tty_holders` 同步。目標 tty 無法取得身分時回 `unknown`，不再當成確認無 holder。保留既有 permission／unknown 狀態語意。

黑箱測試 helper `_external_holders` 同步採同一判準。既有以假路徑佈置 holder 的單元測試改用實體檔案，rdev-only 正面案例改為非 devpts mknod 別名正面，並新增 inode 別名與跨 devpts 負面案例。新增 `tests/test_holder_probe_namespace.py` 作為 namespace 碰撞回歸（#155：pytest 可覆蓋，免 `regression/` case）。行為變更之擁有者登記見 `CHG-S1X-HOLDER-IDENTITY-01`（OPEN）。
