# Serialwrap 黑箱契約測試套件（tests/contract_blackbox/）

本目錄包含針對 Serialwrap CLI 與 Daemon 外部契約的黑箱測試套件。黑箱測試僅透過命令列介面（CLI）、位元組流（stdin/stdout/stderr）以及通訊端點（AF_UNIX socket / TCP loopback / PTY 假 DUT）與受測目標互動，嚴格禁止 import 任何產品內部模組（`sw_core`、`serialwrap`、`serialwrapd`）。

## 環境變數與 A/B 選擇器

黑箱 harness 透過下列環境變數選定受測 artifact：

- `SERIALWRAP_CLI_EXE`：受測 CLI 執行檔路徑。未設定時預設使用 Python 基線入口（POSIX 為 `<repo>/serialwrap`；Windows 為 `[sys.executable, <repo>/serialwrap.py]`）。若指向 `.py` 結尾檔案，harness 自動於前面加上直譯器。
- `SERIALWRAP_DAEMON_EXE`：受測 Daemon 執行檔路徑。未設定時預設使用 `<repo>/tests/contract_blackbox/bin/py-serialwrapd`（Windows 為 `[sys.executable, <repo>/serialwrapd.py]`）。
- `SERIALWRAP_AB_LABEL`：A/B 測試標籤，預設為 `"python-baseline"`。當以 `"rust"` 開頭時（例如 `"rust-1a"`），`is_rust()` 為真，代表目前處於 Rust 受測輪。

## 獨立執行命令

黑箱測試可完全脫離本專案根目錄的 `conftest.py` 獨立執行：

```bash
python3 -m pytest -q -m "core_portable or posix_only" tests/contract_blackbox --confcutdir=tests/contract_blackbox -p no:cacheprovider
```

在獨立執行模式下，`conftest.py` 會於 `pytest_sessionfinish` 嚴格斷言 `sys.modules` 中未載入任何 `sw_core` 相關模組。

## Marker 說明

- `rust_only`：僅在 Rust 受測輪執行。當 `is_rust()` 為假（即測試 Python 基線時），測試會自動 skip，原因為 `rust-only contract`。
- `needs_py_daemon`：標記需要 Python daemon 基線支援的測試案例。
- `needs_delegation`：標記在 Rust CLI 輪需要透明委派支援的測試案例（Task 16）。
- 平台與資源 marker 一律遵循 P0 T14 規範（如 `core_portable`、`posix_only`、`windows_native`、`process_rpc`、`posix_pty` 等）。

## Windows 原生 interop 執行入口

在 Task 17 交付後，可在 WSL2 環境透過 interop 腳本呼叫 Windows 原生直譯器執行黑箱測試：

```bash
scripts/rust/win-blackbox.sh
```

或在原生 Windows 命令提示字元／PowerShell 下執行：

```cmd
py -3.12 -m pytest -q -m "core_portable or windows_native" tests\contract_blackbox --confcutdir=tests\contract_blackbox -p no:cacheprovider
```

`scripts/rust/win-blackbox.ps1` 另接受選用參數 `-Tests <逗號分隔的 pytest 路徑>`（P7 Task 18A），只跑指定案例（如 `-Tests tests\contract_blackbox\test_p7_events.py`）；未給時維持整個 `tests/contract_blackbox`。

## P7（Slice 2.3）測試工具

- `bb_faketarget.FakeTarget` 新增 keyword-only 參數 `login_steps`、`commands`（`overrides`）、`delay_ms`、`noise`、`console_lock`、`boot_on_start`，與方法 `emit`、`freeze_rx`／`thaw_rx`、`boot`、`rx_mark`／`rx_log_since`；全部未給時逐位元組維持 P3 行為（語意見模組 docstring）。
- `bb_p7.py`：`p7_profile_dir(tmp, name, **subst)` 複製 `p7_profiles/<name>.yaml` 並代換 `${BY_ID}`、`${ENV_FILE}` 等大寫佔位（缺值 → `KeyError`）；`cli_json`、`wait_state`；fixture `p7` 回傳 `P7Rig`（`start(profile, targets)`、`cli(...)`、`wal_records(sel)`、`stop()`）。
- 自測：`tests/contract_blackbox/test_bb_faketarget_p7_selftest.py`。
