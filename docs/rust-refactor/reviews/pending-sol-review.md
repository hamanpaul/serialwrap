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
| feature/rust-refactor | 58e539d | 1 Stage 1 exit closure 第 1 輪：閘門與 A/B 腳本 | Sonnet max | 已補審（sol 2026-10-23）；Sol #1 修復 97e3b9f；#2/#5 修復 7649dc8；#3 已由 c0c3c29 修復；#4 已由 f5b7d28 修復；第三輪 Cargo provenance 缺失／錯 schema／缺 `git.end` 回歸與 FAIL 優先修復 eada6ee；第四輪 provenance reason 保留與真實 FAIL 優先序 fixed 317add5 |
| feature/rust-refactor | d457a72 | 1 Stage 1 exit closure 第 1 輪：Docker systemd 腳本 | Sonnet max | 已補審（sol 2026-10-23）；Sol 修復 300f383 |
| feature/rust-refactor | b3ec03b | 1 Stage 1 exit closure 第 1 輪：contract_blackbox 夾具 | Sonnet max | 已補審（sol 2026-10-23）；Sol 修復 71f14d7；Reviewer A follow-up #1 修復 77c4c8f；第三輪 zombie／退場 EACCES 與短 socket 修復 6c9f73b；第四輪 zombie leader/live sibling fail-closed 與可攜 temp cleanup fixed 4ad9b39 |
| feature/rust-refactor | f8e31b2 | 1 Stage 1 exit closure 第 1 輪：閘門單元測試 | Sonnet max | 已補審（sol 2026-10-23）；58e539d #2 錯誤 PASS 預期修復 7649dc8；第四輪 Cargo provenance／oplock workload 負向回歸 fixed 317add5 |
| feature/rust-refactor | 79475c8 | 1 Stage 1 exit closure 第 1 輪：凍結案例 add-only 與計畫指令 | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 87592d4 | 1 Stage 1 exit closure 第 2 輪：ab_blackbox_1b 腳本模式與身分 | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：87592d4；Sol 修復 97e3b9f、2e62107 |
| feature/rust-refactor | 0d49f83 | 1 Stage 1 exit closure 第 2 輪：daemon EIO／current-release 路徑 | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 5283883 | 1 Stage 1 exit closure 第 2 輪：P4 登記拒絕髒樹 | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 03d101a | 1 Stage 1 exit closure 第 2 輪：L15 延遲回覆與 runner fail-closed | Sonnet max | 已補審（sol 2026-10-23）；Sol 修復 8854b82 |
| feature/rust-refactor | c0c3c29 | 1 Stage 1 exit closure 第 2 輪：Windows COM 閘門 schema | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：c0c3c29 |
| feature/rust-refactor | 99e5db3 | 1 Stage 1 exit closure D-python-handoff-drain-window-baseline-change（oracle 變更） | Sonnet max | 已補審（sol 2026-10-23）；Sol 修復 ed8cd75；Reviewer A follow-up #4/#5 修復 41d4914，P8 文件同步 aa51b4c；第三輪 P8 TX fence timeout／rollback 測試表補列 0ab19ba |
| feature/rust-refactor | ccf29ce | 1 Stage 1 exit closure B-windows-com-gate-and-l15-coverage | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：ccf29ce；Sol 修復 8265dd6 |
| feature/rust-refactor | 72a887e | 1 Stage 1 exit closure sw-cli-handoff-ran-rollback | Sonnet max | 已補審（sol 2026-10-23）；Sol 低嚴重度 finding 修復 bde73ff |
| feature/rust-refactor | 6273337 | 1 Stage 1 exit closure B/E-win-delegate-job | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | ca0106f | 1 Stage 1 exit closure A-bridge-tool-revision-and-windows-selftest（工具 revision 未核准） | Sonnet max | 已補審（sol 2026-10-23）；Sol 修復 228aa76；Reviewer A #8：目前 tree d44a761a… 與核准 c4c22c0e… 不符，保持 OPEN／不改核准值 |
| feature/rust-refactor | d063fa5 | 1 Stage 1 exit closure E-win-cli-help-golden | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 6bff431 | 1 Stage 1 exit closure E-ab-blackbox-bundle-identity | Sonnet max | 已補審（sol 2026-10-23） |
| feature/rust-refactor | 1f55cb2 | 1 Stage 1 exit closure C-test-oracle-edits（test oracle 變更） | Sonnet max | 已補審（sol 2026-10-23）；Sol 修復 71f14d7；Reviewer A follow-up #1 修復 77c4c8f；第三輪 zombie／退場 EACCES 處理與 restart 實跑 6c9f73b；第四輪 thread-level holder oracle fixed 4ad9b39 |
| feature/rust-refactor | afc1ec0 | 1 Stage 1 exit closure C-l1-lock-registration-add-only | Sonnet max | 已補審（sol 2026-10-23）；已由 61919dd 替換 ignored 列；第四輪 official add-only 新增 11 筆、移除 0 筆 fixed e71b516 |
| feature/rust-refactor | 2c4eedf | 1 Stage 1 exit closure B-spike3-git-clean | Sonnet max | 已補審（sol 2026-10-23）；第三輪 Spike 3 ready 原子發布、持有 barrier、launch-failure evidence 與 timing barrier 修復 183212a；OD-19 主注入文件校正 0ab19ba；第四輪 live lease go/ack、oplock workload/schema/gate 與 bounded ready retry fixed 317add5 |
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
| feature/rust-refactor | 3f05797 | 1 Stage 1 exit closure G4-p4-evidence-pipeline-applicability-and-bundle | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：3f05797／f5b7d28；Sol #1/#3 修復 7649dc8；#2 修復 bde73ff；#4 已由 f5b7d28 修復；Reviewer A follow-up #3 修復 bb791b4、#7 修復 19bcc16；第三輪 frozen absolute-dir nodeid 說明與 oracle 審查標記 0ab19ba |
| feature/rust-refactor | 3750453 | 1 Stage 1 exit closure G2-driver-and-G3-wrappers | Sonnet max | 已補審（sol 2026-10-23）；已修正項，原修正 commit：3750453／38fdbd2；三項 Sol finding 已由 38fdbd2 修復 |
| feature/rust-refactor | afe7142 | 1 Stage 1 exit closure addenda-CHG-OPEN-rows | Sonnet max | 已補審（sol 2026-10-23）；第四輪 Cargo provenance 與 holder thread oracle change rows 維持 OPEN，見本次 docs(reviews) commit |
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

