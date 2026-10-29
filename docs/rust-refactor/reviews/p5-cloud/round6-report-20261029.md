# P5（Slice 2.1）第 6 輪雲端回報（2026-10-29）

- 分支：`feature/rust-refactor-p5`（本輪唯一寫入者）；起點 `110bd58`，終點 `86b54d8`。全部 commit 皆以 fast-forward 推送，未被拒。
- 開工時 merge `origin/feature/rust-refactor`（`62ffec7`：OD-45～47 登錄，`16c78ac`），期間再 merge 兩次（`897a3cd`：SC-18 證據替身，`5d7fb2f`；`8963d07`：0.4.0 release 前置／OD-48，`d657a7c`，含衝突解法）。L1 鎖只 add-only，另依 OD-55 逐名 `--allow-removal` 27 筆（見 §2）。
- 環境：cloud Linux（4 核、ext4／virtio、容器 `vm:docker`）。**本輪所有測試與 gate 皆以非 root（uid 1000，`setpriv`）執行**。Python 3.12（PyYAML、pytest 9.2.0、jsonschema 4.10.3、pyserial 3.5，對齊 `.github/workflows/tests.yml`）、Rust 1.96.1＋rustfmt、`x86_64-pc-windows-gnu`＋mingw。
- 審查依據：最新 sol 報告 `2472f78_c4cfb96-sol.md`、`event-latency-method-review-20261029.md`（Codex gpt-6.1-sol，**rejected**）、`rulings/p5.md`（開工時末 10 列＋本輪期間新增 2 列：OD-61 rotate 停頓、`test_two_daemons_same_tty` harness），以及本輪期間發佈的增量審查 `c4cfb96_4f8acdf-sol.md`（雲端 Opus 代理 sol 單審）。

## 1. commit 清單

| commit | 內容 |
|---|---|
| `16c78ac` | merge 主線（OD-45～OD-47 登錄） |
| `239e025` | event_latency 依方法審閱修正（High×3、Medium×3）＋OD-58 上限；method_revision `2026-10-29.2`；L1 探針改逐事件見證數 |
| `7953f98` | OD-55：s21_core.linux 能力列移入 `s21_gate:cap_<cap>`，非能力列 3 筆移除 |
| `bebefe9` | OD-58：`s21-event-latency` 逐名移出 l15_ab（`covered_by_sections`） |
| `d373eab` | OD-60：`stage2_estimate_weeks: 6`、`case_verdicts.json` 彙整、每次正式實跑附加斷路器快照 |
| `99591b3` | F108 linux_l1 非 root、F139 daemon_version 取自 RPC、F50／F57 第一批集合、F100 勘誤 |
| `02bfebf` | OD-61：wal_s1w_perf 外部匯入介面（WSL2 證據＋主機標註、可追溯性核對）；雲端只作診斷 |
| `f7e00db` | P6 SPEC-GAP #4：非 root holder 掃描回歸 |
| `5d7fb2f` | merge 主線（`897a3cd`） |
| `c2f6882` | F50／F57 第二批：blackbox_python／blackbox_rust／pytest_full 依實際收集登記 |
| `289f757` | `cargo_l1_results.py` 能力測試以名稱最後一段判定；T22 gate 第六輪紀錄、event_latency 新證據、斷路器快照 |
| `ba8bf0f` | OD-61 實測 High：WAL rotate 停頓根因與修正（每 1 MiB 非同步回寫） |
| `4f8acdf` | `test_two_daemons_same_tty` harness 缺陷（`share_tty_with`） |
| `a443d8d` | 新增測試的資源 marker 對齊（終點全套揭露的 `test_resource_markers` 兩支） |
| `d657a7c` | merge 主線 `8963d07`：`proc_scan/posix.rs` 保留 P5 兩種 holder 語意並加入 OD-48 方法（`uart_holders_same_uid` 走 fail-closed 掃描）；`behavior_changes.yaml` revision 7 含 SC-37＋SC-39 |
| `86b54d8` | sol `c4cfb96..4f8acdf`：High×1、Medium×2（§2.4） |

每組皆跑：受影響 crate `cargo test --features test-fakes`（非 root）、`cargo check --workspace --target x86_64-pc-windows-gnu`、觸及的 Python 測試、`python3 -m tools.contracts check`、`python3 -m policy_check --repo .`（fail 0）、`check_cfg_boundary.py`、`regen_lock.py --check`。全套 pytest 於起點、`c2f6882`、gate 內、終點各跑一次（見 §3.2）。**揭露**：`5d7fb2f` 的 commit 訊息因 heredoc 拼接，trailer 與主旨之間缺空行（trailer 內容齊全）；未改寫已推送歷史。

