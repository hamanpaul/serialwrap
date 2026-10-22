---
type: fix
scope: test-infra
---

修正 Stage 1B A/B 執行器在全 `rust_only` 選取下的 A 輪釋放檢查（S1X 出口 G2，PENDING-OD-S1X-WIN-AB-A-ROUND）。Windows 平台的 `ab_blackbox_1b` 只選到 `test_autostart_l15_tx_once`（rust_only），A 輪（Python 基線）於 conftest 即以 `rust-only contract` skip、不啟動受測行程，因此不會留下 PID 日誌；原本的 release 判定要求必有 PID 日誌，判為 `ok:false`，B 輪未啟動，`test_cli_mgmt_ab_suite_execution`、`s1a_blackbox.windows`、`s1b_20groups.windows` 與 `sc18-windows-ondemand-autostart` 因此 FAIL。Linux 的 `_release_check` 在同一條件下有相同問題。

依據 plan 05 Task 16（`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` 第 3523 行）：「`rust_only` 案例只跑 B 並驗絕對契約」。新增共用前置判斷 `_a_round_release`（Linux 與 Windows 共用），僅當下列條件全部成立時，release 記為 `ok:true`、`a_round_spawned:false`、`reason` 為「全部選取節點為 rust_only，A 輪僅收集並 skip，未啟動受測行程」，並照常啟動 B 輪：

- 每個選取節點皆為 `_approved_python_skips` 核准的 `rust_only` 節點。
- A 輪 exit 0，JUnit 可解析，且每個選取節點恰一列、結果為 `rust-only contract` 的 skip（passed、xfail 或其他理由的 skip 不符合）。
- `a.pids` 不存在或無任何非空白列。

任一條件不成立（含選取中有共用節點、有 PID 列）一律沿用既有 `_release_check`，缺 PID 日誌仍判 FAIL（fail-closed）。

未放寬任何 judge 或斷言：`test_cli_mgmt_ab_suite_execution` 的斷言、`ab_compare` 判定規則、`_junit_required` 的 skip 核准規則與凍結 lock（`p4_required.lock.json`、`p4_case_map.yaml`）皆未改動；`_release_check` 本身的判定也未改，僅在上述前置條件成立時不呼叫它。B 輪仍須通過 Windows COM pair 與 selftest。`_junit_matches` 只抽出既有的節點比對邏輯，行為不變。

測試以 failing-first 方式加入 `tests/contract_blackbox/test_cli_mgmt_ab_suite.py`：全 rust_only 且 A 輪 skip 乾淨者放行並啟動 B 輪；混入共用節點者仍 `ok:false`；全 rust_only 但 A 輪有 passed、未核准 skip 或 exit 非 0 者仍 `ok:false`；PID 日誌有列者沿用既有檢查。

**待非實作者審查**：本變更對應 `docs/rust-refactor/owner-decisions-addenda.md` 中 PENDING-OD-S1X-WIN-AB-A-ROUND 的選項 (b)（放寬 release 判定），依該登記須經擁有者或非實作者核准。在核准前不得記為 PASS；該項狀態更新由 G5 負責，本 fragment 不改變其 OPEN 狀態。

**核准範圍（2026-10-23）**：Reviewer A（Sonnet max，非實作者）判定 G2 實作 sound；`_a_round_spawned_nothing` 之前置條件在真實凍結資料上 Windows 僅選到 `test_autostart_l15_tx_once`（核准 rust_only）、Linux 選取 13 節點且含共用節點故不觸發。正式核准仍待辦，PENDING-OD-S1X-WIN-AB-A-ROUND 維持 OPEN。
