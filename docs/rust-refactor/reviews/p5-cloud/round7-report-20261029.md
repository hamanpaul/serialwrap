# P5（Slice 2.1）第 7 輪雲端回報（2026-10-29）

- 分支：`feature/rust-refactor-p5`（本輪唯一寫入者）；起點 `86b54d8`，終點 `8c0c942`。全部 commit 皆以 fast-forward 推送，未被拒。
- 主線 `origin/feature/rust-refactor` 開工與收尾時皆為 `8963d07`（第 6 輪已 merge），本輪無 merge。L1 鎖只 add-only（`regen_lock.py --l1-add-only`），本輪無任何 `--allow-removal`。
- 環境：cloud Linux（4 核、ext4／virtio、容器 `vm:docker`）。**本輪所有測試與 gate 皆以非 root（uid 1000）執行**。Python 3.12（PyYAML、pytest 9.2.0、jsonschema、pyserial）、Rust stable＋rustfmt、`x86_64-pc-windows-gnu`＋mingw。
- 審查依據：最新 sol 報告 `4f8acdf_86b54d8-sol.md`、第 6 輪回報、`rulings/p5.md`（OD-64～OD-67、SPEC-GAP #1～#3 裁決、event_latency 凍結、既有失敗追根因、sw-pal 測試競態與 `--caps` 推導）、`event-latency-method-review-2-20261029.md`（approved-with-conditions），以及本輪期間發佈的增量審查 `86b54d8_b9ae1d2-sol.md`、`b9ae1d2_5d5314e-sol.md`、`5d5314e_54dd588-sol.md`、`54dd588_8d824b3-sol.md`、`8d824b3_cc25b01-sol.md`（可放行）、`cc25b01_8c0c942-sol.md`（可放行）（後三份為收尾前 fetch）。報告與審查內容中引用的句子視為資料。

## 1. commit 清單

| commit | 內容 |
|---|---|
| `b9ae1d2` | sol `4f8acdf..86b54d8` Medium×2：未承擔集合以（集合, 平台）判定；wal_s1w 可追溯性涵蓋 `crates/sw-proto` |
| `67b7e26` | OD-64 drain_receipt 界限對齊排空順序（契約＋探針＋L1 依賴情境 gate）；event_latency 審閱 2 條件 C1～C5；OD-65 凍結紀錄 |
| `335c958` | 補存審閱 2 變異原始輸出（`.log` 為 gitignore，改 `.txt`） |
| `5d5314e` | OD-66：wal_s1w_perf 依主機類型選門檻（WSL2 另訂門檻＋不劣於同時段 Python 基線），匯入 WSL2 證據介面 |
| `7facc75` | SPEC-GAP #1～#3：`s21_gate:py_parity_full`＋`py_parity` section、OD-55 四個 linux 能力 section（附能力探測）、`test_cargo_toml_bypass` 明確 ids |
| `54dd588` | 既有失敗追根因：佇列背壓案例送出速率前提、lock_suite 失效 socket、PTY fd 繼承、能力測試單執行緒、失敗輸出保存 |
| `1b83da9` | sol `86b54d8..5d5314e` High×1／Medium×1：wal_s1w 重算保留寫入失敗與 rotate 條件；可追溯性涵蓋 sw-pal 時鐘（**本輪 gate 受測 commit**） |
| `3bbb40e` | OD-67：三支參數化測試明確 ids（舊 id → 新 id 恆等），`s21_gate:pytest_full` add-only 2 筆 |
| `8d824b3` | sw-pal 測試 fd 重用／fork 時窗競態（各 200 次 0 敗）；gate `--caps` 改 OD-53 推導 |
| `cc25b01` | sol `5d5314e..54dd588` Medium×2：收集器 `--caps-only`、能力 section 只跑能力列、`linux_cap_posix_pty` 比照 F108 降權 |
| `8c0c942` | T22 gate 第 7 輪紀錄（`r7-261029`）、event_latency 證據、斷路器快照 |

每組皆跑：`check_cfg_boundary.py`、`python3 -m tools.contracts check`、`python3 -m policy_check --repo .`（fail 0／warn 2：R-19、R-22 既有）、`regen_lock.py --check`、觸及的 Python 測試；動到 Rust 的組（`67b7e26`、`54dd588`、`8d824b3`）另跑受影響 crate `cargo test --features test-fakes`（非 root），程式組另跑 `cargo check --workspace --target x86_64-pc-windows-gnu`（`8c0c942` 只有紀錄與證據，未跑 cargo）。全套 pytest 於起點、gate 內、終點各跑一次（§3.2）。

## 2. High／Medium 與裁決的處理

### 2.1 最新 sol 報告 `4f8acdf_86b54d8-sol.md`（雲端 Opus 代理 sol 單審；Medium 2、Low 3）

