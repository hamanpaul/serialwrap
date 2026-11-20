# P5-T20 黑箱 A/B 交付報告（release artifact；F103）

- 日期：2026-10-28（cloud session，root 執行；4 核）。分支 `feature/rust-refactor-p5` @ `4f4082f`。
- 受測 artifact（§15.9.1，`cargo build --release -p sw-daemon -p sw-cli --target x86_64-unknown-linux-gnu`，未帶 test-fakes）：

| 檔案 | sha256 |
|---|---|
| `target/x86_64-unknown-linux-gnu/release/serialwrap` | `6eca14be3ecdfd1a1233ed54d4714ff8427d46eb4af1f02b05a2f7f52f7c0885` |
| `target/x86_64-unknown-linux-gnu/release/serialwrapd` | `2ea74890af3965207552bfcc4d8225ac63128ff8af8d49180750726e4968c509` |

- Python 基線：本分支 `serialwrapd.py`（`tests/contract_blackbox/bin/py-serialwrapd`）＋Python CLI，python 3.12.3。另以 `origin/feature/rust-refactor`
  （`32f6dd1`，含 #64／OD-19 等 R-00 所述較新的 `sw_core`）的 Python daemon 重跑失敗的 10 案，結果相同（10 failed），故以下差異**不是** R-00 基線落後所致。

## 命令

```bash
# Python 基線（兩輪）
python3 -m pytest -q tests/contract_blackbox/slice21 -m cap_posix_pty -rA --junitxml=<py{1,2}.xml>
# Rust（兩輪；Rust 標籤須以 ELF CLI 執行，conftest 的 artifact 身分檢查）
R=target/x86_64-unknown-linux-gnu/release
SERIALWRAP_CLI_EXE=$R/serialwrap SERIALWRAP_DAEMON_EXE=$R/serialwrapd SERIALWRAP_AB_LABEL=rust-s21 \
  python3 -m pytest -q tests/contract_blackbox/slice21 -m cap_posix_pty -rA --junitxml=<rs{1,2}.xml>
# 既有套件（Rust 以 env 指向 release；Python 輪 env 不設＝原本路徑）
SERIALWRAP_CLI_EXE=$R/serialwrap SERIALWRAP_DAEMON_EXE=$R/serialwrapd python3 -m pytest -q tests/test_multiagent_e2e.py
SERIALWRAP_CLI_EXE=$R/serialwrap SERIALWRAP_DAEMON_EXE=$R/serialwrapd python3 -m pytest -q tests/test_human_agent_coexist.py \
  -k "t2_wal_reset_seq_resets_to_zero or t3_wal_current_seq_matches_live or t4_bind_idempotent_ready_session"
for c in sm-01-attach-to-ready ha-05-concurrent-submit sm-04-device-unplug; do
  SERIALWRAP_CLI_EXE=$R/serialwrap SERIALWRAP_DAEMON_EXE=$R/serialwrapd python3 func-test/runner.py --case $c --report-dir <dir>/$c; done
```

## 結果摘要

| 項目 | Python 輪 1 | Python 輪 2 | Rust 輪 1 | Rust 輪 2 |
|---|---|---|---|---|
| slice21 黑箱（46 案） | 33 passed／10 failed／3 skipped | 同左（失敗集合相同） | 36 passed／8 failed／2 skipped | 同左（失敗集合相同） |
| `tests/test_multiagent_e2e.py` | 1 passed | — | 1 passed | — |
| `tests/test_human_agent_coexist.py` t2／t3／t4 | 3 passed | — | 3 passed | — |
| func-test `sm-01`／`ha-05`／`sm-04` | 3/3 PASS | — | 3/3 PASS | — |