## 2. High／Medium 與裁決的處理

### 2.1 最新 sol 報告（`2472f78_c4cfb96`）

| 項目 | 處理 |
|---|---|
| P5-3084388-2（Medium，累計 4 輪） | **確認已於 `110bd58` 修正**：sol 審查時點為 `c4cfb96`（早於 `110bd58`）。`110bd58` 起 `p5-evidence/` 只剩 `event-latency-od45-rust-evlat-r5-c4cfb96.json`，其 identity 帶 `artifact_source_commit=464adec5…`、`artifact_build_run_id=r5b-261029`、`artifact_build_mismatch=null`；sol 所指 `…-2472f78.json` 已刪。本輪 method_revision 改為 `2026-10-29.2`，舊證據因工具／契約 sha256 不符而刪除，改為 gate `r6-261029` 同 run 量測的 `event-latency-od45-rust-r6-261029.json`（artifact 建置於本 run、建置當下 sha256 相符、身分由 judge 核對，F170／F173／F186） |
| B 段 | NO FINDINGS |

### 2.2 event_latency 方法審閱（rejected）→ 本輪 High／Medium

| 發現 | 處理 |
|---|---|
| [High] flood 失效未判 | 修（F171）：`_Flood` 計數送出失敗、記起訖與存活；`flood_problems()` 驗證失敗、中途停止、速率 ≥ 90%、持續時間（release／cancel 每輪 ≥ `seconds`，stop 每樣本暖機 `stop_warmup_s`=1.0 s）；無效→證據不足。本輪 gate：flood 實際 19944～20004 B/s、送出失敗 0 |
| [High] drain_receipt L1 探針漏算 | 修（F172）：mailbox 影子佇列＋`stopped` 平行佇列逐事件計數，timer／tick 以到期後 data 數；紅燈 `mailbox::witness_counts_data_overtaking_stopped_receipt`（D,D,D,L,W → 3）。**契約衝突列 OWNER-BATCH #1** |
| [High] 建置失敗舊 binary 綁 HEAD；judge 不讀證據 | 修（F173）：建置前刪舊 artifact、rc 分存、建置當下 sha256 綁定；judge 讀證據 verdict 並核對 daemon_sha256／artifact_source_commit／run_id／tool／contract／method_revision；Linux 建置失敗即 FAIL |
| [Medium] 超限被錯誤降級；release 重新 attach 失敗遺失耗時 | 修（F174） |
| [Medium] 未套 OD-58 | 修（F175）：`release_request` idle 250／flood 300 ms，`retest_before: "0.5.0"`、`owner_decision_ref: OD-58` 與重測條件註記。審閱 rejected → **不凍結**（`pending_review_record`、三個審閱欄位維持 null），審閱歷程記於契約註解 |
| [Medium] 作廢只看兩端、監測缺失放行 | 修（F176）：`_HostMonitor` 每秒取樣、看峰值；缺 cpu／loadavg 即證據不足 |

方法已改，**需編排者重新提交 method_revision `2026-10-29.2` 的獨立審閱**。

### 2.3 rulings/p5.md 末 10 列＋本輪新增 2 列

