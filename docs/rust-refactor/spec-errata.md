# 定稿規格與盤點勘誤表（spec-errata.md）

本文件記錄 serialwrap 重構至 Rust 期間發現的規格（`serialwrap-rust-spec-final.md`）與盤點文件（`docs/rust-refactor/inventory/*.md`）勘誤。
依主計畫與政策規範，`serialwrap-rust-spec-final.md` 定稿本文維持唯讀，所有與 Python 基線事實不符之處統一於本文件彙整，供擁有者於下一個 spec revision 審查併入。

- 建立者：P0-T7
- 規範依據：`docs/rust-refactor/plans/01-stage0-contracts.md` §Task T7、§Task T15、`docs/rust-refactor/plans/spec-reconciliation.md`

---

## 盤點勘誤（Inventory Errata）

本節記錄在撰寫基線特徵化測試（P0-T7）與契約比對期間發現的盤點文件描述差異。所有行為皆以 Python 實際執行結果為準。

### INV-ERR-01：`TUNNEL_STATUS_ERROR` 觸發路徑

- **涉及檔案**：`docs/rust-refactor/inventory/03-errors.md` §3.4 L190、`docs/rust-refactor/inventory/01-cli.md` §6.11 步驟 3、§6.12 步驟 4、§6.13
- **盤點原文描述與歧義**：盤點內文 `01-cli.md` §6.11 步驟 3 明載 `serialwrap remote status` 直接印 `rt.status(run_dir)` 且 exit 0，§6.12 步驟 4 與 §6.13 將 `TUNNEL_STATUS_ERROR` 歸於 `connect --close` 與 `benches`。但在 `03-errors.md` §3.4 L190 表格簡述「remote status 回應異常」易被誤讀為 `remote status` 子命令，且計畫 `01-stage0-contracts.md` §Task T7 準備表誤寫為 patch status 後執行 `remote status`。
- **Python 基線事實**：`serialwrap remote status` 為直接透傳且 exit 0；真正經 `_status_tunnels` 檢查並在狀態損毀（如回應非 dict、ok 為 false、或缺少 `tunnels` 清單）時拋出 `TunnelError("TUNNEL_STATUS_ERROR")` 的 CLI 命令為 `serialwrap benches`（`_run_benches`）與 `serialwrap connect --close`。
- **特徵化測試**：`tests/test_contracts_gap_codes.py::TestGapRemoteCli::test_tunnel_status_error_malformed` 透過 `cli.main(["benches"])` 驗證此碼之觸發與錯誤輸出。

### INV-ERR-02：`DEVICE_NOT_BOUND` 正常路徑不可達（僅防禦性分支）

- **涉及檔案**：`docs/rust-refactor/inventory/03-errors.md` §3.1、`sw_core/session_manager.py:717-719, 2146`
- **Python 基線事實**：`SessionManager.__init__` 對未設 `device_by_id` 之 profile 直接略過不建立 session；建立 session 之處（`:722`、`:986`）與所有 attach/rebind 路徑（`:720`、`:2077`、`:2561`、`:2820`）皆帶有非空 `device_by_id`，且系統無 unbind 途徑。正常執行下 `s.profile.device_by_id is None` 分支不可達，目錄記載之 agent_action「先以 session.bind 綁定裝置」無從適用。特徵化測試需以合成替換 profile 之方式觸發。

### INV-ERR-03：`TUNNEL_STATUS_ERROR` 在未注入異常下不可達

- **涉及檔案**：`docs/rust-refactor/inventory/03-errors.md` §3.4、`sw_core/remote_tunnel.py:500-530`、`sw_core/cli.py:1262-1277`
- **Python 基線事實**：`remote_tunnel.status` 於正常無例外時恆回傳 `{"ok": True, "tunnels": [...]}` 字典；`_status_tunnels` 之非 dict、not ok、缺 tunnels 三分支在無外部狀態檔損毀或測試替身注入時無法直接觸發，與 `SSH_NOT_FOUND` 同屬防禦性健全檢查。

### INV-ERR-04：`UNKNOWN_BENCH_CODE`、`INVALID_BENCH_CONFIG`、`TUNNEL_STATUS_ERROR` 在 Windows 基線不可達