| 編號 | 嚴重度 | 處理 |
|---|---|---|
| A 段 | — | 前輪 finding 皆「已修」或「依裁決不處理」，本輪無待修 |
| P5-86b54d8-1 | Medium | 修 `b9ae1d2`（F193）：未承擔集合判定改為（集合, 平台）對，只計 `TEST_SECTIONS` 內 section 的 `required_set`／expect `additional_required_sets`／片段 `additional_required_sets`；非測試類 section 自帶宣告不算承擔。紅燈：`test_uncarried_gate_set_is_judged_per_platform`、`test_non_test_section_fragment_cannot_carry_gate_set`（修正前皆 READY） |
| P5-86b54d8-2 | Medium | 修 `b9ae1d2`（F194）：`wal_s1w.relevant_paths` 加整個 `crates/sw-proto`；新增 `tests/test_slice21_wal_s1w_import_git.py`（臨時 git repo 實跑，只改 sw-proto → EVIDENCE_MISSING） |
| P5-86b54d8-3～5 | Low | 不修（收斂規則） |

### 2.2 本輪期間發佈的增量審查

| 報告 | 編號 | 嚴重度 | 處理 |
|---|---|---|---|
| `86b54d8_b9ae1d2-sol.md` | P5-b9ae1d2-1 | Medium | 修 `1b83da9`（F210）：`relevant_paths` 加 `crates/sw-pal/src/{clock.rs,clock,lib.rs,traits.rs}`；`86b54d8..HEAD` 在全部相關路徑無變更，WSL2 證據仍有效 |
| 同上 | P5-b9ae1d2-2／-3 | Low | 不修（-3 審查者確認已消失） |
| `b9ae1d2_5d5314e-sol.md` | P5-5d5314e-1 | **High** | 修 `1b83da9`（F209）：OD-66 數值重算只限延遲門檻；`write_failures>0` 或任一流未跨越 rotate（§1.1.3）→ FAIL，缺欄／型別或長度不符 → EVIDENCE_MISSING（匯入與本機皆適用）。紅燈 13 案（`5d5314e` judge 下皆 PASS） |
| 同上 | P5-5d5314e-2～5 | Low | 不修 |
| `5d5314e_54dd588-sol.md` | P5-54dd588-1 | Medium | 已於 `8d824b3`（F213／F215）依確切修法做法一處理：壓力測試改在重入的單執行緒子行程（spawner 只存在於子行程）；`stale_socket_is_unlinked_then_acquired` 改用公開的 `bind_stale_unix_socket`（只 bind 不 listen）；EACCES 斷言一律於子行程。驗證：lock 執行檔整檔連跑 **300 次 0 失敗（14 支、預設平行，17:21～17:32Z，執行檔 sha256 `da40fea0…`）**（審查者條件下修正前 4/300） |
| 同上 | P5-54dd588-2 | Medium | 修 `cc25b01`（F216）：收集器 `--caps-only`（略過預設 run、只跑 `--ignored cap_<cap>__ --test-threads=1`、其餘收集列 deselected）；judge `caps_plan.caps_only`（每個必要集合 selector 都只選 ignored 能力列才為 true）→ gate 四個能力 section 帶 `--caps-only`；`how_to_run`／runbook／expect 命令同步；附帶指出的 `linux_cap_posix_pty` root 必紅：F108 降權抽成 `nonroot_cmd` 共用、judge `NON_ROOT_SECTIONS` 加入。紅燈 7 支（修正前皆紅），失敗情境以 `test_caps_without_caps_only_runs_default_tests_and_fails` 固定。實機：非 root 實跑——caps-plan 推導 `--caps-only --caps sys_admin -- --workspace --features test-fakes -- --ignored cap_sys_admin__`，收集 1816、deselected 1815、唯一能力列 passed，報告 argv 與凍結 selector 相同，`l1_judge`（帶 selector）PASS——非 root 下該測試 early return，故只證明機制；gate 仍須能力探測為 available 才判定。依「測試以非 root 執行」規則未以 root 重跑審查者的重現 |
| 同上 | P5-54dd588-3～6 | Low | 不修 |
| `54dd588_8d824b3-sol.md`（收尾前 fetch） | P5-54dd588-2 複核「未修」 | Medium | 該審查對象止於 `8d824b3`；修正在其後的 `cc25b01`（上列）。審查者以 root 實跑另見 sw-daemon `daemon_process` 5 支 root 專屬失敗（`--workspace` 範圍）——`--caps-only` 不執行預設 run，這些列記 deselected、不再影響能力 section。紅燈先行以假 libtest 重現「預設列失敗 → FAIL」（`test_caps_without_caps_only_runs_default_tests_and_fails`），未以 root 重跑 |
| 同上 | P5-54dd588-1 | Medium | 審查者確認已修（`8d824b3`，300 次 0 失敗）；F215 補上編號引用 |
| 同上 | P5-8d824b3-1～3 | Low | 不修 |
| `8d824b3_cc25b01-sol.md`（結論：可放行） | P5-54dd588-2 | Medium | 審查者確認已修（`cc25b01`） |
| 同上 | P5-cc25b01-1／-2 | Low | 不修（-1：`--caps-only` 的 deselected 依結果而非選取計算；-2：runbook 未註明降權使用者須可寫 `CARGO_TARGET_DIR`） |
| `cc25b01_8c0c942-sol.md`（結論：可放行） | P5-8c0c942-1 | Low | 不修；指出 gate 紀錄把 `test_daemon_start_stop_ab_real_daemon` 歸因並行負載而未附「同範圍連跑兩次」實測——本報告 §3.2 改寫為不主張環境因素，以本報告為準 |

