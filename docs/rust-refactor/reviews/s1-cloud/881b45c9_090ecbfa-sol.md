> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1（含 d7d8e9ed） | 0 | `_wait_dut_tx` 已移除，改為 `_checkpointed_dut_tx`（`tests/contract_blackbox/test_cli_mgmt_daemon.py:178`）：先呼叫 bridge `barrier` 排空非同步 writer，再以 `checkpoint` 的 `wire_len`／`wire_sha256` 封住封閉 TX 區間並核對 SHA-256；零 TX 檢查與 B 輪基線皆改走此路徑。新增 `test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 覆蓋「barrier 前已寫、落盤延遲」案例。 |
| 2 | Medium | 已修 51bd8fa5＋b89e90f7 | 0 | `crates/sw-pal/tests/exec_delegate.rs:237`：只有實際被委派啟動的孫行程才寫 `delegated-ready`（含自身 pid），父行程以 `process_is_running(delegate_pid)` 確認存活後才送事件；b89e90f7 補負向突變 `delegate_ready_rejects_child_stopped_before_exec`（委派前停住 → 不得出現孫行程 ready、逾時 FAIL）。 |
| 3 | Medium | 已修 ddcbdde0／4e3ba13f／2ae832c8 | 0 | `scripts/rust/stage1_exit_driver.sh:438` 結尾改由 `l15_exit_driver.py` 對完整 L1.5 報告做第二層判定，只有全部非 PASS 列精確符合 gate 核准的 OD-30～OD-32 延後規則才不阻擋；空 stdout／摘要無效不得誤判通過（2ae832c8）；`tests/test_rust_stage1_exit_driver.py` 補 driver 層回歸。後續 54d7115d／2bb93064 再接 OD-75 基線缺陷重判。 |
| 4 | High | 已修 12b3a929＋407ed7e3 | 0 | 升級夾具改為隔離 pipx 安裝（`--no-index --find-links=/opt/sw-old` wheelhouse，`run_upgrade_container.py:291`），0.4.0 bundle 自帶 PyYAML；`run-upgrade.sh:162` 強制 runtime `pyvenv.cfg` 含 `include-system-site-packages = false`，否則 rc=3 不建映像。 |
| 5 | Medium | 已修 12b3a929／ac6c4e4e | 0 | `mkbundle.sh` 新增 `--release-bundle`（`:14`、`:178-180`），只有 `--dev` 才帶 `sw-cli/test-fakes`；release plan 配方（`release-0.4.0-plan.md:40`）改用 `--release-bundle --profile release`，release.yml smoke 另掃 `SW_TEST_UPDATE_*` 字串。 |
| 6 | Low | 已修 f9b17848／ce676829 | 0 | `release-0.4.0-plan.md:10` 改寫為「`VERIFIED`，或 `IMPLEMENTED` 且只剩 OD-27～OD-32 允許的 `BLOCKED-OWNER`」，並納入 OD-75 基線缺陷不擋出口的敘述。 |

以上修正皆落在 881b45c9 之前的分支歷史，本輪以 HEAD 現況逐檔核對；未重跑 Docker 升級與 Windows 原生。

## B 新發現

本輪 881b45c9..090ecbfa 共 3 個非 merge commit（12664ee8 實作、276f5e29 契約文件、090ecbfa 回歸 case），無 merge commit。無 High／Medium。

- **S1-090ecbf-1 [Low]** `contracts/behavior_changes.yaml:9` — SC-39 `compatibility_impact` 於 af58b315 與 276f5e29 兩度改寫，頂層 `revision` 仍為 8。先前 DF-02 修訂的編排者裁決要求「依此修訂並遞增 revision」，本次契約變更未比照。無程式消費此值，`tools.contracts check` 全綠，無失敗情境。修法：下次契約修改時將 `revision` +1。
- **S1-090ecbf-2 [Low]** `crates/sw-cli/src/legacy_handoff.rs:522` — 停止前快照只掃一次。若該次掃描恰遇暫時性 `ProcListFailed`／`Unknown` 且無法檢查清單為空，之後每輪 `Partial` 都會因 `new_uninspectable` 不穩定，等滿 330 s 回 `legacy_stop_pending`（舊 daemon 已停、不重啟）。屬防禦性建議，未實測重現。修法：快照掃描結果為 `Unknown`／`Permission` 時重試一次，或在逾時回應標示 `snapshot_status`。
- **S1-090ecbf-3 [Low]** `crates/sw-cli/tests/mgmt_legacy_handoff.rs:650` — 三支新測試以腳本第一筆當快照，只驗「第一次掃描」而非「送出 stop 之前」。若實作把 `uninspectable_before_stop` 誤移到 `send_stop` 之後，測試仍綠。修法：比照 `fail_instances_while` 以 `stop_signal` 驅動替身，讓 555 只在 stop 送出後才出現。
- **S1-090ecbf-4 [Low]** `regression/serialwrap_regression/cases/f08_legacy_upgrade.py:148` — `serialwrapd_pids` docstring 寫「子行程不另計」，實作未做祖先合併（與 `scripts/docker/upgrade_checks.py:238` 的 `scan_daemon_instances` 不同）。目前 POSIX launcher 以 `execve` 取代自身（`crates/sw-pal/src/process/posix.rs:66`），實機不會多算；若日後 launcher 改為 spawn＋wait，本 case 會假 FAIL `two_reader`。修法：沿用 `upgrade_checks` 的祖先合併邏輯，或修正 docstring。
- **S1-090ecbf-5 [Low]** `crates/sw-cli/src/legacy_handoff.rs:478` — `plan.attached_paths` 為空時仍對全部同 uid 行程做一次 fd 掃描當快照，但 `wait_stopped` 在此情況直接回 `Ok`，結果不會被使用。純效能，無失敗情境。修法：`attached_paths` 為空時跳過快照。

## 既有碼待辦（不計入結論）

- `changelog.d/legacy-handoff-uninspectable-holders.md` 檔名無 `<issue>-` 前綴（CLAUDE.md 要求 `changelog.d/<issue>-<slug>.md`）。檔案由 af58b315 建立、本輪 276f5e29 僅改內容；R-09 仍 pass，待擁有者開 issue 後改名即可。

## 對裁決的異議（不計入結論）

無。

---

驗證（唯讀，未碰 production、未改檔，工作樹乾淨）：`cargo test -p sw-pal --test legacy_supervision`（42 passed）、`-p sw-cli --test mgmt_legacy_handoff`（26 passed）、`cargo check --workspace --all-targets` 通過；`pytest` 定向 8 檔（回歸 case 判定、acceptance 鎖檔／schema、regression harness／guards／pluginfiles、resource markers）144 passed；`python3 -m tools.contracts check` ok；`policy_check` 僅 R-19／R-22 warn（非 PR 上下文、3121 筆既有懸空引用）。SC-39 契約文字與 `legacy_handoff.rs`／`posix.rs` 實作逐項對齊（四 uid、停止前快照、`seen_before_stop`、`daemon_scan`、`last_unstable`、`TargetVanished`／`ProcVanished` 不算失敗）；`strict_status` 保持 `serialwrapd_tty_holders` 原判定；`from_pair` 的 `Partial` 轉 `Unknown` 為 fail-closed；README 中英兩段同步更新；cases.yaml `source_ref` 行號 197 與 `@_case` 位置相符；v0.3.2 已含 #100 確定性 COM 配號，實機 case 的「原 COM 重新 attach 同一 tty」判準成立。

結論：可放行