- **涉及檔案**：`docs/rust-refactor/inventory/01-cli.md` §6.11-6.13、`sw_core/cli.py:1334, 1464`、`sw_core/remote_tunnel.py:10`
- **Python 基線事實**：Python 基線在 Windows 下 `_run_connect` 與 `_run_benches` 均在最開頭檢查 `os.name == "nt"` 並直接回傳 `REMOTE_NOT_SUPPORTED`（exit 1），且 `remote_tunnel.py` 頂層依賴 POSIX 的 `fcntl`。因此在 Python 基線之 Windows 環境此三碼不可達，特徵化測試歸入 `posix_only`。

---

## 規格勘誤（Spec Errata）

> 本節由 P0-T15 依調和表（`spec-reconciliation.md`）指派之列填寫。

### T8-ERR-01：`device.list` 非副作用方法（計畫 T8 前提錯誤）

- **來源**：`docs/rust-refactor/plans/01-stage0-contracts.md` Task T8 的 `test_side_effects_declared_for_readonly_whitelist` 列與「`device.list` 的 prune」前提。
- **事實**：`SessionManager.list_devices()`（`sw_core/session_manager.py`）僅於鎖內對 `_devices` 做排序快照，不 prune、不寫狀態；`_dispatch` 的 `device.list` 分支直接回傳該結果。因此 `device.list` 無副作用。
- **處置**：`contracts/rpc_schema.json` 的 `x-methods.device.list.side_effects` 記為 `[]`，不自行發明效果。
- **待計畫擁有者確認**：`test_side_effects_declared_for_readonly_whitelist` 的 `device.list` 斷言由「非空且 idempotent」改為「`side_effects == []`」；此列屬契約測試，修改需獨立審閱（§10.4）。

### CLI-11.01：全域選項另有 `--version`、`-v/--verbose` 與 `SERIALWRAP_LOG_LEVEL`

- **規格章節**：§4.2 第 1 節「全域選項」。
- **規格原文摘要**：全域選項只列 `--socket`、`--endpoint`、`--bench`、`--timeout`、`--retries` 五個。
- **Python 基線事實**：另有 `--version`、`-v/--verbose`（count，對應 CLI trace）與環境變數 `SERIALWRAP_LOG_LEVEL`（盤點 `docs/rust-refactor/inventory/01-cli.md` §11 第 1 項；`sw_core/cli.py:1908-1950`）。
- **處置**：KEEP＋DOC，現況即契約。
- **契約對應**：`contracts/cli_schema.json` 的 `x-global-options` 收錄 `--version`、`-v/--verbose`，`x-env` 收錄 `SERIALWRAP_LOG_LEVEL`；由 `tests/test_contracts_cli_schema.py::test_global_options_match_build_parser` 比對 parser。

### CLI-11.03：重試行為與規格舉例的方法名有誤

- **規格章節**：§4.2 第 1 節（`--retries` 白名單與不重試舉例）。
- **規格原文摘要**：不重試舉例列出 `cmd.status`、`cmd.result_tail` 等方法名。
- **Python 基線事實**：白名單 11 個方法與程式一致；重試只對 transient 碼 `TIMEOUT`、`SOCKET_ERROR`、`EMPTY_RESPONSE` 生效（`sw_core/client.py` `_RETRYABLE_TRANSIENT_ERROR_CODES`），退避 0.5×2ⁿ、單次上限 5 s；TIMEOUT 會補 `daemon_reachable`、`daemon_busy`；`event.*` 的 dispatch 不轉送 `--retries`（盤點 `docs/rust-refactor/inventory/01-cli.md` §11 第 3 項）。規格舉例的正確方法名為 `command.get`、`command.result_tail`。
- **處置**：KEEP＋DOC（基線；規格勘誤方法名）。
- **契約對應**：`contracts/cli_schema.json` 的 `x-retry`（`methods`、`transient_codes`、`backoff_base_s`、`backoff_max_s`）；`tests/test_contracts_cli_schema.py::test_retry_methods_match_client`。

### CLI-11.11：`connect` 沒有子命令