### 2.3 擁有者／編排者裁決

| 裁決 | 處理 |
|---|---|
| OD-64（drain_receipt 界限對齊排空順序） | `67b7e26`（F195）：契約 drain_receipt `l1_bound: at_most_pre_dependency_data_plus_one_after_receipt`＋`owner_decision_ref: OD-64`＋`l1_bound_rule`＋`l1_bound_definition`。mailbox（test-fakes）於 receipt 移入 `stopped` 時記下 data lane 筆數、依 FIFO 逐筆核對 epoch 得 N_pre（`take_control_pre_dependency_data()`），actor `ProbeEntry::pre_dependency_data`；排空順序不變（D,D,D,L,W）。紅燈：mailbox 4 支（未實作即不能編譯）、`test_event_latency_od64_od65_records`。**oracle 變更揭露**：`test_event_latency_contract_values` 原斷言五事件皆 `at_most_one_data_item_before_control`，依 OD-64 改為 drain_receipt 用新識別字、其餘四事件不變 |
| OD-65（release_request 維持 250／300） | `67b7e26`（F198）：契約註解與 `freeze_note` 記 Python r6 idle 三輪 154.4／**255.4**／157.7 ms、Rust r6 最大 42.7 ms、`retest_before: "0.5.0"` 保留 |
| OD-66（WSL2 另訂門檻＋不劣於同時段 Python 基線） | `5d5314e`（F200）＋`1b83da9`（F209）：judge `WAL_S1W_HOST_THRESHOLDS`（與 expect `host_thresholds` 一致）依 `env.virtualization` 選門檻、數值重算延遲門檻；WSL2 須附同主機（virtualization／cpu／cpu_count／memory_bytes／os_build／storage_device／fs_type）同工作量的 Python 基線，任一項劣於基線即 FAIL；原生維持 §1.2；可匯入多輪取最差；`verdict.json` 的 `thresholds` 與第一個原因標明所用門檻。gate：`SW_GATE_WAL_S1W_IMPORT` 可列多輪（`:` 分隔）、新增 `SW_GATE_WAL_S1W_PY_BASELINE`。**oracle 變更揭露**：既有 WSL2 匯入案依 OD-66 改附基線；`_doc` 夾具 FAIL 數值改為同時超出 §1.2 與 OD-66 |
| SPEC-GAP #1（py_parity_full） | `7facc75`（F202）：`regen_lock --l1-add-only` 新增 `s21_gate:py_parity_full.linux`（selector＝舊集合 18 筆 nodeid，實際收集 18、實跑 18 passed），gate 新增 `py_parity` section 以其 selector 實跑；舊 `s21_gate:py_parity` 不改不刪，仍為 `pytest_full` 的附加必要集合 |
| SPEC-GAP #2（能力集合 section） | `7facc75`（F203）：`linux_cap_{sys_admin,windows_native,interactive_desktop,supervisor_runtime}`；gate 先探測（euid 0＋CapEff bit 21；`systemctl --user is-system-running`；linux 恆不具 Windows 能力），具備才 `--caps <cap>` 實跑；judge 無 `capability_probe{available:true}` 一律 EVIDENCE_MISSING 並標所需能力、處置（sys_admin／supervisor_runtime → BLOCKED-OWNER；windows_native／interactive_desktop → LOCAL-WINDOWS-TODO）與實跑方式（runbook 新節）。鎖中所有（s21_gate 集合, 平台）現皆有 section 承擔 |
| SPEC-GAP #3（明確 ids） | `7facc75`（F204）：`test_cargo_toml_bypass`（只在 P5 的 `s21_gate:pytest_full`）給 `ids=`（沿用 pytest 9.0.3 自動 id 形式），已凍結 3 筆不變、第 4 筆在 pytest 9.0.3／9.2.0 收集相同並 add-only 登記；`test_classify_pyserial_open_error`（s0_bc、s0_win_pytest）與 `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue`（s0_win_pytest）屬主線 Stage 0 鎖 → 不改，列 SPEC-GAPS（其後依 OD-67 給明確 ids，主線集合恆等不需動作，§2.5、§6.3） |

