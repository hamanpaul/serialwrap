> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 [`978aa1e1`](file://tests/contract_blackbox/test_cli_mgmt_daemon.py#L144-L192) | 0 | 於 [`tests/contract_blackbox/test_cli_mgmt_daemon.py`](file://tests/contract_blackbox/test_cli_mgmt_daemon.py) 移除基於固定 sleep 的 `_wait_dut_tx`，改以 bridge 的 barrier／checkpoint 完整封閉 TX wire 視窗（[`_checkpointed_dut_tx`](file://tests/contract_blackbox/test_cli_mgmt_daemon.py#L177-L192)），並於 daemon 停止後排空比對；同時補上延遲第二筆落盤的負向回歸測試 [`test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles`](file://tests/contract_blackbox/test_cli_mgmt_daemon.py#L144-L175)。 |
| 2 | Medium | 已修 [`b89e90f7`](file://crates/sw-pal/tests/exec_delegate.rs#L6-L75) | 0 | 於 [`crates/sw-pal/tests/exec_delegate.rs`](file://crates/sw-pal/tests/exec_delegate.rs) 中，Windows Ctrl 事件測試改由真正受委派執行的孫行程寫出帶有 PID 的 ready 標記，父行程驗證該孫行程 PID 存活後才發送事件；並新增跨平台可攜負向自測 [`delegate_ready_rejects_child_stopped_before_exec`](file://crates/sw-pal/tests/exec_delegate.rs#L37-L75)，透過 `SW_PAL_EXEC_PARK_BEFORE_DELEGATE` 突變鉤子驗證委派前阻塞時 ready 等待必定逾時拒絕。 |
| 3 | Medium | 已修 [`4e3ba13f`](file://scripts/rust/l15_exit_driver.py#L26-L100) | 0 | 於 [`scripts/rust/l15_exit_driver.py`](file://scripts/rust/l15_exit_driver.py) 與 [`scripts/rust/stage1_exit_driver.sh`](file://scripts/rust/stage1_exit_driver.sh) 串接兩層判定，嚴格比對 gate 摘要與 Windows L1.5 原始報告／執行 metadata，確認所有非 PASS 項目皆精確符合 OD-30～OD-32 核准之 `BLOCKED-OWNER` 延後條件時才略過非零退出碼，其他失敗或報告異常依舊 fail-closed。 |
| 4 | High | 已修 [`12b3a929`](file://tools/docker/upgrade.Dockerfile#L12) | 0 | 於 [`tools/docker/upgrade.Dockerfile`](file://tools/docker/upgrade.Dockerfile) 移除預裝 `python3-yaml`，並於 [`scripts/docker/run_upgrade_container.py`](file://scripts/docker/run_upgrade_container.py) 移除 `--system-site-packages`，舊版 pipx 透過離線 wheelhouse 安裝；0.4.0 bundle 則由獨立 Python runtime 內嵌隔離 PyYAML（`include-system-site-packages = false`），並由單元測試 [`test_bundle_runtime_contains_isolated_pyyaml`](file://tests/test_rust_mkbundle.py#L176-L192) 鎖定。 |
| 5 | Medium | 已修 [`12b3a929`](file://docs/rust-refactor/release-0.4.0-plan.md#L20) | 0 | 在 [`docs/rust-refactor/release-0.4.0-plan.md`](file://docs/rust-refactor/release-0.4.0-plan.md) 與 [`scripts/rust/mkbundle.sh`](file://scripts/rust/mkbundle.sh) 規範 `--release-bundle --profile release` 正式產物不啟用 `sw-cli/test-fakes`；後續於 [`ac6c4e4e`](file://crates/sw-cli/src/lib.rs#L130-L150) 進一步排除 `SERIALWRAP_TEST_` 委派覆寫，並由 [`test_release_profile_binary_excludes_test_hooks`](file://tests/test_release_workflow.py#L216-L248) 及 release workflow 進行二進位字串掃描檢驗。 |
| 6 | Low | 已修 [`12b3a929`](file://docs/rust-refactor/release-0.4.0-plan.md#L10) | 0 | 於 [`docs/rust-refactor/release-0.4.0-plan.md`](file://docs/rust-refactor/release-0.4.0-plan.md) 第 10 行修正前置條件敘述，改為接受 Stage 1A gate overall 為 `VERIFIED`，或為 `IMPLEMENTED` 且僅剩 OD-27～OD-32 允許之 `BLOCKED-OWNER`。 |

---

## B 新發現

NO FINDINGS

---

## 既有碼待辦（不計入結論）

1. Windows 測試交叉編譯時，未修改的既有測試檔案 [`crates/sw-pal/tests/osstr.rs`](file://crates/sw-pal/tests/osstr.rs)、[`crates/sw-pal/tests/process_ctl.rs`](file://crates/sw-pal/tests/process_ctl.rs) 與 [`crates/sw-cli/tests/support/mod.rs`](file://crates/sw-cli/tests/support/mod.rs) 存在 unused imports 編譯警示，可於後續清理輪次處理。
2. Policy check R-19 的 CI 步驟解析提示與 R-22 的既有文件懸空引用警告（2928 處）屬於主線既有 advisory 項目，維持原狀。

---

## 對裁決的異議（不計入結論）

無

---

結論：可放行
