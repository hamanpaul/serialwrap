# 08 — 測試資產盤點（tests/、func-test/、regression/、realhw/、reliability/、tools/docker）

- 盤點對象：`~/prj_pri/serialwrap-worktrees/rust-refactor`，HEAD `93553e6`（VERSION `0.3.2`）。唯讀盤點；唯一執行的命令是 `func-test/runner.py -l` 與 `pytest --collect-only`，兩者都不跑任何 case。
- 規格參照：`docs/rust-refactor/serialwrap-rust-spec-final.md` §3.2、§8.3、§11、§15，另參照 `docs/rust-refactor/owner-decisions-2026-10-15.md`（OD-07/11/13）。
- 行號標記 `file:line` 一律相對於 repo root。

---

## 0. 總表：各套件如何選定受測 serialwrap 執行檔

| 套件 | 入口 | CLI 執行檔來源 | daemon 執行檔來源 | 版本閘門（#154） | 能否「只改設定」改指 Rust artifact |
|---|---|---|---|---|---|
| `tests/` e2e（`test_multiagent_e2e.py`、`test_human_agent_coexist.py`） | `python3 -m pytest -q tests/` | 寫死 `ROOT_DIR/"serialwrap"`（repo 內 Python wrapper，`tests/test_multiagent_e2e.py:17`、`tests/test_human_agent_coexist.py:27`） | `[$PYTHON or "python3", ROOT_DIR/"serialwrapd.py", --profile-dir, --socket, --lock]`（`test_multiagent_e2e.py:190`、`test_human_agent_coexist.py:167`） | 無 | **不能**：只有 `PYTHON` env 可換直譯器，沒有 exe 覆寫鉤子 |
| `tests/test_bootloader_recovery.py::test_cli_parser_has_allow_attached_flag` | pytest | `[sys.executable, repo_root/"serialwrap", "session","interactive-open","--help"]`（`:1035`） | — | 無 | 不能 |
| `tests/test_minicom_router.py` | pytest | env `SERIALWRAP_BIN`＝測試寫出的 fake bash script（`:68`、`:445`） | — | — | 受測物是 shell router，不是 CLI |
| `func-test/` | `python3 func-test/runner.py` | 寫死 `ROOT_DIR/"serialwrap"`（`func-test/lib/daemon_harness.py:19`；`cli_client.py:22`） | `[$PYTHON or "python3", ROOT_DIR/"serialwrapd.py", ...]`（`daemon_harness.py:111-123`） | 無 | **不能**（spec §15.5 要求 DaemonHarness 擴充成可啟動指定 artifact，目前沒有實作） |
| `regression/`（TestPilot `serialwrap_regression`） | `testpilot run serialwrap_regression` | testbed `serialwrap_exe`，預設 `~/.local/bin/serialwrap`（展開 `~`；`regression/serialwrap_regression/core.py:15-35`；`testbed.yaml.example:3`）；`drivers.SwCli(exe=...)`（`core.py:151`） | prod daemon 不由測試啟動；ThrowawayDaemon 用 `Path(serialwrap_exe).with_name("serialwrapd")`，以 `nohup` 啟動（`guards.py:169-178`） | **有**：`preflight.version_gate()`（`regression/serialwrap_regression/preflight.py:39`）用 `serialwrap_exe --version` 對 `daemon status.version` 比對；不一致就整場拒跑 | **可**：在 `testbed.yaml` 改 `serialwrap_exe`。但有三個隱性依賴：(a) Rust CLI 旁邊必須有同名 sibling `serialwrapd`；(b) console case 經 tmux 執行 `serialwrap-minicom`（PATH），router 取用的是**它自己旁邊的 `serialwrap`**，不是 pinned exe（見 §3.11）；(c) `--version` stdout 必須含 `X.Y.Z` |
| `realhw/`（`python3 -m realhw`） | `python3 -m realhw --tier ...` | `drivers.SwCli()` 預設 `"serialwrap"`，走 **PATH**（`realhw/__main__.py:37`；`drivers.py:116`） | 不啟動 daemon；用 `systemctl show -p MainPID serialwrap`（system scope）取 PID | **無**；只有 capability `deployed_recent`：`parse_version(--version) >= MIN_DEPLOYED=(0,2,3)`（`realhw/preflight.py:36,53-63`） | 只能靠調整 PATH；沒有 pin |
| `reliability/`（TestPilot `serialwrap_reliability`） | `testpilot run serialwrap_reliability` | `drivers.SwCli()`，走 PATH（`reliability/serialwrap_reliability/core.py:169,300`） | 同 realhw | **無** | 只能靠調整 PATH |
| `tools/docker/remote_smoke.sh`、`remote_tunnel_test.sh` | `bash tools/docker/...` | 容器內 `pip install .` 產生的 console_script `serialwrap`（PATH，`Dockerfile:22`）；`uart_harness.py`／`remote_lab.py` 自身則用 `/opt/serialwrap/serialwrap` | 透過 `func-test` 的 DaemonHarness → `python3 /opt/serialwrap/serialwrapd.py` | 無 | 不能（image 由 repo 原始碼 build） |

`--version` 輸出契約：`argparse` 的 `action="version", version=f"serialwrap {_resolve_version()}"`（`sw_core/cli.py:1920`），stdout 一行 `serialwrap 0.3.2`，exit 0。測試端用 `SwCli.run("--version")["_raw"]` 取值：因為輸出不是 JSON，`json.loads` 會失敗，所以落成 `{"_raw": out}`（`realhw/drivers.py:119-128`）。版本 regex 是 `(\d+)\.(\d+)\.(\d+)`，取第一個匹配（`regression/serialwrap_regression/preflight.py:27`、`realhw/preflight.py:35`）。

`daemon status.version`：`health()` 明確帶 `"version": self._version`（`sw_core/service.py:577`）；`rpc()` 另以 `result.setdefault("version", version)` 把版本補進**所有** dict 回應（`sw_core/service.py:690-703`）。

---

## 1. `tests/`（pytest，L1）

### 1.1 數量與 CI

- 目錄共 153 項（另有 `__pycache__/`）：150 個 `test_*.py`，加上 `conftest.py`（118 行）、`liveguard.py`（252 行）、`state_iso.py`（47 行）。
- `pytest --collect-only -q tests/` 收集到 **1984 個 test item**（含 parametrize 展開）；AST 計算的 `def test_*` 為 1818 個。CLAUDE.md 記載的「1636 passed, 16 skipped」（合計 1652）已過時。
- 沒有 `pytest.ini`、`setup.cfg`、`tox.ini`；`pyproject.toml` 也沒有 `[tool.pytest]`。marker 只用到 `pytest.mark.parametrize`（44 處）與 `pytest.mark.skipif`（9 處），**沒有** `core_portable`、`posix_only`、`windows_native`，也沒有任何 resource marker。
- CI 定義在 `.github/workflows/tests.yml`：`on: [pull_request]`、`ubuntu-latest`、Python 3.12、`pip install pytest pyyaml pyserial`。依序執行 `python3 -m pytest -q tests/`、安裝 `google-re2`、再跑一次 `python3 -m pytest -q tests/`。整條 CI 只有 Linux，**沒有 Windows job**。

### 1.2 `tests/conftest.py`：#120 三層隔離防線

時序：module top-level 在 collection 之前執行。`sw_core/constants.py` 的路徑常數在 import 時就凍結，所以必須在任何 `sw_core` import 之前改好 env。

- **第 0 步：live 快照**（在覆寫 env 之前，`conftest.py:20-29`）
  - `_MODE = os.environ.get("SERIALWRAP_LIVE_GATE","strict").strip().lower()`
  - `_SHELL_WAL_DIR = os.environ.get("SERIALWRAP_WAL_DIR")`
  - `_PRE_STATE`、`_PRE_WAL`、`_PRE_CONFIG`：`liveguard.snap_file(live_*_path())`
  - `_PRE_SHELL_WAL`：只有 `_SHELL_WAL_DIR` 存在時才擷取 `<dir>/raw.wal.ndjson`
  - `_PRE_DAEMON = liveguard.snap_daemon()`
- **第 1 層：強制 env 隔離**（`conftest.py:32-71`）
  - `_ISO_ROOT = tempfile.mkdtemp(prefix="sw-pytest-iso-")`
  - 硬覆寫 9 個變數，每個都指向 `_ISO_ROOT/<name>` 並先 mkdir：`SERIALWRAP_STATE_DIR→state`、`SERIALWRAP_RUN_DIR→run`、`SERIALWRAP_WAL_DIR→wal`、`SERIALWRAP_CONFIG_DIR→config`、`SERIALWRAP_LOG_DIR→blog`、`SERIALWRAP_EVENTS_DIR→events.d`、`SERIALWRAP_EVENTS_RUNTIME_DIR→events-rt`、`SERIALWRAP_BY_ID_DIR→by-id`（空目錄，防 two-reader）、`SERIALWRAP_BY_PATH_DIR→by-path`。
  - pop 5 個高優先變數：`SERIALWRAP_ENDPOINT`、`SERIALWRAP_PROFILE_DIR`、`SERIALWRAP_TTYMCU_PATH`、`SERIALWRAP_EVENTS_LOG_PATH`、`SERIALWRAP_DATA_DIR`。
  - tripwire：此時若 `"sw_core.constants" in sys.modules`，就 `raise RuntimeError`（刻意不用 assert，避免 `-O` 把檢查消掉）。
- **第 2 層：autouse fixture `_isolate_state_path`**：`monkeypatch.setattr(sw_core.session_manager, "STATE_PATH", str(tmp_path/"state.json"))`（`conftest.py:75-80`）。
- **第 3 層：`pytest_sessionfinish`**（`conftest.py:84-118`）
  - 判定項目：state 用 `classify_state(mode)`、wal 用 `classify_wal(mode)`、config 用 `classify_config`（不吃 mode）、daemon 用 `classify_daemon(mode)`，另視情況判 `shell-wal`。
  - 任一 FAIL 時印 `[live-guard:<name>] FAIL: ...`。只有在 `session.exitstatus == 0` 時才把它升為 1，不覆蓋 2／3 等更高語意的 exit status。
  - 結束時 `shutil.rmtree(_ISO_ROOT)`。

### 1.3 `tests/liveguard.py`

- 頂層刻意不 import `sw_core`；需要 RPC 時才在函式內 lazy import `sw_core.client.rpc_call`。
- 常數：
  - `POLLUTION_MARKERS = ("/tmp/sw-", "test-tpl", '"test:', "fake-uart")`
  - `STRUCTURAL_KEYS = ("aliases","bindings","released")`
  - `VERDICT_FAIL="FAIL"`、`VERDICT_WARN="WARN"`、`VERDICT_PASS="PASS"`、`VERDICT_SKIP="SKIP"`
- dataclass：
  - `FileSnap(exists: bool, content: bytes=b"", size: int=0)`（frozen）
  - `DaemonSnap(reachable: bool, active: bool=False, main_pid: int|None=None, sessions: dict[str, tuple[last_tx_at, bridge_generation, state]])`（frozen）
- live 路徑公式刻意忽略 `SERIALWRAP_*`：
  - `live_state_path() = ${XDG_STATE_HOME:-~/.local/state}/serialwrap/state.json`
  - `live_wal_path() = .../serialwrap/wal/raw.wal.ndjson`
  - `live_config_path() = ${XDG_CONFIG_HOME:-~/.config}/serialwrap/config.yaml`
- `snap_daemon()`（`:70-114`）：
  1. 依序試 `systemctl show -p ActiveState,MainPID serialwrap`（system scope）與加 `--user` 的版本（user scope），timeout 5s；
  2. 再對 live socket 候選逐一 `rpc_call(ep,"session.list",{},timeout_s=3.0)`，回應 `ok` 為真才算 reachable。
  - 候選清單 `_live_socket_candidates()`：先放 `config.yaml socket_path`（缺項時用 `/run/serialwrap/serialwrapd.sock`），再加 `$XDG_RUNTIME_DIR/serialwrap/serialwrapd.sock`。
  - **Rust 影響**：live guard 依賴 RPC `session.list` 的 `sessions[].session_id/last_tx_at/bridge_generation/state` 欄位，以及 systemd unit 名 `serialwrap`。
