# feature/rust-refactor d2118cd..be2c4c6 非實作者審查（Claude Opus）

## 範圍

- 分支 `origin/feature/rust-refactor`，`d2118cd..be2c4c6`，共 11 個 commit、31 個檔案（+930/-82）。範圍內沒有 merge commit。
- 依序為：`fdbb148`、`7f195f7`、`6f7de84`、`80514c5`、`63d9bdb`、`d7d8e9e`、`9a710b6`、`37166b3`、`ab8515d`、`6ad67f1`、`be2c4c6`。
- 本範圍的產品程式只有兩處變更：`crates/sw-pal/src/process/windows.rs`（只加註解）和 `crates/sw-pal/tests/service_ctl.rs`（測試清理）。其餘是測試、oracle 腳本、凍結鎖、政策設定與文件。

## 依據

- `docs/rust-refactor/serialwrap-rust-spec-final.md`（§132 startup_100、SC-18）
- `docs/rust-refactor/owner-decisions-2026-10-15.md`：OD-23～OD-30。OD-24、OD-25 在 `fdbb148`／`7f195f7` 登錄，OD-26～OD-30 在 `6f7de84` 登錄。
- `docs/rust-refactor/owner-decisions-addenda.md`、`owner-decisions-s1x-g-list.md`
- docs/parity-amendments 的 `plans/parity-amendments-2026-10-26.md`，以及修訂後的 plans 00／04／05／09
- Python 基線 `sw_core/*.py`
- 前次報告 `reviews/s1-cloud/0e97195_d2118cd-opus.md` 的 3 個 Low，以及 `reviews/s1x-oracle-review-1.md` 的 High、Medium、Low 是否已在本範圍處理

## 方式

- 用 `be2c4c6` 建唯讀 detached worktree 做主審查，另建 scratch worktree 做反證：還原產品修正或注入變異後實跑，確認測試會紅。兩個 worktree 用完都已清理並移除。`CARGO_TARGET_DIR` 使用指定的獨立目錄，cargo 一律 `-j2`。
- 凍結鎖：以 base 與 HEAD 的 JSON 逐 set、逐 platform 比對差集，再用 `regen_lock.py --check --base d2118cd` 驗證。不帶核准跑一次；再帶 CI 的 `--allow-removal` 清單跑一次，清單從 `tests.yml` 以 shlex 抽出，共 550 個三元組。
- 本機為 root，沒有 Windows、`powershell.exe` 與 WSL interop。凡是宣稱「Windows 原生」或「擁有者機台」實跑的項目，只能核對證據指標可不可追溯，無法重跑。
- 報告內不寫任何家目錄絕對路徑（理由見 6ad67f1 一節）。

## 結論

- **High 0、Medium 2、Low 7。**
- 凍結鎖的變更符合規定：移除恰好 5 筆，與 OD-24 逐名列出的三元組完全相同；其餘都是 add-only 新增。
- cfg 邊界檢查通過。
- 本範圍的回歸測試都經實跑反證：還原修正後確實會紅。
- 前次報告的 3 個 Low 已由 `ab8515d` 和 `9a710b6` 處理。oracle 審查 1 的 High 由 `63d9bdb`、Low 由 `d7d8e9e`、Medium 由 `be2c4c6` 處理。
- 兩個 Medium 都是證據可追溯性問題：宣稱 Windows 原生或擁有者機台已實跑的項目，repo 內只有截斷的 sha256，或完全沒有原始輸出指標（`37166b3`、`7f195f7`）。
- 沒有發現 Windows 原生項目被假裝完成：OD-29 (4) 的 ctrl_c 案例誠實標為 BLOCKED-OWNER，startup_100 的回歸測試在本機正確 SKIP，並寫明「證據不足，非 PASS」。

## 實際執行的驗證命令與結果

