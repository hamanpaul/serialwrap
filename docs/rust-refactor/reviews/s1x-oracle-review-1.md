# S1X oracle 專項審查 1（Claude Opus，非實作者）

審查基準 HEAD：`d2118cd`（origin/feature/rust-refactor，工作樹 /home/user/wt-s1，detached）。未修改 repo 任何檔案；所有實驗都在 scratchpad `exp/` 下以 `git archive HEAD` 展開的副本中進行，逐項還原修正後實測。
實驗前提：`tests/test_rust_win_scripts.py` 的模組層 skipif 需要 `cmd.exe`，因此在 PATH 放一個假的 `cmd.exe`（exit 1）才能跑 SC-18 的 monkeypatch 單元案例；這些案例不呼叫 cmd.exe。

背景事實（影響 3、4 的判斷）：
- Stage 1 的 daemon 仍是 Python `sw_core`。`crates/sw-daemon/src/launcher.rs:60` 只負責啟動 python 實作，crates/ 裡沒有 Rust 版 session 狀態機。所以 SC-18 案例裡「多久進 READY」是 Python 基線自己的行為，Rust CLI 不負責。
- Python passthrough 要在 ATTACHED 後連續 `PASSTHROUGH_READY_QUIET_S = 30.0` 秒（`sw_core/constants.py:79`）沒有 RX 才升 READY。還沒 READY 就送命令，會在進 arbiter 之前被回 `SESSION_NOT_READY`（`sw_core/service.py:780`），被拒的請求在結構上就是零 TX。

---

### CHG-S1X-SC18-TX-BASELINE-01
VERDICT: ACCEPT

理由：
- 新判準忠於 Python 外部行為。`sw_core/uart_io.py` 的 `send_command` 是 `payload = cmd.encode(...)`，結尾不是 `\n` 才補 `\n`（約 2361–2363 行），不會送 `\r`。所以輸入 `cmd submit --selector COM0 --cmd "echo X"` 時，DUT 端預期收到 `b"echo X\n"`（hex `6563686f20580a`），與 addenda 第 98 列記錄的 Python 實測一致。原斷言 `count(b"echo X\r") == 1` 對正確實作會永遠 FAIL（假 FAIL），不是放寬。
- 新判準比舊的更嚴。`tests/contract_blackbox/test_cli_mgmt_daemon.py:450` 在案例開始時記下 TX 長度當基線，`:457` 要求基線之後的 TX 位元組必須恰好等於 `b"echo X\n"`。舊斷言只數子字串次數，夾帶其他 TX 也會過；新斷言下，任何多出的 TX（例如 attach 時送 probe、命令送兩次、行尾錯）都會 FAIL。
- 基線只會造成假 FAIL，不會造成假 PASS。若同一 B 輪前一個案例的 TX 晚於基線才落盤，結果是多出位元組而 FAIL。
- 回歸測試在變更前確實會紅。實測把 `:457` 改回整份 wire 比對（`_dut_tx_bytes(wire).count(b"echo X\n") == 1`，即沒有基線），`test_windows_com_autostart_counts_only_its_own_tx` FAIL。HEAD 上 10 個 `windows_com_autostart*` 全數 PASS。
- 可追溯：`cc44df8`（斷言＋測試）、`7298a21`（docstring 與 plans/05:1356 更正為 `\n`）、`28ba96d`（addenda 登記）都對得上。

### CHG-S1X-SC18-WIRE-FLUSH-01
VERDICT: ACCEPT

理由：
- 成功路徑：`_wait_dut_tx`（`test_cli_mgmt_daemon.py:143`）最多等 `_WIRE_FLUSH_WAIT_S = 10.0`（`:139`），等到基線後的 TX 長度達到預期，再 settle 1 s 重讀，最後仍要求恰好等於 `b"echo X\n"`（`:457`）。這只處理 bridge EvidenceWriter 非同步落盤造成的「CLI 已回傳、TX 列還沒寫入」，等待之後判準沒有放寬。命令沒送、送兩次、多出位元組都仍然 FAIL。settle 之後才出現的 TX 不會被看到，但 CLI 行程此時已結束、不可能再重送，所以變更前後這個窗口都存在，沒有變寬。
- 回歸測試在變更前確實會紅。實測把 `:457` 改回立即讀取（`_dut_tx_bytes(wire)[tx_baseline:] == b"echo X\n"`），`test_windows_com_autostart_waits_for_lagging_wire_record` FAIL。
- 被拒請求的零 TX 檢查移到 `:198`（READY 等待之後、唯一一次送出之前），方向正確。
- 可追溯：commit `0789e1f`。addenda 第 99 列沒有寫 commit hash，要用 `git log -S` 才找得到（Low，建議補上）。