- 判定規則（皆為純函式，由 `tests/test_liveguard.py` 的 39 個 case 釘住）：
  - `classify_state`：前後都不存在→PASS；前無後有→FAIL；前有後無→FAIL；內容 byte 相同→PASS；`_structural_damage` 判為結構性破壞→FAIL；`warn` 模式下其餘變更→WARN；`strict` 模式下其餘變更→FAIL。
  - `_structural_damage` 的結構性破壞包括：檔案被刪、內容非 JSON、STRUCTURAL_KEYS 的值不是 dict、既有 key 消失、`bindings` 既有 entry 的值被改寫，或 post 版本新出現 POLLUTION_MARKERS。
  - `classify_wal`：前有後無→FAIL（不受 warn 影響）；檔案縮小時 warn→WARN、strict→FAIL；其餘→PASS（前無後有視為正常）。
  - `classify_shell_wal`：exists、size、content 任一不同→FAIL。
  - `classify_config`：exists 或 content 不同→FAIL。
  - `classify_daemon`：pre 不可達→SKIP；post 不可達→FAIL；pre 有 `main_pid` 時，post 不再 active 或 PID 改變→FAIL；只迭代 pre 的 session，`last_tx_at`、`bridge_generation`、`state` 任一改變→strict 下 FAIL、warn 下 WARN。

### 1.4 `tests/state_iso.py`（供 unittest 跑法使用）

- `isolated_state()` contextmanager：
  - 建立 `tempfile.mkdtemp(prefix="sw-state-iso-")`；
  - patch `sm.STATE_PATH`、`wal_mod.WAL_DIR`、`svc.EVENTS_DIR`、`svc.EVENTS_RUNTIME_DIR`、`svc.EVENTS_LOG_PATH`；
  - 結束時還原並刪除暫存目錄。
- `isolate_testcase(tc) -> str`：在 setUp 內呼叫，靠 `addCleanup` 還原。
- 實際有 **13 個**檔案使用它：`test_boot_quiet`、`test_command_guard`、`test_service_human_console`、`test_session_activity`、`test_file_transfer_chunk_config`、`test_issue24_heartbeat`、`test_mcu_cli_rpc`、`test_ready_reconfirm_bounds`、`test_session_capture`、`test_daemon_service_selector`、`test_issue27_newline_reject`、`test_flash_service_wiring`、`test_state_path_injection`。conftest docstring 與 CHANGELOG 寫的是「8 檔」，已過時。

### 1.5 分類：黑箱（可指向部署 artifact）與白箱

圖例：
- **BB-proc**：以 subprocess 啟動真 CLI＋真 daemon，只看 JSON 與外部效果。
- **BB-script**：受測物是 shell 資產或 CLI subprocess，不經 daemon。
- **WB-cli**：in-process 呼叫 `sw_core.cli.main()` 或 `build_parser()`，常搭配 stub RPC server 或 mock。可移植成「Rust CLI subprocess 對 stub daemon」的契約黑箱。
- **WB**：直接 import `sw_core` 內部類別（`SessionManager`、`UARTBridge`、`CommandArbiter`、`SerialwrapService` 等），只能當行為規格參考，必須由 Rust 單元測試重寫。
- **INFRA**：測的是測試基礎設施（realhw／regression／reliability／liveguard／架構文件），與產品語言無關。

每列格式為「檔名（collect 數）」。

#### A. BB-proc：真 daemon 加真 CLI（目前只有 2 檔，9 個 item）

| 檔案 | item | 啟動方式 | 依賴 |
|---|---|---|---|
| `test_multiagent_e2e.py` | 1：`TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict` | 檔內自帶 `FakeTarget`（PTY；boot banner `boot done\r\nroot@prplOS:/# `；每 50ms 送雜訊 `KDBG:tick:N`；每個命令回 `EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# `）；daemon `--profile-dir/--socket/--lock`；env 設 `SERIALWRAP_STATE_DIR/RUN_DIR/BY_ID_DIR/BY_PATH_DIR/WAL_DIR/CONFIG_DIR/EVENTS_DIR` | POSIX PTY（`pty`、`termios`） |
| `test_human_agent_coexist.py` | 8：t1 `wal_reset_preserves_console`、t2 `wal_reset_seq_resets_to_zero`、t3 `wal_current_seq_matches_live`、t4 `bind_idempotent_ready_session`、t5 `bind_idempotent_preserves_console`、t6 `human_sees_agent_commands`、t7 `agent_runs_while_human_types`、t8 `full_run_simulation` | 同上；human console 實際上是在 tmux 內執行 `cat <vtty>`（不是 minicom）；tmux session 名 `sw_test_<pid>` | 需要 `tmux`（`tmux -V`）與 POSIX PTY |

兩檔都用 `--socket <tmp>/run/serialwrapd.sock`；CLI 呼叫統一是 `[SERIALWRAP, "--socket", sock, *args]`，並解析 stdout JSON。這兩檔是 spec §11.3「CLI subprocess 行為」的現成黑箱基礎，但必須先加上 exe／daemon 覆寫鉤子（例如 `SERIALWRAP_EXE`、`SERIALWRAPD_EXE` env）。

#### B. BB-script

- `test_minicom_router.py`（10）
  - 受測物：`sw_core/assets/tools/minicom_router.sh`。
  - fake serialwrap 回應的 JSON 形狀是凍結契約：`session list → {"ok":true,"sessions":[{"com","alias","session_id","state"}]}`、`console-attach → {"ok":true,"client_id","vtty"}`、`console-detach → {"ok":true}`。
  - router 也會呼叫 `serialwrap supervision-mode`（`minicom_router.sh:252`）。
  - router 的 env 契約：`SERIALWRAP_BIN`、`SERIALWRAP_SOCKET`、`SERIALWRAP_AUTO_START_DAEMON`（預設 1）、`SERIALWRAP_ATTACH_WHEN_NOT_READY`（1）、`SERIALWRAP_ATTACH_WAIT_TICKS`（60）、`MINICOM_WAIT_READY`（0）、`MINICOM_WAIT_READY_TICKS`（120）、`SERIALWRAP_PREFERRED_COM`（COM0）、`MINICOM_BIN`（`/usr/bin/minicom`）、`BLOG_DIR`、`MINICOM_AUTO_CAPTURE`、`MINICOM_CAPTURE_MODE`、`MINICOM_CAPTURE_WRAPPER`、`MINICOM_DEFAULT_COLOR`。
- `test_bench_tools_cli.py`（20）：受測物是 `tools/bench-issue.sh`、`tools/bench-enroll.sh`（clean env、`HOME=tmp`），與產品 CLI 無關。
- `test_bootloader_recovery.py::TestCliParser...test_cli_parser_has_allow_attached_flag`（1）：CLI `--help` 必須含 `--allow-attached`。

#### C. WB-cli：in-process CLI，可移植為「Rust CLI 對 stub daemon」

`test_attach_error_surface`(11)、`test_autospawn_gate`(5)、`test_bench_connect_cli`(33)、`test_cli_daemon_start`(22)、`test_cli_diagnostics`(26；含 `_TcpReplySocket`)、`test_cli_profile_test`(6)、`test_cli_stderr_full_error`(4)、`test_cli_timeout_robustness`(33；`_UnixRpcServerMixin`，需要 AF_UNIX)、`test_cli_version`(5)、`test_cli_version_mismatch_warning`(6)、`test_codex_fixes`(8)、`test_event_cli`(7)、`test_remote_cli`(12)、`test_remote_endpoint`(24；`TestRpcCallOverRealTcpSocket`)、`test_remote_tunnel`(45；`_FakeProc`、`_Clock`)、`test_resolve_endpoint_fallback`(12)、`test_resolve_endpoint_sentinel`(6)、`test_setup_cli`(36)、`test_skill_cmd`(5)、`test_win_cli_endpoint`(20)、`test_win_daemon_start`(28)、`test_doctor`(41)、`test_doctor_endpoint_reachable`(6)、`test_runtime_config`(5)、`test_runtime_paths`(2)、`test_runtime_paths_xdg`(4)、`test_constants_endpoint`(2)、`test_setup_materialize`(7)、`test_setup_reconcile`(10)、`test_legacy_detect`(2)、`test_wsl_systemd`(6)、`test_systemd_units`(4)、`test_service_ctl`(4)、`test_sysenv`(18)、`test_packaging_entrypoints`(6)、`test_platform_backends`(5)。

上述 CLI 相關檔案大多以 `mock.patch("sw_core.cli.subprocess.Popen")` 這類 Python 內部 patch 注入，無法原樣對 Rust 執行。可移植的是其中的 I/O 契約：stdout JSON、stderr、exit code、spawn argv。

#### D. WB：daemon 內部（L1 行為規格來源），依領域分組

- **RPC／IPC／daemon 骨架**：`test_rpc_tcp`(8)、`test_rpc_blocking_offload`(4，POSIX skipif)、`test_issue52_rpc_concurrency`(2)、`test_daemon_lock`(1)、`test_lock_win`(3，只在 Windows 跑)、`test_daemon_service_selector`(15)、`test_daemon_version`(6)、`test_multi_open_detect`(13)、`test_holder_probe_portability`(8)、`test_thread_survival`(3)
- **arbiter／命令契約**：`test_arbiter_flush_on_recovery`(9)、`test_issue27_newline_reject`(6)、`test_command_guard`(4)、`test_command_completion`(25)、`test_bounded_memory`(12)、`test_stress_submit`(4)、`test_multiagent_stress`(5)、`test_resilience_timeout`(6)、`test_issue24_heartbeat`(5)、`test_bad_command_recovery`(7)、`test_concurrency_races`(49)
- **background／capture**：`test_issue28_bg_lifecycle`(11)、`test_issue159_bg_fast_capture`(3)、`test_session_capture`(41)、`test_log_tail_latest`(13)
- **session 狀態機、綁定、持久化**：`test_session_bind`(37)、`test_com_rank`(11)、`test_profile_pin_sticky`(22)、`test_profile_reresolve_on_attach`(25)、`test_explicit_target_device_ownership`(2)、`test_state_path_injection`(9)、`test_state_persistence_atomic`(6)、`test_state_save_race`(5)、`test_alias_registry`(1)、`test_session_activity`(24)、`test_session_console_endpoint`(3)、`test_resilience_hotplug`(9)、`test_device_handoff`(31)、`test_windows_claim`(4)、`test_device_source`(6)、`test_refine_ownership`(21)、`test_cowork_session_usability`(22)
- **login／ready／boot**：`test_login_fsm`(24)、`test_login_error_detail`(7)、`test_login_recovery_lease`(5)、`test_auth`(16)、`test_credentials_unresolved`(5)、`test_brcm_prompt_regex`(11)、`test_config_profiles`(13)、`test_boot_quiet`(61)、`test_bootloader_recovery`(59＋1 BB)、`test_readiness_reprobe`(11)、`test_ready_reconfirm_bounds`(15)、`test_rx_flood`(18)、`test_transport_stall`(21)、`test_console_unlock`(43)
- **UART／console**：`test_uart_io`(38)、`test_serial_port`(57；部分只在 POSIX＋PTY 或裝了 pyserial 時跑)、`test_interactive_raw`(14，PTY)、`test_agent_defer_tx`(7，PTY；會 subprocess 開 vtty 驗外部 peer)、`test_suspend_resume_reentrant`(5)、`test_service_human_console`(6)、`test_telnet_filter`(34)、`test_issue26_interactive_guard`(1)
- **WAL**：`test_wal`(12)、`test_wal_integrity`(5)、`test_wal_missing_detection`(21，root 下 skip)
- **file transfer**：`test_file_transfer`(34)、`test_file_transfer_chunk_config`(38)、`test_transfer_portability`(29；會真的執行 `openssl`、`/bin/sh`)、`test_refine_transfer_integration`(5)
- **MCU／flash**：`test_flash_endpoint`(6，POSIX)、`test_flash_guard`(4)、`test_flash_probe`(8)、`test_flash_pump`(6)、`test_flash_service_wiring`(6)、`test_flashing_state`(17)、`test_uart_flash_bridge`(4)、`test_refine_flash_precedence`(1)、`test_mcu_patterns`(7)、`test_mcu_cli_rpc`(2)
- **event engine**：`test_event_counter`(5)、`test_event_dispatcher`(5)、`test_event_engine`(5)、`test_event_line_buffer`(7)、`test_event_log`(3)、`test_event_matcher`(17)、`test_event_registry`(5)、`test_event_rpc`(4)、`test_event_schema`(9)
- **bench registry**：`test_bench_registry`(13)

#### E. INFRA

- `test_conftest_isolation`(3)：驗證 9 個 iso env、pop 5 個、autouse patch。
- `test_liveguard`(39)、`test_architecture_docs`(10；用 `tools/architecture_review.py` 驗 `docs/architecture/{facts,architecture}.json`)。
- realhw 系列：`test_realhw_cases`(4)、`test_realhw_drivers`(16)、`test_realhw_harness`(17)、`test_realhw_longrun`(6)、`test_realhw_main`(1)、`test_realhw_preflight`(18)。
- regression 系列：`test_regression_brcm_reboot`(4；含 `test_wal_markers_pages_through_truncated_range`、`test_wal_markers_rejects_missing_wal`)、`test_regression_guards`(24)、`test_regression_harness`(11)、`test_regression_pluginfiles`(5)、`test_regression_preflight`(7；版本 gate 3 個 case、stale client note、`daemon_version_probe` 的快路徑／fallback／空白值)、`test_f7_failure_verdict`(3)。
- reliability 系列：`test_reliability_core`(35)、`test_reliability_pluginfiles`(4)、`test_reliability_reporter`(2)、`test_reliability_testbed`(9)。

