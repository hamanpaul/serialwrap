# wp510r 第 26 項：Windows 證據版本核對與 DR-1(d) 確定性回歸

起點 `2a9a9bc423b46ae3e32802033f3a3605c82d5582`，分支 `feature/rust-refactor-p8`。僅處理第 8 次整包審查的 Windows 證據 Medium，以及擁有者 2026-11-12 補充裁決指定的 DR-1(d)。本報告是實作與驗證交付，逐項審查由 driver 接續。

## High／Medium 處理

- **Windows 證據 Medium：已修正。** `slice21_gate.sh` 啟動時固定候選 commit，匯入及 assemble 使用同一值。三個 OD-55 Windows 能力 section 改由 `slice21_gate_judge.py` 的 `import-windows-section` 匯入；在複製、宣告能力可用之前，核對 `platform=windows`、`finished=true`，以及 `provenance.git` 與 `provenance.git.end` 的 `head` 均為候選、`clean` 均為真正的布林 `true`。缺失、版本不同或工作樹不乾淨均拒收，片段記錄原因與 `LOCAL-WINDOWS-TODO`；重判也執行相同檢查，回 `EVIDENCE_MISSING`／`INSUFFICIENT`，不向 parity 的 passed pool 提供節點。合法證據才交給既有 L1 judge；其中的失敗仍為 FAIL。
- **DR-1(d)：已修正測試情境。** 舊測試以 `hold_actor_and_fill_control` 暫停 actor、填滿 control lane，但 `ActorHandle::request_with_ack_notice` 把 `ReleaseWithAck` 送往 safety lane（`actor.rs:274-282`）。第一次 release 實際已入列，500 ms 只是呼叫端等回覆逾時；放行 actor 後它仍會執行，後續 release 因而和先前請求的首次期限及關閉義務交錯。這是設計 DR-1(e) 的情境，不能證明 DR-1(d)「未入列」。新增前置斷言直接在起點失敗，確認 safety lane 留有請求。現在另填滿真正的 safety lane，確認逾時後佇列長度不變，並在放行後以同步查詢確認兩條 lane 排空，再送下一個 release。所有原有結果斷言保留，另加強情境斷言；不修改產品的關閉、期限或取消語意。產品來源唯一新增項為 `#[cfg(feature = "test-fakes")] actor_safety_for_test`。

Python 對照：`origin/main:sw_core/session_manager.py:3117-3125` 僅在實際受理 release 時設定首次期限與取消；`:3188-3204` 等待該作業；`:4339-4355` 於成功關閉後收斂。已受理的請求不因呼叫端逾時而撤銷，因此測試必須明確區分「未入列」與「已入列但未處理」。

本項沒有新增 actor 事件、請求或效果，T14 的 Open／Pending／Permanent 分類不變。凍結節點保留名稱、原步驟順序及原結果斷言；沒有刪除、放寬斷言或更改 FAIL／SKIP。新 Python 回歸實際收集 34 個節點，以 `regen_lock.py --l1-add-only` 分別增登 `s21_gate:pytest_full.linux` 與 `p7_gate:python3-m-pytest-q-tests.linux`，每集合新增 34、移除 0；既有鎖列保留，未宣稱重收其他工作包。

## 紅燈、負載與接線證據

輸出統一位於 `target/s2-par/wp510r/i26/`。

- `gate-red.log`：未修判定器時，新回歸 **28 failed、6 passed**；包含三個 section 的舊 commit、缺 provenance、髒工作樹、結束狀態不符與候選缺失。修正後 `python-focused.log` 為 **149 passed**，包含三個 section 的合法證據交 L1 judge 判 PASS／FAIL，以及實際匯入入口產生片段後再重判的接線測試。
- `dr1-red.log`：新增「逾時的 release 不得已入列 safety lane」前置斷言，未修情境 **0 passed、1 failed**。修正後 `dr1-green.log` 為 **1 passed**。
- 在另一個前景命令執行 `cargo test --workspace --features test-fakes --no-fail-fast` 的負載下，修改前 DR-1(d) **30 次中 26 failed、4 passed**（`before-load-dr1-summary.json`、逐次 `before-load-dr1-*.log`）；修正後 **30 passed、0 failed**（`after-load-dr1-summary.json`、逐次 `after-load-dr1-*.log`）。兩組都等所有程序結束；DR-1(d) 全程使用記憶體 ManagerRig／FakeSerial。
- Gate 接線：shell 固定候選 → `import-windows-section` → `windows_evidence_problems` → 同一份 bytes 驗證／複製／SHA256 → `record-section` → `judge` → `_judge_test_section` 再核對 provenance → 既有 `l1_judge.judge`。沒有外部證據時仍是 `LOCAL-WINDOWS-TODO`，沒有 Windows 原生執行或 COM 證據。
- Release 接線：`Service::rpc("device.release")`（`service.rs:854`）→ `SessionManager::route` → `route_inner` → `route_dispatch` → `route_after_release_settle` → `ActorHandle::request_with_ack_notice` 的 safety lane → actor `ReleaseWithAck` → `ClaimByIdForRelease` → closer → `CustodyTerminal` → RELEASED／持久化／RPC 回覆。`servicerig-release.log` 執行既有正式 `ServiceRig` 案例 `dispatch::wp510_review9_rpc_release_with_parked_handle_of_bound_session_closes_it`，**1 passed**；驗證 RPC 成功前 handle 已關閉、只關閉一次、紀錄移除、沒有重開或額外 TX。

## 驗收

