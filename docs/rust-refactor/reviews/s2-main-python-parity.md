# S2-PAR-syncmain：Python daemon parity 檢查

## 範圍與比較基準

本文件只檢查 ITEM 2：main 同步是否帶入需要 Rust daemon 跟進的 `sw_core/` 行為變更。合併 commit 為 `3864e9b5e405a1c90140276fbb5b0deaaaf7d3a5`，第一父節點（合併前整合線）為 `c4913aed6e86f3d9d64870da9110d1de15e6497a`，第二父節點（`origin/main`）為 `47097715183698b138f1fa8bacd9d0f25c6edae7`。

依要求執行 `git diff c4913aed6e86f3d9d64870da9110d1de15e6497a..3864e9b5e405a1c90140276fbb5b0deaaaf7d3a5 -- sw_core/`，檔案清單與差異皆為空；兩端 `sw_core/` tree object 同為 `a9ceb49e324eca4068ff0ecfd4a41cbac9efd498`。兩父節點共同祖先是 `1fa7b78520c1146c5cb80a4b486f387fab7b0104`（PR 255）。共同祖先到本次 `origin/main` 端的 `sw_core/` 差異也為空。因此本次同步沒有新增 Python daemon 行為可移植。

## 逐項判斷

下表同時列出任務點名的 PR 253／255／258，並標明它們是否屬於本次 `sw_core/` 差異。

| Python file:line | 變更摘要 | Rust 對應位置 | 需要跟進／不需要與理由 |
|---|---|---|---|
| 無；本次合併範圍 `sw_core/` 無變更 | PR 247～263 同步沒有帶入 `sw_core/` 變更。PR 258 的 legacy daemon handoff 變更位於 Rust CLI 管理路徑，並未修改 Python daemon。 | `crates/sw-cli/src/legacy_handoff.rs`；本次 `sw_core/` 比對無 Rust daemon 對應的 Python delta | **不需要跟進。** 本次沒有 Python daemon 行為差異。 |
| `sw_core/session_manager.py:4595-4616`（PR 253，已在共同祖先） | DF-25：取消 interactive 命令只撤銷該命令自己的 TX permit；已建立的 lease 可繼續使用。提交前的取消仍由原命令 permit 阻擋。這段程式已存在於合併前整合線，不是本次 merge 新帶入。 | RPC 路徑：`crates/sw-daemon/src/service.rs:771-786` → `CommandRegistry::cancel` → `SessionRequest::CancelQueued`；`crates/sw-core/src/session_core.rs:1970-1981` 只撤銷命令 grant 的 `op_id`。interactive-open 命令與後續 lease send 分別走 `crates/sw-core/src/op_runner.rs:387-394`。ServiceRig 回歸見 `crates/sw-daemon/tests/dispatch.rs:3493-3558`，測試取消原命令後仍以同一 lease 成功送出兩次。 | **不需要新增跟進。** Rust 取消只撤銷原命令 op，後續 lease send 使用新 op；既有 ServiceRig 測試驗證 lease 存活及後續 TX。`contracts/behavior_changes.yaml:1724-1750` 將 DF-25 標為 `IMPLEMENTED`，並要求 Python 與 Rust 都符合 OD-77 B1。 |
| 無；PR 255 只同步契約編號，沒有 Python runtime 變更 | 將 interactive cancel 行為登記為 DF-25，避免與整合線既有 DF-23／DF-24 撞號；該 commit 是本次 merge 的共同祖先。 | `contracts/behavior_changes.yaml:1724-1750`；Rust 對應行為與測試如上 | **不需要 runtime 跟進。** 契約編號調整不改 Python 行為，且不屬本次 merge 的差異。 |

## SPEC-GAPS

無。本次合併帶入的 `sw_core/` 行為差異為零；任務點名的 DF-25 已在合併前基線，Rust 對應路徑與既有 ServiceRig 測試符合其行為要求。