#### F. 平台 skip（目前唯一的「平台分類」形式）

- `skipUnless(hasattr(socket,"AF_UNIX"))`：`test_cli_diagnostics.py:359`、`test_cli_timeout_robustness.py:163`、`test_remote_endpoint.py:70,82`
- `skipif(not win)`：`test_lock_win.py:18,30`、`test_win_daemon_start.py:311`、`test_win_cli_endpoint.py:126`
- `skipif(win)`：`test_win_cli_endpoint.py:121`
- `skipUnless(posix and openpty)`：`test_serial_port.py:158,226,280,528`
- `skipUnless(pyserial)`：`test_serial_port.py:572`
- module 層 `pytestmark` skipif：`test_flash_endpoint.py:14`
- 其他：`test_daemon_service_selector.py:63,98`、`test_rpc_blocking_offload.py:46`；`skipIf(geteuid==0)`：`test_wal_missing_detection.py:97`；`skipUnless(which("script"))`：`test_minicom_router.py:86,117`

---

## 2. `func-test/`（YAML 驅動的整合測試）

### 2.1 檔案

`runner.py`(504)、`README.md`(160)、`lib/__init__.py`(1)、`lib/cli_client.py`(54)、`lib/console_client.py`(125)、`lib/daemon_harness.py`(199)、`lib/expect_engine.py`(85)、`lib/fake_target.py`(248)、`lib/yaml_loader.py`(70)，以及 `cases/*.yaml` 13 個。

### 2.2 `runner.py` CLI

`argparse`：`--category/-c`、`--case/-t`（比對 yaml stem）、`--repeat/-r int=0`（0 表示沿用 `meta.repeat`）、`--verbose/-v`、`--list/-l`。

- 找不到符合條件的 case 時印「找不到符合條件的測試案例。」並 exit 1。
- 任一 case FAIL 就 exit 1；全部通過時 exit 0（印「🎉 全部通過！」）。
- 每個 case 的流程：建立 `TestRunner(tc).run()` → `DaemonHarness.start()` → 等 socket 檔出現（5s，`_wait_for_socket`）→ 依序執行 steps → finally 關閉 console fd 並 `harness.stop()`。
- 結果型別 `(bool, str)`；`StepError` 轉成 FAIL 訊息；其他例外訊息為 `未預期例外: <Type>: <msg>`。

### 2.3 action 對照（`runner.py:109-127`，共 17 種）

| action | 參數（預設） | 實作 |
|---|---|---|
| `wait_ready` | `timeout_s=15.0`、`selector="COM0"` | 每 0.3s 執行 `session list`，直到 `sessions[].com==selector` 且 `state=="READY"` |
| `cli` | `argv`（支援 `{var.field}` 展開）、`timeout=10.0`、`save_as`、`expect` | `cli_run(argv)` 後跑 `check_expect` |
| `wait_command_done` | `cmd_id`、`timeout_s=10.0` | 每 0.2s 執行 `cmd status --cmd-id`，直到 `command.status ∈ {done,error,canceled}` |
| `parallel` | `steps` | 每個子步驟一條 thread，`join(timeout=30)` |
| `attach_console` | `selector="COM0"`、`label="func-test-console"`、`save_as` | 執行 `session console-attach --selector --label`，以 `os.open(vtty, O_RDWR|O_NONBLOCK)` 開 vtty；存入變數 `{client_id, vtty}` |
| `console_write` | `console_id`、`input`、`delay_ms=0` | 寫入 utf-8；`delay_ms>0` 時逐字元寫入。**沒有實作 `input_raw`** |
| `console_read` | `console_id`、`timeout_s=5.0`、`expect`、`save_as` | 以 select 輪詢 0.1s，有資料後遇到短暫靜默即停；`expect` 包成 `{"output": expect}` |
| `detach_console` | `console_id`、`selector` | 先 close fd，再執行 `session console-detach --selector --client-id` |
| `assert_state` | `selector`、`expect` | 執行 `session list`，取 `com==selector` 的 session 做 `check_expect` |
| `assert_wal` | `selector`、`expect` | 執行 `log tail-raw --selector X --from-seq 0 --limit 10000`；**只支援** `min_tx_count`（整數或 `{gte}`）與 `has_source`（list） |
| `sleep` | `seconds=1.0` | — |
| `inject_device_event` | `event ∈ {remove, add}` | 刪除或重建 `<root>/by-id/fake-uart0` symlink |
| `target_stop_responding` | `duration_s=5.0` | 呼叫 `FakeTarget.pause_responding` |
| `repeat` | `count=1`、`steps` | — |
| `shell` | `cmd`、`timeout=10.0` | `subprocess.run(shell=True)`，rc≠0 視為 FAIL（README 沒有列出） |
| `target_emit` | `payload` | `os.write(master_fd, payload.utf8)`（README 沒有列出） |
| `wait_for_file` | `path`、`timeout_s=5.0` | 輪詢 `os.path.exists`（README 沒有列出） |

變數展開：正則 `\{([a-zA-Z0-9_.]+)\}` 以 dot path 查 `self.variables`；查不到就保留原字串。

### 2.4 `expect_engine.check_expect(data, expect) -> list[ExpectError]`

- 特殊 key：`has_keys`（list，每個 dot path 解析結果不得為 None）、`not`（dict，解析值不得等於指定值）。
- 一般 key 是 dot path。值為 dict 時依序檢查 `contains`（字串包含）、`matches`（`re.search`）、`gte`、`lte`；其他 dict 做精確比對。純量做精確比對。
- `ExpectError(path, expected, actual)`，訊息格式 `[path] 期望 X，實際 Y`。

### 2.5 `yaml_loader`

- `TestCase(name, category, severity, description, tags=[], repeat=1, target={}, profile={}, env_files={}, steps=[], source_path="")`
- 預設值：`name=meta.name or stem`、`category="unknown"`、`severity="medium"`。
- `discover_test_cases`：讀 `sorted(cases_dir.glob("*.yaml"))`；載入失敗時印警告後略過。

### 2.6 `fake_target.py`

- `CommandOverride(response="", delay_ms=0, interactive=False)`
- `LoginStep(expect, respond)`
- `TargetConfig` 預設值：
  - `platform="prpl"`
  - `boot_banner="boot done\r\nroot@prplOS:/# "`
  - `noise_enabled=True`、`noise_interval_ms=50`、`noise_pattern="KDBG:tick:{tick}\r\n"`
  - `default_response="EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# "`
  - `overrides={}`、`login_steps=[]`
- `TargetConfig.from_yaml` 讀取的 key：`platform`、`boot_banner`、`noise.{enabled,interval_ms,pattern}`、`commands.default`、`commands.overrides.<cmd>.{response,delay_ms,interactive}`、`login_steps[].{expect,respond}`。
- `FakeTarget`：
  - 以 `pty.openpty()` 建立 PTY；slave 設為 raw（iflag=oflag=lflag=0，cflag=`CREAD|CLOCAL|CS8`，VMIN=1，VTIME=0）。
  - `start()` 先寫 boot_banner。
  - cmd 執行緒：以 select 等 0.2s 讀 4096 bytes，以 `\n` 切行並去掉 `\r`。登入階段 `expect in cmd` 時回 respond；未命中時重送提示的最後一行。
  - 空行回 prompt。prompt 取值順序：動態 `_current_prompt` → login_steps 最後一個 respond 的最後一行 → boot_banner 的最後一行。
  - override 的 `interactive` 為真時不回應。
  - `pause_responding(duration_s)` 以 `Event` 暫停；noise 執行緒同步暫停。
- spec §15.5 要求把 DUT 行為模型與 PTY I/O 分離，**目前沒有分離**：PTY 寫死在 `__init__` 裡。

### 2.7 `daemon_harness.py`

- `HarnessConfig(target_config=TargetConfig(), profile_overrides={}, com="COM0", alias="func-test", env_files={})`
- `DaemonHarness` 的屬性：`socket_path`、`env`（copy）、`fake_target`、`root`。
- `start()`：
  1. 建立 `TemporaryDirectory(prefix="sw-func-")`，含 `by-id/`、`profiles/`；
  2. 建 symlink `by-id/fake-uart0 → slave_path`；
  3. 寫 `profiles/func-test.yaml`，env_files 也寫進 `profiles/`；
  4. 從 `os.environ.copy()` 出發，設定 `SERIALWRAP_STATE_DIR=<root>/state`、`SERIALWRAP_RUN_DIR=<root>/run`、`SERIALWRAP_BY_ID_DIR`、`SERIALWRAP_BY_PATH_DIR=<root>/by-path`；
  5. socket 為 `<root>/run/serialwrapd.sock`，lock 為 `<root>/run/serialwrapd.lock`；
  6. 以 `Popen([$PYTHON|python3, serialwrapd.py, --profile-dir, --socket, --lock], stdout/stderr=DEVNULL)` 啟動 daemon。
- **隔離缺口**：沒有設定 `SERIALWRAP_WAL_DIR`、`SERIALWRAP_CONFIG_DIR`、`SERIALWRAP_EVENTS_DIR`、`SERIALWRAP_LOG_DIR`，也沒有 pop `SERIALWRAP_ENDPOINT` 或 `SERIALWRAP_PROFILE_DIR`。
  - WAL 會因 `STATE_DIR` 推導而隔離，前提是外層沒有 export `SERIALWRAP_WAL_DIR`。
  - `EVENTS_DIR` 預設 `~/.serialwrap/events.d`（`sw_core/constants.py:66-69`），所以 `ev-01` 的 `event add` 會把 rule 寫進 **live** events.d。
- `stop()`：先 `serialwrap --socket S daemon stop`（timeout 3s），再 terminate、等 3s、必要時 kill，最後停掉 FakeTarget 並刪除 tmpdir。
- `_build_profile_yaml` 產生的模板：

```yaml
profiles:
  func-test-template:
    platform: {target.platform}
    prompt_regex: '{p.prompt_regex|(?m)^root@prplOS:.*# }'
    ready_probe: '{p.ready_probe|echo __READY__${nonce}}'
    timeout_s: {p.timeout_s|10}
    [login_regex / password_regex(預設 (?i)password) / user_env(USER) / pass_env(PASS) / env_file]  # 只在有 login_regex 時輸出
    [post_login_cmd]
    uart: {baud: 115200, data_bits: 8, parity: N, stop_bits: 1, flow_control: rtscts, xonxoff: false}
targets:
  - act_no: 1
    com: {com}
    alias: {alias}
    profile: func-test-template
    device_by_id: {<root>/by-id/fake-uart0}
```

### 2.8 `cli_client.cli_run(argv, *, socket_path, env, timeout=10.0) -> dict`

- 執行 `[SERIALWRAP, "--socket", socket_path, *argv]`。
- 測試端合成的錯誤碼（不是產品錯誤碼）：`CLI_TIMEOUT`、`EMPTY_STDOUT`、`BAD_JSON:<exc>`。
- 一律以 `setdefault("_rc", returncode)` 附上 return code。

### 2.9 `console_client`

- `ConsoleHandle(client_id, vtty, label, fd=None)`，方法 `open_fd()`、`close_fd()`。
- 函式：`attach_console`、`detach_console`、`console_write`、`console_read(max_bytes=65536)`。
- POSIX 限定：直接 `os.open` vtty 路徑。Windows 的 TCP console 沒有對應實作。

### 2.10 13 個 case（`runner.py -l` 實際列出 13 個）

