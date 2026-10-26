# 待 Codex gpt-6.1-sol 補審清單

Codex 帳號額度於 2026-10-21 約 18:28（台北）用完，恢復時間 2026-10-27 14:32。擁有者裁決（2026-10-21）：額度恢復前只推進非 codex 的 Task，保留原指定模型；期間 commit 僅經 Claude Sonnet（max）單審，**須於額度恢復後由 gpt-6.1-sol（medium）補審**，補審發現的問題依一般修正流程處理。

> **補審產物（2026-10-23）**：上表標為「已補審（sol 2026-10-23）」之 main 分支列，依據為 Codex gpt-6.1-sol 唯讀彙整報告 `~/b-log/rust-refactor/sol-backlog-review-20261023.md`（sha256 `6a4866ac3e2c71f34784000fcf9b658fac667894448bb48c264d59fe94ab4686`）。

| 分支 | commit | Task | 單審者 | 補審狀態 |
|---|---|---|---|---|
| feature/rust-refactor-p5 | 8e39875 | P5-T0b Slice 2.1 必要案例凍結與斷路器 | Sonnet max | 待補審 |
| feature/rust-refactor-p9a | 149e420 | P9-T2 Stage 3 共用測試替身 | Sonnet max | 待補審 |
| feature/rust-refactor-p9a | 6075648 | P9-T3 A/B manifest 載入與凍結 | Sonnet max | 待補審 |
| feature/rust-refactor-p9a | 24f42d0 | P9-T7B L1.5 A/B 差分 | Sonnet max | 待補審 |
| feature/rust-refactor-p9a | d6c08e0 | P9-T7 A/B 絕對契約與差分判定 | Sonnet max | 待補審 |
| feature/rust-refactor-p9a | 5a038a1 | P9-T14 Stage 0～3 出口證據索引 | Sonnet max | 待補審 |
| feature/rust-refactor | a1ba28b | 1 Stage 1 exit closure 第 1 輪：sw-cli 收斂 | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 58e539d | 1 Stage 1 exit closure 第 1 輪：閘門與 A/B 腳本 | Sonnet max | 已補審（sol 2026-10-23）；Sol #1 修復 97e3b9f；#2/#5 修復 7649dc8；#3 已由 c0c3c29 修復；#4 已由 f5b7d28 修復 |
| feature/rust-refactor | d457a72 | 1 Stage 1 exit closure 第 1 輪：Docker systemd 腳本 | Sonnet max | 已補審（sol 2026-10-23）；Sol 修復 300f383 |
| feature/rust-refactor | b3ec03b | 1 Stage 1 exit closure 第 1 輪：contract_blackbox 夾具 | Sonnet max | 已補審（sol 2026-10-23）；Sol 修復 71f14d7；Reviewer A follow-up #1 修復 77c4c8f |
| feature/rust-refactor | f8e31b2 | 1 Stage 1 exit closure 第 1 輪：閘門單元測試 | Sonnet max | 已補審（sol 2026-10-23）；58e539d #2 錯誤 PASS 預期修復 7649dc8 |
| feature/rust-refactor | 79475c8 | 1 Stage 1 exit closure 第 1 輪：凍結案例 add-only 與計畫指令 | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 87592d4 | 1 Stage 1 exit closure 第 2 輪：ab_blackbox_1b 腳本模式與身分 | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：87592d4；Sol 修復 97e3b9f、2e62107 |
| feature/rust-refactor | 0d49f83 | 1 Stage 1 exit closure 第 2 輪：daemon EIO／current-release 路徑 | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 5283883 | 1 Stage 1 exit closure 第 2 輪：P4 登記拒絕髒樹 | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 03d101a | 1 Stage 1 exit closure 第 2 輪：L15 延遲回覆與 runner fail-closed | Sonnet max | 已補審（sol 2026-10-23）；Sol 修復 8854b82 |
| feature/rust-refactor | c0c3c29 | 1 Stage 1 exit closure 第 2 輪：Windows COM 閘門 schema | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：c0c3c29 |
| feature/rust-refactor | 99e5db3 | 1 Stage 1 exit closure D-python-handoff-drain-window-baseline-change（oracle 變更） | Sonnet max | 已補審（sol 2026-10-23）；Sol 修復 ed8cd75；Reviewer A follow-up #4/#5 修復 41d4914，P8 文件同步 aa51b4c |
| feature/rust-refactor | ccf29ce | 1 Stage 1 exit closure B-windows-com-gate-and-l15-coverage | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：ccf29ce；Sol 修復 8265dd6 |
| feature/rust-refactor | 72a887e | 1 Stage 1 exit closure sw-cli-handoff-ran-rollback | Sonnet max | 已補審（sol 2026-10-23）；Sol 低嚴重度 finding 修復 bde73ff |
| feature/rust-refactor | 6273337 | 1 Stage 1 exit closure B/E-win-delegate-job | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | ca0106f | 1 Stage 1 exit closure A-bridge-tool-revision-and-windows-selftest（工具 revision 未核准） | Sonnet max | 已補審（sol 2026-10-23）；Sol 修復 228aa76；Reviewer A #8：目前 tree d44a761a… 與核准 c4c22c0e… 不符，保持 OPEN／不改核准值 |
| feature/rust-refactor | d063fa5 | 1 Stage 1 exit closure E-win-cli-help-golden | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 6bff431 | 1 Stage 1 exit closure E-ab-blackbox-bundle-identity | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 1f55cb2 | 1 Stage 1 exit closure C-test-oracle-edits（test oracle 變更） | Sonnet max | 已補審（sol 2026-10-23）；Sol 修復 71f14d7；Reviewer A follow-up #1 修復 77c4c8f |
| feature/rust-refactor | afc1ec0 | 1 Stage 1 exit closure C-l1-lock-registration-add-only | Sonnet max | 已補審（sol 2026-10-23）；已由 61919dd 替換 ignored 列 |
| feature/rust-refactor | 2c4eedf | 1 Stage 1 exit closure B-spike3-git-clean | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 16a69da | 1 Stage 1 exit closure C-plan-and-owner-queue-text | Sonnet max | 已補審（sol 2026-10-23）；已由 3750453 設定 Windows Rust CLI identity |
| feature/rust-refactor | 40bdf77 | 1 Stage 1 exit closure 修正輪：deployed_content 量測修正 | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 1e470cd | 1 Stage 1 exit closure 修正輪：Windows staging 與 bundle 指標 | Sonnet max | 已補審（sol 2026-10-23）；已由 f5b7d28 修正 versions/<V> 指標路徑 |
| feature/rust-refactor | 0b0ff27 | 1 Stage 1 exit closure 修正輪：bc05 A/B 雙輪（oracle 變更）與 doctor 等待 | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 82fe648 | 1 Stage 1 exit closure 修正輪：待裁決項目與 G1 文件修正 | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | d82d146 | 1 Stage 1 exit closure OD-16 (1)(2) L1 凍結鎖修訂（add-only＋逐名移除） | Sonnet max | 已補審（sol 2026-10-23）；Sol 修復 bde73ff |
| feature/rust-refactor | 4736927 | 1 Stage 1 exit closure OD-16 (3) 交接排空窗口接受（SC-11）與 L1.5 證據缺口登記 | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 300f383 | 1 Stage 1 exit closure OD-16 (4)(5) systemd 基底 Ubuntu 24.04 與 harness 修正 | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：300f383；Sol 修復 ecc0277 |
| feature/rust-refactor | 1af76d2 | 1 Stage 1 exit closure 20groups 委派 sentinel 扁平佈局與 doctor pidns 隔離 | Sonnet max | 已補審（sol 2026-10-23）；已由 5545974 納入 owner marker checksum |
| feature/rust-refactor | a87a0d6 | 1 Stage 1 exit closure Windows 委派 terminate 測試競態修正（test-side） | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 7098a97 | bridge 工具 revision c4c22c0e369e 核准（單審） | Sonnet max | 已補審（sol 2026-10-23）；Reviewer A #8：目前工具 tree d44a761a…，核准值 c4c22c0e…，核准與原生 selftest 維持 OPEN |
| feature/rust-refactor | 7228430 | 1 Stage 1 exit closure G5-process-ctl-helper-stdout（含 exec_delegate） | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 61919dd | 1 Stage 1 exit closure OD-17-pytest-replacement-and-strict-gate | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：61919dd |
| feature/rust-refactor | 3f05797 | 1 Stage 1 exit closure G4-p4-evidence-pipeline-applicability-and-bundle | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：3f05797／f5b7d28；Sol #1/#3 修復 7649dc8；#2 修復 bde73ff；#4 已由 f5b7d28 修復；Reviewer A follow-up #3 修復 bb791b4、#7 修復 19bcc16 |
| feature/rust-refactor | 3750453 | 1 Stage 1 exit closure G2-driver-and-G3-wrappers | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：3750453／38fdbd2；三項 Sol finding 已由 38fdbd2 修復 |
| feature/rust-refactor | afe7142 | 1 Stage 1 exit closure addenda-CHG-OPEN-rows | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 5545974 | 1 Stage 1 exit closure G1-sentinel-checksum-owner-marker | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：5545974 |
| feature/rust-refactor | 2880127 | 1 Stage 1 exit closure G2-win-selftest-local-scratch-writeback | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 38fdbd2 | 1 Stage 1 exit closure G3-win-l15-producer-and-report-complete | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：3750453／38fdbd2；Sol 修復 11b1fd4 |
| feature/rust-refactor | 11b1fd4 | 1 Stage 1 exit closure G4-junit-testcase-counts-and-l15-complete-gate | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：11b1fd4；Sol 修復 7649dc8 |
| feature/rust-refactor | 4a6feaf | 1 Stage 1 exit closure G5-sc20-posix-only-l1-add | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | e6d0fac | 1 Stage 1 exit closure G6-coexist-reader-race | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 26473bd | 1 Stage 1 exit closure G3-win-serial-peer-gate-scope | Sonnet max | 已補審（sol 2026-10-23）；已由 896ce0c 修復並補回歸測試 |
| feature/rust-refactor | 896ce0c | 1 Stage 1 exit closure G3-status-peer-open-capability | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：896ce0c |
| feature/rust-refactor | 264182f | 1 Stage 1 exit closure G4-spike1-shared-evidence-dir | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 21c694d | 1 Stage 1 exit closure G2-ab-rust-only-release-check | Sonnet max | 已補審（sol 2026-10-23） |