| 檢查 | 結果與證據 |
|---|---|
| 受影響 crate：`cargo test -p sw-core --features test-fakes --no-fail-fast` | 重跑 **1737 passed、0 failed、1 ignored**，`core-fakes-rerun.log`；首次結果與起點對照詳下節 |
| `cargo test --workspace --no-fail-fast` | **1652 passed、3 failed、46 ignored**，`workspace.log`；3 項均在起點重現，保留 FAIL |
| `cargo test --workspace --features test-fakes --no-fail-fast` | **3457 passed、50 failed、51 ignored**；50 個失敗 id 全部於起點匹配，見 `workspace-fakes-final.log`、`test-summary.json`、`workspace-baseline-comparison.json`；保留 exit 101 |
| `cargo build --workspace --all-targets` | exit 0；候選與起點相同 18 組來源警告，新增 0，`build.log`、`baseline-build.log`、`build-warning-comparison.json` |
| workspace clippy | exit 101；起點既有 lint 錯誤，未豁免，`clippy.log`、`baseline-clippy.log` |
| `cargo clippy -p sw-core --all-targets --features test-fakes --no-deps` | exit 0；候選與起點相同 74 組警告，扣除新增 accessor 的 7 行位移後新增 0；`clippy-core.log`、`baseline-clippy-core.log`、`clippy-core-warning-comparison.json` |
| Windows GNU workspace、all-targets check | exit 0，`windows-check.log`；僅交叉編譯，非原生證據 |
| cfg boundary | exit 0，`cfg-boundary.log` |
| contracts | `ok:true`、findings 0，`contracts-final.log` |
| L1 鎖與起點比對 | exit 0，`lock-check.log`、`lock-add.log`；add-only、無移除 |
| policy | exit 0，25 pass、2 warn，`policy-final.log`；既有 R-19／R-22 WARN 保留 |
| changelog | **1 passed**，`changelog-final.log` |

Python 聚焦測試使用 `--noconftest`，避免全域 conftest 對正式設定、狀態與 daemon 做快照／探測；因此全域註冊 marker 未載入的警告保留。執行過 gate 報告測試所含的 dry-run 骨架檢查，沒有執行真正的 slice gate、出口 driver、全套 pytest 或 Docker。未操作實體 UART、production daemon 或其他工作樹。

## SPEC-GAPS

無。本項兩個修正都有明確裁決，沒有擴及其他工作包。擁有者與硬體項目的既有 BLOCKED-OWNER 狀態保留。

## 既有碼待辦與失敗對照

起點快照以 `git archive 2a9a9bc4` 展開在本工作樹 `target/s2-par/wp510r/i26/baseline/`，另用 `baseline-target/` 建置；沒有切換、回退或改寫工作分支，也沒有使用其他 worktree。

1. `cargo:sw-cli::mgmt_supervision_switch::s1mx6_supervision_home_precedes_passwd`、`...::s1mx6_unit_hold_ignores_xdg` 與 `cargo:sw-cli::mgmt_update::s1mx6_unit_snapshot_restore_ignores_xdg`：測試要求 passwd fixture 不在真實 HOME 下，而本輪 TMPDIR 依寫入邊界放在工作樹內，該工作樹在 HOME 下，故 sandbox assertion 拒絕。`baseline-cli.log` 與 `baseline-update.log` 在起點重現相同失敗與訊息，不改測試防護。
2. 初次 core 全套 `probe_start_rejects_foreground_busy`（WAL TX 數預期 0、實際 1）及 `sticky_persisted_only_when_ready_and_path_unchanged`（等待 `open:/dev/ttyMOVED` 逾時）各失敗一次（`core-fakes.log`）。起點快照聚焦重跑分別於第 **23** 次、第 **7** 次重現相同斷言（`baseline-repro-flash_session_core-23.log`、`baseline-repro-session_manager-07.log`、`baseline-transient-summary.json`）。`unchanged-existing-failures.diff` 為空，相關產品與案例均未變；不將它們稱為已修或歸因於未證實的環境因素。候選及起點各一次完整 core 重跑都是 **1737 passed、0 failed、1 ignored**。
3. workspace clippy：`sw-proto/src/pyre/parse.rs:587` 的 `clippy::never_loop` 於候選與起點皆 exit 101；起點完整 clippy 另報 `sw-pal/tests/proc_scan.rs:431、532` 的 `non_octal_unix_permissions`。產品與測試皆未改動，不加 allow、不處理範圍外 lint。
4. 最終帶 test-fakes 的 workspace 另有 **47** 個 daemon 黑箱失敗：`daemon_process` 23、`event_lifecycle` 3、`handoff_stop` 5、`mcu_rpc` 3、`restart_after_commit` 2、`shutdown_order` 11。工作樹內 canonical 暫存根加上各 rig 的子目錄，使 socket 路徑超過 Unix 上限（`path must be shorter than SUN_LEN`），相應 daemon 無法就緒。以相同 TMPDIR 在起點逐 suite 重跑，47 個 id 全部同樣失敗；連同上述 3 個 CLI 失敗，`workspace-baseline-comparison.json` 為 **50 matched、0 unmatched**。證據為 `baseline-daemon-paths.log`、`baseline-daemon-more-paths.log`、`baseline-restart-path.log`；完整失敗 id 列在 `test-summary.json`。沒有改寫路徑判準、放寬 ready 等待或把失敗改成 PASS。
5. 暫存路徑試跑紀錄保留：`before-workspace.log` 使用工作樹內絕對 TMPDIR；`before-workspace-load.log` 使用相對 TMPDIR，部分 crate 在不同 cwd 找不到目錄；`workspace-fakes.log` 使用 `/proc/<pid>/cwd/target/tmp` 別名，導致 CLI canonical path 比對失敗。這些不是通過證據；最終使用工作樹內 canonical 絕對 TMPDIR，相關原始 FAIL 不刪除或改判。負載量測的 DR-1(d) 是不使用暫存目錄的記憶體 rig。

## 不成立主張

無。

REMAINING: none