Low（不擋）：`:197` 註解寫「READY 等待（≥30 秒）」，但這不是由程式保證的。等待時間取決於被拒請求在 30 s 靜默窗口中的哪個時點，可能遠小於 30 s。我在假 run_cli 中做了反例：首次被拒的請求延遲 0.3 s 才落盤 `echo X\n`、`session list` 立即回 READY、最後一次送出沒有任何 TX，結果案例 PASS（探針測試在 scratchpad `probe/test_zz_probe_delayed_reject.py`）。真機上，Python 拒絕路徑（`service.py:780`）在結構上不會產生 TX，加上 READY 前必然有相當長的靜默，這個組合實際上不會發生，所以不判 REJECT。建議修法：在 `:198` 之前加一段 `time.sleep(_WIRE_SETTLE_S)`；若更保守，確保距離被拒回傳已經過 ≥ `_WIRE_FLUSH_WAIT_S`。再補一支「被拒請求 TX 延遲落盤＋READY 立即＋最後一次送出遺失」必須 FAIL 的回歸測試。

### CHG-S1X-SC18-FIRST-SUBMIT-01
VERDICT: ACCEPT

理由：
- 首次 `cmd submit` 現在接受兩種結果（`test_cli_mgmt_daemon.py:157–208`）：(a) 直接 rc 0，也就是 plans/05:1356 的原驗收形狀；(b) `error_code == "SESSION_NOT_READY"`（`:178`），此時被拒請求零 TX，之後一次送出恰好一次 TX。`TIMEOUT`、非 JSON 等其他結果一律 FAIL，有參數化測試 `test_windows_com_autostart_rejects_unexpected_first_failure` 覆蓋，實測 PASS。
- (b) 忠於 Python 外部行為。passthrough profile 在 attach 後必須靜默 30 s，因此 on-demand 首次啟動後重送的那一次必然碰到 `service.py:780` 的 `SESSION_NOT_READY`，而且零 TX。f2d9533 把「首次必回 rc 2」寫成硬性斷言反而太窄：它排除了合法的 (a)。規格與 plans/05 Task 7 第 4 點只規定「連線階段被拒才自動啟動並重送一次」，沒有規定 CLI 要等到 READY。
- READY 等待 45→120 s 不會掩蓋真實延遲回歸，原因有三：
  1. 被等待的是 Python daemon 的 30 s 靜默升級，不是 Rust 程式碼。
  2. 規格 §6 與 OD-03／OD-12 都沒有對「首次需求啟動到 READY」訂時限，這個案例本來就不是延遲 oracle。45 s 只比 30 s 多 15 s，要容納 Windows 上 launcher 啟動、COM attach 與高負載排程，原本就太緊。
  3. attach 失敗（DETACHED 且帶 last_error）仍會立即 FAIL（`:190`）。
  若日後要約束 autostart→READY 的時間，應另立 OD 並寫成獨立斷言，不應藏在 wait 上限裡。
- 這次變更本身沒有放寬，(b) 的零 TX 是新增的收緊。但有一個既有的覆蓋缺口要記錄（Medium，建議後續處理）：在固定的 COM20 passthrough profile 下，(a) 實際上走不到，所以 L1.5 從來沒有驗到「SC-18 重送的那一次請求把原 payload 恰好送出一次」。最後那一次 TX 來自案例自己的第二次 `cmd submit`，不是 CLI 的重送。L1 的 `crates/sw-cli/tests/mgmt_daemon.rs:202 autostart_only_when_request_never_sent` 只驗 `session.list` 的請求數 = 1，沒有驗重送的 params 與原請求逐位元組相同。這個缺口在 f2d9533 就已存在，不是本項造成的。建議修法：在 mgmt_daemon.rs 補一支以 `command.submit` 連線被拒→自動啟動→重送的 L1 測試，斷言重送 params（含 `cmd` 與 selector）與原請求完全相同，且請求數為 1。
- 首次被拒時，f2d9533 原本斷言 `session.state ∈ {ATTACHING, ATTACHED}`，97d1d8f 移除了這個檢查。替代保護是 `:190` 的 DETACHED＋last_error 立即 FAIL，加上 READY 逾時 FAIL，影響可忽略。
- 可追溯：commit `97d1d8f`（案例＋4 支單元測試）、addenda 第 90 列（`e7548ab` 登記）。addenda 列沒有寫 commit hash（Low）。

### CHG-S1X-SPIKE3-ROTWAIT-01
VERDICT: ACCEPT