| 裁決 | 處理 |
|---|---|
| P6 SPEC-GAP #4（非 root holder 掃描 fail-closed） | 對照 Python `probe_holders`／開埠路徑：Rust 現行 `open_sequence`（`AcceptUnverified`＋警告＋`set_holder_unknown`）與 precheck（只在 pids 非空時 Held）已與 Python 相同，fail-closed 只在 CLI rollback。非 root 實跑 P6 所列 7 案（P5 HEAD）與 2 案（P6 HEAD `f0149022`）皆 PASS——**無法重現**（見 §4）。依裁決補非 root daemon 級回歸 `daemon_process::non_root_holder_scan_permission_does_not_block_open`（root 下 FAIL＝證據不足；Permission→Reject 變異即紅）（F184） |
| OD-55 | linux：實際收集 ignored 28 筆中 s21_core.linux 27 筆——24 個能力列 add-only 新增 `s21_gate:cap_{posix_pty 19, sys_admin 1, windows_native 2, interactive_desktop 1, supervisor_runtime 1}`（selector `--ignored cap_<cap>__`），再 `--allow-removal` 逐名移除 27 筆（含非能力列 3 筆）。**數目差異**：裁決列 23、實際 24（`exec_delegate::cap_interactive_desktop__exec_or_wait_ctrl_c` 有 `cfg(not(windows))` 版）。`test_l1_judge.py` 移植 P6 的 OD-53 豁免（同碼），未放寬其他集合；gate `linux_cap_posix_pty` 承擔 `s21_gate:cap_posix_pty`。windows 依裁決待原生收集（F177） |
| OD-58 | 上限見 §2.2；`s21-event-latency` 逐名移出 l15_ab（該情境不在任何凍結鎖，`--allow-removal` 無對象；以 `L15_AB_EXCLUDED`＋`l15_ab_excluded.covered_by_sections` 執行）（F178）。審閱 rejected → 未凍結 |
| OD-59 | P7 範圍，本輪無 P5 動作 |
| OD-60 | `stage2_estimate_weeks: 6`；`case-verdicts` 彙整（最差判定、缺證據 EVIDENCE_MISSING）；正式實跑附加快照（F179）。gate `r6-261029`：PASS 13／48、窗口內未觸發、OD-02 1.14＋6 ≤ 24 週 |
| OD-61 | gate 新增匯入介面（F183）；本輪期間編排者已發佈 WSL2 證據，以介面實測可匯入（判 FAIL、可追溯）。編排者命令見 §7 |
| OWNER-BATCH #3（F50／F57） | add-only 登記：linux_cap_posix_pty 336、existing_suites_rust 4、blackbox_python 45、blackbox_rust 48、pytest_full 5530（＋1 本輪新案）。排除只有 skipped／xfailed 列與 3 個 pytest 版本相依 id，failed 列照常登記（清單 `p5-evidence/s21-gate-set-registration-r6.md`）。**py_parity 完整 selector 未做**：`regen_lock` 拒絕變更既有 selector（SPEC-GAP #1）（F180／F182） |
| OWNER-BATCH #4（F108） | gate 非 root 執行（root 時經 `SW_GATE_L1_USER` 降權，否則 EVIDENCE_MISSING）；judge 依 `run_euid`。本輪 gate linux_l1 **1728／0／28 PASS**（F181） |
| OWNER-BATCH #5（`test_l1_judge.py` 3 支、P4 證據測試） | OD-55 後重跑仍失敗 3 支，根因：① `test_l1_lock_nodeids_subset_of_collect_only`——pytest 9.2.0 對長字串參數改產 `msg2` 類 id，`s0_bc.linux` 鎖內為 9.0.3 形式；pytest 9.0.3 venv 下 **passed**（環境／版本，OWNER-BATCH #3）；② `test_l1_lock_frozen_set_excludes_capability_gated`、③ `test_l1_lock_cargo_sets_exclude_ignored_tests`——只剩 `s21_core.windows` 13 列（5 列 cap SKIP），依 OD-55 待 Windows 原生收集登記能力集合後才移除（LOCAL-WINDOWS-TODO）。`test_p4_required_cases_have_passing_evidence`：雲端無本機 Stage 1 出口證據目錄，照實列 **EVIDENCE_MISSING**（未 skip、未改 PASS） |
| OWNER-BATCH #6（F100） | 計畫 06 Review Focus 5 與 SC-11 ① `server.stop` 更正為 20 s 並附勘誤說明（F181） |
| OWNER-BATCH #8 | 同 OD-60 |
| 新：OD-61 WSL2 實測 rotate 停頓（High） | **根因（實證）**：非跨流鎖（每流各一 `WalWriter`）；rotate 的 close(sync)→rename→sync_dir 與 Python 相同，但 Rust append 快約 47 倍，預填 60 MiB 後立即輪替時整檔髒頁未回寫，四流同時 fsync。預填後臨時 `sync(2)` 即由 368～440 ms 降到 22～24 ms。**修正**：`PosixWalFile` 每 1 MiB 以 `sync_file_range(SYNC_FILE_RANGE_WRITE)` 啟動非同步回寫（Linux，只在 sw-pal；不改持久性語意）。紅燈 `wal_fs::posix::tests::writes_initiate_async_writeback_every_chunk`。cloud 同主機（診斷）：rotate max 64.9／65.2／66.4 ms，同主機 Python 基線 221～360 ms。**需編排者於 WSL2 以 ≥ `86b54d8` 重測三輪（`86b54d8` 起輸出含 `source_tree_dirty`，匯入必要）**（F187） |
| 新：`test_two_daemons_same_tty` harness（照修） | `start_daemon(share_tty_with=…)`；紅燈 `tests/test_slice21_harness_shared_tty.py`；修正後 Python 基線與 Rust 皆 passed（F188） |