skipped：`test_slice_scope.py::test_deferred_methods` 只在 Rust 標籤執行（Python 輪 skip）；`test_external_open_blocked_then_release_attach_handoff`、
`test_two_daemons_same_tty` 於 root 下以 EVIDENCE_MISSING skip（TIOCEXCL 對 root 無效，§6.2）——兩者非 root 證據屬 BO-S21-04／05。
另：以 shell wrapper `py-serialwrapd` 指定 `SERIALWRAP_DAEMON_EXE` 跑既有套件的 Python 輪會被身分檢查以 `artifact mismatch`（實際 `/usr/bin/dash`）拒絕，屬檢查正確運作；Python 輪依計畫不設 env。

**結論：Task 20 未完成。** Rust 與 Python 在 7 案行為一致但皆不符案例斷言（案例或計畫表的前置／期望與基線不符），1 案為案例 TX 窗口設計缺陷、
2 案為 Python 基線缺陷候選或計時敏感、1 案為只適用 Rust 的列。皆不放寬斷言、不改為 skip，列入待裁決（`slice21_gate_expect.yaml` `PA-OD-T20`）。

## 失敗案例分類

| nodeid | Python | Rust | 實測差異與根因 | 分類／處置 |
|---|---|---|---|---|
| `test_f3_observability.py::test_device_error_names_selector` | FAIL | FAIL | 兩邊 stderr 皆 `serialwrap: session.attach failed: SESSION_NOT_FOUND`（stderr 由 CLI 產生，P4 已 1:1）；selector 在 stdout JSON。案例依計畫表「`_stderr` 含 NOSUCH」，但其承接的 TestPilot `f3-device-error-names-selector` oracle 為「stderr **或** stdout JSON 任一含 selector」 | 計畫表斷言比 F3 oracle 嚴：待裁決（改計畫表與案例為 oracle 原文，或 CLI 擴充 stderr＝新 SC） |
| `test_safety_core.py::test_recovering_submit_session_recovering` | FAIL | FAIL | FakeTarget `stop()` 後立刻 submit，兩邊 daemon 都尚未觀察到 I/O error，皆回 `ok:true`／accepted | 案例前置未建立（無同步點）：待裁決（改為等到 daemon 觀察到錯誤再 submit） |
| `test_safety_core.py::test_reject_paths_zero_tx[SESSION_RECOVERING]` | FAIL | FAIL | 同上 | 同上 |
| `test_safety_core.py::test_reject_paths_zero_tx[READY_UNCONFIRMED]` | FAIL | FAIL | `pause_responding`＋`session recover` 使 session 離開 READY，兩邊皆回 `SESSION_NOT_READY`；`READY_UNCONFIRMED` 只出現在 #162 再確認 pending 逾時／逾次 | 案例前置不會產生該狀態：待裁決 |
| `test_safety_core.py::test_reject_paths_zero_tx[AUTOBOOT_QUIET]` | FAIL | FAIL | `banner_target` 開機即送 banner，session 未曾 READY，兩邊皆 `SESSION_NOT_READY`（`boot_quiet_remaining_s≈179`）；同檔 `test_zero_tx_while_autoboot_quiet`（READY 後才送 banner）兩邊皆 PASS | 案例前置不符：待裁決 |
| `test_safety_core.py::test_reject_paths_zero_tx[SESSION_INTERACTIVE_BUSY]` | FAIL | FAIL | lease 期間 submit 兩邊皆 accepted；計畫表寫「lease 期間的**執行期**拒絕」，案例卻斷言 submit 回應 | 案例檢查點錯置：待裁決（改為斷言命令終態 error_code） |
| `test_safety_core.py::test_stale_context_not_replayed` | FAIL | FAIL | release→attach 後以舊 `interactive_id` `interactive-send` 兩邊皆 `ok:true`、`bytes:10` | 計畫要求 `INTERACTIVE_NOT_FOUND`，基線不是：Python 基線缺陷候選（失效上下文回放，§11.3 不變量）或計畫錯誤，待裁決；Rust 與基線一致，未改 |
| `test_f2_backpressure.py::test_recovery_flushes_queue` | FAIL | PASS | 探針實測 5/5：Python 於 recover 的 Ctrl-C 讓前景 `sleep 30` 看到 prompt 完成後，arbiter worker 先取出並**送出** `echo q0`，recover 的 flush 只終結 q1～q4；Rust 5 筆皆 `FLUSHED_BY_RECOVERY`、Ctrl-C 後零 TX | Python 基線缺陷候選（#156 原子 flush 的殘餘競態，TestPilot F2 oracle 意圖為原子終結）：DF 候選，待擁有者登記；Rust 維持較安全行為 |
| `test_safety_core.py::test_reject_paths_zero_tx[SESSION_QUEUE_FULL]` | FAIL | PASS | Python 輪以 Python CLI 逐筆 submit，填滿 256 筆耗時超過前景 `sleep 30`，窗口內佇列開始排出（`echo fill0…fill15`）；Rust CLI 較快，30 s 內完成 | 計時敏感（前景時長 < 填佇列時間）：待裁決（前景改為不會自行結束的命令） |
| `test_safety_core.py::test_reject_paths_zero_tx[METHOD_NOT_FOUND]` | FAIL | PASS | Python 已實作 `file.push`（Slice 2.4），缺參數回 `INVALID_ARGS`；`METHOD_NOT_FOUND` 只對 Rust Slice 2.1 範圍成立 | 列只適用 Rust：待裁決（不得自行改 skip） |
| `test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]` | PASS | FAIL | 探針：`pause_responding(15)` 後 FakeTarget 仍回應 `\n` probe，兩邊 reopen 後都立刻送 `\n`＋`echo __READY__`；Python 在 attach RPC 回覆前送出，Rust 晚約 40 ms，落入案例「拒絕後 1 s 零 TX」窗口 | 案例窗口把合法的 attach 登入 TX 算進拒絕路徑：待裁決（窗口應排除 attach 序列或等 probe 結束）；非 Rust 對 Python 的行為差異 |

