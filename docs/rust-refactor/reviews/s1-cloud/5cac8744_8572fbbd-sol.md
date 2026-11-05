> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 [`978aa1e1`](file://tests/contract_blackbox/test_cli_mgmt_daemon.py#L144-L192) | 0 | 移除原有 `_wait_dut_tx`，改由 `_checkpointed_dut_tx` 透過 bridge 的 `barrier` 與 `checkpoint` 排空 writer 並封閉比對視窗，在 daemon 停止後比對完整 wire 前綴，並新增 [`test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles`](file://tests/contract_blackbox/test_cli_mgmt_daemon.py#L144-L177) 延遲落盤單元測試確認防線有效。 |
| 2 | Medium | 已修 [`b89e90f7`](file://crates/sw-pal/tests/exec_delegate.rs#L37-L76) | 0 | [`51bd8fa5`](file://crates/sw-pal/tests/exec_delegate.rs#L306-L318) 改由受委派孫行程 helper 寫出 ready 標記並由父行程驗證孫行程 PID 存活；[`b89e90f7`](file://crates/sw-pal/tests/exec_delegate.rs#L37-L76) 補齊可攜負向突變測試 [`delegate_ready_rejects_child_stopped_before_exec`](file://crates/sw-pal/tests/exec_delegate.rs#L37-L76)，驗證在呼叫 `exec_or_wait` 前停住時會逾時拒絕並安全回收測試子行程。 |
| 3 | Medium | 已修 [`ddcbdde0`](file://scripts/rust/l15_exit_driver.py) | 0 | 於 [`scripts/rust/l15_exit_driver.py`](file://scripts/rust/l15_exit_driver.py) 與 [`scripts/rust/stage1_exit_driver.sh`](file://scripts/rust/stage1_exit_driver.sh) 接通第二層判定，非 PASS 列若符合核准延後清單（`BLOCKED-OWNER`）不阻擋出口，並於 [`tests/test_rust_stage1_exit_driver.py`](file://tests/test_rust_stage1_exit_driver.py) 補齊整合測試（後續以 [`4e3ba13f`](file://scripts/rust/l15_exit_driver.py) 強化逐列核對與對帳）。 |
| 4 | High | 已修 [`12b3a929`](file://scripts/rust/mkbundle.sh#L49-L95) | 0 | [`scripts/rust/mkbundle.sh`](file://scripts/rust/mkbundle.sh#L49-L95) 為 Linux bundle 的 Python runtime 內建 wheelhouse 與 PyYAML 封裝，[`tools/docker/upgrade.Dockerfile`](file://tools/docker/upgrade.Dockerfile) 移除預裝系統 `python3-yaml`、隔離 pipx 依賴，並於 [`tests/test_docker_upgrade.py`](file://tests/test_docker_upgrade.py) 新增無系統 PyYAML 升級測試。 |
| 5 | Medium | 已修 [`ac6c4e4e`](file://crates/sw-cli/src/lib.rs#L133-L149) | 0 | [`12b3a929`](file://scripts/rust/mkbundle.sh#L147) 提供 `--release-bundle` 模式排除 `sw-cli/test-fakes`；[`ac6c4e4e`](file://crates/sw-cli/src/lib.rs#L133-L149) 進一步於 [`crates/sw-cli/src/lib.rs`](file://crates/sw-cli/src/lib.rs#L133-L149) 以 `#[cfg(feature = "test-fakes")]` 守衛 `SERIALWRAP_TEST_DELEGATE_GROUP`，並在 release workflow 與 [`tests/test_release_workflow.py:217`](file://tests/test_release_workflow.py#L217-L248) 斷言正式 release binary 排除所有測試與故障注入鉤子。 |
| 6 | Low | 已修 [`12b3a929`](file://docs/rust-refactor/release-0.4.0-plan.md#L10) | 0 | [`docs/rust-refactor/release-0.4.0-plan.md`](file://docs/rust-refactor/release-0.4.0-plan.md#L10) 的 Stage 1A gate 前置條件修訂為接受 `VERIFIED`，或 `IMPLEMENTED` 且僅剩 OD-27～OD-32 明確允許的 `BLOCKED-OWNER`。 |

---

## ## B 新發現

1. **S1-8572fbb-1 [Medium] [tests/contract_blackbox/test_bb_harness_selftest.py:338](file://tests/contract_blackbox/test_bb_harness_selftest.py#L338) — 自測假設 target 目錄恆存，乾淨工作樹下執行拋出 FileNotFoundError 導致測試恆定失敗。**

   在 [`test_ci_sandbox_hides_host_processes_and_runtime`](file://tests/contract_blackbox/test_bb_harness_selftest.py#L301-L350) 中，測試將沙箱內成果路徑指派為 `writable = root / "target" / f"sandbox-probe-{os.getpid()}.txt"`。但在乾淨 checkout 或尚未執行 Cargo 建置的工作樹中，`target/` 目錄並不存在，probe 執行 `Path(os.environ['SW_SANDBOX_WRITABLE']).write_text('ok')` 時直接拋出 `FileNotFoundError: [Errno 2] No such file or directory`，子行程以 exit 1 結束，斷言 `assert result.returncode == 0` 失敗。

   - **具體失敗情境：**
     在未建立 `target` 目錄的乾淨 checkout 下執行：
     `python3 -m pytest -q tests/contract_blackbox/test_bb_harness_selftest.py -k test_ci_sandbox_hides_host_processes_and_runtime`
     預期：PASS。
     實際：
     ```text
     AssertionError: Traceback (most recent call last):
         File "<stdin>", line 16, in <module>
         File "/usr/lib/python3.12/pathlib.py", line 1049, in write_text
       FileNotFoundError: [Errno 2] No such file or directory: '.../target/sandbox-probe-3604238.txt'
     assert 1 == 0
     ```
   - **確切修法：**
     在呼叫子行程前確保父目錄存在：`writable.parent.mkdir(parents=True, exist_ok=True)`，或在 probe 腳本寫入前先建立其父目錄。

2. **S1-8572fbb-2 [Medium] [scripts/rust/blackbox_sandbox.sh:9](file://scripts/rust/blackbox_sandbox.sh#L9) — readlink -e 於無 Docker socket 環境因 set -e 提早終止，第 10 行證據不足 exit 3 無法執行。**

   在 [`scripts/rust/blackbox_sandbox.sh`](file://scripts/rust/blackbox_sandbox.sh#L9-L10) 中，當帶 `--with-docker` 參數時，第 9 行呼叫 `docker_socket="$(readlink -e /var/run/docker.sock)"`。由於腳本頂部設定了 `set -euo pipefail`，若宿主環境未安裝 Docker 或 `/var/run/docker.sock` 不存在，`readlink -e` 返回非零退出碼（1），使得腳本在命令替換處直接崩潰退出（exit 1），第 10 行預期的 `echo '缺少 Docker Unix socket，證據不足' >&2; exit 3` 永遠無法執行，破壞了呼叫端（如測試或出口 gate）依據 exit 3 判定證據不足／能力不足的協議。

   - **具體失敗情境：**
     在無 `/var/run/docker.sock` 的主機上執行：
     `bash scripts/rust/blackbox_sandbox.sh --with-docker true`
     預期：stderr 輸出 `缺少 Docker Unix socket，證據不足`，退出碼為 3。
     實際：stderr 無輸出，腳本因 `set -e` 在第 9 行非零退出碼中斷，退出碼為 1。
   - **確切修法：**
     避免命令替換失敗直接觸發 `set -e`，例如改為：
     ```bash
     docker_socket="$(readlink -e /var/run/docker.sock 2>/dev/null || true)"
     [[ -n "$docker_socket" && -S "$docker_socket" ]] || { echo '缺少 Docker Unix socket，證據不足' >&2; exit 3; }
     ```

---

## ## 既有碼待辦（不計入結論）

1. policy check R-07：`VERSION (0.4.0)` 與最新 tag `(v0.3.2)` 不一致，屬非 PR 上下文的預期基線狀態，留待 release PR 打 tag。
2. `sw-pal::fs_lock::file_lock_nonblocking_contention_wouldblock`：如 round 1 報告所述曾有一次偶發紅燈，但在當前工作區單獨及 workspace 完整測試皆 pass（13 passed），目前尚無證據定性為既有 baseline 或 flaky，留待後續持續觀察。
3. 巢狀 A/B 的 `test_remote_topologies_docker` 在無 Docker daemon 環境下被 skip 導致比較器判為 `evidence-insufficient`：需由具備 Docker 執行能力的 driver 或 CI 環境驗收。

---

## ## 對裁決的異議（不計入結論）

無。

---

結論：需修正