### 2.4 event_latency 第 2 次獨立審閱（`event-latency-method-review-2-20261029.md`，approved-with-conditions）

依編排者指示（21:35）：條件完成後 `freeze_status` 維持 `pending_review_record`，三個審閱欄位依報告填入（`reviewed_by: cloud-claude-opus-xhigh`、`reviewed_at: "2026-10-29"`、`review_conclusion: approved-with-conditions`），待編排者核對後改 `frozen`。method_revision 維持 `2026-10-29.2`（外部口徑未改；審閱涵蓋 `.2`＋C1～C5 增量，已寫入契約註解）。證據目錄 `p5-evidence/evlat-review2-conditions-r7/`。

| 條件 | commit | 證據 |
|---|---|---|
| C1 契約（OD-64） | `67b7e26` | `resource_limits.yaml` drain_receipt：新識別字、`l1_bound_definition{n_pre, bound, counted_in_plus_one, no_dependency}`；`test_slice21_scope.py` 兩支 |
| C2 探針 | `67b7e26` | `Witness.stopped_pre`／`note_stopped_push(epoch)`／`take_control_pre_dependency_data()`、`ProbeEntry::pre_dependency_data`；gate 斷言 `data_while_waiting ≤ N_pre＋1`（`assert_drain_receipt_within_od64_bound`） |
| C3 依賴情境 gate | `67b7e26` | `drain_release_e2e::drain_receipt_dependency_wait_on_{release,shutdown}_within_od64_bound`（在途命令 OpFinished＋有限 backlog 3～7 筆＋1 筆舊 epoch，各 20 輪；斷言 N_pre＝backlog、見證數＝backlog＋1、OpFinished 先於 receipt），已入 l1_gate 並 add-only 登記 s21_core.linux；變異 M3（stopped 即 panic）、M6（stopped 不先排空已讀 data）、M7（N_pre 不分 epoch）皆紅 |
| C4 偵測力 | `67b7e26`／`335c958` | flood gate 每段（release、cancel、lease、shutdown）20 輪；`mailbox::lane_witness_counts_data_returned_via_stopped_and_pending_paths`（L-10）入 stop／cancel／release／drain_receipt 的 l1_gate；M1 5/5 紅、M5 5/5 紅（審閱時 1/6）、M2 由 L-10 自測抓到（審閱時靜默） |
| C5 凍結紀錄 | `67b7e26` | release_request 註解＋`freeze_note`（OD-65 數字）；stop_request 註解＋`external_scope_note`（外部值止於 accept_point，L-1） |

審閱 Low L-2～L-9 不修。另：契約變更使 r6 證據的 `contract_sha256` 失效（設計如此），凍結 commit 後須重跑 gate 取新證據。

### 2.5 編排者追加（OD-67；rulings/p5.md 最新列：sw-pal 測試競態與 `--caps` 推導）

| 項目 | commit | 處理與實測 |
|---|---|---|
| OD-67 明確 ids | `3bbb40e`（F211） | `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue`、`test_classify_pyserial_open_error` 給明確 `ids=`（`test_cargo_toml_bypass` 已於 `7facc75`），沿用 pytest 9.0.3 自動 id 形式。**逐集合「舊 id → 新 id」皆恆等**：鎖內凍結 nodeid 全數不變，任何集合皆無須 `--allow-removal`；實際收集數不變（5／4／4，pytest 9.2.0 與 9.0.3 逐字相同，13 passed）。P5 的 `s21_gate:pytest_full.linux` add-only 新增 2 筆（9.2 下原為 `invalid2`、`msg2-expected2` 而無法入鎖者）。主線 `s0_bc.linux/windows`、`s0_win_pytest.windows` 未改、無須另派（§6.3）。對照表：`p5-evidence/od67-param-ids-r7.md`。附帶：`test_l1_lock_nodeids_subset_of_collect_only`（起點基準失敗）轉 passed |
| `invalid_serial_fd_is_a_poll_wait_error` | `8d824b3`（F212） | 本機重現 **16/200**（close 後同號 fd 被他執行緒取得）。改用 `c_int::MAX`（永不可能配置），並以 `fcntl(F_GETFD)`＝-1／EBADF 前置確認；**POLLNVAL→`ReadError::Wait(EBADF)` 斷言原樣保留**。修正後 **0/200**；sw-pal lib 整體 300 次 0 失敗 |
| `lock_conformance_suite` `[stale_socket_reclaimed]` | `54dd588`（F206）＋`8d824b3`（F213） | `54dd588` 前重現 145/2000（全為 AlreadyRunning：fork 後 exec 前的子行程持有 listener 複本），改只 bind 不 listen 後原迴圈 0/2000。本輪再連跑：`lock_conformance_suite --exact` **0/200**、lock 執行檔整檔 **0/200**（`8d824b3` 後）及 **300 次 0 失敗（14 支、預設平行，17:21～17:32Z，執行檔 sha256 `da40fea0…`）**（P5-54dd588-1 驗證） |
| gate `--caps` 改 OD-53 推導 | `8d824b3`（F214） | judge `derive_caps`（與 P6 `s22_gate_check.derive_caps` 同規則：`-- --ignored cap_<cap>__…` → `--caps <cap>…`；無 ignored 篩選 → `[]`；其餘 → 推導不出）＋`caps_plan`＋`caps-plan` 子命令；gate 的能力 section 一律以推導結果執行，推導不出即 EVIDENCE_MISSING。`cc25b01` 再加 `caps_only` |