| # | 命令（於 be2c4c6 worktree，除非另註） | 結果 |
|---|---|---|
| 1 | `python3 scripts/rust/check_cfg_boundary.py` | rc 0 |
| 2 | `cargo check -j2 --target x86_64-pc-windows-gnu --workspace --all-targets` | rc 0（只有 warning） |
| 3 | `cargo test -j2 --workspace --all-features --no-fail-fast` | 共 81 個 test result：895 passed、2 failed、7 ignored。2 個失敗是已知 root 基準：`sw-pal::oserror_golden::file_eacces_matches_golden`、`sw-pal::rpc_client_conformance::unix_fixture_rows_match_python`；`holder_scan_permission` 這次沒有失敗。新增的 `autostart_resend_carries_original_command_params`、`start_windows_python_launcher_without_runtime_does_not_fall_through` 都 ok |
| 4 | 自寫腳本逐 set／platform 比對 `l1_required.lock.json`（d2118cd 對 be2c4c6） | 移除 5 筆，都在 `s1b_cargo`：linux 的 `mkbundle_respects_cargo_target_dir_override`；windows 的 `mkbundle_respects_cargo_target_dir_override`、`exec_or_wait_tree_{delegate,grandchild,launcher}_entry`。新增 4 筆：兩平台各有 `autostart_resend_carries_original_command_params` 與 `start_windows_python_launcher_without_runtime_does_not_fall_through`。selector 沒有變更 |
| 5 | `python3 tests/acceptance/regen_lock.py --check --base d2118cd`（不帶核准） | 恰好 5 行 FAIL，與 #4 的 5 筆相同 |
| 6 | 同上，加上 CI 的 `--allow-removal`（550 個三元組） | `OK：L1 必要集合鎖結構與基準比對一致`，rc 0 |
| 7 | 核對 OD-24 的承擔者 | `tests/test_rust_mkbundle.py::test_mkbundle_respects_cargo_target_dir_override` 在 `s1_pytest_full.linux`。`exec_or_wait_terminate_kills_default_grandchild`、`exec_or_wait_normal_exit_keeps_detached_grandchild`、`exec_or_wait_refuses_breakaway_grandchild` 同時在 `s1a_cargo.windows` 與 `s1b_cargo.windows`；三個 entry 在原始碼確實帶 `#[ignore = "委派 entry…"]` |
| 8 | `python3.12 -m policy_check --repo .` | R-21 pass；R-19、R-22 為 warn；其餘 pass |
| 9 | scratch：刪掉 `.project-policy.yml` 對 `s1x-oracle-review-1.md` 的 allow 後再跑 policy_check | R-21 FAIL，`structural:1, credential:0, marker:0`，唯一命中為 `docs/rust-refactor/reviews/s1x-oracle-review-1.md:3` |
| 10 | `pytest -q tests/test_l1_judge.py tests/test_rust_cargo_l1_results.py tests/test_resource_report.py tests/test_stage0_wal_av_sim.py tests/test_rust_win_startup_100.py` | 113 passed、9 skipped。`test_rust_win_startup_100.py` 的 7 支全部 SKIP，原因是「需要 WSL interop（powershell.exe／wslpath）與 mingw（證據不足，非 PASS）」 |
| 11 | `PATH=<假 cmd.exe，exit 1>:$PATH pytest -q tests/test_rust_win_scripts.py` | 31 passed、7 failed、1 skipped。7 個失敗都是 `test_runner_*`／`test_win_test_*`，需要真的 cmd.exe；同一指令在 d2118cd 也是同樣 7 個失敗，屬環境因素，不是回歸 |
| 12 | scratch 反證 `fdbb148`：把 `regen_lock.py` 與 `cargo_l1_results.py` 還原到 d2118cd | `test_l1_add_only_excludes_ignored_nodeids_od24[linux/windows]`、`test_l1_add_only_ignored_existing_rows_kept_unless_approved_od24`、`test_list_only_feeds_regen_lock`、`test_list_only_keeps_ignored_non_cap_in_denominator`：5 failed |
| 13 | scratch 反證 `fdbb148`：把鎖檔還原到 d2118cd | `test_l1_lock_cargo_sets_exclude_ignored_tests` FAIL，列出 5 個三元組 |
| 14 | scratch 反證 `63d9bdb`：保留 `real_sleep(0.03)`，break／lease 還原為舊寫法（+25 ms／50 ms，rename 固定 sleep 60 ms） | 3/3 FAIL（`INSUFFICIENT_EVIDENCE`） |
| 15 | HEAD 連跑 `-k records_no_delete_primary_and_oplock_variant_separately` | 無人為負載：第一輪 20 次有 1 次 FAIL（當時另兩個審查的 cargo 同時在跑，失敗內容沒有保存），之後 40＋60（4 路並行）＋30（8 支 CPU 忙迴圈）次都 PASS。另加 6 支 CPU 忙迴圈與 `dd … conv=fsync`：40 次中 2 次 FAIL；scratch 加探針重跑 60 次中 5 次 FAIL，並抓到 1 次的完整時間值（見 63d9bdb Low 1） |
| 16 | scratch 反證 `d7d8e9e`：對 `test_cli_mgmt_daemon.py` 反向套用該 commit | `test_windows_com_autostart_rejected_request_delayed_tx_is_counted` FAIL；HEAD 上 `-k autostart` 11 支全部 PASS |
| 17 | scratch 反證 `9a710b6`：對 `win-blackbox.ps1` 反向套用該 commit | `test_windows_com_bridge_stop_exception_still_copies_back_evidence` FAIL |
| 18 | scratch 變異 `be2c4c6`：`rpc_cmds.rs:298` 自動啟動後重送改傳 `&PyValue::Null` | `cargo test -p sw-cli --test mgmt_daemon autostart`：16 passed、1 failed，唯一 FAIL 是新增的 `autostart_resend_carries_original_command_params`。既有的 `autostart_only_when_request_never_sent` 對此變異仍 PASS，證明新測試補上的是真實缺口 |

