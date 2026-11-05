> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 [`978aa1e1`](file://tests/contract_blackbox/test_cli_mgmt_daemon.py) | 0 | `978aa1e1` 重寫 [`_checkpointed_dut_tx`](file://tests/contract_blackbox/test_cli_mgmt_daemon.py#L178)，以 UART bridge 的 passive barrier（quiet_ms=500、timeout_ms=10000）排空非同步 wire writer，並以 checkpoint 長度與 SHA-256 雜湊封閉 TX 視窗；新增單元回歸 [`test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles`](file://tests/contract_blackbox/test_cli_mgmt_daemon.py#L146) 確保 barrier 前延遲落盤的重複 TX 必納入比對視窗。 |
| 2 | Medium | 已修 [`51bd8fa5`](file://crates/sw-pal/tests/exec_delegate.rs) | 0 | `51bd8fa5` 改由實際受委派之孫行程 helper 寫出 ready 標記與 PID，父行程驗證該 PID 存活後才發送 Ctrl 事件；後續在 [`b89e90f7`](file://crates/sw-pal/tests/exec_delegate.rs#L225) 補上「委派前阻塞／未建立孫行程」的負向突變案例，要求逾時失敗並確保回收測試子行程。 |
| 3 | Medium | 已修 [`ddcbdde0`](file://scripts/rust/l15_exit_driver.py) | 0 | `ddcbdde0` 實作 [`scripts/rust/l15_exit_driver.py`](file://scripts/rust/l15_exit_driver.py) 與 [`scripts/rust/stage1_exit_driver.sh`](file://scripts/rust/stage1_exit_driver.sh) 的雙層判定，逐列比對 gate 報告中的延後項（OD-27～OD-32），只有非 PASS 均符合核准延後規則時才放行；後續在 `4e3ba13f`、`2ae832c8`、`54d7115d`、`2bb93064` 強化出口重判與對帳回歸。 |
| 4 | High | 已修 [`12b3a929`](file://scripts/docker/run_upgrade_container.py) | 0 | `12b3a929` 移除升級容器中的 `--system-site-packages`，改用隔離 pipx 與 `--find-links` wheelhouse；[`scripts/rust/mkbundle.sh`](file://scripts/rust/mkbundle.sh#L287) 將同版同架構之 PyYAML 複製封裝進 bundle runtime `py/` 站點，使 bundle daemon 自給自足；後續經 OD-50（`b84ee5bc`）退役 pipx 安裝並改以 bundle setup 為正式入口。 |
| 5 | Medium | 已修 [`12b3a929`](file://docs/rust-refactor/release-0.4.0-plan.md) | 0 | `12b3a929` 為 [`scripts/rust/mkbundle.sh`](file://scripts/rust/mkbundle.sh#L158) 新增 `--release-bundle` 選項，建置正式產物時不編入 `sw-cli/test-fakes` 特性；發佈計畫同步將配方更新為 `--release-bundle --profile release`，並在 [`tests/test_rust_mkbundle.py`](file://tests/test_rust_mkbundle.py#L82) 補齊斷言。 |
| 6 | Low | 已修 [`12b3a929`](file://docs/rust-refactor/release-0.4.0-plan.md) | 0 | `12b3a929` 更新 [`docs/rust-refactor/release-0.4.0-plan.md`](file://docs/rust-refactor/release-0.4.0-plan.md#L10) 前置條件，將 `overall: PASS` 依實際 gate 輸出改寫為要求 `VERIFIED`，或為 `IMPLEMENTED` 且僅剩 OD-27～OD-32 明確允許之 `BLOCKED-OWNER`。 |

---

## B 新發現

NO FINDINGS

核對本輪修正 [`8572fbbd..de7b6adc`](file://)（含 [`873007a3`](file://scripts/rust/blackbox_sandbox.sh) 與 [`de7b6adc`](file://docs/rust-refactor/reviews/s1m-ci1-round2.md)）：
- [`scripts/rust/blackbox_sandbox.sh`](file://scripts/rust/blackbox_sandbox.sh#L9-L12)：將 assignment 放入條件判斷 `if ! docker_socket="$(readlink -e ...)" || [[ ! -S "$docker_socket" ]]`，解決 `set -e` 下賦值出錯提早以 exit 1 退出問題；解析失敗或非 socket 均輸出診斷並回傳 exit 3，未破壞既有容器路徑。
- [`tests/contract_blackbox/test_bb_harness_selftest.py`](file://tests/contract_blackbox/test_bb_harness_selftest.py#L318-L391)：新增 `clean_sandbox_checkout` fixture 驗證無既有 `target/` 乾淨工作樹時沙箱探針能自動建立目錄並執行；新增缺少 Docker socket 時回傳 exit 3 與空 stdout 的負向回歸；標註繼承模組層 `process_rpc` 與函式層 `posix_only`，符合資源 marker 規範。
- [`tests/test_rust_ci_workflow.py`](file://tests/test_rust_ci_workflow.py#L33-L140)：同步 CI 命令檢查與報告參數，新增拒絕移除沙箱入口的負向突變回歸；標註維持模組層 `core_portable` 與 `memory_only`。
- 本輪變更未修改 Rust 產品程式碼、Python daemon、契約 schema 或凍結鎖；契約檢查（`ok:true`, 0 findings）、cfg 邊界檢查、資源 marker 稽核（9 passed）、單元測試（20 passed、8 passed）及 Cargo workspace 測試（全數通過，0 failed）均維持正常。

---

## 既有碼待辦（不計入結論）

1. **Policy R-07 版本標籤對帳**：`VERSION (0.4.0) mismatches latest tag (v0.3.2)`，屬發布前已知狀態，由後續 0.4.0 發佈流程與 tag 建立對齊。
2. **完整 Docker 驗收**：巢狀 A/B 的 `test_remote_topologies_docker` 與升級 `test_update_systemd_exit1_not_relaunched_l15` 仍待具備 Docker 能力之環境或 CI 實跑驗收。
3. **已登記基線缺陷保留**：WSL interop 與沙箱內 Windows 命令啟動之 setup 失敗依 OD-75 屬於環境／Python daemon 既有基線缺陷，登記於清單而不改判原始紅燈。

---

## 對裁決的異議（不計入結論）

無

---

結論：可放行