| case | category / severity | 主要 steps 與 oracle | 依賴的 CLI 與欄位 | 現況問題（code 實查） |
|---|---|---|---|---|
| `brcm-01-login-flow` | state-machine / high | bcm 平台兩段登入：`login_steps admin→"Password: "`、`admin→"CM> "`、override `sh→"/ # "`；profile `login_regex (?mi)login:\s*$`、`password_regex`、`post_login_cmd sh`、`user_env BRCM_USER`、`pass_env BRCM_PASS`、`env_file brcm.env`；wait_ready 30s → assert READY → `cmd submit --source agent:brcm-test` → `cmd status` 須 `command.status=="done"` | `session list`、`cmd submit/status` | — |
| `co-01-line-buffering` | console-io / high | attach → 逐字元寫 `echo line-test\n`（20ms）→ read contains `RESULT:echo line-test:OK` → detach → READY | `console-attach/detach` | — |
| `cowork-01-agent-bad-cmd` | cowork / critical | 預期 `bad-hang` 會 PROMPT_TIMEOUT，之後恢復 | — | **runner 不認得 `submit_command`、`wait_command` action**，第 2 步必定拋「未知 action」而 FAIL。`commands.<cmd>` 直接放在 `commands` 下（應為 `commands.overrides`），因此被忽略。`profile.login` 也被忽略。platform 為 `shell`、boot_banner `"login: "`，但沒有 `login_regex` |
| `ev-01-spawn-handler` | event-engine / high | shell 寫 `/tmp/sw-ev-test-rule.json`（`schema_version:1, owner:ev, name:panic-marker, kind:tool, selectors:[COM0], pattern:{kind:contains,value:"Kernel panic"}, handler:{shell:"touch /tmp/sw-ev-test-fired"}, auto_enable_com_on_load:true, timeout_ms:3000`）→ `event add --file` → `event status --selector COM0` → `target_emit "Kernel panic - not syncing\n"` → `wait_for_file` 8s → `event tail --rule-id ev.panic-marker -n 10` 須 `has_keys entries` | `event add/status/tail` | 使用固定 `/tmp` 路徑；EVENTS_DIR 沒隔離，會寫入 live `~/.serialwrap/events.d` |
| `ha-01-human-then-agent` | human-agent / high | attach → read 2s → detach → `cmd submit --source agent:1` 須 `has_keys cmd_id` → wait_done → READY | — | — |
| `ha-05-concurrent-submit` | human-agent / critical | noise 50ms；3 個 parallel `cmd submit`（`ls/pwd/whoami`，source 為 `agent:1..3`）→ sleep 5 → READY → `assert_wal min_tx_count gte 3, has_source [agent:1,agent:2,agent:3]` | `log tail-raw` 的 `records[].dir/source` | — |
| `hk-01-special-keys-raw` | human-key / critical | attach → `assert_state has_interactive: true` → `input_raw "\x1b[A"`、`"\x1b[B"`、`"\x09"` → `assert_wal any_tx_contains_raw` | — | **session list 沒有 `has_interactive` 欄位**（`sw_core` 全域 grep 無此字串），assert 必然 FAIL。`input_raw` 沒有實作，實際寫入空字串。`any_tx_contains_raw` 不受支援；YAML 重複 key 也只會保留最後一個 |
| `hk-02-agent-preempt-human` | human-key / critical | attach → 寫 `echo human-first\n` → read contains → agent `cmd submit` → wait → `cmd get` ok → READY → 寫 `echo human-after\n` → read contains | `cmd get` | — |
| `hk-03-deferred-input-flush` | human-key / high | attach → agent submit → `input_raw "echo deferred\r"` → wait_done → READY | — | `input_raw` 沒實作（寫入空字串），case 實際只驗 READY |
| `hk-04-minicom-exit-cleanup` | human-key / high | attach → `has_interactive: true` → detach → `has_interactive: false` → submit → `cmd get` | — | 同樣依賴不存在的 `has_interactive`（None≠True／False），必然 FAIL |
| `rc-01-interactive-mode-switch` | race-condition / critical, `repeat: 5` | repeat 3 次 {attach, sleep 0.2, detach, sleep 0.1} → READY → submit → wait | — | — |
| `sm-01-attach-to-ready` | state-machine / critical | noise 100ms；READY → submit `echo hello` → wait → status ok → READY | — | — |
| `sm-04-device-unplug` | state-machine / high | READY → 移除 symlink → sleep 3 → `session list`（不斷言）→ `daemon status ok` | — | oracle 只確認 daemon 還活著，**沒有驗證轉成 DETACHED**，與 description 不符 |

`func-test/` **沒有任何 CI 或 pytest 接線**（`.github/workflows` 沒有呼叫它）。spec 要求的「外部 bridge 模式」也不存在。

---

## 3. `regression/`：TestPilot plugin `serialwrap_regression`（L2）

### 3.1 打包與 TestPilot 契約

- `regression/pyproject.toml`：
  - `name="serialwrap-regression"`、`version="0.1.0"`、`requires-python>=3.10`
  - 依賴 `testpilot-core>=0.3.4,<1.0`
  - entry point `[project.entry-points."testpilot.plugins"] serialwrap_regression = "serialwrap_regression.plugin:Plugin"`
  - build backend 為 hatchling；只支援 editable 安裝：`preflight._repo_root()` 要求 `parents[2]/realhw/preflight.py` 存在（`preflight.py:16-23`）。
- `agent-config.yaml`：`version:1`；`execution{scope:per_case, mode:sequential, max_concurrency:1, failure_policy:retry_then_fail_and_continue, retry.max_attempts:1, timeout{base_seconds:120, per_step_seconds:120, max_seconds:900}}`；`hooks.enabled_hooks:[pre_case,post_case,on_failure]`、`fail_open:true`；`remediation{enabled:true, allowed_actions:[]}`。
- `plugin.Plugin(PluginBase)`：
  - 基本屬性：`api_version="1.1"`、`name="serialwrap_regression"`、`version="0.1.0"`、`execution_policy → {"mode":"sequential","max_concurrency":1}`。
  - `discover_cases` → `core.build_case_dicts(load_registry(), cfg)`。
  - `prepare_run(case_ids)`：先跑 preflight，`not ok` 時 `raise PreflightRefused("preflight 拒跑：" + "；".join(problems))`；點名不存在的 id 時 `ValueError("未知 case id：...")`。
    - `run_meta={version (=pinned --version raw), git (rev-parse --short HEAD), tiers:"regression", started_at "%y%m%d-%H%M%S", preflight_notes, allow_destructive, boards[]}`
    - artifacts 為 `{"regression_meta", "missing_caps"}`。
  - `setup_env`：建立 `report_dir=~/b-log/regression-reports/tp-<ts>`，`ctx=core.build_ctx(cfg, report_dir)`。
  - `execute_step`：一律回 `success=True`，結果放 `captured.regression=result_to_dict`。Windows（`os.name=="nt"`）上除 `f9-brcm-spaced-prompt-reboot-login` 外一律 SKIP，reason_code `windows_case_unsupported`（`plugin.py:176-177`）。
  - `evaluate`：PASS → True；其餘結果把 `_last_failure` 寫成 payload 並回 False。
  - `teardown`：先 `recover_boards`；若有板子沒恢復，設定 `_broken_by=case_id`；再 `sweep_tmux(prefix)`。
  - `verify_install`：檢查 agent-config、testbed example；POSIX 上檢查 tmux、minicom；檢查 `Path(serialwrap_exe).is_file()`、boards 非空、registry 數量 `>=10`。
  - `capture_dut_firmware_version → {"git": deployed_version}`；`report_formats ["md","json"]`。

### 3.2 testbed

`testbed.yaml.example`（同目錄有 `testbed.yaml` 時以它優先，見 `core.load_testbed`）：

```yaml
serialwrap_exe: ~/.local/bin/serialwrap   # pinned（#154）
allow_destructive: false
tmux_prefix: swreg
boards:
  - {com: COM0, alias: dut-prpl, serial: AC01QZT0, platform: prpl}
  - {com: COM1, alias: com1-brcm, serial: AQ00OAQ7, platform: bcm}
timeouts: {ready_wait_s: 180, boot_wait_s: 240, cmd_timeout_s: 12}
```

`_TESTBED_DEFAULTS`（`core.py:15-20`）與上述相同；timeouts 逐 key 合併，並注入 `_testbed_source`。

### 3.3 preflight（`regression/serialwrap_regression/preflight.py`）

- **POSIX `run_preflight(cfg)`**：
  - `sw=SwCli(exe=serialwrap_exe)`；bench lock 用 `rp.acquire_benchlock(~/.local/state/serialwrap/bench.lock)`（`fcntl.flock LOCK_EX|LOCK_NB`）。
  - 收集 `rp.Checks(git_behind, doctor_ok, boards_ready, boards_expected, tools_missing=[tmux,minicom], leaked_daemons, other_pytest, state_polluted, benchlock_ok, external_testpilot)`，再交給 `rp.evaluate`。
  - 版本 gate：`cli_version=str(sw.run("--version")["_raw"]).strip()`，與 `daemon_version_probe(sw)` 比對。
  - stale note：PATH 上的 `serialwrap` 若 realpath 不同於 pinned exe 且版本不同，加入 notes（不擋）。
  - 回傳 `{ok, problems, notes, missing_caps:{}, deployed_version, benchlock_fd}`。
- **Windows `_run_preflight_windows`**（`preflight.py:100-158`；lock 路徑 `:114`）：
  - lock 檔 `Path.home()/.local/state/serialwrap/bench.lock`，以 `O_RDWR|O_CREAT|O_BINARY` 開啟並 `msvcrt.locking(LK_NBLCK,1)`。
  - 以下任一不滿足就記一條 problem：`daemon status` 的 `ok` 與 `running` 為真、`multi_open` 為假、`wal.healthy` 為真；`doctor` 的 `ok` 為真、`checks` 非空、且每項 `ok` 或 `advisory` 為真；所有板子都 READY；版本 gate 通過。
  - 一律回 `missing_caps={"posix":"windows_posix_tools_unavailable"}`。
- `version_gate(cli, daemon) -> str|None`：解析失敗時回 `"版本解析失敗：cli=... daemon=...（#154 gate）"`；不相等時回 `"client↔daemon 版本不齊：cli=a.b.c daemon=x.y.z（#154 gate，suite-refuse）"`。
- `daemon_version_probe(sw)`：先讀 `daemon status.version`（非空字串就採用）；否則讀 `/proc/<pid>/cmdline` 的 argv0 當 python，執行 `importlib.metadata.version('serialwrap')`（timeout 15s）。這條 fallback 對 Rust daemon 無效。
- `stale_client_note(path_v, pinned_v)`。

### 3.4 guards（`guards.py`）

- `UBOOT_RO_WHITELIST = ("printenv","bdinfo","version","help","echo")`
- `LEAVE_ALLOWED = ("boot","reset")`
- `_FORBIDDEN = r"\b(saveenv|env\s+save|env\s+default|setenv|sf\s+write|nand\s+write|mmc\s+write|tftpboot)\b"`
- `_UBOOT_PROMPT = r"(=>|U-Boot>)\s*$"`（MULTILINE）
- `validate_uboot_cmd(cmd)`：拒絕空字串、含 `;`、`\n`、`&&`、`||`、`|` 的命令、命中禁令者、首 token 不在白名單者，皆 `raise UBootGuardError`。
- `UBootConsole(ctx, com, tmux_session)`：
  - `at_prompt()`
  - `interrupt_autoboot(window_s=15.0)`：每 0.3s 送 Enter
  - `readonly_cmd(cmd, settle_s=1.5)`
  - `leave(via="reset")`
- `ensure_ready(ctx, com, *, timeout_s, recover=True)`：用 `recovery_command(state)` 恢復一次後再等 READY。
- `_TA_ENV_DIRS = (SERIALWRAP_RUN_DIR, STATE_DIR, WAL_DIR, BY_ID_DIR, BY_PATH_DIR, CONFIG_DIR, PROFILE_DIR)`
- `throwaway_env(workdir, by_id_dir, run_dir)`：
  - `STATE=workdir/state`、`WAL=workdir/wal`、`BY_PATH=workdir/bypath`、`CONFIG=workdir/config`、`PROFILE=workdir/profiles`
  - `RUN_DIR` 取 `/tmp` 下的短路徑，因為 AF_UNIX `sun_path` 上限 107 字元
  - pop `SERIALWRAP_ENDPOINT`
- `ThrowawayDaemon(exe, workdir, by_id_dir, profile_yaml)`：
  - 寫入 `profiles/default.yaml`；以 `tempfile.mkdtemp(prefix="swreg-ta-", dir="/tmp")` 建 RUN_DIR。
  - daemon 以 `Popen(["nohup", <exe 同目錄的 serialwrapd>], start_new_session=True)` 啟動，log 寫到 `workdir/daemon.log`。
  - 20s 內以 `run("daemon","status").ok` 輪詢就緒。
  - `run(*args, timeout=30)` 不帶 `--socket`，靠 env 中的 RUN_DIR 推導 endpoint。
  - `wal_text()` 串接 `workdir/wal/*` 全文。
  - `__exit__`：`daemon stop`（15s）→ `killpg(pgid, 15)` → 刪除 run_dir。
- `sh_quote`。

### 3.5 registry 模型（`harness.py`）

- `FAMILY_ORDER = ("F3","F1","F5","F6","F2","F4","F7","F8","F12","F9","F10","F11")`
- `Case(id, family, title, run, issues: tuple[str,...], destructive=False, requires=(), hints=())`（frozen）
- `register`：family 必須在 FAMILY_ORDER 內、issues 非空、id 不重複。
- `load_registry` 先 `ensure_realhw_importable()`，再 import `cases`（`cases/__init__.py` 以 `pkgutil.iter_modules` 自動載入所有 `f*` 模組）。
- `ordered`：排序 key 為 `(FAMILY_ORDER.index, id)`。
- `CaseResult` 沿用 `realhw.harness.CaseResult(verdict, reason="", evidence={}, duration_s=0.0, category="", reason_code="")`。

