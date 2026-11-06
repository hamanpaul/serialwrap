# S1M-241：版本程式碼指紋對齊驗收文件

## 1. 裁決來源

- **編排者裁決**：`origin/docs/parity-amendments:docs/rust-refactor/reviews/rulings/s1.md` 第 18 列「#241 版本程式碼指紋在 Stage 1（Rust CLI＋Python daemon）的跨語言語意」：
  - 採納 (a) CLI 指紋 ＝ 同一 bundle 內附 Python daemon 程式碼指紋。
  - `mkbundle.sh` 建置時以 `sw_core/version.py` 相同演算法計算 bundle 內 `sw_core/**/*.py` 的 `code_fingerprint` 並寫入 `release-manifest.json`。
  - Rust CLI 的 `version`／`doctor`／`setup`（含升級後驗證）與三類 RPC 回應警告均以 manifest 指紋作為「CLI 端指紋」，與 daemon 回報的 `code_fingerprint` 進行比對。
  - 對外輸出格式與 Python 1:1（不登記 DF）；無 manifest（開發建置）或欄位缺失時，比照 Python 無法取得指紋時的輸出（靜默不警告）；不得改用 Rust 執行檔雜湊。
- **擁有者裁決**：OD-14～OD-44、OD-50、OD-74 等相關決策。

## 2. 定位基準與 Python 基線

- **Rust 定位基準**：`ef63f21c`。
- **實際採用的 Python #241 commit**：PR #241 merge commit `464ae776ca52682027ca37c49e4695d03ed80c7d`（分支 `origin/fix/build-fingerprint` tip `0757f1da`），標題為「feat(version): 加程式碼指紋，看得出 daemon 跑的是哪份碼；setup 升級後讓 systemd 服務的 daemon 換新碼」。

## 3. 隔離方式

- 所有驗收與回歸測試嚴格使用 `_isolated_env` 隔離 9 個 `SERIALWRAP_*` 目錄環境變數與所有 XDG 目錄（`XDG_CONFIG_HOME`、`XDG_STATE_HOME`、`XDG_DATA_HOME`、`XDG_RUNTIME_DIR`），搭配每測試專屬 `tmp_path` 與獨立 Unix socket。
- 絕不接觸 production serialwrap daemon（`pipx ~/.local/bin/serialwrapd`）、主設定檔、真實序列埠（`/dev/ttyUSB*`）或 Docker 容器。
- `setup` 子程序測試使用 `_isolated_pidns_prefix()` 於非特權 PID namespace 下隔離執行，避免行程掃描碰觸宿主行程。

## 4. 各分支變更與接線證據

### 4.1 一般 RPC 警告 (`crates/sw-cli/src/commands/rpc_cmds.rs:355`)
- 接上 `crate::install_layout::code_fingerprint(ctx)` 取得 manifest 宣告指紋。
- 呼叫共用 `warn_version_mismatch_with_fingerprint`。
- 保留既有順序：stdout 寫入 JSON、若 `!ok` 在 stderr 印出錯誤行，最後於 stderr 輸出指紋／版本警告；exit code 為 0（成功）或 2（失敗）。

### 4.2 event RPC 警告 (`crates/sw-cli/src/commands/event_cmd.rs:230`)
- 接上相同 manifest 指紋來源與共用 API。
- 保留 Python 基線 `_dispatch_event` 既有順序：stdout 寫入 JSON、stderr 輸出指紋／版本警告、若 `!ok` 由 `mirror_err` 在 stderr 輸出錯誤行；exit code 為 0（成功）或 2（失敗）。

### 4.3 daemon status 警告 (`crates/sw-cli/src/commands/daemon_cmd.rs:1213`)
- `emit_rpc_response()` 接上相同 manifest 指紋來源與共用 API。
- 保留既有順序：stdout 寫入 JSON、若 `!ok` 輸出錯誤行、stderr 輸出指紋／版本警告；exit code 為 0（成功）或 2（失敗）。

