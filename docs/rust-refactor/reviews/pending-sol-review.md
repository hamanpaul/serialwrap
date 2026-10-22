# 待 Codex gpt-6.1-sol 補審清單

Codex 帳號額度於 2026-10-21 約 18:28（台北）用完，恢復時間 2026-10-27 14:32。擁有者裁決（2026-10-21）：額度恢復前只推進非 codex 的 Task，保留原指定模型；期間 commit 僅經 Claude Sonnet（max）單審，**須於額度恢復後由 gpt-6.1-sol（medium）補審**，補審發現的問題依一般修正流程處理。

| 分支 | commit | Task | 單審者 | 補審狀態 |
|---|---|---|---|---|
| feature/rust-refactor-p5 | 8e39875 | P5-T0b Slice 2.1 必要案例凍結與斷路器 | Sonnet max | 待補審 |
| feature/rust-refactor-p9a | 149e420 | P9-T2 Stage 3 共用測試替身 | Sonnet max | 待補審 |
| feature/rust-refactor-p9a | 6075648 | P9-T3 A/B manifest 載入與凍結 | Sonnet max | 待補審 |
| feature/rust-refactor-p9a | 24f42d0 | P9-T7B L1.5 A/B 差分 | Sonnet max | 待補審 |
| feature/rust-refactor-p9a | d6c08e0 | P9-T7 A/B 絕對契約與差分判定 | Sonnet max | 待補審 |
| feature/rust-refactor-p9a | 5a038a1 | P9-T14 Stage 0～3 出口證據索引 | Sonnet max | 待補審 |
| feature/rust-refactor | a1ba28b | 1 Stage 1 exit closure 第 1 輪：sw-cli 收斂 | Sonnet max | 待補審 |
| feature/rust-refactor | 58e539d | 1 Stage 1 exit closure 第 1 輪：閘門與 A/B 腳本 | Sonnet max | 待補審 |
| feature/rust-refactor | d457a72 | 1 Stage 1 exit closure 第 1 輪：Docker systemd 腳本 | Sonnet max | 待補審 |
| feature/rust-refactor | b3ec03b | 1 Stage 1 exit closure 第 1 輪：contract_blackbox 夾具 | Sonnet max | 待補審 |
| feature/rust-refactor | f8e31b2 | 1 Stage 1 exit closure 第 1 輪：閘門單元測試 | Sonnet max | 待補審 |
| feature/rust-refactor | 79475c8 | 1 Stage 1 exit closure 第 1 輪：凍結案例 add-only 與計畫指令 | Sonnet max | 待補審 |
| feature/rust-refactor | 87592d4 | 1 Stage 1 exit closure 第 2 輪：ab_blackbox_1b 腳本模式與身分 | Sonnet max | 待補審 |
| feature/rust-refactor | 0d49f83 | 1 Stage 1 exit closure 第 2 輪：daemon EIO／current-release 路徑 | Sonnet max | 待補審 |
| feature/rust-refactor | 5283883 | 1 Stage 1 exit closure 第 2 輪：P4 登記拒絕髒樹 | Sonnet max | 待補審 |
| feature/rust-refactor | 03d101a | 1 Stage 1 exit closure 第 2 輪：L15 延遲回覆與 runner fail-closed | Sonnet max | 待補審 |
| feature/rust-refactor | c0c3c29 | 1 Stage 1 exit closure 第 2 輪：Windows COM 閘門 schema | Sonnet max | 待補審 |
| feature/rust-refactor | 99e5db3 | 1 Stage 1 exit closure D-python-handoff-drain-window-baseline-change（oracle 變更） | Sonnet max | 待補審 |
| feature/rust-refactor | ccf29ce | 1 Stage 1 exit closure B-windows-com-gate-and-l15-coverage | Sonnet max | 待補審 |
| feature/rust-refactor | 72a887e | 1 Stage 1 exit closure sw-cli-handoff-ran-rollback | Sonnet max | 待補審 |
| feature/rust-refactor | 6273337 | 1 Stage 1 exit closure B/E-win-delegate-job | Sonnet max | 待補審 |
| feature/rust-refactor | ca0106f | 1 Stage 1 exit closure A-bridge-tool-revision-and-windows-selftest（工具 revision 未核准） | Sonnet max | 待補審 |
| feature/rust-refactor | d063fa5 | 1 Stage 1 exit closure E-win-cli-help-golden | Sonnet max | 待補審 |
| feature/rust-refactor | 6bff431 | 1 Stage 1 exit closure E-ab-blackbox-bundle-identity | Sonnet max | 待補審 |
| feature/rust-refactor | 1f55cb2 | 1 Stage 1 exit closure C-test-oracle-edits（test oracle 變更） | Sonnet max | 待補審 |
| feature/rust-refactor | afc1ec0 | 1 Stage 1 exit closure C-l1-lock-registration-add-only | Sonnet max | 待補審 |
| feature/rust-refactor | 2c4eedf | 1 Stage 1 exit closure B-spike3-git-clean | Sonnet max | 待補審 |
| feature/rust-refactor | 16a69da | 1 Stage 1 exit closure C-plan-and-owner-queue-text | Sonnet max | 待補審 |
| feature/rust-refactor | 40bdf77 | 1 Stage 1 exit closure 修正輪：deployed_content 量測修正 | Sonnet max | 待補審 |
| feature/rust-refactor | 1e470cd | 1 Stage 1 exit closure 修正輪：Windows staging 與 bundle 指標 | Sonnet max | 待補審 |
| feature/rust-refactor | 0b0ff27 | 1 Stage 1 exit closure 修正輪：bc05 A/B 雙輪（oracle 變更）與 doctor 等待 | Sonnet max | 待補審 |
| feature/rust-refactor | 82fe648 | 1 Stage 1 exit closure 修正輪：待裁決項目與 G1 文件修正 | Sonnet max | 待補審 |
| feature/rust-refactor | d82d146 | 1 Stage 1 exit closure OD-16 (1)(2) L1 凍結鎖修訂（add-only＋逐名移除） | Sonnet max | 待補審 |
| feature/rust-refactor | 4736927 | 1 Stage 1 exit closure OD-16 (3) 交接排空窗口接受（SC-11）與 L1.5 證據缺口登記 | Sonnet max | 待補審 |
| feature/rust-refactor | 300f383 | 1 Stage 1 exit closure OD-16 (4)(5) systemd 基底 Ubuntu 24.04 與 harness 修正 | Sonnet max | 待補審 |
| feature/rust-refactor | 1af76d2 | 1 Stage 1 exit closure 20groups 委派 sentinel 扁平佈局與 doctor pidns 隔離 | Sonnet max | 待補審 |
| feature/rust-refactor | a87a0d6 | 1 Stage 1 exit closure Windows 委派 terminate 測試競態修正（test-side） | Sonnet max | 待補審 |
| feature/rust-refactor | 7098a97 | bridge 工具 revision c4c22c0e369e 核准（單審） | Sonnet max | 待補審 |
| feature/rust-refactor | 7228430 | 1 Stage 1 exit closure G5-process-ctl-helper-stdout（含 exec_delegate） | Sonnet max | 待補審 |
| feature/rust-refactor | 61919dd | 1 Stage 1 exit closure OD-17-pytest-replacement-and-strict-gate | Sonnet max | 待補審 |
| feature/rust-refactor | 3f05797 | 1 Stage 1 exit closure G4-p4-evidence-pipeline-applicability-and-bundle | Sonnet max | 待補審 |
| feature/rust-refactor | 3750453 | 1 Stage 1 exit closure G2-driver-and-G3-wrappers | Sonnet max | 待補審 |
| feature/rust-refactor | afe7142 | 1 Stage 1 exit closure addenda-CHG-OPEN-rows | Sonnet max | 待補審 |
| feature/rust-refactor | 5545974 | 1 Stage 1 exit closure G1-sentinel-checksum-owner-marker | Sonnet max | 待補審 |
| feature/rust-refactor | 2880127 | 1 Stage 1 exit closure G2-win-selftest-local-scratch-writeback | Sonnet max | 待補審 |
| feature/rust-refactor | 38fdbd2 | 1 Stage 1 exit closure G3-win-l15-producer-and-report-complete | Sonnet max | 待補審 |
| feature/rust-refactor | 11b1fd4 | 1 Stage 1 exit closure G4-junit-testcase-counts-and-l15-complete-gate | Sonnet max | 待補審 |
| feature/rust-refactor | 4a6feaf | 1 Stage 1 exit closure G5-sc20-posix-only-l1-add | Sonnet max | 待補審 |
| feature/rust-refactor | e6d0fac | 1 Stage 1 exit closure G6-coexist-reader-race | Sonnet max | 待補審 |
| feature/rust-refactor | 26473bd | 1 Stage 1 exit closure G3-win-serial-peer-gate-scope | Sonnet max | 待補審 |
| feature/rust-refactor | 896ce0c | 1 Stage 1 exit closure G3-status-peer-open-capability | Sonnet max | 待補審 |
| feature/rust-refactor | 264182f | 1 Stage 1 exit closure G4-spike1-shared-evidence-dir | Sonnet max | 待補審 |
| feature/rust-refactor | 21c694d | 1 Stage 1 exit closure G2-ab-rust-only-release-check | Sonnet max | 待補審 |

後續新增的單審 commit 一律追加於此表。

## 額度用完後未完成、需重新實作（原 implementer 為 codex gpt-6-luna）

- P5-T0c、T1～T9（P5 工作樹有未提交的中途改動，恢復後自 T0c 依序重跑）。P5-T0 的 sol 審查已完成，但其實作尚未獨立 commit，恢復時一併核對。
- P9：implementer 為 luna 的 Task（T5B、T6、T14B 等）。
- Stage 1 出口收尾：第 1～2 輪修正未執行；改以非 codex 實作者（agy gemini／haiku）重跑。