### 3.6 core 執行模型（`core.py`）

- `case_to_dict`：`{"id","name","topology":{"devices":{COMx:{role,serial,platform}}},"steps":[{"id":"exec","action":"run_case","target":"bench"}],"pass_criteria":["regression_case_verdict"],"metadata":{family,issues,destructive,requires,hints}}`。com 缺項或重複時 `ValueError`。
- `filter_for_run`：點名時取子集；**未點名時跑全套**，destructive case 交給 `runtime_skip`。
- `runtime_skip` 判定順序：
  1. destructive 且 `not allow_destructive` → `("destructive_gated", ...)`；
  2. 已有 `broken_by`，且 case 為 destructive 或有任何 requires → `(f"broken_by:{id}", ...)`；
  3. requires 命中 missing_caps → `(code, ...)`。
- `make_skip_result` → `CaseResult("SKIP", category="environment", reason_code=...)`。
- `failure_payload`：
  - PASS → None；
  - 其餘回 `{category (空值時 SKIP 補 environment、其他補 test), reason_code, comment, evidence:[...], metadata:{regression_verdict}}`。
- `build_ctx`：`Ctx(cfg, report_dir, case_dir=report_dir, sw=SwCli(exe=serialwrap_exe), tmux=TmuxCtl(tmux_prefix), usbipd=None, systemd=Systemd(), win=None)`。
- `run_case_blackbox`：未捕捉的例外轉成 `FAIL category=test reason_code=uncaught_exception`；查無 id 時 `FAIL configuration/invalid_case_config`。
- `recover_boards(..., ready_timeout_s=60)`、`sweep_tmux(prefix)`（kill 掉 `<prefix>-` 開頭的 tmux session）。

### 3.7 分診契約（`docs/regression-plugin.md`）

| category | TestPilot 分類 | 意義 |
|---|---|---|
| `test` | FailTest | 回歸 |
| `environment` | FailEnv | 環境問題；SKIP 也映射到這裡 |
| `configuration` | FailConfig | 設定問題 |

報告由 `RegressionReporter.build_reports` 呼叫 `realhw.harness.write_reports(report_dir, meta+fw_ver+run_id+issues_map, results, hints)`，產出 `report.md` 與 `report.json` 並複製到 artifact_dir。回傳 `{"plugin","run_id","deployed_version","report_dir","reports","diagnostic_counts","cases"}`。

### 3.8 實際 case（**38 個**，依執行順序）

逐 case 欄位：`id`、issues、destructive（D）、requires、定義位置。

| fam | id | issues | D | req | 位置 | CLI 依賴 |
|---|---|---|---|---|---|---|
| F3 | f3-cmd-fail-observable | #94 | | | f03_observability.py:32 | `session attach --selector NOSUCH`；`cmd submit --selector NOSUCH --cmd "echo x"` |
| F3 | f3-device-error-names-selector | #16 | | | f03:48 | `error_code` 與 `_stderr` 須點名該 selector |
| F3 | f3-fail-error-code | #94 | | | f03:19 | 失敗時 `error_code` 非空且 stderr 非空 |
| F3 | f3-log-tail-latest | #124 | | | f03:66 | `log tail-text --selector X` 的 `lines` 須含最新 marker |
| F1 | f1-cmdid-survives-timeout | #15 | | | f01_cmd_contract.py:124 | `cmd submit --cmd "sleep 20" --cmd-timeout N`；`cmd status` 不得回 `CMD_NOT_FOUND` |
| F1 | f1-limits-queryable | #129,#27 | | | f01:31 | `daemon status.limits.max_submit_cmd_bytes`、`warn_submit_cmd_bytes` |
| F1 | f1-near-limit-no-logout | #19 | | | f01:89 | 接近 warn 門檻的命令執行後，session 仍為 READY |
| F1 | f1-newline-rejected | #27 | | | f01:74 | `CMD_CONTAINS_NEWLINE` |
| F1 | f1-too-long-rejected | #23,#27 | | | f01:48 | `CMD_TOO_LONG`；`--cmd-timeout 5` |
| F5 | f5-console-peer-gone-recycled | #53,#11 | | tmux | f05_console_coexist.py:202 | tmux `serialwrap-minicom COMx`；`session console-list` 的 `consoles[].interactive_owner` |
| F5 | f5-deferred-bytes-flushed | #78 | | tmux | f05:138 | 同上；`cmd submit --cmd "sleep 3" --cmd-timeout 8` |
| F5 | f5-raw-ownership-survives-agent-rounds | #78 | | tmux | f05:72 | 5 輪 agent 命令＋Tab 補完 |
| F5 | f5-second-console-linebuffer | #7,#8 | | tmux | f05:258 | 兩個 minicom，恰一個 owner |
| F6 | f6-ping-during-long-op | #80 | | | f06_rpc_liveness.py:41 | `cmd submit sleep 5/8`；`daemon status` 往返時間；`cmd cancel` |
| F6 | f6-two-boards-no-starvation | #80,#52 | | | f06:91 | 雙板 |
| F2 | f2-history-bounded-rss | #81 | | | f02_backpressure.py:157 | `daemon status.pid` 加上讀 `/proc/<pid>/status` VmRSS（POSIX）；200 次命令，任一次未 done 即 FAIL |
| F2 | f2-queue-full-backpressure | #81 | | | f02:97 | `SESSION_QUEUE_FULL` |
| F2 | f2-recovery-flushes-queue | #128,#156 | | | f02:309 | `session recover`；10s 內佇列有界排空 |
| F2 | f2-rx-window-crossing-prompt | #158 | | | f02:249 | — |
| F4 | f4-activity-classification | #34 | | | f04_session_semantics.py:43 | `session activity --selector` → `session.activity_classification ∈ {active, idle-healthy, quiet-suspicious, newly-attached, offline}` |
| F4 | **f4-background-result-tail-consistent** | #28 | | | f04:95 | 見 §3.10 |
| F4 | f4-interactive-line-cmd-defined | #26 | | | f04:168 | `session interactive-open --owner agent:f4-regression --timeout 20`；lease 期間 `cmd submit`；`interactive-close` |
| F7 | f7-binary-roundtrip-md5 | #32,#21,#161,#166 | | | f07_file_transfer.py:300 | `--timeout N file push/pull --selector --local --remote`；錯誤碼集合 `CHECKSUM_MISMATCH`、`CHECKSUM_VERIFY_FAILED`、`EMPTY_RESPONSE`、`MOVE_TIMEOUT`、`SOCKET_ERROR`、`TARGET_DECODER_FAILED/MISSING`、`TARGET_ENCODER_FAILED/MISSING`、`TRANSFER_ECHO_STALL`、`TRANSFER_TIMEOUT` |
| F7 | f7-larger-file-not-truncated | #21,#161,#166 | | | f07:309 | 1MB 檔案 |
| F8 | f8-second-daemon-detected | #101 | | | f08_daemon_singleton.py:79 | 以 ThrowawayDaemon（profile `profiles: {}\ntargets: {}\n`，by-id 空目錄）啟動第二個 daemon；prod `daemon status.multi_open` 或 `multi_open_detail.daemons` 長度 >1，15s 內每 2s 輪詢 |
| F8 | f8-tty-holders-reported | #101,#53 | | | f08:20 | `daemon status.foreign_holders`（tty→pid）、`doctor` |
| F12 | f12-version-reported-consistent | #154 | | | f12_diagnostics.py:29 | 見 §3.9 |
| F12 | f12-wal-path-live | #148 | | | f12:80 | 見 §3.9 |
| F9 | f9-attach-during-boot-reprobes | #69,#14,#20 | D | | f09_boot_uboot.py:163 | `cmd submit reboot --cmd-timeout 8`；`session clear`、`session attach` |
| F9 | f9-brcm-spaced-prompt-reboot-login | #224 | D | | f09_brcm_reboot_login.py:85 | 讀 `cat /proc/sys/kernel/random/boot_id`；`sync; reboot`（`--source agent:serialwrap-regression --mode line`）；`wal current-seq`；分頁執行 `log tail-raw --from-seq N --limit 200`，最多 20 頁，依 `truncated` 判斷是否續讀，`wal_file_exists`，`records[].{dir,payload_b64,seq}`；**唯一可在 Windows 跑的 case** |
| F9 | f9-quiet-window-agent-passthrough | #130 | D | | f09:77 | `boot_quiet_remaining_s` |
| F9 | f9-reboot-autoboot-unmolested | #130 | D | | f09:27 | `log tail-text` |
| F9 | f9-spontaneous-reboot-agent-gated | #139,#130 | D | tmux | f09:347 | 須回 `AUTOBOOT_QUIET`、`READY_UNCONFIRMED`；`recommended_action`、`ready_reconfirm_pending`、`ready_reconfirm_failed` |
| F9 | f9-uboot-readonly-and-console-kept | #44,#130 | D | tmux | f09:227 | `UBootConsole`；`session console-list` |
| F10 | f10-creds-fixed-then-ready | #140 | D | | f10_login_creds.py:416 | `device release --selector X --source agent:swreg --reason "f10 creds regression"` → ThrowawayDaemon 開同一裝置 → `device attach` 收回；`session list` 的 `last_error`、`state`、`device_by_id` |
| F10 | f10-unresolved-creds-terminal | #140 | D | | f10:286 | `last_error == CREDENTIALS_UNRESOLVED`；以 WAL payload 確認沒有送出空帳密 |
| F11 | f11-flood-probe-classified | #153 | D | | f11_rx_flood.py:99 | 洪水命令 `P=FFFFFFFFFF; …; for i in $(seq 1 8000); do echo $i $P; done &`；`session self-test` 的 `classification == RX_FLOOD`；`session activity` 的 `rx_bytes_last_10s`；容許狀態 `ATTACHED_NOT_READY`、`PROMPT_UNAVAILABLE`、`TARGET_UNRESPONSIVE` |
| F11 | f11-last-rx-age-sane | #150 | | | f11:41 | `last_rx_age_s`、`last_tx_age_s`、`last_error_detail` |

family 分布：F1=5、F2=4、F3=4、F4=3、F5=4、F6=2、F7=2、F8=2、F9=6、F10=2、F11=2、F12=2，合計 **38**。destructive 共 9 個（F9 全部 6 個、F10 兩個、`f11-flood-probe-classified`）。

### 3.9 F12 的 WAL oracle（目前以 mtime 判斷）

`f12_wal_path_live`（`regression/serialwrap_regression/cases/f12_diagnostics.py:94-134`）：

1. 讀 `daemon status.wal_path`；缺項時 FAIL `wal_path_missing`。
2. `os.path.exists(wal_path)` 為假時 FAIL `wal_path_not_found`。
3. `doctor.checks[].check` 中必須有 `"wal_dir"`，否則 FAIL `doctor_wal_dir_check_missing`。
4. 記下 `mtime_before = os.path.getmtime(wal_path)`。
5. 對 `boards[0].com` 執行 `submit_and_wait("echo MARKER_<6 位亂數>")`；`status != "done"` 時判 `environment/submit_precondition_failed`。
6. 記下 `mtime_after`；**`mtime_after <= mtime_before` 時 FAIL `test/wal_not_advancing`**。
7. evidence：`daemon-status.json`、`doctor.json`、`submit.json`、`wal-mtime.txt`。

注意：這個 case **沒有讀 WAL 內容、沒有查 marker、沒有驗 CRC 或 seq**。marker 只送出、從未被比對。spec §8.3 與 OD-13 要求改寫的正是這條 oracle（BC-01 常駐 handle 在 Windows 上 mtime 不可靠）。

`f12_version_reported_consistent`：
- `daemon status.version` 須為非空字串，否則 FAIL `version_field_missing`；
- `parse_version(daemon)` 與 `parse_version(--version _raw)` 任一無法解析時 FAIL `version_unparseable`；
- 兩者不同時 FAIL `cli_daemon_version_mismatch`。

相關的另一個 mtime oracle（L3，spec 沒有提到）：`realhw/cases/p0.py:128-143 p0-wal-live`。
- 它寫死 `Path.home()/.local/state/serialwrap/wal/raw.mirror.log`，比對送命令前後 `st_mtime`，sleep 2s 後要求 mtime 前進，且檔尾 8000 字元內含 marker `P0WAL_<5 位亂數>`。
- 同樣以 mtime 為主要條件，並且讀的是 **mirror log**，不是 `wal_path`。

### 3.10 f4 哨兵（`f4-background-result-tail-consistent`）

程式碼位置：`f04_session_semantics.py:229-299`。