### 2.4 本輪期間的增量審查 `c4cfb96_4f8acdf-sol.md`（結論：需修正）

| 編號 | 嚴重度 | 處理 |
|---|---|---|
| A 段 P5-3084388-2 | Medium | 審查者確認已修（`289f757`），累計 4 輪結案 |
| P5-4f8acdf-1 | High | 修（F190，修法 2）：judge 對鎖中未被任何 section 承擔的 `s21_gate:*` 集合判 INCOMPLETE 並列名；以新判定器重判 r6 列出 `cap_{interactive_desktop,supervisor_runtime,sys_admin,windows_native}`。這 4 個能力在雲端不可執行，新增 section／BO／runbook 屬計畫變更（SPEC-GAP #2），現已不可能被默默放行 |
| P5-4f8acdf-2 | Medium | 修（F191）：`wal_s1w` env 新增 `source_tree_dirty`，匯入須為 false；`relevant_paths` 加 workspace／crate `Cargo.toml` |
| P5-4f8acdf-3 | Medium | 修（F192）：`HolderScanStatus::py_name()`，WARNING 印 `status=permission`，與 `daemon_logging.yaml` 逐字；回歸改斷言 Python 字樣 |
| P5-4f8acdf-4～7 | Low | 依收斂規則不修（交 low-backlog） |

## 3. gate 與全套數字

### 3.1 T22 gate `r6-261029`（非 dry-run、非 root、受測 `c2f6882`）

- 跑前確認無洩漏 daemon／bridge；loadavg 開跑 0.33／0.66／0.63（08:43:50Z）、結束 0.69／0.74／0.75（11:03:56Z）。**verdict FAIL**（exit 1）。

| section | 判定 | 數字 |
|---|---|---|
| cfg_boundary | PASS | — |
| linux_l1 | **PASS** | 1728／0／28（run_euid 1000） |
| linux_cap_posix_pty | FAIL | 350／1／8：`sw-pal::lock::lock_conformance_suite` 1 次失敗（同 gate linux_l1 內 passed、gate 外連跑 3 次 PASS，根因未明）；4 個 lib 能力測試 skipped 為 `cargo_l1_results.py` 缺陷，`289f757` 已修（gate 外重跑 355／0／4，cap 全 passed） |
| windows_native_l1 | EVIDENCE_MISSING | 無原生 Windows |
| windows_cap_windows_com | EVIDENCE_MISSING | BO-S21-01（允許） |
| blackbox_python | FAIL | 43／2／4（`test_two_daemons_same_tty`→`4f8acdf` 已修；`test_queue_full_backpressure` 見 §3.2） |
| blackbox_rust | FAIL | 47／1／1（`test_two_daemons_same_tty`→已修） |
| existing_suites_rust | **PASS** | 4／0；func-test 3/3 |
| l15_ab | EVIDENCE_MISSING | pytest 全綠；只因 `s21-event-latency` 的承擔 section 未 PASS |
| event_latency | EVIDENCE_MISSING | Rust 18 輪全 PASS（max：stop 1.00～4.16、cancel 2.81～50.58、release 9.72～42.70 ms）、L1 gate passed、證據身分核對通過；**唯一原因：未凍結** |
| wal_s1w_perf | EVIDENCE_MISSING | OD-61：雲端只作診斷（append p50 0.0245／p95 0.0438／p99 0.0683 ms、rotate 444 ms，修正前） |
| case_uptake | **PASS** | 13／48（rate 0.271），斷路器未觸發 |
| pytest_full | FAIL | 5526／7／205（見 §3.2） |
| policy_check | PASS | fail 0 |

其餘：identity 齊全（`daemon_version` 0.3.2、`build` Linux／Windows exit 0）；parity PASS 69／INCOMPLETE 34／BLOCKED-OWNER 2。Python 診斷（50 樣本）idle `release_request` 三輪 max 154.4／**255.4**／157.7 ms——Python 基線本身有一輪超過 OD-58 的 250 ms（OWNER-BATCH #2）。

### 3.2 全套 `python3 -m pytest -q tests/`（非 root、序列執行）

| 時點 | failed | passed | skipped |
|---|---:|---:|---:|
| 起點 `16c78ac`（乾淨 worktree） | 6 | 5484 | 206 |
| `c2f6882`（本輪中段） | 7 | 5526 | 204 |
| gate `r6-261029` 內（`c2f6882`） | 7 | 5526 | 205 |
| `4f8acdf` | 8 | 5527 | 204 |
| 終點 `86b54d8`（含主線 merge） | 6 | 5579 | 204 |