Reviewer A（Sonnet max）對上一輪提交提出 8 項 finding，處置如下。此段原記錄稱 Reviewer B（Codex gpt-6.1-sol）未實際執行且 `.last` 不是 review；第四輪核對證明這項記錄不正確：`rev-sol-r3.last` 含兩項 Medium finding，round-r2 `.last` 另含一項 High 與一項 Medium。round-r3 所稱的 `approve`／0 findings 來自 implementer session 內的協作子代理，不是獨立 Reviewer B；詳見第四輪補審表。

| Finding | 處置 |
|---|---|
| #1 holder overlap oracle | **fixed `77c4c8f`，退場競態補強 `6c9f73b`**：隔離 `pidns_rig` 每筆樣本重掃 `/proc`；zombie／已退出 PID 不再因 EACCES 形成 skip，存活 PID 權限錯誤重試 40×5 ms 後仍 fail-closed。`test_restart_no_stale_console_replay` 隔離 PID namespace 實跑 `1 passed`；Rust-only restart 測試仍未實跑。 |
| #2 Spike 3 AV share mode | **fixed `19bcc16`、`92cadf4`；第三輪 helper barriers／ready repair `183212a`、normative docs `0ab19ba`**：主注入使用 no-delete share；oplock 獨立記錄。舊四份 Windows producer artifact 是在 `92cadf4` 產生，不能代表目前 HEAD；須 Windows 原生重採。Stage 1A 單案 gate 仍維持 BLOCKED-OWNER（延遲上限待裁決），oracle 尚待獨立複審。 |
| #3 P4 pointer slash rule | **fixed `bb791b4`**：註冊器先驗 raw `dir`，反斜線與絕對路徑拒絕；恢復原 `test_manifest_pointer_absolute_dir_is_used_as_is` nodeid 並用 OD-18 (3) 期望驗證。 |
| #4 handoff contract/docs | **fixed `41d4914`、`aa51b4c`**：SC-11、RPC side_effects、P8 plan 與 addenda 同步記錄 TX fence timeout、`SESSION_BUSY`／`tx.in_flight` 與凍結 rollback 語意；待非實作者審查。 |
| #5 AF_UNIX long path | **fixed `41d4914`**：handoff child socket 使用 `/tmp/sw-*` 並清理，POSIX pytest 預設 basetemp 採短路徑。 |
| #6 latency bound branches | **fixed `19bcc16`**：新增 owner bound 高於／低於最大樣本的 PASS／FAIL 定點測試；owner 數值仍未裁決，正式列保持 BLOCKED-OWNER。 |
| #7 Cargo provenance | **fixed `19bcc16`**：`rustc -vV` 使用 repo cwd；Stage 1A gate 核對 provenance 起訖 HEAD 與 clean 狀態。 |
| #8 scope／carry-over／oracle inventory | OD-20 超出 OD-14..OD-19 範圍，未改文件消費者或 verdict；36 筆既有未由 OD-16／17 授權的 removal 保持原樣、待 owner；凍結 `test_spike3_pass_with_known_edr_is_pass` nodeid 保留且明確斷言 BLOCKED-OWNER；absolute-dir 原 nodeid 已恢復；handoff infrastructure、addenda count 與 G-REG 鎖數已補正。Bridge 核准 tree `c4c22c0e…` 與目前 `d44a761a…` 不同，保持 OPEN，不改核准值；full pytest 的 docker bridge case 為 INSUFFICIENT_EVIDENCE。 |

