# P7（Slice 2.3）第 5 輪雲端回報（2026-10-29）

- 分支：`feature/rust-refactor-p7`，起點 `9d35b01` → 終點 `7bb9cfd`（全數 fast-forward push，無被拒）。
- 執行身分：ubuntu（非 root）；Rust 1.96.1（rustfmt、x86_64-pc-windows-gnu＋mingw）、CPython 3.12.3＋PyYAML＋pytest＋jsonschema。
  非 root 下 chmod 類 cargo 測試全數通過，無 root 環境類例外。
- 依據：rulings/p7.md、review-convergence-rules.md、OD-14～OD-47、p7-cloud 最新 sol 報告（f8381e0_9d35b01、9d35b01_9065f14、9065f14_c5f0fc9）。

## Commit 清單

| commit | 摘要 |
|---|---|
| `afebff0` | merge：併入 origin/feature/rust-refactor-p5（含主線 8ef93f4）；衝突裁決見 commit 訊息，cases.lock 以 P5 鎖為底 `--add-only` 重算 |
| `0baf6e8` | test：P7-71fe017-4 health／rollback 兩條正式路徑各補會紅回歸 |
| `9065f14` | test：merge 後 `test_sc30_reserved_for_p7` 的 SC 集合納入 SC-37（OD-21） |
| `392daa5` | fix：probe 失敗接上 transport stall 精煉（`refine_probe_failure` 原無正式呼叫端） |
| `5e704bd` | fix(rust-pal)：event handler 子行程清空 signal mask、還原 SIGPIPE／SIGXFSZ |
| `53ed5c9` | test：Slice 2.3 黑箱契約與 §11.3 不變量案例（Task 18，A/B 皆通過） |
| `ec3371b` | feat：Slice 2.3 出口 gate 報告產生器與驗證器（Task 19 部分）＋接線審計 |
| `5227d8d` | test：A/B 執行器 run_p7_ab.sh 與 §11.3-6 案例；Slice 2.3 L1.5 補列三案 |
| `c5f0fc9` | test：delay+noise 等待上限；第 11 輪全套 pytest 證據與 OD-34 重跑結果 |
| `29aaa7b`、`9bd0c03` | docs：A/B 執行紀錄 r11-261029；改寫 repo 相對路徑（R-21） |
| `7bb9cfd` | fix：gate／A/B 報告器依 sol 9065f14_c5f0fc9 收緊判定 |

## High／Medium 與裁決處理

| 項目 | 處理 |
|---|---|
| rulings P7-71fe017-4（Medium，照修） | **已修**：修法主體隨 merge `afebff0` 由 P5 帶入（`multi_open_tty_holders` 供 health、`serialwrapd_tty_holders` fail-closed 供 rollback）；`0baf6e8` 補兩條正式路徑回歸：`sw-daemon::health_multi_open_p7`（真實 PosixProcScan，消失 tty → `holders_status:"ok"`，Python 3.12 同輸入實測一致）、`sw-cli::rollback_holders_p7`（多開掃描 ok、fail-closed Unknown → 仍 DEVICE_STILL_HELD）；兩支皆以突變驗證會紅。P7-BU-9 結案 |
| rulings P7-71fe017-3（High，移交 P5） | 不處理；P5 `0a7d092` 經 merge 帶入，sol 9d35b01_9065f14 已核對為已修 |
| f8381e0_9d35b01-sol B 段 | NO FINDINGS |
| P7-9065f14-1（Medium） | **SPEC-GAPS／BLOCKED-UPSTREAM P7-BU-10**：`slice22_evidence_map.yaml`（P6 擁有）仍列 OD-34 延後的 `l15-delayed-reply`，`test_contracts_slice22.py::test_slice22_case_set_frozen_and_mapped` 紅；審查者修法亦為由 P6 移除後合入 |
| P7-c5f0fc9-1／-2／-4／-5（High）、-3（Medium） | **已修 `7bb9cfd`**（皆先紅後綠，見 p7-fix-responses） |
| P7-c5f0fc9-6（Medium） | **已修 `29aaa7b`** |
| 本輪自行發現 T18-1（High 級：新增介面未接正式路徑） | **已修 `392daa5`**：transport stall 精煉接上 attach／既有 bridge 再探／自動 reprobe 三個失敗收斂點；回歸 `transport_stall_wiring_p7` 4 支（修前 3 紅）、端到端 `test_p7_stall.py`（修前 Rust 紅） |
| 本輪自行發現 T18-2（High 級：與 Python 1:1 偏離） | **已修 `5e704bd`**：handler 背景 job 繼承被阻擋的 SIGTERM、逾時 30 s 才收尾；回歸 conformance 2 支（修前紅）、端到端 `test_handler_timeout_kills_group` |
| OD-34 | `l15-delayed-reply`、`s21-reboot-minimal-guard` 於 Rust daemon（`SERIALWRAP_GATE=1`）PASS；`cowork-01-agent-bad-cmd` FAIL（P6 `session.console_attach` METHOD_NOT_FOUND，BLOCKED-UPSTREAM P7-BU-7） |
| OD-31 | `ev-01-spawn-handler` Linux PASS；Windows 原生列 LOCAL-WINDOWS-TODO P7-LW-7 |
| OD-35（SC-30） | 黑箱 `sc30-quiet-mid-login-aborts` 與 §11.3-5 SC-30 子案 Rust PASS（Python 依 rust_only 登記 SKIP） |
| BC-04（OD-04，ab_participation: diff） | self-test RELEASED 子案依 /proc 可讀性決定 Rust 期望（非 root 下 Rust `wait_external_flash`、Python `device_attach`） |