- **規格章節**：§4.2 指令群組表（`connect` 群組）。
- **規格原文摘要**：`connect` 被寫成「遠端／測試台連線輔助子命令」，帶子命令。
- **Python 基線事實**：實際為 `connect <code> [--close]`，只有一個 positional 加一個旗標，沒有子命令（盤點 `docs/rust-refactor/inventory/01-cli.md` §11 第 11 項；§6.12）。
- **處置**：KEEP＋DOC（基線）。
- **契約對應**：`contracts/cli_schema.json` 的 `x-groups` 中 `connect` 群組 `subcommands: null`；`tests/test_contracts_cli_schema.py::test_every_subcommand_options_match`。

### CLI-11.13：多開偵測位於 daemon 的 `health()`

- **規格章節**：§4.2（`daemon status` 描述）與 §9.4 占用欄位。
- **規格原文摘要**：§4.2 第 14 項 `daemon` 群組原文為「涉及本地行程管理、多開偵測（`DAEMON_ALREADY_RUNNING_ELSEWHERE`）與監管轉送」（`serialwrap-rust-spec-final.md:420`）。
- **Python 基線事實**：CLI 只送 `health.status`；多開偵測在 daemon 端 `health()` 內完成。CLI 端的 `/proc` 多開檢查只存在於 `daemon start` 的 spawn 防線（盤點 `docs/rust-refactor/inventory/01-cli.md` §11 第 13 項）。
- **處置**：KEEP＋DOC（基線）。
- **契約對應**：`contracts/rpc_schema.json`（`health.status` 結果）；`tests/test_contracts_rpc_schema.py::test_foreign_holders_includes_self_pid`。

### RPC-D-03：`stream` 不是真串流

- **規格章節**：§4.2 第 8 項 `stream`（「RPC 串流讀取」）。
- **規格原文摘要**：`stream` 群組為即時串流讀取。
- **Python 基線事實**：不存在 streaming RPC；`serialwrap stream tail` 只發一次 `result.tail`（無 `cmd_id` 的 legacy 分支，range 模式），一請求一回應（盤點 `docs/rust-refactor/inventory/02-rpc.md` §10 D-03）。
- **處置**：DOC。
- **契約對應**：`contracts/rpc_schema.json` 的 `x-methods` 無串流方法；本節為規格修訂依據。

### RPC-D-04：RPC 方法名有誤且規格漏列方法

- **規格章節**：§4.2 方法名舉例。
- **規格原文摘要**：寫 `cmd.status`、`cmd.result_tail`，漏列多個方法。
- **Python 基線事實**：實際 RPC 名為 `command.get`、`command.result_tail`（`cmd status` 只是 CLI 子命令名）；另有 `result.tail`、`daemon.stop`、`session.get_state`、`wal.current_seq`、`event.*` 等（盤點 `docs/rust-refactor/inventory/02-rpc.md` §7 與 §10 D-04）。方法總數 54。
- **處置**：DOC；以 `docs/rust-refactor/inventory/02-rpc.md` §7 的 54 個方法為準。
- **契約對應**：`contracts/rpc_schema.json` 的 `x-methods`；`tests/test_contracts_rpc_schema.py::test_methods_equal_dispatch_plus_daemon_stop`。

### RPC-D-09：`truncated` 只存在於 `log.tail_*`

- **規格章節**：§8.3、§9.4（WAL 查詢 range、分頁、`truncated=true`）。
- **規格原文摘要**：`wal.range` 以 `truncated=true` 表示仍有下一頁。
- **Python 基線事實**：`truncated` 只存在於 `log.tail_raw`、`log.tail_text`；`wal.range` 結果只有 `ok`、`records`、`wal_path`、`wal_file_exists`、`available_from_seq`、`rotated_out`（盤點 `docs/rust-refactor/inventory/02-rpc.md` §10 D-09）。
- **處置**：KEEP＋DOC；`wal.range` 以回傳筆數是否等於 `limit` 判斷是否有下一頁。
- **契約對應**：`contracts/rpc_schema.json` 的 `x-methods["wal.range"]`；`tests/test_contracts_rpc_schema.py::test_wal_range_to_seq_filters_locally`。

### RPC-D-12：白名單方法並非零副作用