OD-67 逐集合「舊 id → 新 id」（完整逐列對照見 `p5-evidence/od67-param-ids-r7.md`）：

| 集合 | 擁有 | 鎖內筆數 前→後 | 舊 id → 新 id | 實際收集（前／後） |
|---|---|---|---|---|
| `s0_bc.linux` | 主線 | 4→4 | 4 筆恆等（9.2 自動 id `msg2-expected2` 改回凍結的長 id） | 4／4 |
| `s0_bc.windows` | 主線 | 4→4 | 同上 | 4／4 |
| `s0_win_pytest.windows` | 主線 | 9→9 | 9 筆恆等（`invalid2`、`msg2-expected2` 改回凍結的長 id） | 9／9 |
| `s21_gate:pytest_full.linux` | P5 | 11→13 | 11 筆恆等（含 `toml2-target-section` 改回凍結 id）＋add-only 2 筆：`…[invalid2]` → `…[aaaa…]`、`…[msg2-expected2]` → `…[could not…ecified.', None, 2)-expected2]` | 13／13 |

## 3. gate 與全套數字

### 3.1 T22 gate `r7-261029`（非 dry-run、非 root、受測 `1b83da9`）

- 命令：`SERIALWRAP_L15_SELFTEST_REPORT=… SW_GATE_WAL_S1W_IMPORT=<od61-wal-s1w-wsl2-86b54d8 三輪> SW_GATE_WAL_S1W_HOST=owner-wsl2-ext4-vhdx SW_GATE_WAL_S1W_PY_BASELINE=<同目錄 Python 基線> bash scripts/rust/slice21_gate.sh --evidence-run --run-id r7-261029`。
- 跑前確認無洩漏 daemon／bridge；loadavg 開跑 0.48／0.50／0.75（14:51:53Z）、結束 0.58／0.74／0.81（17:19:51Z），約 148 分鐘。**verdict FAIL**（exit 1）。
- **揭露**：15:04～15:44Z 旁支 worktree 的 `nice -n 19` 測試迴圈與 blackbox、l15_ab、event_latency 第 1 輪重疊；另有三次短時單行程 pytest（15:51～15:54Z、16:26～16:28Z、16:29～16:30Z，最後一次與 pytest_full 開頭重疊）。重疊期間各 section 皆通過、量測無作廢。

| section | 判定 | 數字 |
|---|---|---|
| cfg_boundary | PASS | — |
| linux_l1 | **PASS** | 1788／0／28（run_euid 1000） |
| linux_cap_posix_pty | **PASS** | 373／0／4（第 6 輪的 lock_suite 失敗與 lib 能力 skipped 皆消失） |
| linux_cap_sys_admin | EVIDENCE_MISSING | 探測 `euid=1000 CapEff=0`（BLOCKED-OWNER） |
| linux_cap_windows_native／interactive_desktop | EVIDENCE_MISSING | linux 恆不具備（LOCAL-WINDOWS-TODO／OWNER-BATCH #1） |
| linux_cap_supervisor_runtime | EVIDENCE_MISSING | `systemctl --user is-system-running=offline`（BLOCKED-OWNER） |
| windows_native_l1 | EVIDENCE_MISSING | 無原生 Windows |
| windows_cap_windows_com | EVIDENCE_MISSING | BO-S21-01（允許） |
| blackbox_python | **PASS** | 45／0／4（首次全綠：F205、`4f8acdf`） |
| blackbox_rust | **PASS** | 48／0／1 |
| existing_suites_rust | **PASS** | 4／0；func-test 3/3 |
| l15_ab | EVIDENCE_MISSING | pytest 全綠；只因 OD-58 承擔 section event_latency 未 PASS |
| event_latency | EVIDENCE_MISSING | Rust 18 輪全 PASS（max：stop 1.77～5.79、cancel 2.71～16.08、release 9.62～76.32 ms；上限 50／100、50／100、250／300）、L1 gate（含 OD-64 依賴情境 2 支）passed、證據身分核對通過；**唯一原因：未凍結** |
| wal_s1w_perf | **PASS** | OD-66 WSL2 門檻；三輪 append p50 0.0211～0.0216、p95 0.0291～0.0331、p99 0.0505～0.0591 ms，rotate max 41.3～55.2 ms，write_failures 0、每流跨越 rotate；皆優於同主機 Python 基線（p50 1.010／p95 2.251／p99 3.207 ms、rotate 902.3 ms） |
| case_uptake | **PASS** | 13／48（rate 0.271），斷路器未觸發 |
| py_parity | **PASS** | 18／0／0（`py_parity_full` 首次實跑） |
| pytest_full | FAIL | 5630／5／205：5 支皆在起點基準 7 failed 內（見 §3.2） |
| policy_check | PASS | pass 25／fail 0／warn 2 |

