# Slice 2.2 擁有者證據手冊（BLOCKED-OWNER #1～#7）

對應 `plans/07-slice22-console.md` 文末「BLOCKED-OWNER 總表」。本手冊各節為 `tests/acceptance/slice22_evidence_map.yaml`
中 `status: BLOCKED-OWNER` 條目的 `procedure`；雲端／CI 環境不得產出這些證據，gate（`scripts/rust/s22_gate_check.py`）
一律回報 `BLOCKED-OWNER`、不得標 PASS。擁有者回填證據後經審查才能解除。Slice 2.1 繼承的 L2／L3／Windows 條目沿用
`docs/rust-refactor/slice21-owner-runbook.md` 的 BO-S21-07／08／13／14／16。

### BO-S22-01

**Windows-native Console L1.5（Telnet console＋Windows 側 DUT serial），Python 與 Rust daemon Sequential A/B（總表 #1；OD-15 可執行 gate）**

- 案例：`win-telnet-raw-owner`、`win-telnet-linebuffer`、`win-telnet-suspend-resume`、`win-telnet-deferred`。
- 前置：OD-08 com0com DUT-side pair `COM20:COM21`；daemon 以 OD-11 隔離實例管理 `COM20`（`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20`），
  `COM21` 接 `tools/uart_stdio_bridge` Windows pySerial 端點與 DUT 模擬器（`local-process`）；Rust bundle 與 Python 基線皆已安裝。
- 命令：`pwsh -File scripts/rust/win-blackbox.ps1 -Subset console`（先 Python、停機確認 `COM20` 釋放，再 Rust；兩輪各用新模擬器實例）。
- 預期證據：`target/win-blackbox/<run_id>/{python,rust}/junit.xml`、`bridge.v1` 紀錄、`daemon.stderr`、`ab_summary.json`。
- PASS 條件：兩輪 junit 皆無 failed／error，`ab_summary.json` 無非登記差異。LOCAL-WINDOWS-TODO：`-Subset console` 與 Windows `telnet_daemon` adapter（07 Task 12）。

### BO-S22-02

**Console-side com0com Serial console（OD-01 選配；總表 #2）**

- 案例：`sc29-com0com-serial-console`。
- 程序：`docs/rust-refactor/procedures/windows-serial-console.md`（前置、`windows.serial_console`、Tera Term Serial 逐字元互動、
  DTR／RTS／baud 不外洩、`cap_windows_com__console_com0com_*`）。

### BO-S22-03

**Tera Term／PuTTY／`telnet.exe` 互動桌面驗證（總表 #3）**

- 程序：`docs/rust-refactor/procedures/windows-telnet-frontends.md`。

### BO-S22-04

**L2：F5（4 案）、F6（2 案）、F8（2 案）以 Rust bundle 在 live bench 執行（總表 #4）**

- 案例：`f5-console-peer-gone-recycled`、`f5-deferred-bytes-flushed`、`f5-deferred-dropped-after-reboot`、`f5-raw-ownership-survives-agent-rounds`、
  `f5-second-console-linebuffer`、`f6-ping-during-long-op`、`f6-two-boards-no-starvation`、`f8-second-daemon-detected`、`f8-tty-holders-reported`；
  `f4-interactive-line-cmd-defined`（Slice 2.2 分母；Python 基線黑箱回 `SEND_FAILED` 的 parity 判定待擁有者，見 p6-fix-responses）。
- 前置：交 bench 前先以 `scripts/docker/run-l15.sh` 跑 `L1.5-docker`（不計 L2）；testbed 的 `serialwrap_exe` 指向 Rust bundle `bin/serialwrap`；bench 互斥鎖。
- 命令：`testpilot run serialwrap_regression --family F5,F6,F8`，另 `testpilot run serialwrap_regression --case f4-interactive-line-cmd-defined`。
- 預期證據：TestPilot 原始結果（`~/b-log/regression-reports/tp-<ts>/`）＋外層 envelope（§11.4）；逐案彙整 `case_verdicts.json`。

### BO-S22-05

**L3：realhw P0／P1 console 與 WAL 案例（總表 #5）**

- 案例：`p0-console-raw`、`p0-blog-clean`（Windows 為 P2 登記的 gap，該平台組合不適用）、`p1-con-fanout`、`p1-con-defer`、`p1-con-busy`、
  `p1-con-softpreempt`、`p1-con-liveness`、`p1-con-orphan`、`p1-con-second`、`p1-cmd-modes`、`p1-wal-reset`、`p1-wal-fullrun`。
- 前置：realhw bench（`two_boards`、`tmux`）；部署 Rust bundle。
- 命令：`python3 -m realhw --tier p0,p1 --only <上列案例> --report-dir ~/b-log/rust-refactor/p6/bo-s22-05/<ts>/<linux|windows>`。
- 預期證據：`engine_result`＋`release_gate_result`（兩平台報告目錄）。

### BO-S22-06

**跨 reboot 實板情境（總表 #6）**：`docs/rust-refactor/procedures/cross-reboot-console.md`。

### BO-S22-07

**規格 §1.2 Console 按鍵至回顯 SLO（總表 #7）**：`docs/rust-refactor/procedures/console-echo-loopback.md`（in-process 回歸門檻為
`crates/sw-core/tests/console_echo_latency.rs`，不得以之取代實體 loopback 量測）。

### #108（parity 承接，LOCAL-WINDOWS-TODO）

**Windows TCP `attach_console` 不開 lease（B:D-61、D-101；rulings/p6.md P6 OWNER-BATCH #4 承接拆分）**

- 承接拆分：file 傳輸 suspend 時既有 human 視窗 defer（`tests/test_refine_ownership.py::TestRefineOwnership::test_existing_human_owner_window_is_deferred_before_suspend`）改列 P8（Slice 2.4）必要案例，不在本節；本節只承擔 Windows TCP console 後端部分。
- Linux 可驗部分：SC-28 TCP 後端的回應形狀由 `crates/sw-daemon/tests/rpc_console.rs::attach_tcp_shape` 承擔（s22_gate:cargo_workspace）。
- 前置：Windows 11 host（擁有者 Windows runner），Python 3.12＋PyYAML＋pytest；Rust workspace 以原生 `x86_64-pc-windows-gnu` 建置。
- 命令：
  1. `python -m pytest -q tests/test_refine_ownership.py::TestRefineOwnership::test_tcp_accept_during_operation_gate_does_not_get_raw_owner`（Python 基線：TCP accept 不取得 raw owner、admission 關閉期間不 TX）。
  2. `bash scripts/rust/win-test.sh --package sw-daemon -- --features test-fakes`（含 07 Task 12 的 `windows_console_native.rs`：Windows TCP console 的 `session.console_attach` 回應不含 `interactive_session_id`、不開 human lease，之後 interactive_open 不被該 console 擋）。
- 預期證據：pytest junit、`target/win-test-report/*.json` 與其 `l1-results`。
- PASS 條件：兩者皆無 failed／error；Rust 側斷言與 Python 行為一致。LOCAL-WINDOWS-TODO：`windows_console_native.rs`（07 Task 12）尚未撰寫。