驗證記錄：全套 `python3 -m pytest -q tests/` 僅執行一次，`5063 passed, 111 skipped, 1 xfailed, 4 failed`；兩個暫時 lock 範圍失敗修正後定點重跑 `2 passed`，未重跑全套。P4 evidence 缺失案例已於 `3684f06` 基線重現；bridge revision mismatch 亦為既有核准落差。Stage 1 exit driver 未執行；COM22 owner/hardware 複核仍 OPEN。

## 2026-10-26 Reviewer A follow-up（第三輪）

Reviewer A（Sonnet max）對第二輪修正提出 9 項補審 finding。每列保留既有 Sol 補審日期；本輪只記修正與驗證，不將 owner／硬體項升格為通過。

| Finding | 處置 |
|---|---|
| #1 holder overlap observer | **fixed `6c9f73b`**：zombie／已退出 PID 不再因 `/proc/<pid>/fd` EACCES 造成不確定 skip；存活 PID 權限錯誤重試 40×5 ms 後仍 fail-closed；加入實際 zombie、取樣中退出、namespace 與數字 PID 測試。`test_restart_no_stale_console_replay` 隔離 PID namespace 實跑 `1 passed`；該輪當時 Rust-only `test_restart_no_reopen_before_handoff` 尚未實跑，第四輪已使用本 worktree Rust bundle 在隔離 PID namespace 實跑 `1 passed`，thread-level holder 回歸另由 `4ad9b39` 加入。 |
| #2 OD-19 Spike 3 文件 | **fixed `0ab19ba`**：normative reconciliation 與 Stage 0 計畫均寫明主注入為 `share_no_delete`，oplock 為獨立第二變體且不能替代主注入；原四份 Windows artifacts 不是本輪 HEAD 證據，須重採。 |
| #3 Cargo provenance negative branches | **fixed `eada6ee`**：可明確測 absent provenance、錯 schema 與缺 `git.end`；另保護真實 `FAIL` 不被 provenance evidence-insufficient 覆寫。 |
| #4 helper launch-failure evidence | **fixed `183212a`**：主 helper 缺省 `oplock` 記為 `not_requested`；schema ValidationError 會寫出 `INSUFFICIENT_EVIDENCE` JSON。 |
| #5 ready publication race | **fixed `183212a`**：producer 用同目錄暫存檔＋`os.replace` 原子發布；consumer 對部分／格式錯誤內容重試至期限。 |
| #6 frozen absolute-dir nodeid | **clarified `0ab19ba`**：保留凍結 nodeid，docstring 明記此案例沒有增加覆蓋；addendum 加上「oracle 變更，待非實作者審查」標記。 |
| #7 P8 TX-fence table | **fixed `0ab19ba`**：加入 TX fence timeout 的 `SESSION_BUSY`／`tx.in_flight`／rollback 語意，以及 handoff rollback 保留既有 shutdown freeze 的列。 |
| #8 Python daemon long socket | **fixed `6c9f73b`**：`start_python_daemon` 改用 `/tmp/sw-*` run directory 並清理；隔離 restart 測試實跑通過。 |
| #9 Spike 3 timing barriers | **fixed `183212a`**：rotate、reader-handle、F12 都在 helper 確認持有既存 WAL 後才開始刺激；現行 Linux 定點測試 `211 passed, 2 skipped`，Windows 原生 cases 未在本機執行。 |