起點 6 failed：`test_two_daemons_same_tty`、`test_l1_judge.py` 3 支、`test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native`（需原生 Windows）、`test_p4_required_cases_have_passing_evidence`（EVIDENCE_MISSING）。
中段多出的 `test_f2_backpressure.py::test_queue_full_backpressure`：同範圍單檔連跑 2 次皆 FAIL，**起點 commit 乾淨 worktree 單檔亦 FAIL**——與本輪變更無關（未改 Python daemon 與該測試），症狀同第四輪未結項（前景 `sleep 30` 期間 300 筆未見 `SESSION_QUEUE_FULL`），根因未明，不以 flaky 放行（既有碼待辦）。`4f8acdf` 多出的 `test_resource_markers.py` 兩支為本輪新增測試的 marker 混用，`a443d8d` 已修。
**終點 6 failed＝起點 6 failed 去掉已修的 `test_two_daemons_same_tty`、加上上述既有的 `test_queue_full_backpressure`**；未引入新失敗。

## 4. 不成立主張

1. **P6 SPEC-GAP #4 的「非 root 下開埠 fail-closed」在現行碼不成立**：開埠路徑權限不足時已與 Python 相同（警告＋照常開啟），且非 root 以 P5 HEAD 實跑 P6 所列 7 案、P6 HEAD 實跑 2 案皆 PASS。P6 第 4 輪觀察可能源於當時環境（P6 cont 報告亦記非 root 全部 PASS、歸因環境差異）。依裁決仍補了非 root 回歸防止退化。
2. **OD-61 rotate 停頓「疑似跨流共用鎖」不成立**：實驗證明停頓來自 fsync 髒頁量（§2.3），已修。
3. 方法審閱的 drain_receipt 發現：探針漏算成立並已修；但「至多處理一筆 data」的界限在同 epoch 依賴等待時與既有排空順序衝突，屬契約問題（OWNER-BATCH #1），非實作可修。

## 5. SPEC-GAPS

1. `s21_gate:py_parity` 的「完整 selector」：`regen_lock --l1-add-only` 拒絕變更既有 selector，`--check --base` 亦判 FAIL；add-only 無法達成。需擁有者核准 selector 變更（或新增逐名核准 selector 變更的旗標）。
2. linux 能力集合 `s21_gate:cap_{sys_admin, windows_native, interactive_desktop, supervisor_runtime}` 在 gate 無承擔 section（section 列舉由 schema 固定；需 CAP_SYS_ADMIN／原生 Windows／互動桌面／systemd 執行期）。新增 section 屬計畫變更；在此之前 judge 以「未承擔集合」判 INCOMPLETE（P5-4f8acdf-1）。
3. pytest 版本相依的 3 個參數化 id（`test_invalid_regex_rpc_and_arbiter_reject_without_enqueue[invalid2]`、`test_cargo_toml_bypass[toml2-target-section]`、`test_classify_pyserial_open_error[msg2-expected2]`）無法穩定入鎖；CI 未釘 pytest 版本。建議釘版本或於測試給明確 `ids=`。

## 6. 既有碼待辦（不修）

- `test_f2_backpressure.py::test_queue_full_backpressure`（Python 基線）時序相依失敗，見 §3.2。
- `sw-pal::lock::lock_conformance_suite` 於 gate cap section 失敗 1 次、他處皆 PASS，根因未明。
- `cargo check --target x86_64-pc-windows-gnu` 既有 dead_code 警告（`device_source::posix::PosixDeviceSource` 等）。
- 本輪合併主線帶入的 sw-cli `mgmt_legacy_handoff` 等新測試未登記任何 s21 集合（屬主線集合，非 P5 範圍）。
- 第五輪所列（`attached_real_paths` 死碼、測試觀測點未標 `test-fakes`、repo 未 rustfmt 乾淨）仍在。

## 7. OWNER-BATCH