- **規格章節**：§4.1（`sw_core/client.py` 11 方法重試白名單，稱為唯讀）。
- **規格原文摘要**：11 個白名單方法稱為唯讀。
- **Python 基線事實**：`session.list`、`health.status`、`session.console_list` 會 prune stale console；`session.get_state`、`session.activity` 會推進 `expire_ready_reconfirm`。皆冪等，但並非零副作用（盤點 `docs/rust-refactor/inventory/02-rpc.md` §10 D-12）。
- **處置**：KEEP＋DOC（基線）。
- **契約對應**：`contracts/rpc_schema.json` 各方法的 `side_effects` 欄位；`tests/test_contracts_rpc_schema.py::test_side_effects_declared_for_readonly_whitelist`。

### ERR-7.3-3：`FLUSHED_BY_RECOVERY` 的觸發面比規格寬

- **規格章節**：§5.3 狀態轉移表（「Recovery 清除未執行命令」列，`serialwrap-rust-spec-final.md:582`）、§9.2 表一（`FLUSHED_BY_RECOVERY` 既有常數碼）；盤點 `docs/rust-refactor/inventory/03-errors.md` §7.3 第 3 項。
- **規格原文摘要**：`Recovery 清除未執行命令` 的結果為 `FLUSHED_BY_RECOVERY`（規格把觸發面限定於 recovery）。
- **Python 基線事實**：任何 detach 都會經 `_on_detached` 呼叫 `arbiter.unregister_session`，以 `FLUSHED_BY_RECOVERY` 終結排隊命令（盤點 `docs/rust-refactor/inventory/03-errors.md` `FLUSHED_BY_RECOVERY` 條目）。
- **處置**：KEEP＋DOC（基線）。
- **契約對應**：`contracts/error_codes.yaml` 的 `FLUSHED_BY_RECOVERY` 條目，`meaning` 寫明「recovery 或 detach」。

### WAL-D1：`crc32` 是 8 位小寫 hex 字串

- **規格章節**：§8.1 WAL 紀錄欄位表（`crc32` 型別 `u32`）。
- **規格原文摘要**：`crc32` 型別為 `u32`。
- **Python 基線事實**：`crc32` 為 `f"{crc32:08x}"` 產生的 8 位小寫十六進位字串，不是 JSON 數字；空 payload 為 `"00000000"`（盤點 `docs/rust-refactor/inventory/04-wal-state.md` §0 第 2 項、§1.3 欄位表 `crc32` 列，以及 diff 表 D1）。
- **處置**：KEEP＋DOC。
- **契約對應**：`contracts/wal_record.schema.json` 的 `crc32` 以 `^[0-9a-f]{8}$` 定義；`tests/test_contracts_wal_state_schema.py::test_wal_crc32_uppercase_rejected`。

### WAL-D4：`wal.range` 沒有 `truncated`

- **規格章節**：§8.3。
- **規格原文摘要**：WAL range 的 `truncated=true` 表示仍有下一頁。
- **Python 基線事實**：同 RPC-D-09，`wal.range` 結果不含 `truncated`。
- **處置**：KEEP＋DOC。
- **契約對應**：`contracts/rpc_schema.json` 的 `x-methods["wal.range"]`；`tests/test_contracts_rpc_schema.py::test_wal_range_to_seq_filters_locally`。

### UIO-12.7：console PTY 沒有 symlink

- **規格章節**：§7.1 POSIX PTY（「保留既有 PTY Console、穩定 symlink」）。
- **規格原文摘要**：規格提及穩定 symlink。
- **Python 基線事實**：console 的 vtty 直接是 `/dev/pts/N`（`os.ttyname`），沒有 symlink；穩定性來自 `PreservedConsoles` 跨 bridge 重建時保留同一 PTY。只有 `/dev/ttyMCU` 有 symlink（盤點 `docs/rust-refactor/inventory/06-uartio-console.md` §12.7）。
- **處置**：KEEP＋DOC。規格的 symlink 只指 `ttyMCU`，console 穩定性靠 `PreservedConsoles`。
- **契約對應**：本節為文件勘誤依據；`/dev/ttyMCU` 端點的契約由 flash 相關條目承接（盤點 `docs/rust-refactor/inventory/06-uartio-console.md` §9）。

### TST-D-1：L2 實際為 38 個 case

