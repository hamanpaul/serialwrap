> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

前輪 6 項對應的修正 commit 都已在 6b36a383 的歷史中，並以 HEAD 現況逐項核對（修正早於本輪範圍 090ecbfa，但本輪為首次就此報告做增量核對）。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1 | 0 | `_checkpointed_dut_tx`（tests/contract_blackbox/test_cli_mgmt_daemon.py:178）改為先呼叫 bridge `barrier` 再取 `checkpoint` 的 `wire_len`／`wire_sha256`，以封閉前綴做最終比對（:519 斷言 `final_tx == b"echo X\n"`）；另有 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 覆蓋延遲落盤。 |
| 2 | Medium | 已修 51bd8fa5、b89e90f7 | 0 | ready 標記改由 `exec_or_wait` 實際啟動的孫行程寫出（含 pid，crates/sw-pal/tests/exec_delegate.rs:237）、父行程確認 pid 存活後才送事件；b89e90f7 補「委派前停住、不建孫行程」突變案例 `delegate_ready_rejects_child_stopped_before_exec`，要求逾時不得 PASS。 |
| 3 | Medium | 已修 ddcbdde0、4e3ba13f（2bb93064 再收緊） | 0 | 原始退出碼保留，由 `scripts/rust/l15_exit_driver.py` 對完整報告做第二層逐列核對，只有全部非 PASS 列精確符合核准延後規則才不擋出口；`tests/test_rust_stage1_exit_driver.py` 有 driver 整合回歸。 |
| 4 | High | 已修 12b3a929 | 0 | 升級容器移除 `--system-site-packages`／系統 `python3-yaml`，舊版走隔離 pipx＋wheelhouse；`mkbundle.sh --release-bundle` 把 PyYAML 納入隔離 runtime，`scripts/docker/run-upgrade.sh:162` 強制 `pyvenv.cfg` 含 `include-system-site-packages = false`。 |
| 5 | Medium | 已修 ac6c4e4e（配合 mkbundle `--release-bundle`） | 0 | `scripts/rust/mkbundle.sh:178-180` 只在 `--dev` 編入 `test-fakes`；release 計畫 §2 改用 `--release-bundle --profile release`；`tests/test_release_workflow.py` 驗正式產物不帶測試鉤子。 |
| 6 | Low | 已修（release 計畫改寫） | 0 | docs/rust-refactor/release-0.4.0-plan.md:10 已改為「`VERIFIED`，或 `IMPLEMENTED` 且只剩 OD-27～OD-32 允許的 BLOCKED-OWNER」，不再要求不存在的 `overall: PASS`。 |

## B 新發現

本輪 4 個 commit（5e20b250、85620f8f、0e9c3d81、6b36a383，無 merge commit）核對結果：

- 5e20b250 的實作與編排者裁決（2026-11-07）逐點對應：`UninspectableSnapshot::from_scan` 只以 pid＋start_ticks 放行、不看 uid；停止後才出現者不論 `UidMatch` 都推 `new_uninspectable`；快照掃描有其他失敗時 `Failed` 且 `admits` 恆 false（fail-closed）；`uart_holders_same_uid` 掃描範圍不變（四 uid 全異者仍排除）；`serialwrapd_tty_holders` 的 `strict_status` 未動。兩支新紅燈測試（`legacy_d5_2_before_stop_snapshot_failure_admits_nothing`、`posix_uart_holder_scan_reports_setuid_uninspectable_process_as_partial`）以 090ecbfa 的程式靜態推演確實會紅（前者舊 `uninspectable_before_stop` 會把 `Permission(listed)` 附帶清單當快照放行；後者 setuid 行程落在 `Unconfirmed` 而回 `Permission`）。改寫的兩支測試保留原名、斷言由「掃描層 Permission」改為「列入 Partial 且不被快照放行」，與裁決一致，非削弱。
- 85620f8f：README 英、中兩段同步改寫；`contracts/behavior_changes.yaml` SC-39 的 `compatibility_impact` 更新（該檔條目無 `revision` 欄位，與前一輪 276f5e29 作法相同）；changelog fragment、plan 05、spec-reconciliation、release 計畫一致。`python3 -m tools.contracts check` 全綠。
- 0e9c3d81：`issues=("#257",)` 格式與其他 case 一致，`gh issue view 257` 為 OPEN 且標題相符；`pending_issue` 移除後 `register()` 規則仍成立（定向 pytest 通過）。
- 6b36a383：lock diff 純新增（無刪除、無分母改動），25 個 nodeid 全部登在 `s1a_cargo/linux` 與 `s1b_cargo/linux`，未登進 windows 集合（`#[cfg(unix)]` 測試不會造成 Windows L1 證據不足）；case map 列出的測試名稱全部存在於對應 Rust 檔；`regen_lock.py --check` OK。

**S1-6b36a38-1 [Low]** crates/sw-cli/src/legacy_handoff.rs:526 — 停止前快照只掃一次；該次掃描若恰逢瞬時 `Unknown`／`Permission`（例如 `/proc` 列舉中途失敗、某行程部分 fd 條目讀取失敗），整份記 `Failed`，之後每輪 `Partial` 都推 `new_uninspectable`，必然等滿 330 秒回 `legacy_stop_pending`，等於回到 #257 修前的體驗。fail-closed 方向正確，但無重試機會。未找到實機上穩定可觸發的情境，故列 Low。— 確切修法：快照掃描失敗時在送出 stop 前重試有限次數（例如 3 次、間隔 `POLL`），全部失敗才記 `Failed`；逾時回應的 `before_stop_snapshot` 另附失敗原因（`holders_status` 字串），並補「第一次快照失敗、第二次成功則照常放行」的 sw-cli 回歸。

其餘無 High／Medium：**NO FINDINGS**（High／Medium 層級）。

## 既有碼待辦（不計入結論）

- `crates/sw-pal/tests/legacy_supervision.rs` 的 `posix_uart_holder_scan_covers_any_same_uid_process_and_skips_other_uids`（c2379b58 新增，D5.2 掃描範圍測試）未登記在 `sc39-legacy-daemon-handoff` 的 case map 與 L1 lock；本輪 add-only 登記未納入，屬既有疏漏，可於下次 `--l1-add-only` 補登。
- `tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence` 依賴未追蹤的 `target/p4-evidence`，在乾淨 worktree 必紅（本機 28 項缺檔，全部與本輪 diff 無關）；屬環境依賴，由編排者 host 全套基線判定。

## 對裁決的異議（不計入結論）

無。

---

驗證：`cargo test -p sw-pal --features test-fakes --test legacy_supervision` 29 passed；`cargo test -p sw-cli --features test-fakes --test mgmt_legacy_handoff` 45 passed（`CARGO_TARGET_DIR=/tmp/solrev-s1-legacy-handoff-takeover-target`）；`tools.contracts check` ok；`regen_lock.py --check` OK；定向 pytest 5 檔 131 passed、1 failed（上述環境依賴）；policy check 25 pass、0 fail、2 warn（R-19、R-22，非 PR 上下文）。commit 訊息所述紅燈紀錄位於未追蹤的 `target/s1-pr250/round2/`，未能直接讀取，改以靜態推演確認。未修改任何檔案、未碰 production daemon，工作樹乾淨。

結論：可放行