### fdbb148 fix(l1): add-only 排除 #[ignore] 列並依 OD-24 移除 s1b_cargo 5 筆

逐項核對結果：

- 移除的 5 個三元組與 OD-24 (3) 逐名一致（驗證 #4、#5、#6）。CI 的 `--allow-removal` 清單與 `tests/test_resource_report.py` 的 pinned 清單都逐筆列名。`test_od24_ignored_removal_triples_are_approved_in_ci` 同時核對 OD-24 列、CI 清單與 PENDING 列的 RESOLVED 狀態。
- 收集數量自洽：
  - linux 收集 904 筆、ignored 7 筆，鎖內 897 筆。
  - windows 收集 741 筆、ignored 8 筆，鎖內 733 筆。
  - 兩平台的 selector 都與收集參數一致（windows 為 `--package sw-pal --package sw-cli`）。
- `regen_lock.py` 的語意變更：
  - 新增時排除 ignored。
  - 鎖中既有的 ignored 列仍算 collected，不視為消失。
  - 只有逐名核准才能移除既有 ignored 列，計算式為 `removed |= allowed ∩ spec ∩ ignored`。
  - 核准一個「仍收集且未 ignored」的列，會被 `unused_approval` 拒絕，有測試覆蓋。
  - collect JSON 的 `ignored ⊄ collected` 時 fail-closed。
  - `new_spec` 由 `collected ∪ (spec − removed)` 改為 `added ∪ (spec − removed)`，兩者只差在 ignored 列，符合 OD-24 (1)。
- `test_list_only_*` 兩支測試的預期由 `[A,B,C]` 改為 `[A,B]`，屬 OD-24 授權的行為變更，不是削弱：plan 04 的表格同步改寫，「ignored 仍在分母」的斷言保留，並新增「既有 ignored 列保留」的斷言。
- 回歸測試在修正前會紅（驗證 #12、#13）。

1. [Low] `tests/test_l1_judge.py:806`（以及本 commit 新增的 `test_l1_add_only_*_od24`、`tests/test_resource_report.py::test_od24_ignored_removal_triples_are_approved_in_ci`）
   - 問題：這幾支防線測試本身不在任何 L1 凍結集合（以 nodeid 子字串查 `l1_required.lock.json`，結果為空）。日後刪除或改名不需要 `--allow-removal`，防線可能被無聲撤掉。同類的既有防線（例如 `test_l1_lock_frozen_set_excludes_capability_gated`、`test_od23_*`）也一樣沒有入鎖，屬既有模式。
   - 修法：以 `regen_lock.py --l1-add-only --set s1_pytest_full --platform linux --collect <pytest --collect-only 輸出>` 把上述 nodeid 登記進凍結集合，並在 addenda 註明。

### 7f195f7 fix(stage1a): 依 OD-25 將 startup_100 判定改為隔離環境