後續新增的單審 commit 一律追加於此表。

> 2026-10-24 補審修復記錄：本輪對應 Sol finding 的修復提交已逐列標記；L1 三元組範圍與 POSIX namespace 修正為 `bde73ff`，P4／JUnit provenance 修正為 `7649dc8`。Stage 1 exit driver 未執行，原生 Windows 與 owner/hardware 證據仍依下方阻擋狀態處理。

## 2026-10-26 Reviewer A follow-up

Reviewer A（Sonnet max）對上一輪提交提出 8 項 finding，處置如下。Reviewer B（Codex gpt-6.1-sol）工作未實際執行；其 `.last` 是 launcher prompt 回覆，沒有 review findings，**不構成補審或 clean pass**。

| Finding | 處置 |
|---|---|
| #1 holder overlap oracle | **fixed `77c4c8f`**：隔離 `pidns_rig` 每筆樣本重掃 `/proc`；測試與 producer 不再以 restart 返回後的 `watch_pid` 補登 successor；時間線 PID 必須等於 daemon PID。定點測試含未告知 PID 的 successor 負向案例。 |
| #2 Spike 3 AV share mode | **fixed `19bcc16`、`92cadf4`**：主注入使用 no-delete share；oplock 獨立記錄。首輪 Windows artifact 發現 unlink 在 helper 首掃描前已成功；加時序回歸測試（先紅後綠）及 ready／持有檔名 barrier。修正後四份 Windows producer artifact 均 PASS；Stage 1A 單案 gate 均維持 BLOCKED-OWNER（延遲上限待裁決）。修正後 oracle 尚待獨立複審。 |
| #3 P4 pointer slash rule | **fixed `bb791b4`**：註冊器先驗 raw `dir`，反斜線與絕對路徑拒絕；恢復原 `test_manifest_pointer_absolute_dir_is_used_as_is` nodeid 並用 OD-18 (3) 期望驗證。 |
| #4 handoff contract/docs | **fixed `41d4914`、`aa51b4c`**：SC-11、RPC side_effects、P8 plan 與 addenda 同步記錄 TX fence timeout、`SESSION_BUSY`／`tx.in_flight` 與凍結 rollback 語意；待非實作者審查。 |
| #5 AF_UNIX long path | **fixed `41d4914`**：handoff child socket 使用 `/tmp/sw-*` 並清理，POSIX pytest 預設 basetemp 採短路徑。 |
| #6 latency bound branches | **fixed `19bcc16`**：新增 owner bound 高於／低於最大樣本的 PASS／FAIL 定點測試；owner 數值仍未裁決，正式列保持 BLOCKED-OWNER。 |
| #7 Cargo provenance | **fixed `19bcc16`**：`rustc -vV` 使用 repo cwd；Stage 1A gate 核對 provenance 起訖 HEAD 與 clean 狀態。 |
| #8 scope／carry-over／oracle inventory | OD-20 超出 OD-14..OD-19 範圍，未改文件消費者或 verdict；36 筆既有未由 OD-16／17 授權的 removal 保持原樣、待 owner；凍結 `test_spike3_pass_with_known_edr_is_pass` nodeid 保留且明確斷言 BLOCKED-OWNER；absolute-dir 原 nodeid 已恢復；handoff infrastructure、addenda count 與 G-REG 鎖數已補正。Bridge 核准 tree `c4c22c0e…` 與目前 `d44a761a…` 不同，保持 OPEN，不改核准值；full pytest 的 docker bridge case 為 INSUFFICIENT_EVIDENCE。 |

驗證記錄：全套 `python3 -m pytest -q tests/` 僅執行一次，`5063 passed, 111 skipped, 1 xfailed, 4 failed`；兩個暫時 lock 範圍失敗修正後定點重跑 `2 passed`，未重跑全套。P4 evidence 缺失案例已於 `3684f06` 基線重現；bridge revision mismatch 亦為既有核准落差。Stage 1 exit driver 未執行；COM22 owner/hardware 複核仍 OPEN。

## 額度用完後未完成、需重新實作（原 implementer 為 codex gpt-6-luna）

- P5-T0c、T1～T9（P5 工作樹有未提交的中途改動，恢復後自 T0c 依序重跑）。P5-T0 的 sol 審查已完成，但其實作尚未獨立 commit，恢復時一併核對。
- P9：implementer 為 luna 的 Task（T5B、T6、T14B 等）。
- Stage 1 出口收尾：第 1～2 輪修正未執行；改以非 codex 實作者（agy gemini／haiku）重跑。