其餘：identity 齊全（CLI `22ca6f34…`、daemon `79cf7908…`、`daemon_version` 0.3.2、build Linux／Windows exit 0）；parity PASS 69／INCOMPLETE 34／BLOCKED-OWNER 2。Python 診斷（50 樣本）idle release_request 三輪 max 156.4／154.3／154.2 ms。紀錄：`p5-t22-gate-run.md` 第七輪（`8c0c942`）。

### 3.2 全套 `python3 -m pytest -q tests/`（非 root、序列執行、無並行負載）

| 時點 | failed | passed | skipped |
|---|---:|---:|---:|
| 起點 `86b54d8` | 7 | 5578 | 204（＋1 xfailed） |
| gate `r7-261029` 內（`1b83da9`） | 5 | 5630 | 205（含 1 xfailed） |
| 終點 `8c0c942` | **4** | 5644 | 204（＋1 xfailed） |

起點基準：`86b54d8` 工作樹（本輪開工、未改動前），uid 1000、python3.12、pytest 9.2.0，13:33～14:26Z（52 分 44 秒）。7 failed：`test_l1_judge.py` 3 支（`test_l1_lock_nodeids_subset_of_collect_only`、`test_l1_lock_frozen_set_excludes_capability_gated`、`test_l1_lock_cargo_sets_exclude_ignored_tests`）、`test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native`（需原生 Windows）、`test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`（EVIDENCE_MISSING）、`test_f2_backpressure.py::test_queue_full_backpressure`（F205 已修）、`tests/contract_blackbox/test_cli_mgmt_daemon.py::test_daemon_start_stop_ab_real_daemon`（失敗訊息為 multi_open 偵測到其他 daemon；當時我並行執行會啟動 daemon 的 sw-daemon cargo 測試與背壓探針。其後單檔隔離重跑 2/2 passed，gate 內與終點全套亦 passed。**未做政策要求的「同範圍連跑兩次、失敗集合不同」實測，故不主張為環境因素**：記為「基準失敗、其後通過，根因推定與並行 daemon 有關但未依政策取證」）。
**終點 4 failed＝起點 7 failed 去掉 `test_l1_lock_nodeids_subset_of_collect_only`（OD-67）、`test_queue_full_backpressure`（F205）、`test_daemon_start_stop_ab_real_daemon`（其後通過，見上）**：剩 `test_l1_judge.py` 2 支（OD-55 windows 能力列待原生收集）、`test_cargo_workspace_tests_windows_native`、`test_p4_required_cases_have_passing_evidence`。未引入新失敗，無須重跑佐證。終點跑前確認無洩漏 daemon、無並行負載（17:34:15～18:23:34Z，loadavg 1.55→0.68）。

## 4. 既有失敗追根因（不以 flaky 放行）