程式碼審查（本機無法執行，驗證 #10 的 7 支都 SKIP）：

- 隔離根目錄建在 `[System.IO.Path]::GetTempPath()` 之下（`win-startup-100.ps1:93`）。執行前檢查恰好只有 2 個項目，否則記入 `snapshot_errors`，即 fail-closed。
- `TEMP`、`TMP`、`LOCALAPPDATA` 以 Process 層級設定，並在 `finally` 還原。`Start-Process` 帶重導向時走 `UseShellExecute=false`，子行程繼承本行程環境，所以隔離設定會生效。
- 隔離目錄內任何新增都計入 `isolated_new`，即 FAIL，比舊版只看目錄／exe／dll 更嚴。
- exe 目錄的條件不變。
- ambient 快照排除輸出目錄、隔離目錄與 exe 目錄，只做記錄。
- `ok` 的組成與 OD-25 文字逐項相符。
- 下游 `stage1a_gate.py:1057` 與 `win-blackbox.sh:207` 只讀 `ok`，新欄位不影響它們。
- 規格 §132 的判定對象是「CLI 造成的新增」，OD-25 已明列不涵蓋以 known-folder API 取路徑的解壓器，擁有者已知悉。

1. [Medium] `docs/rust-refactor/owner-decisions-addenda.md:100`（CHG-S1X-WIN-STARTUP100-ISOLATION-01）
   - 問題：本列與 commit 訊息宣稱三項原生實跑結果，但 repo 內沒有任何可追溯指標：
     - 「舊版 6 紅 1 綠、新版 7 綠」：透過 WSL interop 的原生 PowerShell 執行；
     - 「以實際 Rust release CLI 於真實環境試跑 `ok:true`」；
     - `ambient_changes` 記到 Claude-Data IndexedDB 等項目。
   - 缺的指標包括 run 目錄、`startup-100.json` 的 sha256、受測 exe 的 sha256、pytest 原始輸出檔。
   - 這是 oracle 變更，雲端審查者無法重跑（7 支測試都會 SKIP），所以這三項宣稱目前只能取信於實作者自述。另外，本列仍寫著「待非實作者審查」。
   - 修法：
     - 在本列補上兩次 `tests/test_rust_win_startup_100.py` 原生執行的原始輸出檔路徑與完整 sha256（舊腳本 6F1P、新腳本 7P）。
     - 補上實際 Rust CLI 試跑的 `startup-100.json` 路徑、完整 sha256、`exe_sha256` 與來源 commit。
     - 本報告的程式碼審查結論可登記為「程式碼審查 ACCEPT、原生實跑證據待補指標」。

### 6f7de84 docs(rust-refactor): 登錄 OD-26～OD-30 並補 OD-24 g-list 註記

核對結果：

- `windows.rs` 只加註解，沒有改程式。
- OD-27 寫明「不得記為 PASS、不得刪除案例」，plan 00 §3 的步驟與此一致。
- OD-28 的授權範圍限定在 throwaway 工作，且 Stage 1 不執行，plan 09 的 BLOCKED-OWNER (1) 已註明。
- OD-30 (2) 維持 ST-12 `NOT_RUN`，沒有新增 `NOT_RUN` 允許。
- g-list 的 OD-24 補註與「不得由工具自動排除」的規定不衝突：工具只在新增時排除，不移除既有列。

1. [Low] `crates/sw-pal/src/process/windows.rs:42`
   - 問題：註解寫「正式 `sw-cli` 無執行緒」，與實情不符。`crates/sw-pal/src/sysenv.rs:163` 的 `start_pipe_reader` 會開 `thread::spawn` 讀子行程 pipe，正式 CLI 會走到這條路徑。結論本身不受影響，因為該執行緒不會建立行程，與 `spawn_daemon` 沒有並行 spawn，但 OD-26 (c) 以「程式碼審查為證」，前提敘述應該準確。
   - 修法：改為「正式 `sw-cli` 沒有並行建立行程的執行緒（`sysenv` 的 pipe reader 執行緒只讀取、不 spawn）」，OD-26 (c) 的文字同步修正。