Reviewer B round-r2 `.last` 並非空白：檔案有 1777 bytes，包含一項 High（oplock 變體沒有執行工作負載）與一項 Medium（ready JSON 非原子發布）；round-r3 prompt 把它誤記為空，這兩項當時未登錄處置。真正的 `rev-sol-r3.last` 有兩項 Medium（portable 測試硬編碼 `/tmp`；首次 ready 的 seeded lease 可能在刺激前到期）。round-r3 所稱 `approve`／0 findings 來自 implementer session 內的協作子代理，不是獨立 Reviewer B，該 clean-review 記錄撤回。四項 Sol findings 的處置列於第四輪補審表。

本輪驗證：定點測試 `211 passed, 2 skipped`；全套 `python3 -m pytest -q tests/` 執行一次，`5081 passed, 109 skipped, 1 xfailed, 2 failed`（92 subtests passed）。失敗 nodeid 為 `tests/test_docker_l15.py::test_docker_smoke_two_container`（bridge 自測不符合核准 revision/tree）與 `tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`（正式 P4/L1 evidence 缺檔）；兩案均於 `3684f06` archive 副本以同一 nodeid 重現相同失敗類型。`cargo test --workspace` exit 0；`tools.contracts check` 為 0 findings；policy check 為 25 pass／0 fail／2 warn（R-19、R-22）；`regen_lock.py --check --base HEAD` 通過。Stage 1 exit driver 未執行。

## 2026-10-26 Reviewer A／Sol 第四輪補審

Reviewer A 提出 10 項 finding；Reviewer B/Sol round-r2、round-r3 的四項 finding 亦在本輪列入處置。下表中的修復提交只記錄程式與文件修正，不代表原生 Windows、owner/hardware 或 Stage 1 出口通過。