| 項目 | 根因（實測） | 處理 |
|---|---|---|
| `test_f2_backpressure.py::test_queue_full_backpressure`（Python 基線恆失敗） | **測試前提錯**，非 Python 基線行為：暫時探針實測每次 `serialwrap cmd submit` CLI 行程 0.12～0.18 s，300 筆約 37～40 s；前景 `sleep 30` 結束後佇列即開始消化，pending 到不了 256（Python `arbiter.py` `pending >= CMD_PENDING_MAX`）。Rust CLI 是原生 binary、送得快才通過 | `54dd588`（F205）依案例設計修正：前景期間以 `command.submit` RPC（同 CLI 預設參數）填佇列，斷言不變，另加 CLI 層溢位回應斷言。Python 基線 passed（36.5 s） |
| `sw-pal::lock::lock_conformance_suite`（r6 gate 失敗一次、無輸出） | **重現 145/2000**（`--test-threads=1` 0/1000、單獨 `--exact` 0/1000）：皆為 `[stale_socket_reclaimed] … AlreadyRunning`——lock_suite 以 bind→drop 製造失效 socket，同執行檔平行測試 spawn 子行程時，子行程在 exec 前持有 listener 複本，drop 後仍在 listen | `54dd588`（F206）：改為只 bind 不 listen；紅燈 `lock_conformance_suite_survives_concurrent_spawns`（修正前 5/5 紅），修正後原迴圈 0/2000；`8d824b3` 後 lock 執行檔整檔 300/0（§2.5） |
| 失敗輸出保存（sol 既有碼待辦 2） | `cargo_l1_results.py` 只解析 stdout 不轉印 | `54dd588`（F207）：rc≠0 時轉印 libtest `failures:` 段到 stderr，完整 stdout／stderr 存 `<報告>.failures/` |
| `cap_posix_pty__*` 並行間歇 `Held(pids:[同執行檔子行程])`（sol 既有碼待辦 1） | **重現 11/180**：(1) 夾具 `openpty` fd 不帶 CLOEXEC；(2) 即使 CLOEXEC，fork 後 exec 前的子行程仍暫持全部 fd 複本 | `54dd588`（F208）：夾具改 `posix_openpt(O_CLOEXEC)`＋`open(O_CLOEXEC)`（修正後 5/180）；`cargo_l1_results.py` 能力執行加 `--test-threads=1`（0/180）。產品開埠路徑不受影響 |

## 5. 不成立主張

1. 前輪「`test_queue_full_backpressure` 為 Python 基線時序相依失敗、根因未明」：根因是**測試的送出速率前提**，不是 daemon 行為。實測 Python 基線每次 CLI 行程 0.12～0.18 s，256 筆需 33～40 s，超過前景 `sleep 30`，佇列在填滿前即開始消化；Python `arbiter.py` 的 `pending >= CMD_PENDING_MAX` 判定本身正確（以 RPC 填佇列即回 `SESSION_QUEUE_FULL{pending:256, limit:256}`）。
2. 前輪「`lock_conformance_suite` 根因未明」：重現 145/2000，皆為 fork→exec 時窗的 listener 複本（§4），非產品 lock 邏輯缺陷。

## 6. SPEC-GAPS

1. **py_parity 範圍**：計畫 06 Task 0b 的 py_parity 定義另列 #76～#80 的測試檔（`tests/test_holder_probe_namespace.py` 等）；現行 `s21_gate:py_parity`／`py_parity_full` 只有 #81 類 18 筆。本輪「完整 selector」依裁決以現有 18 筆為準，#76～#80 是否補入須另行裁決。
2. **OD-66「同時段」無法驗證**：匯入的 Python 基線只能核對同主機（virtualization／cpu／cpu_count／memory_bytes／os_build／storage_device／fs_type）與同工作量；兩份證據的時間戳由擁有者主機產生、gate 無從核實是否同一時段。目前以可核對欄位判定，時段一致性靠擁有者附註。
3. **主線 Stage 0 集合的參數化 id（OD-67 收尾）**：`test_classify_pyserial_open_error`（`s0_bc.linux/windows`、`s0_win_pytest.windows`）與 `test_invalid_regex_rpc_and_arbiter_reject_without_enqueue`（`s0_win_pytest.windows`）已給明確 ids（沿用 pytest 9.0.3 自動 id 形式），主線集合凍結 nodeid 與新 id 逐字相同，**無須另派**；若主線日後改用其他 id 形式才需處理。

## 7. 既有碼待辦（不修）

- `crates/sw-pal/tests/fs_lock.rs` `file_lock_nonblocking_contention_wouldblock`：審查者 4 次 `linux_cap_posix_pty` 實跑中紅 1 次（同檔 `file_lock_cross_process` spawn 子行程，fork→exec 時窗持有 flock 複本），與 F206／F208 同類。
- `rpc_client_conformance` 仍以 bind→drop 製造失效 socket（同類潛在時窗，未見失敗）。
- 產品鎖後 holder 重掃只排除自身 pid；daemon 會 spawn 子行程（`sysenv/posix.rs`、`process_tunnel/posix.rs`），若 spawn 與 attach 並行，是否可能出現 `Held([自身子行程])` 未驗證（審查既有碼待辦 2）。
- 不帶 feature 的 `cargo test -p sw-pal` 因 `tests/registry_view.rs`（`read_bt_ports`）編譯失敗（`54dd588` 前即存在）；`python3` 為 3.13 時 policy_check R-16 fail（argparse help 欄寬與 README 標記不符，3.12 通過）。
- `slice21_gate.sh` 用法說明稱凍結檢查未過時「一律 INCOMPLETE、結束碼 3」，實際有 FAIL section 時 judge 判 FAIL／exit 1（r6、r7 皆是；審查既有碼待辦）。
- `emitted_codes` 寫入 repo 的 `target/`、`cargo check --target x86_64-pc-windows-gnu` 既有 dead_code 警告、`PA-OD-EVLAT` 註解過期、第五輪所列各項仍在。