### 80514c5 docs(rust-refactor): 依非實作者 oracle 審查 ACCEPT 標記五列 APPLIED 並結案 OD-26／OD-30 相關列

核對結果：

- 以字詞級 diff 檢查 addenda 的 7 列，只有狀態改寫、補 commit hash、補 OD-26／OD-30 註記，沒有刪除既有內容。
- 5 個 ACCEPT 與 `s1x-oracle-review-1.md` 摘要表一致。
- 該審查補記的 commit hash 缺漏（Low）已補上。

1. [Low] `docs/rust-refactor/owner-decisions-addenda.md:98`（CHG-S1X-SC18-TX-BASELINE-01）
   - 問題：同一列開頭改成「已由非實作者 Claude Opus 審查 ACCEPT」，結尾卻仍保留「仍待非實作者審查。」，自相矛盾。
   - 修法：刪除該列結尾的「仍待非實作者審查。」。
2. [Low] `docs/rust-refactor/reviews/s1x-oracle-review-1.md`（全檔）
   - 問題：這份文件宣稱「逐字複製」，但沒有可以獨立核對的原始輸出指標（審查 session 連結或原始輸出檔的 sha256）。這與前次報告對 `1bccc4a` 的 Low 1 同類，而 `ab8515d` 已為 bridge 審查補上同類指標。
   - 修法：比照 `ab8515d`，在文件頂部或 addenda 引用處補一列「原始輸出檔路徑＋完整 sha256」或審查 session id。

### 63d9bdb test(stage1): Spike 3 主／oplock 分開記錄案例的替身改由 rename 請求觸發 break

- 這次修正處理 oracle 審查 1 的 High。替身改由 rename 請求觸發 break，lease 200 ms，並常駐 30 ms 排程延遲；judge 沒有改。舊替身加上同一延遲必紅（驗證 #14），修正方向正確。
- 舊的 High 問題是「break 寫死在 +25 ms」，已解決。

1. [Low] `tests/test_stage0_wal_av_sim.py:311`
   - 問題：替身仍以固定的 200 ms lease 涵蓋「lease 取得 → rotation 請求」這段延遲，而這段延遲受排程與 I/O 影響，沒有上限。
   - 實測（驗證 #15）：
     - 無人為負載時，HEAD 第一輪 20 次有 1 次 FAIL，當時另兩個審查的 cargo 正在跑，失敗內容沒有保存；之後 130 次都 PASS。
     - 加 CPU＋fsync 壓力時重現 5/60。其中一次抓到的值：`rotation_requested_ns − lease_acquired_ns = 391 ms`，大於 200 ms lease，所以落到 `INSUFFICIENT_EVIDENCE`。
     - 同一次的 append 2 延遲為 1.08 s，本身已超過 OD-22 的 600 ms。所以這次重現屬於系統飽和，依 CLAUDE.md 不判為產品回歸。
   - 不過「無人為負載仍有 1/20」這點無法完全排除，殘餘的時序依賴仍在。
   - 修法：在本測試對 `module.time.perf_counter_ns`（以及 `wal` 量測使用的時鐘）注入可控的虛擬時鐘：arm_hold、rename 與 append 都推進虛擬時間，使 lease 窗口、break 時點與 append 延遲樣本全部確定，不再依賴真實排程。若暫不改，至少在失敗訊息加上 `lease_acquired_ns`、`rotation_requested_ns`、各 append 延遲，方便日後區分飽和與回歸。

### d7d8e9e fix(stage1b): SC-18 被拒請求零 TX 檢查前等滿 wire 落盤上限

NO FINDINGS。

- 修法完全依照 oracle 審查 1 的 Low 建議：記錄 `initial_returned_at`，零 TX 檢查前補足 `_WIRE_FLUSH_WAIT_S`，再 settle。判準不變，仍要求基線後恰好是 `b"echo X\n"`。
- 回歸測試 `test_windows_com_autostart_rejected_request_delayed_tx_is_counted` 在修正前會紅（驗證 #16）。
- 單元測試把落盤上限縮為 1 s，只影響測試時間，不影響判準。
- Python 基線對應：`cmd submit --selector COM0 --cmd "echo X"`，DUT 端預期收到 `6563686f20580a`。被拒路徑 `sw_core/service.py` 的 `SESSION_NOT_READY` 在進 arbiter 前就回傳，零 TX。