## 逐 nodeid 結果

| nodeid（`tests/contract_blackbox/slice21/` 下） | Python 輪 1 | Python 輪 2 | Rust 輪 1 | Rust 輪 2 |
|---|---|---|---|---|
| `test_f12_diagnostics.py::test_f12_version_reported_consistent` | PASS | PASS | PASS | PASS |
| `test_f12_diagnostics.py::test_f12_wal_path_live_marker_crc` | PASS | PASS | PASS | PASS |
| `test_f1_cmd_contract.py::test_cmdid_survives_timeout` | PASS | PASS | PASS | PASS |
| `test_f1_cmd_contract.py::test_limits_queryable` | PASS | PASS | PASS | PASS |
| `test_f1_cmd_contract.py::test_near_limit_no_logout` | PASS | PASS | PASS | PASS |
| `test_f1_cmd_contract.py::test_newline_rejected` | PASS | PASS | PASS | PASS |
| `test_f1_cmd_contract.py::test_too_long_rejected` | PASS | PASS | PASS | PASS |
| `test_f2_backpressure.py::test_history_bounded` | PASS | PASS | PASS | PASS |
| `test_f2_backpressure.py::test_queue_full_backpressure` | PASS | PASS | PASS | PASS |
| `test_f2_backpressure.py::test_recovery_flushes_queue` | FAIL | FAIL | PASS | PASS |
| `test_f2_backpressure.py::test_rx_window_crossing_prompt` | PASS | PASS | PASS | PASS |
| `test_f3_observability.py::test_cmd_fail_observable` | PASS | PASS | PASS | PASS |
| `test_f3_observability.py::test_device_error_names_selector` | FAIL | FAIL | FAIL | FAIL |
| `test_f3_observability.py::test_fail_error_code` | PASS | PASS | PASS | PASS |
| `test_f3_observability.py::test_log_tail_latest` | PASS | PASS | PASS | PASS |
| `test_f4_session_semantics.py::test_activity_classification` | PASS | PASS | PASS | PASS |
| `test_f4_session_semantics.py::test_background_result_tail_consistent` | PASS | PASS | PASS | PASS |
| `test_f4_session_semantics.py::test_interactive_line_cmd_defined` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_attach_not_ready_last_error_field` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_credentials_unresolved_no_empty_login` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_daemon_stop_from_second_connection_during_force_recover` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_external_open_blocked_then_release_attach_handoff` | SKIP | SKIP | SKIP | SKIP |
| `test_safety_core.py::test_passthrough_quiet_ready_and_cmd` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_pipelined_stop_after_client_gone_not_processed` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_recovering_submit_session_recovering` | FAIL | FAIL | FAIL | FAIL |
| `test_safety_core.py::test_reject_paths_zero_tx[AUTOBOOT_QUIET]` | FAIL | FAIL | FAIL | FAIL |
| `test_safety_core.py::test_reject_paths_zero_tx[CMD_CONTAINS_NEWLINE]` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_reject_paths_zero_tx[CMD_TOO_LONG]` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_reject_paths_zero_tx[INVALID_COMPLETION_REGEX]` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_reject_paths_zero_tx[METHOD_NOT_FOUND]` | FAIL | FAIL | PASS | PASS |
| `test_safety_core.py::test_reject_paths_zero_tx[PROFILE_NOT_COMMAND_CAPABLE]` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_reject_paths_zero_tx[READY_UNCONFIRMED]` | FAIL | FAIL | FAIL | FAIL |
| `test_safety_core.py::test_reject_paths_zero_tx[SESSION_INTERACTIVE_BUSY]` | FAIL | FAIL | FAIL | FAIL |
| `test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]` | PASS | PASS | FAIL | FAIL |
| `test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-DETACHED]` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-PASSTHROUGH]` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-RELEASED]` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_reject_paths_zero_tx[SESSION_QUEUE_FULL]` | FAIL | FAIL | PASS | PASS |
| `test_safety_core.py::test_reject_paths_zero_tx[SESSION_RECOVERING]` | FAIL | FAIL | FAIL | FAIL |
| `test_safety_core.py::test_released_survives_two_restarts_dynamic` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_socket_mode_0660` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_stale_context_not_replayed` | FAIL | FAIL | FAIL | FAIL |
| `test_safety_core.py::test_two_daemons_same_tty` | SKIP | SKIP | SKIP | SKIP |
| `test_safety_core.py::test_wal_unlink_recreates_with_meta` | PASS | PASS | PASS | PASS |
| `test_safety_core.py::test_zero_tx_while_autoboot_quiet` | PASS | PASS | PASS | PASS |
| `test_slice_scope.py::test_deferred_methods` | SKIP | SKIP | PASS | PASS |

## 擁有者裁決後處置（2026-10-28，P5 第四輪）

- OD-36：上表第一類 8 案依裁決修正案例設計（逐案見 `docs/rust-refactor/slice21-chg-register.md` CHG-S21-OD36-*），
  「錯誤實作仍會紅」以 `tests/contract_blackbox/slice21/test_oracle_redproof.py` 證明；修正後 Python 與 Rust release 皆 PASS
  （`reject[READY_UNCONFIRMED]` 兩邊各約 302 s）。
- OD-37：`test_recovery_flushes_queue` 為外部可觀察差異（Python 送出 q0、Rust 5 筆皆 flush），已登記 DF-21，Rust 依 DF 判定（PASS），
  Python 輪逐列標記 DF 差異；`test_stale_context_not_replayed` 登記 DF-20，Rust 實作屬 Slice 2.2（P6），P5 只登記。
  原結論「無 Rust 對 Python 的待修差異」不再適用（審查 9b81d9a_c66f65e 2be8982 #1）。
- OD-38：`reject[METHOD_NOT_FOUND]` 為 Rust 專屬，Python 輪 not_applicable 逐列列名。
- 未列入 OD-36 的 `test_f2_backpressure.py::test_queue_full_backpressure` 在本 session 環境 Python 輪起點即 FAIL
  （Python CLI 逐筆 submit 300 筆超過前景 `sleep 30`，與 `reject[SESSION_QUEUE_FULL]` 同一根因），Rust 輪 PASS；
  未自行改案例，列為待擁有者確認是否比照 OD-36 修正前景時長。