## 8. OWNER-BATCH

1. **linux 列不可能 PASS 的能力集合**：`s21_gate:cap_windows_native.linux`（2 筆）與 `s21_gate:cap_interactive_desktop.linux`（1 筆）的測試本體需原生 Windows／互動 console（非 Windows 版刻意 panic），linux 主機永遠只能 EVIDENCE_MISSING。請裁決改由 Windows 原生集合承擔並逐名移除 linux 列，或接受其為永久 EVIDENCE_MISSING（目前 gate 的 verdict 因此不可能到 READY）。
2. **F205 案例設計修正的登錄**：`test_queue_full_backpressure` 改以 RPC 填佇列（斷言不變、另加 CLI 溢位斷言），係依編排者「案例設計修正」裁決；請於 rulings 登錄，避免下一輪審查視為改案例。
3. **無依賴情境的 receipt 排序（審閱 2 對 P5 OWNER-BATCH #1 的附註）**：無同 epoch 依賴時 safety lane 優先，Rust 的 `WorkerStopped` 會越過同 epoch 的 data backlog 先處理；Python 則是 reader join 後才產生回條（stop 前已讀 RX 一定先處理完）。不影響 OD-64 界限（N_pre＝0 時 ≤1），但屬順序差異：請裁決接受為差異（登錄 DF）或要求對齊。本輪未改。
4. **產品 holder 重掃與自身子行程**（§7 第 3 點）：是否列為 Slice 2.2 前須驗證項。

**編排者待辦**：(i) 核對 C1～C5（§2.4）後把 `contracts/resource_limits.yaml` event_latency 的 `freeze_status` 改 `frozen`；凍結 commit 後須重跑 gate 取新的 event_latency 證據（契約 sha256 會變）。(ii) OD-67 主線集合無須另派（§6.3）。

## 9. LOCAL-WINDOWS-TODO

- `windows_native_l1`、`windows_cap_windows_com`（BO-S21-01）；s21_core.windows 尚未登記本輪與前輪新增 nodeid。
- OD-55 windows：原生收集登記 `s21_gate:cap_{windows_com, windows_native, interactive_desktop, supervisor_runtime}.windows` 後，以 `--allow-removal` 自 s21_core.windows 移除對應列。
- `linux_cap_windows_native`／`linux_cap_interactive_desktop` 的 linux 列（OWNER-BATCH #1）。
- `tests/test_rust_cargo_l1_results.py` 的 Windows `.cmd` 假 libtest 本輪改為與 POSIX 同語意（argv 記錄、能力 run），需以 `scripts/stage0/win-pytest.sh` 實跑確認（本機無原生 Windows）。
- BO-S21-13 Windows 黑箱／L1.5 等前輪所列各項。

## 10. BLOCKED-OWNER

- `linux_cap_sys_admin`（需 root＋CAP_SYS_ADMIN 的可丟棄容器；以 `--caps-only` 執行，見 runbook）、`linux_cap_supervisor_runtime`（需 systemd user 執行期容器，OD-15）。
- BO-S21-01～16（實機／Windows）。

## 11. 距 Slice 2.1 出口還缺什麼

1. **event_latency 凍結**：編排者核對 C1～C5（§2.4）後改 `freeze_status: frozen`；凍結 commit 後重跑 gate（契約 sha256 改變，`r7-261029` 證據不再適用），即可解除 event_latency、l15_ab 的 EVIDENCE_MISSING 與 §5.3 preflight。重跑時不得並行任何負載。
2. **pytest_full**：gate 內 5 支皆為既有失敗——`test_l1_lock_nodeids_subset_of_collect_only` 已由 `3bbb40e` 修正；`test_l1_judge.py` 另 2 支（OD-55 windows 能力列）與 `test_cargo_workspace_tests_windows_native` 需原生 Windows 收集（LOCAL-WINDOWS-TODO）；`test_p4_required_cases_have_passing_evidence` 待 P4 擁有者證據。
3. **能力 section**：`linux_cap_sys_admin`／`linux_cap_supervisor_runtime` 須在對應容器實跑（`cc25b01` 起以 `--caps-only`，BLOCKED-OWNER）；`linux_cap_windows_native`／`linux_cap_interactive_desktop` 待 OWNER-BATCH #1 裁決。
4. **Windows 原生**：`windows_native_l1`、`windows_cap_windows_com`（BO-S21-01）、OD-55 windows 搬移、BO-S21-13。
5. **#104 parity**：INCOMPLETE 34／BLOCKED-OWNER 2 未結。
6. OWNER-BATCH #1～#4、SPEC-GAPS #1～#2；slice 出口整包審查（雙審）。