### 9a710b6 fix(stage1): Windows COM bridge 停止例外路徑也回寫本機證據

NO FINDINGS。

- 處理前次報告 7c94c31 的 Low 1 與 Low 2：
  - 主流程 catch 與 finally 兜底都在 taskkill 之後回寫證據。
  - `Copy-WindowsComBridgeEvidence` 內部自己 try/catch 並回傳錯誤清單，所以主 catch 不會因回寫失敗而丟失 `bridge-stop.json`。
  - 判定維持 FAIL。
  - B 輪另記 `bridge_wire_output`，路徑與 `$bridge.output_dir` 的回寫位置一致（`win-blackbox.ps1:1219` 的 `bridge_wire`）。
- 回歸測試在修正前會紅（驗證 #17）。
- 備註：這支測試是 PowerShell 原文的結構檢查，不是執行期驗證；本機無 PowerShell，執行期行為沒有實測。

### 37166b3 test(stage1): 依 OD-29 實跑擁有者機台 cargo 測試並登記證據

- OD-29 (4) 的 `cap_interactive_desktop__exec_or_wait_ctrl_c` 誠實標為 BLOCKED-OWNER。runbook 給出完整 exe sha256、來源 commit 與預檢指令，沒有假裝完成。
- 沒有回填凍結鎖（驗證 #4），符合 OD-29。

1. [Medium] `docs/rust-refactor/owner-decisions-addenda.md:101`（CHG-S1X-OD29-OWNER-CAPS-01）
   - 問題：5 支測試標為「已實證（OD-29）」，其中 4 支是 Windows 原生，但證據不可獨立核對：
     - 所有 sha256 都截斷成 8 個十六進位字元（`94a4a1aa…`、`c898c6cf…`、`80e5f8be…`、`92eda874…`、`985f70da…`、`4f19f7c7…`、`b369d688…`、`819c1155…`、`f2c2e9b3…`）。
     - 報告檔放在 `scripts/rust/win-test.sh` 寫入的 `<worktree>/target/win-test-report/<run_id>.json`。`target/` 在 `.gitignore` 裡，屬單一 worktree 的暫存，`cargo clean` 或移除 worktree 就會消失。
     - 本列沒有寫出報告檔的完整路徑，也沒有原始 libtest 輸出。
   - OD-29 本身要求「附 run id／sha256」，截斷的雜湊不足以讓第三方核對檔案。
   - 修法：
     - 逐項補上完整 64 字元 sha256 與檔案完整路徑。
     - 把這些報告檔、l1-results、log、systemd 快照複製到不會被清掉的證據位置（例如 `~/b-log/rust-refactor/od29/<run_id>/`，或比照 `evidence/stage0/owner/` 入庫），並在本列改指該位置。
     - `windows_switch_running_exe_rename` 另補受測 exe 的完整 sha256 與來源 commit。
2. [Low] `crates/sw-pal/src/service_ctl/systemd.rs:414`
   - 問題：systemd-user 的 hold release 只移除 drop-in 檔，會留下空的 `<name>.service.d` 目錄。本 commit 只在測試的 Cleanup 補了 `remove_dir`（`crates/sw-pal/tests/service_ctl.rs:1285`），產品路徑對正式的 `serialwrap.service` 仍會在使用者的 `~/.config/systemd/user` 留下空目錄。
   - Python 基線沒有 hold 機制，不涉及 1:1。空 drop-in 目錄對 systemd 無害，所以列 Low。
   - 修法：在 `release_named` 的 SystemdUser 分支 `remove_file` 成功後，對 `path.parent()` 做 best-effort `remove_dir`（目錄非空就忽略錯誤）；SystemdSystem 分支在 `sudo rm -f` 後補 `sudo rmdir --ignore-fail-on-non-empty <dir>`。測試 Cleanup 的 `remove_dir` 可以保留，作為兜底。

### ab8515d docs(rust-refactor): bridge 第 4 次核准原文補原始輸出檔 sha256 指標

NO FINDINGS。