理由：
- 被撤除的條件是 `rotation_completed_ns − rotation_requested_ns ≤ 600 ms`（`721df38` 刪掉的 `scripts/rust/stage1a_gate.py` 約 926–935 行）。這個條件找不到授權來源：
  - OD-22 的 600 ms／p99 100 ms 只寫明適用於「WAL append 延遲樣本」。
  - OD-19 (1) 規定「寫入路徑不得因 rotate／unlink 阻塞」，(2) 對 rotate 只要求「最終完成」。
  - Python 基線本來就把輪替延到之後的 append 非阻塞重試（`sw_core/wal.py:32` 註解、`:242 _schedule_rotate_retry_locked`）。跨多次 append 才完成輪替正是規格要的行為，把 600 ms 套在 request→completion 上會把合規實作判成 FAIL。
- 撤除後不會讓遺失資料過關。資料完整性在同一函式內有獨立檢查（`stage1a_gate.py` 約 458–600 行）：
  - 記錄數與序號：`records == append_count + continuity_meta_count`、`first_seq == 1`、`last_seq == records`、`seq_continuous`。
  - 錯誤計數：`crc_errors == []`、`write_failures == 0`、`deferred_delete_pending == 0`、`pre_rotate`／`post_rotate` 皆 ok。
  - rotation 本身：`rotation_completion_succeeded is True`；request 與 completion 都必須落在各自 append 時間窗內，且時間窗不得超過該 append 的延遲樣本（樣本仍受 OD-22 上限約束）；completion append 不得早於 request append，且必須在本次 workload 的樣本範圍內。
  - oplock break 必須在 lease 內通知，ACK 必須涵蓋到期後的完成。
  永遠不完成的 rotation 仍然 FAIL：`test_spike3_oplock_unbounded_completion_cannot_pass`（`tests/test_rust_stage1a_gate.py:1530`）在 HEAD PASS。
- 回歸測試在變更前確實會紅。實測把 `721df38` 對 `stage1a_gate.py` 的修改反向套用，`test_spike3_rotation_wait_beyond_append_bound_is_not_an_od22_failure`（`:1550`）FAIL；HEAD 上 PASS。案例具體設定：第 1 次 append 提出 rotation、第 3 次 append 完成，各樣本 300 ms／200.001 ms 都 ≤ 600，合計 700 ms，預期 PASS。
- 同檔另一支 `test_junit_provenance_plugin_records_head_require_cargo_and_argv` 在副本中 FAIL，原因是 `git archive` 副本沒有 `.git`，`git rev-parse` 失敗。在 wt-s1 原地單獨跑是 PASS，屬實驗環境因素，與本項無關。
- 可追溯：commit `721df38`、addenda 第 89 列。

### CHG-S1X-L1-READD-REVERT-01
VERDICT: ACCEPT

理由：
- `68d0941` 對 `tests/acceptance/l1_required.lock.json` 新增 19 列。`e66e190` 逐名撤回其中 10 列，數目與分類都和 addenda 第 87 列一致：
  - `s1b_cargo.linux` 6 列：`real_mkbundle_dev_output_verifies`、`windows_switch_running_exe_rename`、`cap_windows_native__launcher_gui_subsystem`、`cap_windows_native__launcher_parent_kill_kills_child`、`cap_interactive_desktop__exec_or_wait_ctrl_c`、`cap_supervisor_runtime__systemd_hold_blocks_restart_on_failure`。
  - `s1b_cargo.windows` 4 列：`real_mkbundle_dev_output_verifies`、`windows_switch_running_exe_rename`、`cap_interactive_desktop__exec_or_wait_ctrl_c`、`cap_supervisor_runtime__windows_task_roundtrip`。
- 逐一 grep 確認這 10 個 `set:platform:nodeid` 三元組都已在 `.github/workflows/tests.yml` 的核准移除清單中（例如 571、572、587、591、592、594 行），而且在 `68d0941^` 就已存在。撤回只是回到 OD-16 (1)／OD-17 已裁決的狀態，不是新的未授權移除。
- 保留的 9 列：真實新增 4 列（`start_windows_python_launcher_uses_manifest_runtime` ×2、`proc_scan` ×2）＋5 列 `#[ignore]`。5 列 `#[ignore]` 是無三元組授權的待裁決項，留在鎖中屬 fail-closed：必要列沒有執行只會不合格、不會放行。已正確升級為 PENDING-OD-S1X-L1-IGNORED-READD（addenda 第 88 列）。
- 回歸測試在變更前確實會紅。實測把 `e66e190` 對鎖檔的修改反向套用，`tests/test_l1_judge.py::test_l1_lock_frozen_set_excludes_capability_gated`（`:748`）FAIL；HEAD 上 `test_l1_judge.py` 22/22 PASS。
- 可追溯：commit `e66e190`、addenda 第 87 列（`e7548ab` 登記）。addenda 列沒有寫 commit hash（Low）。

### 0e97195（Spike 3 oplock 替身依 rename 請求時點觸發 break）
VERDICT: ACCEPT