| Finding | 處置 |
|---|---|
| Reviewer A #1 portable socket test | **fixed `4ad9b39`**：移除 `/tmp` 假設，使用平台 temp root；cleanup 只依 `SW_G5_TEMP_RUN_DIR`，不會從 socket path 推導並刪除 parent。 |
| Reviewer A #2 expired ready lease | **fixed `317add5`**：新增 go/ack 握手，在刺激前對既有 WAL 重新取得 seeded lease；延遲超過 500 ms 的案例測試要求重新武裝後才放行。 |
| Reviewer A #3 missing case-level regression | **fixed `317add5`**：新增 ready 失敗即 stop/error、case_rotate 首筆 append/helper/rearm/其餘 399 筆順序，以及四案 barrier 與 atomic ready AST 檢查。 |
| Reviewer A #4 review record | **fixed in this docs(reviews) commit**：修正 round-r2 `.last`、round-r3 Sol findings 與非獨立 approve 的記錄；補上第四輪逐項處置。 |
| Reviewer A #5 oplock has no workload | **fixed `317add5`**：每案在獨立 oplock lease 期間執行 append、rotate、F12 前後 marker；schema 與 gate 驗證記錄數、連續 seq、rotation health、CRC、interference。CHG 保持 OPEN 等待獨立複審與 Windows 重採。 |
| Reviewer A #6 ready replace PermissionError | **fixed `317add5`**：ready 同目錄原子發布遇 PermissionError 有界重試 40 次、25 ms 間隔；新增注入兩次後成功測試。 |
| Reviewer A #7 zombie leader with live thread | **fixed `4ad9b39`**：只有所有 `/proc/<pid>/task/*/stat` 都終止或消失才把 EACCES 視為無 holder；增加 pthread leader-exit、live sibling 與 PTY 回歸。 |
| Reviewer A #8 Cargo provenance reason | **fixed `317add5`**：即使 verdict 是真實 FAIL 仍附 provenance reasons，FAIL 優先序不變；owner addenda 新增 OPEN oracle change。 |
| Reviewer A #9 changelog front matter | **fixed `317add5`**：修復 fragment YAML front matter；兩份相關 fragment 均由 `policy_check.changelog.parse_fragment` 成功解析。 |
| Reviewer A #10 safe cleanup | **fixed `4ad9b39`**：短 socket 測試使用 env 記錄的專屬 temp run dir 清理，移除可刪除 `/tmp` parent 的斷言／推導。 |
| Sol round-r2 #1 oplock no workload | **fixed `317add5`**：新增實際 append／rotation／F12 workload 並由 schema、gate 與負向測試驗證。 |
| Sol round-r2 #2 non-atomic ready | **already fixed `183212a`**：完整暫存檔後原子 replace；第四輪再由 `317add5` 對 Windows PermissionError 加有界重試。 |
| Sol round-r3 #1 `/tmp` portability | **fixed `4ad9b39`**：使用可攜 temp root，POSIX socket byte limit 僅在 POSIX 斷言。 |
| Sol round-r3 #2 expired lease | **fixed `317add5`**：go/ack 在案例刺激前重新武裝 live seeded lease，過期的首次 ready 不再足以通過 barrier。 |

第四輪驗證：觸及測試 `180 passed, 2 skipped`；`tests/test_resource_markers.py` 為 `9 passed, 11 warnings`；Rust-only `test_restart_no_reopen_before_handoff` 在隔離 PID namespace 實跑 `1 passed`。全套 `python3 -m pytest -q tests/` 僅執行一次：`5093 passed, 109 skipped, 1 xfailed, 2 failed`（92 subtests passed）。失敗為 `tests/test_docker_l15.py::test_docker_smoke_two_container`（bridge exact-revision qualification 回 `INSUFFICIENT_EVIDENCE`）及 `tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`（正式 P4/L1 evidence 缺檔）；兩者都在 `target/baseline-3684f06-r3` 以相同 nodeid 與相同失敗類型重現。基線快照中的兩個測試檔及 Docker runner 與 `git show 3684f06:<path>` 的 SHA-256 相同。`cargo test --workspace` exit 0；`tools.contracts check` 為 0 findings；policy check `25 pass／0 fail／2 warn`（R-19、R-22）；frozen lock `--check --base HEAD` 與 `git diff --check` 通過。未執行 Stage 1 exit driver；沒有 Windows 原生 Spike 3 重採或 COM／硬體執行。

## 額度用完後未完成、需重新實作（原 implementer 為 codex gpt-6-luna）

- P5-T0c、T1～T9（P5 工作樹有未提交的中途改動，恢復後自 T0c 依序重跑）。P5-T0 的 sol 審查已完成，但其實作尚未獨立 commit，恢復時一併核對。
- P9：implementer 為 luna 的 Task（T5B、T6、T14B 等）。
- Stage 1 出口收尾：第 1～2 輪修正未執行；改以非 codex 實作者（agy gemini／haiku）重跑。