## Task 進度（08-slice23-login-events.md）

- **Task 18**：新增 `test_p7_login.py`（10）、`test_p7_quiet_reconfirm.py`（+4）、`test_p7_selftest_recover.py`（10）、`test_p7_stall.py`、`test_p7_events.py`（6）、`test_p7_log_capture.py`、`test_p7_latency_noise.py`（1）、`test_p7_invariants.py`（5）；`run_p7_ab.sh`＋`p7_ab_report.py`。A/B 實跑 r11-261029（`docs/rust-refactor/evidence/p7-ab-261029.md`）：34 個 contract-blackbox p7 案例中 31 個 A／B 皆 PASS（SC-30 A 端登記 SKIP），3 個 INCOMPLETE（見缺項）。
- 案例設計修正（Python 實測不可達或值不同，不改契約語意，逐列記於 p7-fix-responses 第 11 輪，交編排者依收斂規則 §4 裁決）：ATTACHED 只能經 READY 後失敗抵達（計畫寫「無 READY 歷史」）；自動 reprobe 與手動 recover 的競爭次序；SC-34 的 READY_NONCE_TIMEOUT 取得方式；CTRL_C 救援的外部可觀察欄位（無 `PROMPT_TIMEOUT_RECOVERED`）；雜訊行須獨立成行；released 依 BC-04。
- **Task 19**：`p7_gate_report.py`（build／verify）與 16 項測試；`p7_gate.sh`、`cap_windows_com_p7.rs`／`com_peer.rs` 未建立（下一輪／LOCAL-WINDOWS-TODO）。
- **接線＋端到端驗收**：`docs/rust-refactor/reviews/p7-evidence/wiring-audit-r11.md`——P7 模組 pub fn 呼叫端掃描，唯一「已實作未接線」為 transport stall（已修）；其餘無呼叫端者為 Task 0 `todo!` 骨架（`self_test::assemble`／`nonce_probe_result`／`released_result`、`login_run::apply_login_finished`）或重複純函式（`login_error_detail`、`capture::log_path_for`、`Rule::cooldown_ms_sat`），列 Low。各模組對應一條 daemon 級黑箱；突變實證者為 transport stall 與 handler signal mask 兩條，其餘為構造推論。

## Gate 數字

| 項目 | 結果 |
|---|---|
| cargo test --workspace --features test-fakes（非 root） | 全綠；`sw-pal::lock::lock_conformance_suite` workspace 平行跑 1 次間歇紅、單獨連跑 4 次綠（既有碼待辦） |
| cargo check --workspace（含 `--tests --features test-fakes`）--target x86_64-pc-windows-gnu | 綠 |
| check_cfg_boundary／tools.contracts check／regen_lock --check | 綠 |
| policy_check --repo . | 0 fail、2 warn（R-19、R-22，非 PR 情境） |
| 全套 pytest 起點 `9d35b01` | 5464 passed、21 failed（`pytest-fail-start-9d35b01.txt`；slice21 黑箱 10 筆為與 g1 並行時的負載類，後續未重現） |
| 全套 pytest `53ed5c9` | 5538 passed、13 failed（1:08:59）；相對起點 +2／−10：`+test_slice22_case_set_frozen_and_mapped`（P7-BU-10）、`+test_login_with_delay_and_noise`（本輪新案例，同範圍連跑 2 次與 4 份並行皆綠，等待上限 90→180 s 於 `c5f0fc9`） |
| A/B r11-261029 | 31 PASS／PASS、1 SKIP(A, SC-30)／PASS、3 INCOMPLETE；結論 INCOMPLETE（exit 3） |
| test_slice23_l15.py（Rust，GATE=1） | 5 passed、2 failed（皆 P6 console_attach） |

