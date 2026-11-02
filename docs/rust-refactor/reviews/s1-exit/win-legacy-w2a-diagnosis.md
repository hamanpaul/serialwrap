# S1-WIN-W2a：Windows legacy handoff `INVALID_ENDPOINT` 診斷

## 結論

三支 Windows 原生失敗是**測試夾具錯誤**，不是 `restop()` 的產品邏輯錯誤。`restop()` 從 fake ProcScan 取得的 `DaemonProc.socket` 是帶 `unix://` 的 RPC URL；真實 ProcScan 則從 daemon 的 `--socket` argv 擷取 socket 參數。Windows 測試中的 POSIX 形狀路徑以磁碟代號開頭，fake URL 因而不符合 `unix://` endpoint 語法。

## 重現與確切來源

基準為 `a0d4e0e6`。在 WSL interop 直接執行 Windows 原生測試執行檔：

```text
target/x86_64-pc-windows-gnu/debug/deps/mgmt_legacy_handoff-8c1d7172c623e32e.exe --test-threads=1 --exact legacy_e_single_relaunch_is_restopped
```

結果為 `0 passed; 1 failed`，失敗輸出含 `Unconfirmed { code: "INVALID_ENDPOINT", probe: "daemon.handoff_stop", outcome: "refused" }`。為追出 endpoint，暫時在 `restop()` 加入單行 `eprintln!`，以 Windows target 重建並重跑 exact 測試；完成後已還原該追蹤行，產品與測試 source 無差異。

追蹤到 `restop()` 收到的實際字串為：

```text
unix://C:\Users\haman\AppData\Local\Temp\.tmpQj2k2l\run/serialwrapd.sock
```

`crates/sw-cli/src/legacy_handoff.rs:585-586` 直接把 `relaunched.socket` 傳給 `restop()`。`crates/sw-cli/src/client.rs:319-322` 在 `parse_endpoint()` 失敗時產生 `INVALID_ENDPOINT`；`crates/sw-proto/src/endpoint.rs:343-349` 拒絕 `unix://` URL 中非空的 netloc。上述字串被拆成 scheme `unix`、netloc `C:\Users\...`，因此在送出 RPC 前即被拒絕。

## 根因與平台差異

`TestEnv::posix()` 建立 POSIX 能力的測試環境，但 `tempfile` 在原生 Windows 上仍回傳 `C:\Users\...`。`default_socket()` 再以 `/` 串上 `serialwrapd.sock`（`crates/sw-cli/tests/support/mod.rs:365-370`），`isolated_endpoint()` 將它包成 `unix://C:\...`（同檔 `907-915`）。

初始 daemon 與 `with_legacy_relaunches()` 的假 `ProcDetails.argv` 確實使用去除 `unix://` 的裸路徑（`crates/sw-cli/tests/support/mod.rs:669-688, 768-780`）；但重拉 PID 不走這份 argv。`FakeProcScan::serialwrapd_instances()` 依 RPC endpoint 合成新 `DaemonProc`，並在 `crates/sw-pal/src/fakes/proc.rs:264-279` 原樣填入 `socket: Some(endpoint.clone())`。`restop()` 使用這個欄位，因此拿到的是 `unix://C:\...`。

Linux 上相同 fixture 的 temp 路徑以 `/tmp/...` 開頭，RPC endpoint 會是 `unix:///tmp/...`。其 netloc 為空且 path 為絕對路徑，`parse_endpoint()` 接受它，所以同一測試不失敗。真實 Windows ProcScan 在 `crates/sw-pal/src/proc_scan/mod.rs:111-114` 以 `extract_socket_arg(&proc.argv)` 取得 argv 值，不會把 fake 的 RPC URL 填入 `DaemonProc.socket`。

## 失敗案例與建議修法

Windows 原生執行 `legacy_` 子集合共 26 支，23 支通過、3 支因相同 `INVALID_ENDPOINT` 失敗：

- `legacy_e_relaunch_loop_after_three_stops`
- `legacy_e_single_relaunch_is_restopped`
- `legacy_k_relaunch_inside_stability_window_is_not_stopped`

建議修正 `crates/sw-pal/src/fakes/proc.rs:264-279` 的 `FakeProcScan::serialwrapd_instances()`：合成 Unix daemon 時，將 `unix://` endpoint 轉成與實際 `--socket` argv 一致的裸路徑，再填入 `DaemonProc.socket`；TCP endpoint 維持原樣。保留現有測試斷言，以這三支 Windows 原生案例驗證修復。無需在 `crates/sw-cli/src/legacy_handoff.rs:628-650` 的 `restop()` 對產品 endpoint 做額外正規化。

## 驗證

| 命令／範圍 | 結果 |
|---|---|
| Windows 原生 `legacy_` 子集合 | 23 passed、3 failed（上述三支） |
| Linux `mgmt_legacy_handoff` 完整測試檔 | 35 passed、0 failed |
| `cargo test -p sw-cli --features test-fakes` | 628 passed、0 failed、3 ignored |
| `cargo test --workspace` | 967 passed、0 failed、7 ignored |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | 通過 |
| `python3 scripts/rust/check_cfg_boundary.py` | 通過 |
| `python3 -m tools.contracts check` | `ok: true`，0 findings |
| `python3 tests/acceptance/regen_lock.py --check` | 通過，鎖檔與案例快照一致 |
| `python3 -m policy_check --repo .` | 25 pass、0 fail、2 warn（R-19 CI gate parser advisory、R-22 既有文件引用警告） |

本輪只新增本診斷文件；沒有修改產品碼、測試、凍結鎖或 changelog。未執行全套 pytest、slice gate、Docker；沒有 Python 測試檔被觸及。
