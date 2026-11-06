> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1 | 0 | `tests/contract_blackbox/test_cli_mgmt_daemon.py` 已改用 `_checkpointed_dut_tx`，透過 bridge 的 passive barrier（quiet 500 ms、有界 10 s）確認 settled，並依 checkpoint 的長度與 SHA-256 雜湊封閉完整 wire 視窗；已補「barrier 前第二筆延遲落盤重複 TX」回歸測試（`test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles`）。 |
| 2 | Medium | 已修 51bd8fa5 | 0 | `crates/sw-pal/tests/exec_delegate.rs` 在 `51bd8fa5` 改由實際受委派的孫行程寫出 `DELEGATE_READY_ENV` 標記與 PID，父行程以 Win32 API 驗證該孫行程 PID 存活後才發送 Ctrl 事件，並於事件後確認孫行程終止；後續 `b89e90f7` 再補齊跨平台「委派前停住／未建立孫行程」負向突變案例（`delegate_ready_rejects_child_stopped_before_exec`），要求逾時拒絕。 |
| 3 | Medium | 已修 4e3ba13f | 0 | `scripts/rust/l15_exit_driver.py` 在 `4e3ba13f` 接通第二層判定，由 driver 對完整原始報告與 metadata 作逐列比對，只有在全部非 PASS 列均精確符合核准的 OD 延後清單時才略過 Windows wrapper 非零碼；`tests/test_rust_stage1_exit_driver.py` 已補齊 driver 整合回歸（後續 `2ae832c8` 與 `2bb93064` 進一步防禦無效輸出與基線缺陷對帳）。 |
| 4 | High | 已修 12b3a929 | 0 | `12b3a929` 自 `tools/docker/upgrade.Dockerfile` 移除系統 `python3-yaml`，`scripts/docker/run-upgrade.sh` 移除 `--system-site-packages`，舊版 pipx 依賴改由 wheelhouse 提供；`scripts/rust/mkbundle.sh` 對 Python runtime 建立獨立隔離環境（`include-system-site-packages = false`）並自帶 PyYAML；`tests/test_rust_mkbundle.py` 已補齊 `test_bundle_runtime_contains_isolated_pyyaml` 測試。 |
| 5 | Medium | 已修 12b3a929 | 0 | `scripts/rust/mkbundle.sh` 新增 `--release-bundle` 選項，正式發佈建置不帶 `sw-cli/test-fakes` feature；`docs/rust-refactor/release-0.4.0-plan.md` 發佈配方已更新為 `--release-bundle`；`tests/test_rust_mkbundle.py` 補上 `test_release_bundle_build_does_not_enable_test_fakes` 斷言正式建置參數不含測試故障注入。 |
| 6 | Low | 已修 12b3a929 | 0 | `docs/rust-refactor/release-0.4.0-plan.md` 第 10 行發佈前置條件已改寫為實際 gate 產物：接受 `VERIFIED`，或 `IMPLEMENTED` 且僅剩 OD-27～OD-32 明確允許之 `BLOCKED-OWNER` 列。 |

---

## B 新發現

NO FINDINGS

審查範圍：`0ea3cc9c..d6cf1c9c`（單一 commit `d6cf1c9c`：`fix(bridge): 依寫入進度判定證據停滯`）。
變更涵蓋：
- `changelog.d/bridge-evidence-stall.md`
- `tools/uart_stdio_bridge/src/uart_stdio_bridge/bridge/core.py`
- `tools/uart_stdio_bridge/src/uart_stdio_bridge/evidence/writer.py`
- `tools/uart_stdio_bridge/tests/test_writer.py`

審查要點與驗證結果：
1. **進度判定機制**：`_wait_with_progress` 與 `_mark_progress` 藉由 `_progress_lock` 與 `_progress_changed` condition 變數追蹤寫入進度，短寫與大封包循序排空時每次有效位元組寫入均推進進度，避免大佇列排空因整體時間長於固定 deadline 而被誤判為逾時；單次操作並受 `_ABSOLUTE_WAIT_CAP_S`（300 s）有界約束，防止無界等待。
2. **鎖序與併發**：所有 `_notify_progress_waiters()` 與 `_mark_progress()` 呼叫皆在釋放 `self._lock` 或無持鎖狀態下觸發；`_acquire_with_progress` 採非阻塞 `acquire(blocking=False)` 輪詢，無 Lock Inversion 或死鎖風險。
3. **錯誤分類與 detail**：`close_stall`、`flush_stall`、`backpressure_stall`、`write_error` 正確附於 `EvidenceFailure.detail` 與 `self._failure_detail`，並由 `Bridge._finish` 以 additive 欄位寫入 stderr 的 `BRIDGE_FAILED` JSON，不破壞既有欄位契約。
4. **實測與稽核**：
   - `python3 -m pytest -q tools/uart_stdio_bridge/tests/test_writer.py`：44 passed, 2 warnings（新增 7 支進度判定與 detail 測試全數通過）。
   - `python3 -m pytest -q tests/test_resource_markers.py`：9 passed, 11 warnings。
   - `python3 -m tools.contracts check`：0 findings（`ok: true`）。
   - `python3 -m policy_check --repo .`：25 pass, 0 fail, 2 warn（R-19 與 R-22 為預期既有警示）。
   - `python3 -m pytest -q tools/uart_stdio_bridge/tests/`：318 passed, 5 skipped, 1 failed。唯一失敗為 `test_approved_revision_matches_tool_tree`，此為治理預期（修改者不得私自更新 `APPROVED_REVISION.json`；工具 tree SHA-256 現為 `0021ba6703decf05cea53668060984dbdf1fa3dcee32a84cfa0d2c3ef4704c1c`，待審查放行後於獨立 chore commit 登錄）。

---

## 既有碼待辦（不計入結論）

無

---

## 對裁決的異議（不計入結論）

無

---

結論：可放行