## 不成立主張

- 無針對審查 finding 的不成立主張。計畫原文與 Python 基線不符者以「案例設計修正」處理（見上），未略過任何案例。

## SPEC-GAPS

1. `contracts/resource_limits.yaml` 無 `control_latency_flood_p99_ms`（`event_latency.freeze_status: pending_review_record`），`test_control_latency_under_flood_with_events` 無凍結門檻可用，未建立（A/B 列 INCOMPLETE）。
2. P7-9065f14-1：`tests/acceptance/slice22_evidence_map.yaml` 屬 P6，需 P6 移除 `l15-delayed-reply`（及 OD-34 的 `cowork-01-agent-bad-cmd` 移出 2.2 時同步）。

## 既有碼待辦（不修）

- `sw-pal::lock::lock_conformance_suite`（`stale_socket_reclaimed: AlreadyRunning`）於 workspace 平行執行間歇失敗。
- `cargo fmt --all -- --check` 對既有檔案（如 `sw-cli/src/cli_trace.rs`）有格式差異。
- Python 基線：login 進行中 `device release` 後 `device attach` 落 DETACHED＋`ATTACH_FAILED:TxIncompleteError` 且不自動重試（Rust 回 READY），見 OWNER-BATCH 2。
- Low：接線審計列出的 Task 0 `todo!` 骨架與重複純函式。

## OWNER-BATCH

1. `control_latency_flood_p99_ms` 數值門檻凍結（或裁定該案改用 `event_latency` 既有規則）。
2. release-during-login 後 device attach：Python 落 `ATTACH_FAILED:TxIncompleteError` 且不重試、Rust 回 READY——是否視為必須 1:1 重現的外部行為，或登記為 DF。
3. P7-BO-1（s21_core.linux 兩筆替換 nodeid 的 `--allow-removal` 核准）與 P7-BO-4（OD-24 防線對 P5 s21_core `#[ignore]` 列）維持待簽。

## LOCAL-WINDOWS-TODO

- P7-LW-1～LW-7 維持；LW-3 新增 `transport_stall_wiring_p7`（4）、`rollback_holders_p7`（1）的 Windows 原生執行。`health_multi_open_p7` 與 handler 的兩支 `*_blocks_term` 為 POSIX-only。
- OD-31 `ev-01-spawn-handler` Windows 原生 PASS（P7-LW-7）。
- Task 19 `cap_windows_com_p7` 三案（B-06，擁有者 host COM20:COM21）。

## BLOCKED-OWNER

- B-01～B-08（實機、bench、Windows 工作站），P7-BO-1、P7-BO-2、P7-BO-4。

## BLOCKED-UPSTREAM（P6）

- P7-BU-7：`cowork-01-agent-bad-cmd`、`l15-dut-reboot-port-alive`（`session.console_attach`）。
- §11.3-2 `test_login_bytes_not_interleaved_with_agent_or_human`、§11.3-3 `test_recovery_and_reboot_do_not_replay_stale_input`（human console）。
- P7-BU-10：slice22 evidence map。
- P7-BU-1～BU-6 維持。

## 距 Slice 2.3 出口還缺

1. P6 human console 合入後：§11.3-2／-3 兩案、`cowork-01-agent-bad-cmd`、`l15-dut-reboot-port-alive` 於 Rust PASS；P7-BU-10 map 同步。
2. `control_latency_flood_p99_ms` 凍結後補 `test_control_latency_under_flood_with_events`。
3. Task 18 其餘：`tests/acceptance/l15/slice23/*.yaml` 六案（bridge 模式 wire 證據）、`run_p7_ab.sh` 併入先前切片必要回歸（slice21／slice22 黑箱、l15 slice21）與 func-test 兩案。
4. Task 19：`p7_gate.sh` 編排、`cap_windows_com_p7.rs`／`com_peer.rs`、gate 自身集合以 `regen_lock --l1-add-only` 登記（`p7_gate:contract-blackbox-p7-a` 等）、`p7-b01-procedure.md`；之後試跑 gate。
5. Windows 原生（LW-1～7、B-06）、擁有者證據 B-01～B-08。
6. slice 出口整包審查（Codex sol＋Claude Opus 雙審）。