- 處理前次報告 1bccc4a 的 Low 1：三份原始輸出檔都附上完整 64 字元 sha256 與路徑，`APPROVED_REVISION.json` 沒有變更。
- 檔案在擁有者 host，本機無法比對雜湊，但指標本身完整、可獨立核對。

### 6ad67f1 chore(policy): R-21 放行逐字複製的雲端 oracle 審查原文

- 範圍檢查：allow 只列一個確切路徑，沒有用萬用字元。刪掉這條 allow 後，R-21 只有 1 個 structural 命中，位於該檔第 3 行，即雲端容器工作樹路徑（驗證 #9）。

1. [Low] `.project-policy.yml:20`
   - 問題：`secret_scan.allow` 依 policy engine 的 `_is_exempt` 是**整檔豁免**。它不只放行家目錄路徑樣式，連 `_PRIVATE_KEY`、`_AWS_ACCESS_KEY`、`_GITHUB_TOKEN` 這些 credential 偵測器與 marker 偵測器也一併停用。為了 1 行路徑，該檔日後任何編輯（例如補記、彙整者抽查段）都失去憑證外洩防線，豁免範圍比需要的寬。
   - 另外，日後每份雲端審查原文都可能需要新增 allow；前次報告 `0e97195_d2118cd-opus.md` 與本報告都沒有寫入家目錄絕對路徑，可見原文不含此類路徑是做得到的。
   - 修法（擇一）：
     - (a) 把第 3 行的路徑改成 `<雲端容器工作樹>`，並在該行加註「原文此處為雲端容器路徑，依 R-21 遮蔽，其餘逐字」，然後移除 allow。
     - (b) 若擁有者堅持逐字，保留 allow，另加一支 pytest 防線：以 policy engine 同一組正規式掃描該檔，斷言 credential 與 marker 命中為 0，且 structural 命中恰為第 3 行這 1 處。
   - 同時在雲端審查的 routine 指示中要求原文不寫家目錄絕對路徑。

### be2c4c6 test(stage1b): L1 驗證 SC-18 自動啟動後重送的 command.submit params 與原請求相同

NO FINDINGS。

- 處理 oracle 審查 1 的 Medium。新測試以「daemon 存活時同一命令列送出的 params」為基準，斷言以下各項：
  - 重送的 `cmd == "echo X"`、`selector == "COM0"`；
  - 整體 params 相同；
  - `command.submit` 請求數為 1；
  - spawn 次數為 1；
  - stderr 留有「已自動啟動」。
- 產品 `rpc_cmds.rs:294-300` 重送時傳同一個 `&params`。變異驗證（驗證 #18）顯示只有新測試能抓到重送 params 被改。
- 鎖只做 add-only，兩平台各 1 筆（驗證 #4）。
- Python 基線沒有 on-demand 自動啟動（`sw_core/cli.py` 查無此功能），SC-18 是 Rust 新增行為，所以這裡以 Rust 自身的直接送出路徑當基準是合理的。

## High／Medium 摘要

| 等級 | commit | 位置 | 摘要 |
|---|---|---|---|
| Medium | 7f195f7 | `docs/rust-refactor/owner-decisions-addenda.md:100` | OD-25 oracle 變更宣稱的原生 PowerShell 回歸（舊 6F1P、新 7P）與實際 Rust CLI `ok:true` 試跑，沒有任何 run 目錄、報告 sha256、exe sha256 或原始輸出指標；雲端無法重跑，宣稱不可追溯 |
| Medium | 37166b3 | `docs/rust-refactor/owner-decisions-addenda.md:101` | OD-29 的 5 支「已實證」（4 支 Windows 原生）只附 8 字元截斷 sha256，報告檔放在會被清掉的 `target/win-test-report/`，沒有完整路徑與原始輸出，第三方無法核對 |

High：0。

Low（7）：
- fdbb148#1：防線測試沒有入鎖。
- 6f7de84#1：OD-26 註解「無執行緒」不準確。
- 80514c5#1：TX-BASELINE 列自相矛盾。
- 80514c5#2：oracle 審查原文缺原始輸出指標。
- 63d9bdb#1：替身仍依賴固定 200 ms lease 的真實時序。
- 37166b3#2：產品 hold release 留下空 `.service.d`。
- 6ad67f1#1：R-21 整檔豁免連 credential 偵測也停用。
