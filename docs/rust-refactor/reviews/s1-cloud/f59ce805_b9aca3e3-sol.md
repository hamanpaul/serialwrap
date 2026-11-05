> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `978aa1e1` | 0 | `tests/contract_blackbox/test_cli_mgmt_daemon.py` 取代原本提早結束的 `_wait_dut_tx`，改以 `_checkpointed_dut_tx` 透過 bridge control 呼叫 `barrier`（排空非同步寫入佇列）與 `checkpoint` 封閉 wire 雜湊與長度，並新增延遲落盤重複 TX 判定測試（後續 commit `897a3cdc` 對齊替身）。 |
| 2 | Medium | 已修 `51bd8fa5` | 0 | `crates/sw-pal/tests/exec_delegate.rs` 改由實際受委派執行的孫行程寫出帶有 PID 的診斷標記，父行程透過 `process_is_running` 確認孫行程活體後才發送 Ctrl 事件，並新增 `tests/test_rust_exec_delegate.py` 回歸防線。 |
| 3 | Medium | 已修 `ddcbdde0` | 0 | `scripts/rust/stage1_exit_driver.sh` 接通 `l15_exit_driver.py` 作第二層判定，核對 gate 報表與延後列名單，合法 BLOCKED-OWNER 不再因 runner 原始 exit code 阻擋出口（後續 commit `4e3ba13f` 逐列核對證據並補齊測試）。 |
| 4 | High | 已修 `12b3a929` | 0 | `tools/docker/upgrade.Dockerfile` 移除預裝 `python3-yaml`，`run_upgrade_container.py` 移除 `--system-site-packages` 改由 wheelhouse 提供 0.3.2 依賴；`scripts/rust/mkbundle.sh` 將 PyYAML 封裝進 bundle 自身 site-packages，避免依賴系統 Python。 |
| 5 | Medium | 已修 `12b3a929` | 0 | `scripts/rust/mkbundle.sh` 新增 `--release-bundle` 選項，正式 bundle 建置不帶 `--features sw-cli/test-fakes`，故障注入變數不編入產物，並同步修正 `release-0.4.0-plan.md` 建置配方與 `tests/test_rust_mkbundle.py`。 |
| 6 | Low | 已修 `12b3a929` | 0 | `docs/rust-refactor/release-0.4.0-plan.md` 前置條件已更新為實際狀態判準：接受 `VERIFIED`，或 `IMPLEMENTED` 且僅剩 OD-27～OD-32 明確允許之 `BLOCKED-OWNER` 列。 |

## B 新發現

NO FINDINGS

審查範圍 `f59ce805..b9aca3e3` 僅單一 commit `b9aca3e3`（`fix(release): 修正 bundle smoke 的指紋版本判定`）：
- `.github/workflows/release.yml` 的 Linux bundle smoke 測試原先以字串恆等式要求 `serialwrap ${VERSION}`，導致帶有程式碼指紋的合法輸出被拒絕；本輪改採 inline Python 腳本以 `re.fullmatch` 嚴格匹配 `rf"serialwrap {re.escape(version)}(?: \(code [0-9a-f]{{12}}\))?"`，外部可觀察行為 1:1 對齊 Python 基線（`sw_core/version.py`）與 Rust CLI 輸出格式。
- 新增 `changelog.d/s1m-ba1-release-smoke-version.md` 符合 fragment 命名與格式規範。
- `tests/test_release_workflow.py` 增補合法（純版本行、帶 12 位十六進位指紋）與 17 種非法畸形／錯版輸出（含大寫 hex、前導／尾隨空格、多餘換行等）之正反向回歸驗證，全數 54 passed。
- `python3 -m tools.contracts check` 通過（ok: true），`python3 -m policy_check --repo .` 通過（25 pass, 0 fail, 2 pre-existing warn）。未引入新缺陷。

## 既有碼待辦（不計入結論）

無

## 對裁決的異議（不計入結論）

無

結論：可放行