- **規格章節**：§11.1、§15.1、§11.6（Slice 2.4「全部適用 37 個既有案例」）。
- **規格原文摘要**：L2 有 12 family、37 case。
- **Python 基線事實**：registry 實際載入 38 個 case；F9 有 6 個，`f9-brcm-spaced-prompt-reboot-login`（#224）為後加。family 數確為 12（盤點 `docs/rust-refactor/inventory/08-tests-assets.md` §D-1、§3.8）。
- **處置**：DOC；承接清單以 38 為準。
- **契約對應**：本節為承接清單展開依據，P2 測試基礎據此展開。

### TST-D-2：realhw 實際為 37 個 case

- **規格章節**：§15.1、§11.6（「L3 36 case」）。
- **規格原文摘要**：`realhw` 有 P0×8、P1×20、remote×7、longrun×1，共 36 個。
- **Python 基線事實**：實際 remote 為 8（rm-topo 5 個、rm-live 3 個），合計 37（盤點 `docs/rust-refactor/inventory/08-tests-assets.md` §D-2、§4.6）。
- **處置**：DOC；同 TST-D-1，以 37 為準。
- **契約對應**：本節為承接清單展開依據。

### TST-D-3：remote 拓樸為 4 種加 gwports

- **規格章節**：§3.2、§15.1、§15.5（`tools/docker/uart_harness.py` 的 remote 三拓樸驗收）。
- **規格原文摘要**：remote 驗收為三拓樸。
- **Python 基線事實**：`tools/docker/remote_tunnel_test.sh` 有 4 個拓樸（direct、nat_host、dual_nat、agent_pull）加 1 組 gwports 額外驗收；腳本 log 字串仍寫「三拓樸」（盤點 `docs/rust-refactor/inventory/08-tests-assets.md` §D-3）。
- **處置**：DOC。
- **契約對應**：本節為承接清單展開依據。

### EVT-D-S2：常數碼的分布與規格描述不符

- **規格章節**：§9.2（「6 個既有常數碼」）。
- **規格原文摘要**：既有錯誤碼中有 6 個為常數碼。
- **Python 基線事實**：具名常數共 4 個——`sw_core/constants.py` 的 `ERROR_AUTOBOOT_QUIET`、`ERROR_READY_UNCONFIRMED`，以及 `sw_core/arbiter.py:20-21` 的 `ERROR_CMD_TOO_LONG`、`ERROR_CMD_CONTAINS_NEWLINE`；其餘 2 個為字面值：`DAEMON_ALREADY_RUNNING`（如 `sw_core/lock_posix.py`）與 `FLUSHED_BY_RECOVERY`（`sw_core/arbiter.py` 預設參數）。`ERROR_CREDENTIALS_UNRESOLVED`、`ERROR_RX_FLOOD`、`ERROR_TRANSPORT_STALL`、`ERROR_BOOTLOADER_STUCK` 屬 `last_error` 或分類（盤點 `docs/rust-refactor/inventory/09-events-transfer.md` §D-S2、`docs/rust-refactor/inventory/03-errors.md` 開頭註記）。
- **處置**：KEEP＋DOC。
- **契約對應**：`contracts/error_codes.yaml` 以字面碼登錄全部 RPC／CLI 碼；`contracts/session_last_errors.yaml` 承接 last_error 值。

### EVT-D-S8：`stream` 是一次性請求

- **規格章節**：§4.2 第 8 項。
- **規格原文摘要**：`stream` 為即時串流讀取。
- **Python 基線事實**：只有 `stream tail`，為一次性請求回應（RPC `result.tail` legacy 路徑，range 模式、無 metadata、無 `WAL_MISSING` 檢查）；無 follow 或 subscribe（盤點 `docs/rust-refactor/inventory/09-events-transfer.md` §D-S8）。
- **處置**：KEEP＋DOC。
- **契約對應**：同 RPC-D-03，`contracts/rpc_schema.json` 無串流方法。

### EVT-D-S9：`log` 群組只有 tail

- **規格章節**：§4.2 第 7 項。
- **規格原文摘要**：`log` 群組為「所有日誌操作子命令」。
- **Python 基線事實**：只有 `log tail-raw`、`log tail-text`（讀 WAL）；agent log capture 在 `session log-start`、`session log-stop`、`session log-status`（盤點 `docs/rust-refactor/inventory/09-events-transfer.md` §D-S9）。
- **處置**：KEEP＋DOC。
- **契約對應**：`contracts/cli_schema.json` 的 `x-groups` 中 `log` 群組。