- 命令：`cmd submit --selector COM0 --cmd "for i in $(seq 1 50); do echo L$i; done" --mode background --cmd-timeout 30`。
- 讀取迴圈：60s 期限，每 0.5s 執行 `cmd result-tail --cmd-id X --from-chunk N`。
  - 收集 `chunks`（`list[str]`）；
  - 下一輪起點 `from_chunk = int(next_chunk or from_chunk+len(chunks))`；
  - 終止條件：`status ∈ {done, error}` 且本次 `chunks` 為空。
- 判定：以 `L(\d+)` 計數，1..50 每個 marker 必須恰出現一次。
  - 期限內未到終態：`environment/background_result_tail_timeout`；
  - 有缺漏：`test/result_tail_dropped_chunk`；
  - 有重複：`test/result_tail_duplicated_chunk`。
- 程式碼裡**沒有 XFAIL 機制**。plugin 與 realhw 的 `CaseResult` 只有 PASS／FAIL／SKIP 三種 verdict。
- 狀態記載互相矛盾（見 §8 D-6）：
  - case hint 寫「#159 已修…本 case 為其常駐回歸防線…若再轉紅屬回歸，勿改 oracle 遷就」；
  - `CHANGELOG.md:454` 也記錄 #159 已修、oracle 不變；
  - 但 `docs/regression-plugin.md:91` 仍列為「FAIL（刻意保留的紅燈哨兵）…#159 修復即轉綠」；
  - spec §11.4 與 §15.10 則稱它為「紅燈哨兵…維持 #159 對應」。

### 3.11 Console case 的 CLI 實際來源（Rust 重要）

- F5、F9（uboot、spontaneous）、realhw p0／p1_console、lr-mixed 都用 `ctx.tmux.new(ses, "serialwrap-minicom COM0")`。
- `serialwrap-minicom` 是 `serialwrap setup` 把 `sw_core/assets/tools/minicom_router.sh` 複製到 `~/.local/bin/serialwrap-minicom` 而來（`sw_core/setup_cmd.py:219-228`）。
- router 自行決定 CLI：`SERIALWRAP_BIN="${SERIALWRAP_BIN:-${BASE_DIR}/serialwrap}"`，其中 BASE_DIR 優先取 script 所在目錄（`minicom_router.sh:4-13`），也就是 `~/.local/bin/serialwrap`。**這條路徑與 testbed pin 無關。**
- router 依賴 CLI 子命令 `session list`、`session console-attach --selector --label`、`session console-detach --selector --client-id`、`supervision-mode`、`daemon start`、`session recover`，以及 `jq` 對 JSON 欄位 `com/alias/session_id/state/client_id/vtty` 的解析。

---

## 4. `realhw/`（L3 穩定性引擎；stdlib-only；禁止 import `sw_core`）

### 4.1 入口：`python3 -m realhw`（`realhw/__main__.py`）

- 參數：
  - `--tier`（預設 `p0`；`p0|p1|remote|longrun` 逗號多選）
  - `--only`、`--skip`（逗號分隔）
  - `--duration`（預設 `"32h"`，格式 `^(\d+)([hms])$`）
  - `--report-dir`（預設 `~/b-log/realhw-reports/<%y%m%d-%H%M%S>`）
  - `--list`
- 流程：
  1. `load_cfg()`、`cfg["duration_s"]`、`select_cases`；
  2. 建立 `SwCli()`（PATH）、`WinSwCli(cfg.win_serialwrap_exe)`、`Ctx(...)`、`TmuxCtl(tmux_prefix)`、`Usbipd(usbipd_exe)`、`Systemd()`；
  3. 取 bench lock，lock 失敗時 `benchlock_ok=False`；
  4. `preflight.collect` 加 `evaluate`，不通過時 **return 2**；
  5. `collect_capabilities`（`remote status.ok`、`--version`、`docker info`）與 `missing_capabilities`；
  6. `run_cases`、`write_reports`；
  7. 有任何 FAIL 時 return 1，否則 0。
- `meta` 欄位：`{version (=--version _raw), git, tiers, started_at, preflight_notes, capabilities_missing, win_daemon_present, win_daemon_holds}`。

### 4.2 `config.json`（本機設定；stdlib json）

- boards：
  - `{"com":"COM0","alias":"dut-prpl","serial":"AC01QZT0","busid":"8-1","platform":"prpl"}`
  - `{"com":"COM1","alias":"sta-prpl","serial":"AQ00OAQ7","busid":"8-2","platform":"brcm","profile":"brcm-template"}`
- `usbipd_exe="/mnt/c/Program Files/usbipd-win/usbipd.exe"`、`win_serialwrap_exe="/mnt/c/serialwrap/serialwrap.exe"`、`tmux_prefix="realhw"`
- `timeouts{ready_wait_s:180, reboot_wait_s:300, human_active_window_s:60}`
- `longrun{snapshot_interval_s:300, agent_workers:4}`
- `_CFG_DEFAULTS={"win_serialwrap_exe":"", "tmux_prefix":"realhw"}`

### 4.3 `drivers.py`

- 正則 `_ANSI = r"\x1b(?:\[[0-9;?]*[A-Za-z]|\([A-Za-z]|[=>])"`；函式 `strip_ansi`、`find_marker`。
- `parse_usbipd_list(output)`：取 `Connected:` 與 `Persisted:` 之間符合 `^(\d+-\d+)\s` 的行。
- `parse_win_held(payload)`：排除 state 為 `DETACHED`、`RELEASED` 或空值的 session，回 `{com, state, device_by_id}`。
- `match_held_for_serial`、`plan_hp_rescue(win_available, held_com, retries_done, *, max_retries=2)`。
- `classify_topology_run(rc, log_tail)`：
  - rc 0 且 log 含 `SKIP：` → `SKIP/environment/docker_unavailable`；rc 0 其餘情況 → PASS；
  - rc -1 → `FAIL/environment/harness_timeout`；
  - 其他 rc：log 含「docker build 失敗」→ `docker_build_failed`；含「逾時未就緒」→ `harness_not_ready`；其餘 → `FAIL/test/tunnel_assertion_failed`。
- `remote_state_dir(env)` 依序取：`$SERIALWRAP_RUN_DIR/remote` → `$SERIALWRAP_STATE_DIR/remote` → `$XDG_RUNTIME_DIR/serialwrap/remote` → `${XDG_STATE_HOME:-~/.local/state}/serialwrap/run/remote`。
- `_run(argv, timeout=30)`：`capture_output`、`encoding="utf-8"`、`errors="replace"`。
- `SwCli(exe="serialwrap")`：
  - `run(*args, timeout=30) -> dict`：stdout 能解析成 JSON 就用 JSON，不能就回 `{"_raw": out}`；一律附 `_rc`、`_stderr`。
  - `sessions()`、`session(com)`。
  - `submit_and_wait(com, cmd, *, cmd_timeout=12.0, settle_s=1.5, poll_s=0.5)`：執行 `cmd submit --selector --cmd --cmd-timeout` 後先 sleep settle；終態判定為 `command.status ∈ {done,error,timeout}`，期限 `cmd_timeout+10`；失敗時回 `{"_error": ...}`。
  - `wait_state(com, want, *, timeout_s, poll_s=2.0)`。
- `TmuxCtl(prefix)`：
  - `name(tag) = f"{prefix}-{tag}-{os.getpid()}"`
  - `new` = `tmux new-session -d -s`；`send` = `send-keys -l --` 視需要加 Enter
  - `send_key`、`capture` = `capture-pane -p`、`kill`
- `Usbipd(exe)`：`list_busids`、`detach -b`、`attach -w -b`（timeout 60）。
- `Systemd`：`UNIT="serialwrap"`。
  - `restart()` 執行 `sudo -n systemctl restart serialwrap`；
  - `main_pid()` 執行 `systemctl show -p MainPID serialwrap`。兩者都是 **system scope**，沒有 `--user`。
- `WinSwCli(exe)`：`available()`、`run`、`held_devices()`、`release(com)`。`release` 執行 `device release --selector --source agent:realhw --reason "realhw hp-rescue"`。

### 4.4 `harness.py`

- `CaseResult(verdict PASS|FAIL|SKIP, reason="", evidence={}, duration_s=0.0, category="", reason_code="")`
- `Case(id, tier, title, run, destructive=False, requires=(), hints=())`
- `REGISTRY`、`register`（id 重複時 ValueError）
- `select_cases(registry, *, tiers, only, skip)`：`only` 不存在時拋 `UnknownCaseError`。
- `parse_duration`、`load_cfg(config_path=None, *, injected=None)`、`render_report_md`、`write_reports`（`report.json` 以 `ensure_ascii=False, indent=1` 輸出，另寫 `report.md`）。
- `Ctx(cfg, report_dir, case_dir, sw, tmux, usbipd, systemd, win=None)`，`.note(name, content) -> relpath`。
- `recovery_command(state)`：`RELEASED → ("device","attach")`；`DETACHED → ("session","attach")`；其他狀態 → `("session","recover")`。
- `run_cases(cases, ctx, *, boards, missing_caps)`：
  - `broken_by` 已設定時，requires 含 `two_boards` 或 destructive 的 case → `SKIP environment broken_by:<id>`；
  - requires 命中 missing caps → SKIP；
  - case 內未捕捉的例外 → `FAIL reason_code=uncaught_exception`（此處 category 為空）；
  - 每個 case 跑完後，對非 READY 的板子執行 `recovery_command` → sleep 5 → `wait_state READY 60s`；仍未恢復就記為 `broken_by`。

### 4.5 `preflight.py`

- `Checks(git_behind, doctor_ok, boards_ready, boards_expected, tools_missing, leaked_daemons, other_pytest, state_polluted, benchlock_ok=True, external_testpilot=(), win_daemon_present=False, win_daemon_holds=())`
- `MIN_DEPLOYED=(0,2,3)`、`parse_version`
- `Capabilities(remote_capability, deployed_version, docker)`；`missing_capabilities` 的 key→code：`remote_capability → remote_capability_missing`、`deployed_recent → deployed_daemon_stale`、`docker → docker_unavailable`。
- `evaluate` 判定：
  - `git_behind>0` 只警告不擋；
  - `doctor` 不通過、板子不 READY（Windows daemon 持有時附註歸因）、缺工具、有殘留 daemon、有其他 pytest、state 被污染、bench lock 被占用、有外部 testpilot，任一成立即 FAIL。
- 收集項：
  - `_git_behind` = `git fetch -q origin` 後 `rev-list --count HEAD..origin/main`
  - `_doctor_ok`：每個 check 須 `ok` 或 `advisory`
  - `_tools_missing`：tmux、minicom、usbipd 檔案、`sudo -n true`
  - `_leaked_daemons`：`pgrep -af 'sw-coexis[t]|sw-pytest-iso'`
  - `_other_pytest`：`pgrep -af 'pytes[t]'`
  - `_state_polluted`：`~/.local/state/serialwrap/state.json` 的 `bindings` 含 `/tmp/`
  - `bench_lock_path() = ~/.local/state/serialwrap/bench.lock`；`acquire_benchlock` 使用 `fcntl.flock`，Windows 下 raise RuntimeError
  - `_external_testpilot`：`pgrep -af 'testpilot ru[n]'`

### 4.6 實際 case（**37 個**：p0=8、p1=20、remote=8、longrun=1）