### 4.4 測試與實跑覆蓋 (`tests/test_s1m241_cli_fingerprint_parity.py`)
- `test_s1m241_formal_rpc_warning_paths_use_manifest`：參數化覆蓋一般 RPC（`device list`）、event（`event list`）與 daemon status（`daemon status`），確認在 daemon 程式碼指紋不符且版本相符時，三類正式 Rust CLI 命令皆在 stderr 輸出 Python 原文指紋警告，且 stdout 維持單一 JSON。未接線時因無警告而先紅，接線後轉綠。
- `test_s1m241_bundle_cli_and_daemon_share_fingerprint`：以真實 dev bundle 啟動 Python daemon，實跑 Rust `--version`、`--help`、一般 RPC、`doctor`。manifest、CLI 顯示及 daemon 回報指紋完全一致；`--version` 與 `--help` 保留純文字格式；`doctor` 的 `daemon_code_current` 檢查使用同一來源且維持 advisory（`ok=true`, `advisory=true`）。
- `test_s1m241_rpc_warning_edge_cases`：
  - 指紋相符時不警告。
  - 缺 manifest 端指紋時靜默不警告。
  - 版本不同時（如 client 0.3.2 vs daemon 0.2.0）只印版本警告，不印指紋警告。
  - 失敗 RPC 保留原錯誤行（stderr 第一行）及指紋警告（stderr 第二行），exit code 恆為 2。
- `test_s1m241_setup_current_manifest_is_reported`：matched 指紋實跑 `--on-demand`。
- `test_s1m241_setup_stale_systemd_restarts_and_verifies`：stale 指紋實跑 `setup --user` 自動重啟與驗證。
- `test_s1m241_setup_stale_on_demand_reports_manual_restart`：stale 指紋實跑 `setup --on-demand` 報告 `action=manual_restart`。
- `test_s1m241_setup_stale_systemd_no_restart_reports_skipped`：stale 指紋實跑 `setup --user --no-restart` 報告 `action=skipped_no_restart_flag` 且不執行 restart。

### 4.5 複製離開 manifest 的 CLI（S1M-x2）

A/B 的 setup reconcile 會把 bundle CLI 複製到沒有 `release-manifest.json` 的目錄，避免走 SC-23。裁決仍要求這份 CLI 的指紋等於同一 bundle 內 Python daemon 的程式碼指紋，不得改用執行檔雜湊。

- `mkbundle.sh` 在 `cargo` 前以 `sw_core/version.py` 計算 repo `sw_core` 指紋，寫入 `crates/sw-cli/src/embedded_code_fingerprint.bin`（`SWFP1:` 加 12 位小寫十六進位）。`cargo` 結束或失敗都還原占位 `SWFP1:000000000000`，工作樹不留下真指紋。開發建置維持占位，視為沒有指紋。
- `code_fingerprint`：找到 manifest 時只用其中欄位（缺欄位為 `Ok(None)`，不退回編譯期值）；`ManifestNotFound` 才讀編譯期戳記；manifest 損壞仍回錯誤。
- `doctor` 的 `daemon_code_current`：manifest 有效時用欄位，找不到 manifest 時用編譯期戳記，manifest 損壞維持無法計算。
- `tests/test_rust_mkbundle.py::test_s1m_x2_copied_cli_setup_matches_python_daemon_code`：複製後的 `--version` 與 `setup --user`（mock daemon 不回報指紋）必須與 Python 基線的 `daemon_code` 相同，且占位檔已還原。

## 5. 未完成平台項目與測試屬性

- 本變更已完全由 pytest 及 mock / subprocess 測試覆蓋。
- 本環境不得且未宣稱任何實體 UART 證據。
- Windows 原生環境測試（`windows_native` / `windows_com`）受限於 WSL2 環境，需由原生 Windows runner（`win-pytest.sh`）執行採集。