理由：
- 這個 commit 只改 `tests/test_stage0_wal_av_sim.py` 的替身，judge（`scripts/stage0/spike3_win_wal.py` 的 `_collect_oplock_variant`）與各 failure point 的期望值都沒有改。
- 新替身比舊的更貼近真實 oplock 語意：rename 請求開檔時才觸發 break 通知（`:507`），helper 持有到 lease 到期才 close／ACK，rename 隨後完成。舊替身把 break 寫死在 lease 起點 +5 ms、lease 只有 10 ms，負載下 rotation 請求晚於 5 ms 就會被 judge「正確地」判成 INSUFFICIENT，這是替身本身的時序缺陷。
- lease 放寬為 200 ms 後，append 2 延遲約 215 ms，仍 ≤ 600 ms。樣本共 403 個，p99 不受這一個離群值影響。所有既有斷言（`rotation_requested_ns < lease_until_ns < rotation_completed_ns`、時間窗包含關係等）都保留。`break-after-expiry`、`records`、`drain`、`close` 四個 failure point 仍各自得到 FAIL／INSUFFICIENT。
- 回歸測試在變更前確實會紅。實測把 0e97195 反向套用，再只加入 `:406` 的 `real_sleep(0.015)` 排程延遲，`test_spike3_oplock_variant_runs_append_rotation_and_f12` 3/3 次 FAIL；HEAD 上該案例連跑 30＋ 次全數 PASS。
- 可追溯：commit 訊息寫明紅燈基準（`ee812ab`）與根因，但 addenda 沒有對應 CHG 列（替身修正不屬 oracle 變更，可以接受）。

**High（後續必修，不影響本 commit 的可接受性）：同一根因在相鄰測試沒有修。** `tests/test_stage0_wal_av_sim.py::test_spike3_records_no_delete_primary_and_oplock_variant_separately` 的 `StubHelper` 仍把 `break_notified_ns` 寫死為 `lease_acquired_ns + 25_000_000`（`:317`），lease 為 50 ms（`:306`）。只要 rotation 請求晚於 25 ms 就會得到 INSUFFICIENT_EVIDENCE。實測在 HEAD 上 `-k oplock` 連跑 20 次有 2 次 FAIL（run 10、13，訊息 `'INSUFFICIENT_EVIDENCE' == 'PASS'`），另一輪 9 次中也有 1 次。這違反 CLAUDE.md「不得引入新的測試失敗」，以及「已知 flaky 不得當放行理由」。
確切修法：在該測試的 `StubHelper`／`DelayedRenameFs`（`:288–330`）套用 0e97195 的同一模式：
1. 共用 `lease_state = {}`。
2. `arm_hold` 寫入 `lease_state["until_ns"]`，lease 改為 200 ms。
3. `DelayedRenameFs.rename_noreplace_or_suffix` 先記 `lease_state["break_ns"] = perf_counter_ns()`，再 sleep 到 `until_ns` 之後 +2 ms 才委派。
4. `stop()` 的 `break_notified_ns` 改用 `lease_state["break_ns"]`，`close_started_ns` 保持 `lease_until_ns`。
5. 在 rotation 請求前常駐 `real_sleep(0.015)`，讓這個時序常駐重現。

---

### 摘要
| # | 項目 | 判定 |
|---|---|---|
| 1 | CHG-S1X-SC18-TX-BASELINE-01（cc44df8） | ACCEPT |
| 2 | CHG-S1X-SC18-WIRE-FLUSH-01（0789e1f） | ACCEPT（Low：零 TX 檢查前補 settle 並加回歸測試） |
| 3 | CHG-S1X-SC18-FIRST-SUBMIT-01（97d1d8f，READY 45→120 s） | ACCEPT（Medium 後續：L1 補「重送 params 與原請求相同」） |
| 4 | CHG-S1X-SPIKE3-ROTWAIT-01（721df38） | ACCEPT |
| 5 | CHG-S1X-L1-READD-REVERT-01（e66e190） | ACCEPT |
| 6 | 0e97195（oplock 替身 break 時點） | ACCEPT（High 後續：相鄰測試 `test_spike3_records_no_delete_primary_and_oplock_variant_separately` 同根因仍 flaky） |

另外：addenda 第 87、89（僅寫測試名）、90、99 列都沒有寫實際 commit hash，要靠 `git log -S` 追溯，建議補上（Low）。

## 彙整者抽查（Claude Opus routine 主實例）

- High（`tests/test_stage0_wal_av_sim.py:317` StubHelper break 寫死 +25 ms）已獨立重現：HEAD d2118cd 上 `-k records_no_delete_primary_and_oplock_variant_separately` 連跑 20 次 1 次 FAIL。