### GAP-X1：`SERIALWRAP_TCP_PORT` 為空字串時崩潰

- **規格章節**：盤點 `docs/rust-refactor/inventory/01-cli.md` §4.1 與 `docs/rust-refactor/inventory/07-platform-mgmt.md` §0 的主張。
- **規格原文摘要**：`DEFAULT_TCP_PORT = int($SERIALWRAP_TCP_PORT or "48700")`，空字串會退回 48700。
- **Python 基線事實**：`sw_core/constants.py:47` 為 `int(os.environ.get("SERIALWRAP_TCP_PORT", "48700"))`；空字串在 import 時 `ValueError`，CLI 與 daemon 皆 traceback 並 exit 1，不會退回 48700（盤點 `docs/rust-refactor/inventory/10-gaps.md` §3 X1，裁定 B 正確）。
- **處置**：DOC＋KEEP；Rust 保留 exit 1。
- **契約對應**：`tests/test_contracts_keep_baseline.py::test_tcp_port_empty_env_import_fails`（子程序以空字串 `SERIALWRAP_TCP_PORT` import `sw_core.constants`，斷言 exit 1 且 stderr 含 `ValueError`）；不新增契約欄位。

### GAP-X2：spawn argv 描述有矛盾

- **規格章節**：盤點 `docs/rust-refactor/inventory/02-rpc.md` §3.1 的主張（CLI 也以 `-m sw_core.daemon` 啟動）。
- **規格原文摘要**：CLI 以 `[sys.executable, "-m", "sw_core.daemon"]` 或同層 `serialwrapd.exe` 啟動。
- **Python 基線事實**：POSIX 固定 `[sys.executable, _daemon_script_path()]`（`sw_core/cli.py:538`）；`-m` 只在 Windows 非 frozen 且 repo 無 `serialwrapd.py` 時使用（`sw_core/cli.py:526`）。盤點 `docs/rust-refactor/inventory/10-gaps.md` §3 X2 裁定 01、07 正確。
- **處置**：DOC；依原始碼。
- **契約對應**：本節為唯一記錄；POSIX spawn 的風險見盤點 `docs/rust-refactor/inventory/01-cli.md` §7.4。

### GAP-X3：`USER_ENV_MISSING` 不可達

- **規格章節**：盤點 `docs/rust-refactor/inventory/05-session.md` §16.5、`docs/rust-refactor/inventory/02-rpc.md` §8.3 的主張。
- **規格原文摘要**：`USER_ENV_MISSING` 列為會產生的 `last_error` 值。
- **Python 基線事實**：`sw_core/login_fsm.py:208-216` 的 `err` 之後被 `_wait_or_fail` 的回傳覆寫，從未回出；全 repo 只有 `_maybe_login` 呼叫 `_resolve_login_user`。對外不可達（盤點 `docs/rust-refactor/inventory/03-errors.md` §5.2、§7.5；`docs/rust-refactor/inventory/10-gaps.md` §3 X3 裁定 03 正確）。
- **處置**：DOC；不可達。
- **契約對應**：`contracts/session_last_errors.yaml` 的 `USER_ENV_MISSING` 標 `reachable: false`，`unreachable_reason` 指向本節。

### GAP-X4：`session list` 欄位歸屬有矛盾

- **規格章節**：盤點 `docs/rust-refactor/inventory/08-tests-assets.md` §7.6 的主張。
- **規格原文摘要**：L2／L3 依賴的 `session list` 之 `sessions[]` 含 `recommended_action`、`human_attached`。
- **Python 基線事實**：`SessionPublic`（`to_public_dict`）沒有這兩個鍵。`human_attached` 由 `session self-test` 回應頂層讀取（LeaseContext 展開）；`recommended_action` 由 `command.submit`（READY_UNCONFIRMED）、`session.self_test`、`session.recover` 回應提供（盤點 `docs/rust-refactor/inventory/10-gaps.md` §3 X4 裁定 02、05 正確）。
- **處置**：DOC；`human_attached` 屬 self-test，`recommended_action` 屬 submit、self-test、recover。
- **契約對應**：`tests/test_contracts_rpc_schema.py::test_session_public_order_matches_runtime`（`SessionPublic` 欄位順序與執行期一致）。