| tier | id | D | requires | 位置 |
|---|---|---|---|---|
| p0 | p0-doctor | | two_boards | p0.py:20 |
| p0 | p0-cmd-async | | two_boards | p0.py:40 |
| p0 | p0-console-raw | | tmux,two_boards | p0.py:55 |
| p0 | p0-clear-reattach | | two_boards | p0.py:85 |
| p0 | p0-selftest | | — | p0.py:94（`probe_ok` 且 `classification=="OK"`） |
| p0 | p0-blog-clean | | tmux | p0.py:105（`~/b-log/mini_COM0_*.log` 不得含 ESC 或 `Script started`） |
| p0 | p0-wal-live | | — | p0.py:128（mirror log mtime，見 §3.9） |
| p0 | p0-multiopen | | — | p0.py:146（`foreign_holders` 中除自身 pid 外不得有其他持有者，`multi_open` 為假） |
| p1 | p1-con-fanout | | tmux,two_boards | p1_console.py:34 |
| p1 | p1-con-defer | | tmux,two_boards | p1_console.py:59 |
| p1 | p1-con-busy | | tmux,two_boards | p1_console.py:113（`SESSION_INTERACTIVE_BUSY`） |
| p1 | p1-con-softpreempt | | tmux,two_boards | p1_console.py:140（`soft_preempted`） |
| p1 | p1-con-liveness | | tmux,two_boards | p1_console.py:175（`human_attached`） |
| p1 | p1-con-orphan | | tmux,two_boards | p1_console.py:206 |
| p1 | p1-con-second | | tmux,two_boards | p1_console.py:266 |
| p1 | p1-cmd-modes | | two_boards | p1_cmd.py:51（line／background／interactive；`interactive-send --encoding plain|key`；**會呼叫 `interactive-status`**，`screen` 欄位；錯誤碼 `FOREGROUND_BUSY`、`SESSION_NOT_FOUND`、`SESSION_QUEUE_FULL`） |
| p1 | p1-cmd-serial | | two_boards | p1_cmd.py:133 |
| p1 | p1-cmd-file | | two_boards | p1_cmd.py:185（`--timeout 120 file push/pull`） |
| p1 | p1-wal-reset | | tmux,two_boards | p1_wal.py:38（`wal reset` 後 `current-seq` 須為 0 並重新累積，且等於 `wal_path` 檔尾 seq） |
| p1 | p1-wal-fullrun | | tmux,two_boards | p1_wal.py:87（seq 嚴格遞增；`wal export --from-seq 0` 的 `records` 非空） |
| p1 | p1-rst-daemon | D | two_boards | p1_restart.py:31（`sudo systemctl restart`；`bridge_generation`、`profile`、`device_by_id` 不變） |
| p1 | p1-rst-reboot | D | two_boards,deployed_recent | p1_restart.py:77 |
| p1 | p1-rst-bootwindow | D | two_boards,deployed_recent | p1_restart.py:124 |
| p1 | p1-rst-recover | D | two_boards | p1_restart.py:146 |
| p1 | p1-ho-cycle | D | two_boards | p1_handoff.py:28（`device release`；外部 tmux 執行 `minicom -D <attached_real_path> -b 115200`；`device attach` 須回 `DEVICE_STILL_HELD`；`--force`） |
| p1 | p1-ho-persist | D | two_boards | p1_handoff.py:80（RELEASED 狀態跨 systemd restart 維持） |
| p1 | p1-hp-cycle | D | two_boards | p1_hotplug.py:92（usbipd detach／attach；Windows 救援） |
| p1 | p1-hp-reorder | D | two_boards | p1_hotplug.py:142 |
| longrun | lr-mixed | | tmux,two_boards | longrun.py:274 |
| remote | rm-topo-direct | | docker | remote.py:77 |
| remote | rm-topo-nat-host | | docker | remote.py:83 |
| remote | rm-topo-dual-nat | | docker | remote.py:89 |
| remote | rm-topo-agent-pull | | docker | remote.py:95 |
| remote | rm-topo-gwports | | docker | remote.py:101 |
| remote | rm-live-e2e | | docker,two_boards,remote_capability | remote.py:203 |
| remote | rm-live-orphan | | docker,two_boards,remote_capability | remote.py:283 |
| remote | rm-live-cycle | | docker,two_boards,remote_capability | remote.py:336 |

remote 常數（`remote.py:22-27`）：`_SCRIPT=tools/docker/remote_tunnel_test.sh`、`_TOPO_TIMEOUT_S=1800`、`_IMAGE_TAG=$IMAGE_TAG or "serialwrap:remote-tunnel-test"`、`_LIVE_PREFIX="rhwlive"`、`_LIVE_PORT=7777`。rm-topo 系列以 `SUFFIX=rhw<pid>` 執行 `bash remote_tunnel_test.sh <topo>`，結束後 `_sweep_docker(suffix)`。rm-live 系列：host 端執行 `remote expose|connect ... --ssh-opt=-oStrictHostKeyChecking=no --ssh-opt=-oUserKnownHostsFile=/dev/null`，容器內執行 `docker exec -u tester <c> serialwrap ...`。

### 4.7 `lr-mixed`（`realhw/cases/longrun.py`）

- 常數：`MAJOR_BOTH_DOWN_S = 900`、`FAIL_STUCK_S = 900`、`_COUNTED_KINDS = ("submit","done","error")`。
- 參數：`duration_s = cfg.duration_s or longrun.duration_s or 0`、`n_workers = longrun.agent_workers or 4`、`snap_interval = longrun.snapshot_interval_s or 300`。
- 產出檔（位於 `case_dir`）：`events.ndjson`、`snapshots.ndjson`、`longrun-analysis.md`。
- **agent worker**（source 為 `agent:rhw<i>`）：第 n 輪的 board 為 `boards[(i+n) % len]`。
  - `n%10==0`：interactive。`interactive-open --owner source --timeout 10` 成功就 close。`ok` 時記 done（detail `soft_preempted`）；`SESSION_INTERACTIVE_BUSY` 時記 `busy`，不計入 error；其他記 error。
  - `n%3==0`：background。`cmd submit --mode background --cmd "echo BG_<tag>_<n>"`，**只要回了 `cmd_id` 就記 done，不追命令終態**。
  - 其餘：line。`submit_and_wait(echo LR_<tag>_<n>_<rand4>, cmd_timeout=10)`，須 `status=="done"` 且 stdout 含 marker。
  - 每個動作都包在對應板子的 `board_locks[board]` 內；動作間 sleep `uniform(0.5, 2.0)`。
- **human worker**：tmux 執行 `serialwrap-minicom COM0`。死掉時記 `minicom_dead` 並重開；每 `uniform(120,300)` 秒執行 `send "echo HUMAN_TICK_<n>"`。
- **snapshot worker**：
  - 每次取 `pid = systemd.main_pid()`、`sessions = {com: state}`，以及 `rss = /proc/<pid>/status VmRSS`；
  - 寫入 `{"t","sessions","rss_kb","pid"}`；
  - `pid==0` 時記 major `daemon_death` 並停止；兩板同時非 READY 達 900s 時記 major `both_boards_stuck`。
- 收到 SIGINT 時記 `sigint` 並停止。
- `analyze(snapshots, events)`（純函式）回傳：
  - `per_source{src:{submit,done,error}}`
  - `stuck_attached[{com,from_t,to_t,duration_s}]`
  - `pid_changes`：只計前後兩端 PID 皆非 0 的變更
  - `daemon_death_at`：pid 首次變成 0 的 t
  - `rss_trend{first_kb,last_kb,delta_kb}`
- FAIL 條件：有 major 事件、`daemon_death_at` 非 None、任一 stuck 段 ≥900s，或 `tot_submit >= 10` 且 `error/submit > 0.5`。
- reason_code 優先序：`daemon_died` → `both_boards_stuck` → `board_stuck` → `cmd_error_rate_high`。
- **平台依賴**：`/proc`（RSS）、system-scope systemd 的 MainPID、tmux、minicom。在 systemd-user 或 on-demand 部署下，`main_pid()` 回 0，第一個 snapshot 就會被判成 `daemon_death`。

### 4.8 `realhw/acceptance/`

**不存在。** spec §15.11.4 指定的唯一發版門檻來源 `realhw/acceptance/longrun_acceptance.yaml`、`tests/acceptance/cases.yaml`、`tests/acceptance/bridge_record.schema.json`、`tests/contract_blackbox/`、`contracts/`、`tools/uart_stdio_bridge/` 在 repo 中都還沒有建立。spec §3.2 已明說「新增路徑不代表目前 repo 已存在」。

---

## 5. `reliability/`：TestPilot plugin `serialwrap_reliability`（L3 的第二前端）

- `pyproject.toml`：`name="serialwrap-reliability"`、`0.1.0`；entry point `serialwrap_reliability = "serialwrap_reliability.plugin:Plugin"`；依賴 `testpilot-core>=0.3.4,<1.0`。
- `agent-config.yaml` 與 regression 相同，只差 `per_step_seconds: 60`。
- `testbed.yaml.example`：
  - `testbed.name=serialwrap-reliability-bench`、`run_backend=serialwrap`
  - devices：`DUT{role:dut, transport:serialwrap, selector:COM0, alias:dut-prpl, serial:AC01QZT0, busid:"8-1", platform:prpl}`、`STA{role:sta, selector:COM1, alias:sta-prpl, serial:AQ00OAQ7, busid:"8-2", platform:brcm, profile:brcm-template}`
  - `serialwrap_reliability{usbipd_exe, win_serialwrap_exe, tmux_prefix:realhw, timeouts{180,300,60}, longrun{duration:"15m", snapshot_interval_s:300, agent_workers:4}}`
- `testbed_loader`：
  - `_BOARD_OPTIONAL_KEYS=("alias","serial","busid","platform","profile")`、`_SECTION_KEYS=("usbipd_exe","win_serialwrap_exe","tmux_prefix","timeouts")`
  - `testbed_to_cfg(raw)` 的結果與 `config_json_to_cfg(path, duration=None)` 等價，兩者都經 `realhw.load_cfg`；板子依 selector 排序，排序 key 為 `(alpha, int)`。
- `core`：
  - `resolve_repo_root`（只支援 editable 安裝）、`load_registry`（import `realhw.cases`）
  - `synth_longrun_steps(duration_s, interval_s)`：產生 `N = max(1, duration//interval)` 個 `checkpoint-NNN` step
  - `filter_for_run`：**未點名時排除 destructive**（regression 不排除，改由 gate 記 SKIP）
  - `runtime_skip`：沒有 destructive gate
  - `build_ctx`：`sw=SwCli()`（**PATH**）、`win=WinSwCli(cfg["win_serialwrap_exe"])`
  - `run_preflight`：與 realhw 相同；**沒有版本 gate**；`deployed_version` 取自 `collect_capabilities`
  - `LongrunRunner`：以背景 thread 執行，`wait_checkpoint(index,total)` 回 `{checkpoint,total,snapshots_seen,finished}`
- 報告目錄：`~/b-log/realhw-reports/tp-<ts>`（`plugin.py:111`）。
- `verify_install` 檢查：`which serialwrap`、tmux、minicom、usbipd 存在，以及 registry 數量 `>=29`。

---

## 6. `tools/docker/` 與 `Dockerfile`

- **`Dockerfile`**：
  - base image `python:3.11-slim`；apt 安裝 `jq minicom socat openssh-server openssh-client autossh iproute2`。
  - `COPY . /opt/serialwrap` 後 `pip install --no-cache-dir .`，console_scripts `serialwrap` 與 `serialwrapd` 會進 PATH。
  - 使用者：`tester`（passwordless；內建 ed25519 key `id_ed25519`，`authorized_keys` 為自己；建立 `~/.sw-relay`）與 `otheruser`。
  - `/home/tester/.ssh/config` 設 `StrictHostKeyChecking no`。
  - `sshd_config`：`GatewayPorts no`、`PasswordAuthentication no`、`PermitRootLogin no`、`PubkeyAuthentication yes`、`AllowTcpForwarding yes`、`UsePAM no`；另產生 `/etc/ssh/sshd_config.gwyes`（`GatewayPorts yes`）。
  - `CMD ["bash"]`。
- **`uart_harness.py`**（140 行；給 `remote_tunnel_test.sh` 用）：
  - 參數：`--selector COM0`、`--ready-timeout 20.0`、`--env-out /home/tester/sw-uart.env`。
  - 以 `DaemonHarness(HarnessConfig(target_config=TargetConfig(platform="prpl", boot_banner="boot done\r\nroot@prplOS:/# ", noise_enabled=False, default_response="EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# "), com=selector, alias="docker-remote-tunnel"))` 啟動。
  - `_wait_ready` 執行 `/opt/serialwrap/serialwrap --socket S session list`。
  - 把 `_ENV_KEYS=(SERIALWRAP_STATE_DIR, SERIALWRAP_RUN_DIR, SERIALWRAP_BY_ID_DIR, SERIALWRAP_BY_PATH_DIR)` 以 `export K=<shlex.quote>` 寫入 env 檔。
  - 印出 `{"ok":true,"event":"UART_HARNESS_READY","selector","socket","env_out"}`（compact JSON）後阻塞，直到收到 SIGTERM／SIGINT。
- **`remote_lab.py`**（169 行；給 `remote_smoke.sh` 用）：
  - DaemonHarness（alias `docker-remote`）加 `socat` 把 unix socket 轉成 TCP。
  - 參數 `--listen-host`、`--tcp-port`（預設為 env `SERIALWRAP_REMOTE_PORT`，否則 `7777`）。
  - 合成錯誤碼：`EMPTY_STDOUT`、`INVALID_JSON_STDOUT`。
- **`remote_smoke.sh`**（122 行）：
  - image `serialwrap:remote-smoke`；建立 network `serialwrap-remote-net-<SUFFIX>`；port 7777。
  - 起容器 A（`remote_lab.py --listen-host 0.0.0.0`），再用一次性 B 容器執行 `serialwrap --endpoint tcp://<A>:7777 daemon status / session list / cmd submit --selector COM0 --cmd 'uname -a' / cmd status --cmd-id`。