1. **drain_receipt 的 L1 界限與排空順序衝突**（方法審閱 High，契約語意）：同 epoch 有依賴 RX 的 control（OpFinished／LoginFinished）等待時，mailbox 依設計先處理該 epoch 已讀 RX，再回 `WorkerStopped`（`tests/mailbox.rs::dependency_released_on_worker_stopped` 的 D,D,D,L,W）；新探針量得 receipt 等待期間處理 3 個 data。**Python 基線同序**：`UARTBridge.stop()`→`finish_drain` 於 reader thread join（`sw_core/uart_io.py` `_join_drain_thread`）後才產生 receipt，reader `_loop` 每次讀取即同步執行 RX 回呼，stop 前已讀入的 RX 全數處理完才有 receipt。選項：(a) `drain_receipt` 的 `l1_bound` 改為「receipt 只在該 epoch 已讀 RX 處理完後被處理、且不被其他 epoch 的 data 超越」；(b) 維持原界限並改 mailbox 排空順序（與 Python 不 1:1）。建議 (a)。現行 L1 gate 兩支 flood 測試（無依賴情境）以新探針仍 passed。
2. **OD-58 數值**：本輪 Python 基線診斷 idle `release_request` 一輪 255.4 ms（> 250）；Rust 本輪最大 42.7 ms。0.5.0 前重測條件已記於契約。
3. **pytest 版本**（SPEC-GAP #3）。
4. **py_parity selector 變更授權**（SPEC-GAP #1）。

**編排者待辦**：(i) 重新提交 method_revision `2026-10-29.2` 的獨立審閱；(ii) WSL2 重測 wal_s1w（新 commit），命令：
```bash
git fetch origin && git checkout --detach origin/feature/rust-refactor-p5   # ≥ 86b54d8（含 source_tree_dirty 欄位）、乾淨工作樹
cargo run --release -p sw-core --example wal_s1w -- --out <out>/wal_s1w-N.json --wal-root <ext4 上的 throwaway 目錄>   # 連跑三輪
python3 scripts/rust/slice21_gate_judge.py wal-s1w-verdict --import <out>/wal_s1w-1.json --host-label owner-wsl2-ext4-vhdx   # 單獨核對
SW_GATE_WAL_S1W_IMPORT=<out>/wal_s1w-1.json SW_GATE_WAL_S1W_HOST=owner-wsl2-ext4-vhdx bash scripts/rust/slice21_gate.sh --evidence-run --run-id <id>   # 併入 gate
```
匯入檢查：`wal_s1w.v1`、Linux 門檻、正式參數、`env.virtualization` ∈ {wsl2, native}、標註非空、`env.git_revision` 在本分支歷史且其後 WAL 相關路徑未變更（`110bd58` 的舊證據因 `ba8bf0f` 改了 `crates/sw-pal/src/wal_fs` 已不採用）。

## 8. LOCAL-WINDOWS-TODO

- `windows_native_l1`（`scripts/rust/win-test.sh --package sw-pal --package sw-core --package sw-daemon -- --features test-fakes -- --skip cap_`）；s21_core.windows 尚未登記本輪與前輪新增 nodeid（mailbox witness 等需原生收集）。
- OD-55 windows：原生收集登記 `s21_gate:cap_{windows_com, windows_native, interactive_desktop, supervisor_runtime}.windows` 後，以 `--allow-removal` 自 s21_core.windows 移除 13 列；`mgmt_layout::windows_switch_running_exe_rename` 的原生集合。
- BO-S21-13 Windows 黑箱／L1.5、Winsock 慢讀、state.json CRLF、`configure_port`、overlapped 讀取失敗臂（F09）、`win_l1_into` 綁定 run（F122）。

## 9. BLOCKED-OWNER

- BO-S21-01～16（實機／Windows）；event_latency 凍結（待重新審閱）；OWNER-BATCH #1～#4。

## 10. 距 Slice 2.1 出口還缺什麼

1. event_latency：method_revision `2026-10-29.2` 獨立審閱 → 凍結（解除 event_latency 與 l15_ab 的 EVIDENCE_MISSING 及 §5.3 preflight）。
2. wal_s1w_perf：WSL2 以新 commit 重測；append 門檻是否適用 WSL2 待擁有者裁決（OD-61 編排者裁決 2）。
3. 下一輪 gate 重驗（終點 `86b54d8`；本輪 gate 受測 `c2f6882`）：`linux_cap_posix_pty`（`cargo_l1_results` 修正後）、blackbox 兩邊與 pytest_full（`test_two_daemons_same_tty` 修正後）；`test_queue_full_backpressure`、`lock_conformance_suite` 根因。
4. OWNER-BATCH #1（drain_receipt 界限）、SPEC-GAP #1～#3。
5. Windows 原生（L1、OD-55 windows、BO-S21-13）與 BO 實機項。
6. slice 出口整包審查（Codex sol＋Claude Opus 雙審）。