- **`remote_tunnel_test.sh`**（653 行）：
  - 參數 `direct|nat_host|dual_nat|agent_pull|gwports|all`（預設 all）；image `${IMAGE_TAG:-serialwrap:remote-tunnel-test}`；`SUFFIX=${SUFFIX:-$$}`。
  - **共 4 個拓樸加 1 組額外驗收（gwports）**：
    1. `direct`
    2. `nat_host`
    3. `dual_nat`
    4. `agent_pull`（`-L` 拉 uart 的 `serialwrapd.sock`，uart 容器以 root 跑 sshd）
    5. 額外：`gwports`（GatewayPorts yes 時須 fail-closed）
  - 斷言 ①～⑧：
    - ① 預設不啟用：`remote status` 空、沒有 state 檔、沒有 ssh／autossh 行程、`--endpoint` 回 `SOCKET_ERROR`
    - ② 啟用後 `session list` READY，且 `cmd submit` 再 `cmd status` 得 done，輸出正確
    - ③ daemon pid 不變
    - ④ `close all` 後回到乾淨狀態
    - ⑤ `ss -ltn` 顯示只綁 127.0.0.1，attacker 容器連不到
    - ⑥ relay 上的第二個本機使用者可用 tcp 模式連入，`--remote-socket` 模式被拒
    - ⑦ GatewayPorts yes 時 `-R tcp` 回 `REMOTE_BIND_UNVERIFIED`，`--remote-socket` 仍成功
    - ⑧ 沒有對應 `-R` 時，`-L` 回 `starting`
  - docker 不可用時 exit 0 並印 `SKIP：...`；任一斷言失敗 exit 1，並印 `[serialwrap] FAIL: ...`。
  - 執行檔來源：容器主行程 `python3 /opt/serialwrap/tools/docker/uart_harness.py`；`uart_exec`、`role_exec` 與 `endpoint_exec` 都用 PATH `serialwrap`（pip 安裝的版本），其中 `role_exec` 固定 `SERIALWRAP_RUN_DIR=/home/tester/.sw-run`。
- **`tools/` 其他檔案**：`architecture_browser.py`(69)、`architecture_review.py`(123)、`bench-enroll.sh`(489)、`bench-issue.sh`(380)。

---

## 7. Rust 承接要點（只列直接由程式碼推得的必要條件）

1. **`--version`**：stdout 必須含 `X.Y.Z`（測試端取第一個 `\d+\.\d+\.\d+`），exit 0，輸出不是 JSON（`_raw` 路徑）。`daemon status.version` 必須是非空字串，而且 Stage 1 mixed 組合中 Rust CLI 與 Python daemon 解析出的三段版本必須相等，否則 regression 整場拒跑。
2. **sibling daemon**：regression 的 ThrowawayDaemon（F8、F10）一律在 pinned CLI 同目錄找名為 `serialwrapd` 的執行檔，以 `nohup <dir>/serialwrapd` 無參數啟動，靠 env 中的 `SERIALWRAP_RUN_DIR/STATE_DIR/WAL_DIR/BY_ID_DIR/BY_PATH_DIR/CONFIG_DIR/PROFILE_DIR` 決定所有路徑。Rust daemon 必須遵守這組 env。
3. **multi_open 偵測**：判斷依據是 `/proc/<pid>/cmdline`，argv0 的 basename 為 `serialwrapd`、某個路徑型參數的 basename 為 `serialwrapd`，或某參數以 `serialwrapd.py` 結尾（`sw_core/multi_open.py:27-62`）；另擷取 `--socket` 參數。Rust daemon 的執行檔必須命名為 `serialwrapd`。
4. **Console 前端**：L2／L3 的 human 路徑是 `serialwrap-minicom`（bash router）加上它同目錄的 `serialwrap`。Stage 1 換成 Rust CLI 時，router 實際使用的 CLI 取決於 `~/.local/bin/serialwrap`，與 testbed pin 是兩條獨立路徑。
5. **systemd**：realhw、reliability 寫死 system-scope unit `serialwrap`（`sudo -n systemctl restart serialwrap`、`systemctl show -p MainPID serialwrap`）；live guard 會同時試 system 與 `--user`。
6. **JSON 欄位依賴**（L2／L3 已凍結）：
   - `daemon status`：`pid`、`version`、`running`、`wal_path`、`mirror_path`、`wal.healthy`、`limits.{max_submit_cmd_bytes,warn_submit_cmd_bytes}`、`multi_open`、`multi_open_detail.daemons[]`、`foreign_holders{tty:pid}`
   - `doctor`：`ok`、`checks[].{check,ok,advisory}`，必須含 `wal_dir`
   - `session list`：`sessions[].{com,alias,session_id,state,device_by_id,last_error,last_tx_at,bridge_generation,profile,boot_quiet_remaining_s,ready_reconfirm_pending,ready_reconfirm_failed,recommended_action,human_attached}`
   - `cmd submit`：`cmd_id`、`error_code`
   - `cmd status`：`command.{status,stdout,error_code}`
   - `cmd result-tail`：`status`、`chunks[]`、`next_chunk`
   - `session console-list`：`consoles[].{client_id,interactive_owner}`
   - `session console-attach`：`client_id`、`vtty`
   - `session interactive-open`：`interactive_id`、`soft_preempted`
   - `session activity`：`session.{activity_classification,rx_bytes_last_10s,last_rx_age_s,last_tx_age_s,last_error_detail}`
   - `session self-test`：`probe_ok`、`classification`
   - `log tail-raw`：`ok`、`records[].{seq,dir,source,payload_b64}`、`truncated`、`wal_file_exists`
   - `log tail-text`：`lines`
   - `wal current-seq`：`seq`
   - `wal export`：`records`
   - `remote status`：`ok`、`tunnels[]`
   - `device attach`：`attached_real_path`、`error_code=DEVICE_STILL_HELD`
7. **WAL 檔案格式依賴**：`p1-wal-reset` 直接讀 `wal_path` 檔尾一行 JSON 的 `seq`；`p0-wal-live` 讀 `~/.local/state/serialwrap/wal/raw.mirror.log` 的文字並要求含 marker；`regression ThrowawayDaemon.wal_text()` 讀 `workdir/wal/*` 全文。

---

## 8. 規格與程式碼的差異

| # | 規格主張（位置） | 程式碼實況 |
|---|---|---|
| D-1 | L2 有「12 family、37 case」（§11.1、§15.1、§11.6 Slice 2.4「全部適用 37 個既有案例」） | registry 實際載入 **38** 個 case。F9 有 6 個，`f9-brcm-spaced-prompt-reboot-login`（#224）是後加的；family 確實是 12 個 |
| D-2 | `realhw` 有「P0×8、P1×20、remote×7、longrun×1，共 36 個」（§15.1、§11.6「L3 36 case」） | 實際 **remote=8**（rm-topo 5 個、rm-live 3 個），合計 **37**。`docs/func-test/realhw-stability-checklist.md:167` 也寫「7 條」，已過時；`realhw/cases/__init__.py` docstring 寫的「remote×8」才正確 |
| D-3 | `uart_harness.py`「remote 三拓樸驗收」（§3.2、§15.1、§15.5） | `remote_tunnel_test.sh` 實際有 **4 個拓樸**（direct、nat_host、dual_nat、agent_pull）加 1 組 gwports 額外驗收。腳本 log 字串仍寫「三拓樸」，`realhw/cases/remote.py:3` docstring 寫「rm-topo ×4」，實際 5 個 |
| D-4 | 「既有 pytest 保留以下分類」`core_portable`／`posix_only`／`windows_native`（§11.2） | repo 中**沒有**這些 marker，也沒有 resource marker；平台區分只靠零散的 `skipif`／`skipUnless`（§1.5 F）。CI 只有 Linux |
| D-5 | Python pytest「數量接近 1.7k」（§11.1） | 實際收集 1984 項；CLAUDE.md 的「1636 passed, 16 skipped」也已過時 |
| D-6 | `f4-background-result-tail-consistent` 是「紅燈哨兵」，可登記為 XFAIL（§11.4、§15.10） | 程式碼沒有 XFAIL 機制（verdict 只有 PASS／FAIL／SKIP）。case hint 與 CHANGELOG:454 都記載 #159 已修、此 case 是「常駐回歸防線」，預期為綠；只有 `docs/regression-plugin.md:91` 仍寫「FAIL（刻意保留的紅燈哨兵）」。三方記載互相矛盾 |
| D-7 | F12 改以讀回新增 WAL 紀錄、marker、CRC 判定（§8.3）；OD-13 寫「seq 前進＋檔案大小或讀回最後一筆」 | 現行只比 mtime（§3.9），marker 送出後從未被比對。§8.3 明言「背景雜訊造成的 seq 前進不足以證明」，而 OD-13 把「seq 前進＋檔案大小」列為可接受，兩者標準不一致。另外 **`p0-wal-live`（L3）同樣是 mtime oracle**，且讀 mirror log，spec 沒有列入修訂範圍 |
| D-8 | DaemonHarness「沿用暫存 profile／state／run 目錄與 by-id sandbox 概念」（§15.1、§15.5） | DaemonHarness 沒有隔離 WAL_DIR、CONFIG_DIR、EVENTS_DIR、LOG_DIR，也沒有 pop ENDPOINT／PROFILE_DIR。`ev-01` 會寫入 live `~/.serialwrap/events.d`，還用固定的 `/tmp/sw-ev-test-*` 路徑。不能指定 artifact |
| D-9 | 「既有 13 個 func-test case 的 ID、目的與 oracle 必須保留」（§15.5） | 13 個 case 確實存在，但 `cowork-01` 用了未實作的 action（必定 FAIL）；`hk-01`／`hk-04` 斷言不存在的 `has_interactive` 欄位（必定 FAIL）；`input_raw` 與 `any_tx_contains_raw` 沒有實作，`hk-01`／`hk-03` 的核心 oracle 實際是空的；`sm-04` 沒有驗 DETACHED。也就是「保留 oracle」之前，部分 oracle 本身就不成立。func-test 沒有 CI，README 也沒列出 `shell`、`target_emit`、`wait_for_file` |
| D-10 | Stage 1B L2「以 Rust CLI 作為 pinned `serialwrap_exe`」、L3「以 Rust CLI 執行 P0 與相關 P1」（§11.6） | 只有 regression 有 pin。realhw 與 reliability 都走 PATH 上的 `serialwrap`，**沒有 pin，也沒有 #154 版本 gate**，只檢查 `MIN_DEPLOYED=(0,2,3)` |
| D-11 | 版本閘門：「`daemon status.version` 與 pinned CLI `--version` 比對；不一致整場拒跑」（§15.2） | regression 確實如此實作。但 `daemon_version_probe` 在 version 欄位缺席時會改讀 `/proc/<pid>/cmdline` 並執行 python `importlib.metadata`，這條 fallback 對 Rust daemon 無效（只回空字串，最後判「版本解析失敗」） |
| D-12 | `lr-mixed` 的 Human「每 2～5 分鐘輸入 `HUMAN_TICK`」、snapshot「記錄 session、RSS 與 PID」（§15.11.1） | 與程式碼相符。另有規格沒提到的細節：background 動作只要回了 `cmd_id` 就計為 done，不追終態；interactive 遇到 `SESSION_INTERACTIVE_BUSY` 記為 `busy`，不進分母；PID 只取 system-scope systemd，user scope 或 on-demand 下第一個 snapshot 就會被判 `daemon_death` |
| D-13 | Windows bench lock「須由 testbed 明確解析同一個共用 lock 絕對路徑」（§15.2） | Windows preflight 寫死 `Path.home()/.local/state/serialwrap/bench.lock`（`regression/serialwrap_regression/preflight.py:114`），沒有 testbed 設定項。realhw `acquire_benchlock` 在 Windows 直接 raise |
| D-14 | 既有 Windows L2 主要只有 `f9-brcm-spaced-prompt-reboot-login` 可執行（§15.8.4） | 與程式碼相符（`plugin.py:176`），而且其餘 case 在 Windows 上是**硬性 SKIP**（`windows_case_unsupported`），不是依能力判斷 |
| D-15 | `realhw/acceptance/longrun_acceptance.yaml`、`tests/acceptance/*`、`tests/contract_blackbox/`、`contracts/*`、`tools/uart_stdio_bridge/` | 都不存在（spec §3.2 已註明是目標結構，此處確認目前全無） |
| D-16 | 新增觀測「不得使用會延長 Lease 的 `interactive-status`」（§15.11.3） | 既有 `p1-cmd-modes`（`realhw/cases/p1_cmd.py:115`）會呼叫 `session interactive-status`。它屬於功能 case，不是觀測用途，但會影響 lease，值得標註 |
| D-17 | §15.1 說 `serialwrap_reliability` 是「同一 `realhw` 引擎的 TestPilot 第二前端，不複製測試邏輯」 | 屬實，但兩個前端預設不同：reliability 的 testbed `longrun.duration` 是 `"15m"`，`python3 -m realhw` 的 `--duration` 預設是 `"32h"`；reliability 未點名時排除 destructive，regression 則跑全套並以 gate 記 SKIP |
| D-18 | CLAUDE.md、conftest docstring 寫「8 檔 per-file 隔離」 | 實際有 13 檔使用 `state_iso` |
