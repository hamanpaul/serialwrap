# P5 Slice 2.1：Rust Daemon Core 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付可取代 Python daemon 承接 F1／F2／F3／F4／F12 的 Rust `serialwrapd`（Slice 2.1）：PAL serial／UART 獨占／實例鎖／裝置來源／WAL 檔案層／RPC listener，sw-core 的 WAL（BC-01）、Arbiter、SessionActor 最小狀態機、Passthrough／Shell 最小登入與 `CREDENTIALS_UNRESOLVED` 終態、最小 Quiet Gate、TX 三態（BC-03）、Two-Phase Drain＋Release／Attach＋RELEASED 持久化、有界 mailbox／佇列、`state.json` 雙向相容、alias registry，以及 sw-daemon 的 framing／分派／daemon main。

**Architecture:** 依 `rust-architecture.md` §1.4 採 std thread＋有界 crossbeam channel；每個 session 是 sans-IO `SessionCore::on_event(ev, now) -> Effects` 加一條 `SessionActor` thread，I/O 只在 `IoWorker`（reader＋唯一 `TxWriter`）與 PAL worker 內進行，取消與收斂以 `Fence` 鎖線性化並以 `WorkerStoppedReceipt` 收尾。平台差異只出現在 `sw-pal`；業務 crate 透過 `sw_pal::Platform` 的 trait 物件與 `Capabilities` 取得能力。所有對外行為以 Python Stage 0 修正後基線（P1）為契約，Rust 與 Python 共用 `tests/contract_blackbox` 黑箱案例做 Sequential A/B。

**Tech Stack:** Rust 1.96.1（edition 2021；`x86_64-unknown-linux-gnu`、`x86_64-pc-windows-gnu`）；`crossbeam-channel =0.5.15`、`libc =0.2.186`（sw-pal unix）、`windows-sys =0.61.2`（sw-pal windows）、`crc32fast =1.5.0`、`indexmap =2.11.4`、`fancy-regex =0.16.2`（經 `sw_proto::pyre`）、dev `tempfile =3.23.0`；Python 3.10+／pytest（黑箱與既有套件）；P2 的 `tools/uart_stdio_bridge/` 與 Docker DUT 模擬器（L1.5）。

**Spec:** `docs/rust-refactor/serialwrap-rust-spec-final.md`（§5.1～§5.4、§6.2、§8.1～§8.3、§9.1、§11.6 Slice 2.1、§12.2 Stage 2、§15.6、§15.9）；`docs/rust-refactor/owner-decisions-2026-10-15.md`（OD-04、OD-08、OD-11、OD-12、OD-13、OD-14、OD-15）；`docs/rust-refactor/plans/00-master-plan.md`；`docs/rust-refactor/plans/rust-architecture.md`（§1～§6、§9）；`docs/rust-refactor/plans/spec-reconciliation.md`（SC／DF／KEEP 裁決）；Python 行為來源 `docs/rust-refactor/inventory/02-rpc.md`、`03-errors.md`、`04-wal-state.md`、`05-session.md`、`06-uartio-console.md`、`07-platform-mgmt.md`、`10-gaps.md`。

## Global Constraints

- 平台相依 production code 只在 `sw-pal`；其他 crate 禁止 `#[cfg(unix)]`／`#[cfg(windows)]`／`target_os`／`cfg!`／`cfg_attr` 平台分支與 build script 間接分支（§3.1）；允許 `#[cfg(test)]`、`#[cfg(feature = "test-fakes")]`。每個 Task 完成前跑 `python3 scripts/rust/check_cfg_boundary.py`。
- RPC／CLI `error_code` 與 session `last_error` 為兩個獨立命名空間（ADR-07、§9.3）；`CREDENTIALS_UNRESOLVED` 只屬後者（`sw_proto::session_last_errors::SessionLastError`）；`session.attach`／`session.recover` 不再把 last_error 值放進 `error_code`（SC-34，OD-14 #1）：未達 READY 時固定回 `SESSION_NOT_READY`，另以頂層 `last_error`（`Option<SessionLastError>`，序列化為字串或 null）與 `last_error_detail` 回報；不存在 `RpcErrorCode::LastErrorPassthrough`。
- 錯誤碼計數由 `contracts/error_codes.yaml` 資料產生，不得硬編碼 78（§9.2、OD-06）；本 Phase 不新增任何 `error_code`（`DRAIN_STALLED` 已由 P1-OC 登記於 `error_codes.yaml`，本 Phase 只輸出）。
- CLI JSON：`json.dumps(..., ensure_ascii=False, separators=(",", ":"))` 等價輸出；`state.json`／WAL 以 `sort_keys=True`（`sw_proto::pyjson` 的 `DISK_STABLE`）；RPC 回應用 `RPC_WIRE`，`INVALID_JSON`／`INVALID_REQUEST` 用 `RPC_EARLY`。
- 背壓上限：`CMD_PENDING_MAX=256`、`CMD_HISTORY_MAX=512`、`BG_CAPTURE_MAX_COUNT=64`、`DEFERRED_INPUT_MAX_BYTES=262144`、EventEngine LineBuffer `16384`、每 Console 出向 Ring `1 MiB`（§5.4）；Actor mailbox／RX／TX／WAL 待處理一律有界（control 256、data 1024 chunk×≤8192 B、TxWriter job 64，arch §5.10）。
- Drain timeout 3,000 ms → `RECOVERING`＋`drain_stalled:true`＋`drain_timeout_epoch`（取 `bridge_generation`，§5.2.5、SC-04）；`device.release` 逾時回 `DRAIN_STALLED`（OD-14 #4），drain 中的 attach／clear／bind／recover 仍回 `SESSION_RECOVERING`＋drain 欄位。
- Passthrough（SC-35，OD-14 #2）：`platform=passthrough` 不跑登入／ready_probe／boot quiet gate；`ATTACHED` 連續 `PASSTHROUGH_READY_QUIET_S = 30.0` 秒無 RX（任何 RX 重新起算）由 1 s `ControlEvent::Tick` 轉 `READY`，之後走一般 Arbiter；未命中完成條件不送 CTRL_C／CTRL_D、不降級。
- UDS 權限維持 Python 基線 `0o660`（SC-08 依 OD-14 #3 撤回；`SERIALWRAP_SOCKET_GROUP` 有值時 chgrp，目錄不設 0700）。
- Ready Reconfirm 耗盡：300 秒或 5 次失敗（§5.3）。
- WAL rotate 門檻 `R` 以 Stage 0 擷取的 Python 常數為準：`DEFAULT_WAL_ROTATE_BYTES=67108864`、條件 `size >= R`、寫入前檢查、WAL 與 mirror 各自判斷（§1.1.3、WAL-D7）。
- Windows 序列埠：`CreateFileW(OPEN_EXISTING, dwShareMode=0)`；WAL handle：`FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE`（§6.2、§8.2）。
- Windows RPC 預設 `tcp://127.0.0.1:48700`；信任範圍＝整機本機使用者（OD-12），規格與文件不得宣稱「同使用者隔離」。
- bridge 位置 `tools/uart_stdio_bridge/`，不得 import `sw_core`／連結 `sw-pal`（OD-07、§15.4.2）；本 Phase 只新增 `func-test/scenarios/s21-*.yaml` 情境檔，不改 bridge 工具。P2 runner（`func-test/runner.py`）缺少的動作（`external_open`／`external_close`、`measure_rpc_latency`）不在本 Phase 內順手加：以 Task 21a 的獨立測試資產 PR（§10.4，獨立審閱＋runner 自測）先行合併，Task 21 只消費。
- 記憶體 fake 通過不得計為原生 gate（OD-08、arch §9.1）；需要 COM pair 的 `cap_windows_com__*` 在未提供 `SW_TEST_COM_PAIR` 時保持 ignored 並標「證據不足」。
- OD-15：Spike 1 基本資格已在擁有者 host 通過（com0com 3.0.0.0、HVCI 執行中；pair `CNCA0=COM20`（serialwrap 端）／`CNCB0=COM21`（測試／bridge 端）；COM20 第二次開啟得 `ERROR_ACCESS_DENIED`；證據在擁有者 host 的 `b-log` 下 `rust-refactor/spike1/`）。因此原生 Windows COM handle gate（OD-08）改為**可執行 gate**：由 orchestrator 經 WSL interop 在擁有者 host 以 `SW_TEST_COM_PAIR=COM20:COM21` 執行，daemon 一律以 OD-11 隔離實例（`SERIALWRAP_STATE_DIR`／`_RUN_DIR`／`_CONFIG_DIR` 明確設定且非預設）＋`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20` 啟動，報告附 Windows build、HVCI 狀態與 pair 對應；只有擁有者 host 不可用時才記「證據不足」。Spike 1 剩餘項（控制線隔離、peer-close 語意）仍是結果未知的測試，不得預標 PASS。
- OD-15 docker 優先：L2／L3 案例在交擁有者 bench 前，先以 P2 Task 17 的 `scripts/docker/run-l15.sh`（serialwrap 容器 ⇄ bridge ⇄ DUT 模擬器容器、假 COM PTY pair）跑同一批情境，報告標 `evidence_layer: "L1.5-docker"`；docker 結果**不得**計為真板 L2、實機 L3 或 OD-10 bench 長跑，後三者仍為 BLOCKED-OWNER。systemd 監管行為先在 `scripts/docker/run-systemd.sh` 的 systemd container 驗證。
- repo 政策：不得 commit 到 `main`；本 Phase 在 `wt/rust-refactor/slice21-daemon-core` 分支工作，PR base 為 `feature/rust-refactor`（R-12）；每個 code PR 附 `changelog.d/<issue>-<slug>.md`，本 Phase 以追蹤鍵 `rust-s21` 命名（比照既有 `st06-*` 非數字鍵）；commit 用 Conventional Commits 繁中 subject，含 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer；完成前跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`。
- changelog fragment 格式：`policy_check.changelog.parse_fragment` 要求 YAML front matter，每個 fragment 一律寫成 `---\ntype: feat\nscope: <crate 或 tests>\n---\n<繁中本文>`（比照既有 `changelog.d/224-brcm-leading-whitespace.md`）；各 Task 標頭「Changelog fragment」只列檔名、`scope` 與本文。不得使用 `### Added` 標題格式（`collate` 會 raise `FragmentError`）。
- 測試資產修改（既有 `tests/*.py`、`func-test/` runner／scenario、`tests/contract_blackbox/bb_*.py`、P2 bridge 工具）依規格 §10.4 必須由非實作者獨立審閱並在 PR 描述列出；本計畫凡標「測試資產變更（§10.4）」的步驟皆適用。
- 需要 root 才會改變語意的測試（`TIOCEXCL` 在 `CAP_SYS_ADMIN` 下不生效、`chmod 000` 對 root 無效）：一律以 `geteuid()==0` 或具 `CAP_SYS_ADMIN` 判定後 skip，並在報告列為「證據不足」而非 PASS；另有一個專門的 `cap_sys_admin__tiocexcl_bypass_documented` 測試記錄 root 下的實際行為（§6.2 例外）。

## Review Focus

1. **daemon 在 release 的 `state.json` 提交完成後、回覆送出前被 kill -9**：重啟後該 session 必須仍是 `RELEASED`、不自動 attach、不開 tty；提交前被 kill 則依最後有效 `state.json` 恢復，該 session 不是 `RELEASED`、release 從未被確認（§6.2 交接）。測試加在 Task 19（`kill_between_commit_and_reply_keeps_released_after_restart`、`kill_before_commit_restores_previous_state`；hook 經 `DaemonHooks.{before_state_commit, after_state_commit}` 傳入 `StateStore::spawn_with_hooks`）。
2. **同一實體 tty 同時出現在 by-id 與 by-path，或 by-id symlink 在 session 已 attach 時改指到另一個 tty**：只能有一個 session、一個 handle；改指時先完整 drain 舊 handle（收到回條）才 `open_exclusive` 新路徑，drain 收尾時 session `last_error=DEVICE_REBOUND_REQUIRED`。測試加在 Task 5（realpath 去重）與 Task 16（`rebind_drains_before_reopen`，含 `last_error` 斷言）。
3. **WAL 目錄在 daemon 啟動時唯讀或磁碟滿**：daemon 仍啟動、`health.status.wal.healthy=false`、`unhealthy_reason` 非 null、`write_failures` 遞增、**所有** session 的登入 probe 等自動 TX 停止（全域 `WalHealthFanout` 同步每個 fence 的 `wal_unhealthy`，新建 session 繼承目前值），agent 命令 TX 照常寫出且 WAL 回傳 `loss_flag:true`，不得在記憶體堆積待寫紀錄。磁碟滿以「已開啟 handle 上連續兩次 append（含一次重試）失敗」判定，下一次成功即恢復。測試加在 Task 7（`open_failure_at_startup_marks_unhealthy`、`enospc_on_open_handle_trips_all_fences`、`new_session_inherits_wal_unhealthy`）、Task 13（`disk_full_blocks_login_probe_but_not_agent_cmd`）與 Task 19（`wal_dir_readonly_at_start_blocks_login_probe`、`wal_fault_after_ready_splits_agent_and_auto_tx`）。
4. **系統時間（wall clock）在執行中倒退或跳前一小時**：Quiet window、Ready Reconfirm deadline、Lease 到期、Drain 3000 ms、命令逾時、WAL identity 檢查節流全部只依 monotonic；只有 `wall_ts`／`*_at` 字串反映新 wall time，history 淘汰仍照 Python 以 `done_at` 字串排序（KEEP）。測試加在 Task 11（`wall_clock_jump_does_not_move_deadlines`；`wall_clock_jump_param` 參數化 wall ±1 h，分別斷言 quiet、reconfirm、lease、drain、command timeout 五個期限）與 Task 7（`identity_throttle_ignores_wall_jump`）。
5. **同一 RPC 連線 pipelining `session.recover --force`（最長約 10 s）後緊接 `daemon.stop`，且 client 在中途斷線**：handler 不因斷線取消；回應依請求順序寫出；`daemon.stop` 觸發的關機要等前一個 handler 收斂後依 SC-11 drain，不得在 handler 持有 session 時關閉 WAL；閒置連線不得阻擋 `RpcServer::stop`。測試加在 Task 17（`pipelined_stop_after_long_call`、`idle_connection_does_not_block_stop`、`half_line_and_unread_response_do_not_block_stop`）與 Task 19（`daemon_stop_during_force_recover_with_client_gone`，以真 daemon 驗證 WAL 只在 handler 結束後關閉且 exit 0 於上限內）。
6. **授權通過後、OS 提交前發生 Quiet／drain／lease 換手（R0-F1，規格 §5.2.2）**：授權重驗與 OS 提交必須在同一 Fence 臨界區（`FenceSubmitGate`，arch §4.2／§5.4 第 3 點），不得「鎖內驗證、放鎖、再 write」；鎖只跨非阻塞提交（POSIX 單次 `write(2)`、Windows `WriteFile` 發出），不跨 POLLOUT／`GetOverlappedResult` 等待。驗收：Task 9 `authorized_then_quiet_before_os_submit_is_zero_tx`、`authorized_then_drain_freeze_before_os_submit_is_zero_tx`、`authorized_then_lease_owner_gen_bump_before_os_submit_is_zero_tx`、`quiet_during_os_submit_waits_for_submit_and_counts_committed`、`quiet_between_partial_submits_stops_tail`、`os_submit_always_inside_fence_critical_section`。
7. **Passthrough 安靜升 READY 與命令路徑（SC-35，OD-14 #2）**：安靜視窗只以 monotonic 計、任何同 epoch RX 重新起算、TX 不重算；drain／flash／RELEASED 期間不得升 READY；升 READY 與命令執行全程不得送任何 probe、login、CTRL_C／CTRL_D；`prompt_regex` 可匹配空串（`.*`）時不得把它當完成條件（否則命令會在零輸出時「成功」）；passthrough 逾時不得轉 ATTACHED 或 flush 佇列。

---

## 0. 範圍界定與前置條件

### 0.1 本 Phase 承接（Slice 2.1）

| 面向 | 承接內容 | 依據 |
|---|---|---|
| PAL | POSIX／Windows serial（獨占開啟、可喚醒讀、可取消寫、TX 三態、`CloseToken`）、`UartExclusive::probe_holders`、Tier 1 `InstanceLockFactory`、`DeviceSource`／`DeviceWatcher`（#100 排序前提、Windows SERIALCOMM＋藍牙排除＋`exclude_coms`＋OD-11 allowlist）、`WalFs`、`RpcListenerFactory`、`SignalWait`、POSIX 哨兵 PTY（`create_pty` 最小版） | arch §4.2～§4.7、§4.9 |
| sw-core | `WalWriter`（BC-01、DF-03、DF-17）、`CommandRegistry`＋`SessionQueue`（SC-06）、`TxWriter`＋`Fence`（BC-03、SC-07）、`IoWorker` reader＋`RxWindow`、`SessionCore`／`SessionActor`／`Mailbox`／`Generations`、`drain`（SC-04）、Release／Attach（SC-05、DF-04）、最小 Login（shell／prpl；passthrough 不登入、30 s 安靜轉 READY，SC-35）＋`session_auth`＋`CREDENTIALS_UNRESOLVED`、`quiet_gate`（SC-03、DF-02；passthrough 不適用）、命令執行（line／background／completion_regex／reboot／CTRL_C·CTRL_D 救援）、agent-only Interactive Lease（SC-13 tick）、self-test、background／agent log capture、`StateStore`、`AliasRegistry`、`SessionManager`（rank #100、DF-14、動態 session、detect、selector、bind／pin／clear／recover） | arch §5 |
| sw-daemon | `framing.rs`、`service.rs` 分派（39 個 method 實作、15 個延後 method 回契約碼）、`health.rs`、`daemon_main.rs`（lock、signal、endpoint 寫入、SC-11 有序關機） | arch §6 |
| 測試 | Rust L1（FakeClock／FakeSerial／FakeWalFs）、PAL conformance（fakes＋`cap_posix_pty`＋`cap_windows_com`）、`tests/contract_blackbox/slice21/`、既有 BB-proc 套件改指 Rust、func-test 外部模式子集、L1.5 bridge 案例 | arch §9 |

### 0.2 本 Phase 不承接（fail-closed，全部列入 `tests/acceptance/slice21_scope.yaml`）

| 項目 | 2.1 行為 | 承接切片 |
|---|---|---|
| `session.console_attach`／`console_detach`／`console_list` | `{"ok":false,"error_code":"METHOD_NOT_FOUND","method":<m>,"version":<v>}`，無副作用 | 2.2 |
| `event.rule_set`／`rule_delete`／`rule_list`／`rule_get`／`com_enable`／`com_disable`／`com_status`／`reset`／`reload`／`tail` | 同上 | 2.3 |
| `file.push`／`file.pull` | 同上 | 2.4 |
| Profile 含非空 `post_login_cmd`（brcm-template）或 `console_unlock_env`（smart4R-profile） | 載入但標 `unsupported_in_slice21`：不參與 detect／fallback；explicit target 仍建 session、開 bridge（RX→WAL），但不送任何自動 TX，狀態停 `ATTACHED`、`last_error=None`，submit 回既有 `SESSION_NOT_READY`；daemon stderr 一次性 WARNING | 2.3 |
| 自動 reprobe（`_prepare_reprobe_locked` 的 attach 重試與 READY 重驗 probe） | 1 s tick 只做 `expire_ready_reconfirm`、lease 到期（SC-13）、drain timer 與 passthrough 安靜升 READY（SC-35，零 TX），不送 TX；READY＋pending 只經 attach probe、reboot recovery、`session.self_test` OK、`session.recover` 解除 | 2.3 |
| `TRANSPORT_STALL` 精煉、`/dev/ttyMCU`、flash probe | 不偵測；`mcu.status` 回 `flashing:false`、`flashing_com:null`、`last_detect:null`、`candidates` 照 Python 規則 | 2.3／2.4 |
| Human console（PTY attach、Telnet listener、Raw／Line、DeferredBuffer 回放、soft preempt） | POSIX 只建 internal 哨兵 primary PTY（keepalive＋RX fan-out），對外 `vtty`／`console_count` 與 Python 一致；Windows 不起 TCP listener，SessionPublic 輸出 `console_endpoint:null` | 2.2 |
| `sw_core::state_migrate`、`bench_registry` | 依 arch §1.3（WAL-D18／WAL-D19）分屬 `sw-cli`（P4）與 `sw-proto`（P3）；sw-core 不得出現這兩個模組 | P3／P4 |
| 普通外部 holder（未取 flock）的拒絕 | **已定案，不再待裁決**：規格 §6.2「拒絕已識別的外部衝突」與 OD-04 核准的 BC-04 涵蓋此情境；`TIOCEXCL` 不撤銷既有 handle，外部程式以普通 `open` 先開埠時 `open`→`flock`→`TIOCEXCL` 仍會成功（隔離 PTY 已重現），若放行即兩者同時讀寫 UART。P1 T10「非合作式外部 holder」凍結規則（02）已在 Python 基線 `_PosixSerialPort.open` 加鎖後 holder 重掃；本 Phase 的 `PosixSerialBackend::open_exclusive`（Task 2，arch §4.2）同樣在 TIOCEXCL 之後、termios 與任何 I/O 之前重掃，`pids` 非空 → `OpenError::Held`。適用初次自動 attach、熱插拔、PROBE、`device.attach`（**含 `force:true`**）。以 `s21-exclusive-conflict-plain-open` 判 PASS／FAIL（Task 21） | 2.1 |

### 0.3 前置交付（Consumes；Task 0 以編譯期與 pytest 檢查驗證）

| 來源 | 名稱與簽章 |
|---|---|
| P0 | `contracts/error_codes.yaml`、`contracts/session_last_errors.yaml`、`contracts/behavior_changes.yaml`（含 SC-01～SC-19（SC-08 已依 OD-14 撤回、不在檔內）、SC-34、SC-35、DF-02～DF-17、BC-01～BC-04；`pending_owner_confirmations: []`）、`contracts/rpc_schema.json`、`contracts/wal_record.schema.json`、`contracts/state_file.schema.json` |
| P1 | Python 基線已實作 BC-01～BC-04、SC-01～SC-07、SC-10、SC-11、SC-13、SC-19、DF-02～DF-04、DF-14、DF-15、DF-17；`contracts/golden/rpc/*.json`（Python 擷取的回應位元組） |
| P1-OC | `02b-stage0-owner-confirmations.md`（OD-14）：Python A 側基線已實作 SC-34（attach／recover 回 `SESSION_NOT_READY`＋頂層 `last_error`／`last_error_detail`）、SC-35（passthrough 30 s 安靜轉 READY 與命令路徑、`PASSTHROUGH_READY_QUIET_S`）、SC-04 修訂（release drain 逾時 `DRAIN_STALLED`）、SC-08 撤回（socket 維持 `0o660` 基線）；OD-14 確認簿記（DF-02、SC-26、SC-27 `confirmed`、`pending_owner_confirmations: []`）；P1-OC 重新擷取的 `contracts/golden/rpc/*.json` 為本 Phase 比對基準 |
| P2 | `tests/acceptance/{cases.yaml, cases.schema.json, cases.lock.json, regen_lock.py}`（P2 Task 12；欄位 `case_id`、`daemon_slice`、`necessity`、`prerequisites`、`invariants`、`behavior_changes`、`cross_slice_reason`；`lock["sets"]["slice_2.1"]`＝`necessity=="required"` 且 `daemon_slice ≤ 2.1`）；`func-test/runner.py`（`--mode pty|bridge|both`、`--dut-mode`、`--report-dir`、`--selftest-report`、`--scenarios`；`--case` 為**單值**，每次一個 case）與其 bridge／scenario 動作集合（`op_begin`／`op_end`、`bridge_control`（`pause_pump`／`resume_pump`／`transport_disconnect`／`transport_reconnect`）、`dut_control`（`emit`／`pause_responding`／`logical_reboot`）、`wait_wire_marker`、`assert_zero_tx`、`assert_wire_contiguous`、`assert_compare`、`wal_control`（`unlink_current`／`hold_reader`／`reset`）、`assert_wal_meta`、`assert_wal_marker`、`assert_command_resolved`，加上既有 `cli`／`wait_state`／`target_emit`）；DUT plan（`delay_reply`、`half_prompt`、`silent`、`stop_stdin`、`flood`）；`func-test/scenarios/l15-*.yaml`（§15.7.1 情境 11 筆，其中 `daemon_slice=2.1` 者 8 筆）；`func-test/lib/daemon_harness.py::{resolve_cli_exe, resolve_daemon_argv}`（只供 func-test）；bridge 資格報告 `selftest-report.json`（`uart-stdio-bridge selftest`） |
| P3 | `tests/ab_env.py::AB_ENV`（root conftest pop 之前擷取 `SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE`／`SERIALWRAP_AB_LABEL`）；`tests/contract_blackbox/bb_harness.py::{repo_root, ab_label, is_rust, cli_prefix, daemon_prefix, isolated_env(tmp, **overrides), run_cli, CliResult, NL}`、`bb_faketarget.FakeTarget(workdir, *, prompt, user, password)`（`.start(by_id_dir, name) -> Path`、`.rx_log() -> bytes`、`.stop()`；只模擬 `login:`／`Password:`／`echo X`／`^C`）、`bb_daemon.PyDaemon`、`bin/py-serialwrapd`；`sw_proto::{constants, error_codes::ErrorCode, session_last_errors::SessionLastError, pyjson::{PyValue, PyInt, PyDict, DumpOpts, Sep, loads, dumps, CLI_STDOUT, RPC_WIRE, RPC_EARLY, DISK_STABLE}, pydict!}`；`sw_proto::pycompat::{py_int_parse, py_float_parse, py_strip, py_bytes_strip, splitlines, universal_newlines, decode_utf8_replace, decode_utf8_strict, py_len_chars, py_truthy, PyOsError, sha256_hex}`、`sw_proto::pyfmt::{py_str, py_repr_str, py_repr_float}`；`sw_proto::pyre::{compile, PyFlags, PyRegex, valid_completion_regex}`；`sw_proto::config_types::{load_profiles(dir: &str, fs: &dyn ProfileFs), ProfileFs, LoadResult{profiles, templates, max_sessions}, ProfileTemplate, SessionProfile{com, act_no, alias, device_by_id, template}, UartProfile, RuntimeConfig{data}::{from_text, mode, socket_path}}`；`sw_proto::rpc_types::Method`；`sw_proto::runtime_paths::{RuntimePaths（String 欄位：config_dir, state_dir, run_dir, data_dir, lock_path, socket_path, default_tcp_port, default_endpoint, profile_dir, wal_dir, log_dir, config_yaml）::resolve, EnvSnapshot, NativeTraits, PathsError, resolve_config_path}`；`sw_proto::endpoint::{Endpoint{Unix(String), Tcp{host, port}}, parse_endpoint(&str) -> Result<Endpoint, EndpointError>, is_loopback_tcp}`；`sw_proto::version::{version, version_line}`；`sw_pal::traits::{PalError{kind, os_code, winerror, strerror, py_message}, PalErrorKind, SysEnv, Clock{mono_ns, sleep}, EndpointSpec{Unix(OsString), Tcp}, RpcConnector, RpcStream{send_all, recv}}`；`scripts/rust/check_cfg_boundary.py`、`scripts/rust/win-test.sh`、`scripts/rust/win-runner.sh`、`scripts/rust/win-blackbox.ps1`、`scripts/rust/mkbundle.sh` |
| P4 | `sw_pal::FsOps: FsRead`（`atomic_write(p, data, mode: Option<u32>)`、`write_file`、`mkdirs`、`replace`、`remove_file`、`sync_dir`、`set_mode`、`file_lock_exclusive`）；`sw_pal::ProcScan::{serialwrapd_instances() -> Result<Vec<DaemonProc>, ScanFailure>（P4 實際簽章，P5 Task 0 雙審修正）, serialwrapd_tty_holders(&[PathBuf]) -> (Vec<u32>, HolderScanStatus), pid_alive}（`HolderScanStatus` 由 P4 05 Task 1 依 arch §4.2 定義，本 Phase 沿用）`；`ProcessCtl`；`sw_proto::config_types::RuntimeConfig::{set_mode, set_socket(&mut self, &str), to_yaml}`；`sw_pal::native() -> Platform`；`crates/sw-daemon/src/launcher.rs`（`components.daemon.implementation` 判斷） |
| 本 Phase 補齊（Task 0c；上游未交付者） | `sw_proto::pycompat::{PyErr{message}::value_error, coerce_rpc_bool, py_round, iso_utc_now(us), strftime_utc(us, fmt), strftime_local(us, offset_s, fmt), uuid4_hex(rng), uuid4_dashed(rng), to_printable, clean_text, strip_ansi, a2b_base64_lenient, b2a_base64, shlex_quote, shlex_split(&str) -> Result<Vec<String>, ShlexError>}`（Python 產生 golden：`contracts/golden/pycompat_s21.tsv`；`contracts/golden/pycompat.tsv` 屬 P3 04 Task 4，不覆寫；`shlex_quote`／`b2a_base64` 供 P8 09 Task 3，`shlex_split` 供 P6 07 Task 3）；`sw_proto::rpc_types::Params<'a>::{str_or, int_or, float_or, truthy, raw, selector_chain}`；`sw_proto::config_types::RuntimeConfig::{windows_exclude_coms(&self) -> Vec<String>, windows_test_device_allowlist(&self) -> Result<Option<Vec<String>>, ()>}`；`sw_proto::runtime_paths::{DaemonDirs{by_id_dir, by_path_dir, events_dir, events_runtime_dir}, resolve_daemon_dirs(env: &EnvSnapshot) -> DaemonDirs}`；`sw_pal::traits::Clock` 擴充 `wall_unix_us(&self) -> i64`、`local_utc_offset_s(&self) -> i32`（native 與既有 fake 一併實作）；`sw_pal::traits::RpcServerStream: Read + Write + Send { shutdown_read, set_read_timeout }`（server 端；P3 `RpcStream{send_all, recv}` 維持 client 專用，arch §4.6 同步修訂）；`sw_proto::wal_types::crc32_hex(&[u8]) -> String`（`crc32fast` 只留在 sw-proto，arch §1.2） |

Task 0 的 `crates/sw-core/tests/slice21_prereqs.rs` 與 `tests/test_slice21_prereqs.py` 對**上表每一個符號**各寫一行 `use`／`import`（比照 07 計畫 Task 1 的逐項 grep），缺任一項即編譯／collection 失敗；P3／P4 列缺漏屬上游缺陷，停止並回報，不在本 Phase 自行補；「本 Phase 補齊」列由 Task 0c 交付後才轉綠。

**下游消費名稱（P6 07-slice22-console.md 依此對齊）**：本 Phase 的正式名稱為 `type Mono = u64`、`Effects(pub Vec<Effect>)`（`Effects::push`）、`CommandRegistry`＋`SessionQueue`（無 `Arbiter` 型別）、`RxShared`、`CoreDeps.rng`、`quiet_gate::detect_boot_banner`（`login_fsm` 以 `pub use` 重新匯出）、`SessionCore::begin_stop`。07 計畫列出的 `Mono(u64)`、`Effects.items`、`Arbiter`、`SubmitOrigin`、`RxWindow`、`IdGen`、`drain::phase1` 與此不同，須由 07 計畫改用上述名稱（本 Phase 不另建別名，避免兩套 API）。跨計畫審查核對：07 現行「跨 Phase 介面」段已改用上述名稱；`SubmitOrigin` 由 07「P6 對 P5 的增補清單」#6 新增，不屬 06。

### 0.4 模型分工與執行順序

| Task | 標題 | Test author model | Implementer model |
|---|---|---|---|
| 0 | 範圍凍結、資源上限與事件延遲契約、介面骨架 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 0b | Slice 2.1 必要案例集合凍結與承接率斷路器 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 0c | 上游缺漏的 sw-proto／sw-pal 介面補齊（pycompat、Params、RuntimeConfig accessor、Clock wall、RpcServerStream） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max（含 PAL native `Clock`／`RpcServerStream`，依本節「PAL 一律 luna」） |
| 1 | PAL test-fakes（FakeClock／FakeSerial／FakeWalFs／FakeDeviceSource／MemStream） | haiku-xhigh | agy-gemini-3.8-flash-high |
| 2 | PAL POSIX serial＋UartExclusive＋哨兵 PTY | haiku-xhigh | codex-gpt-6-luna-max |
| 3 | PAL Windows serial（overlapped、CancelIoEx） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 4 | PAL InstanceLock＋SignalWait | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 5 | PAL DeviceSource／DeviceWatcher（#100、SERIALCOMM、OD-11） | haiku-xhigh | codex-gpt-6-luna-max |
| 6 | PAL WalFs（常駐 handle、identity、share-delete） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 7 | sw-core WalWriter（BC-01、DF-03、DF-17） | haiku-xhigh | codex-gpt-6-luna-max |
| 8 | sw-core CommandRegistry／SessionQueue（SC-06） | haiku-xhigh | codex-gpt-6-luna-max |
| 9 | sw-core TxWriter＋Fence（BC-03、SC-07） | haiku-xhigh | codex-gpt-6-luna-max |
| 10 | sw-core IoWorker reader＋RxWindow＋哨兵 fan-out | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 11 | sw-core SessionCore／Mailbox／Generations／Actor | haiku-xhigh | codex-gpt-6-luna-max |
| 12 | sw-core Two-Phase Drain 核心＋IoWorker 收尾＋Release／Attach（sans-IO 與 IoWorker 層） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 13 | sw-core 最小 Login＋帳密＋Quiet Gate＋Profile 能力閘 | haiku-xhigh | codex-gpt-6-luna-max |
| 14 | sw-core 命令執行、agent Lease、self-test、capture | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 12b | Drain／Release 端到端（真 actor＋login＋命令執行）＋RELEASED 持久化 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 15a | 黑箱 FakeTarget 擴充（測試資產變更，§10.4） | haiku-xhigh | agy-gemini-3.8-flash-high |
| 15 | sw-core StateStore＋AliasRegistry＋state.json 雙向相容 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 16 | sw-core SessionManager（裝置、rank、動態 session、selector、管理操作） | haiku-xhigh | codex-gpt-6-luna-max |
| 17 | PAL RpcListener＋sw-daemon framing | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 18 | sw-daemon 分派器（service.rs／health.rs） | agy-gemini-3.8-flash-high | haiku-xhigh |
| 19 | sw-daemon daemon_main（啟動、關機、exit code） | haiku-xhigh | codex-gpt-6-luna-max |
| 20 | 黑箱契約與既有套件改指 Rust（F1／F2／F3／F4／F12） | haiku-xhigh | codex-gpt-6-luna-max |
| 21a | P2 runner 補動作（`external_open`／`external_close`／`measure_rpc_latency`；測試資產變更，§10.4） | haiku-xhigh | agy-gemini-3.8-flash-high |
| 21 | L1.5 bridge 回歸（P2 凍結 2.1 情境＋s21 情境）與事件延遲量測 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 22 | Windows 原生 L1 證據與 Slice 2.1 出口 gate 包 | haiku-xhigh | agy-gemini-3.8-flash-high |
| 23 | Slice 2.1 完成校準報告（§12.2、OD-02） | haiku-xhigh | agy-gemini-3.8-flash-high |

模型分工依 master plan §2：PAL、WAL、Arbiter、SessionActor、Drain、StateStore（release 持久化）、daemon 生命週期一律由 `codex-gpt-6-luna-max` 實作；`agy-gemini-3.8-flash-high` 只實作獨立模組（fakes、測試資產、gate 腳本、報告）；`haiku-xhigh` 只實作範圍明確、介面已定的模組（契約檔、schema、分派表、案例展開）。每列測試作者與實作者不同；審查者不得為該 Task 實作者。

**執行順序（每個 Task 紅→綠，依依賴序）：**

1. **骨架**：Task 0 完整執行 Step 1～5（建立全部型別與 `s21_todo!` 本體，讓之後每個 Task 的紅燈測試可編譯）。
2. **逐 Task 紅→綠**：依下列依賴序，每個 Task 依序執行「Step 0 測試支援（若有）→ Step 1 寫失敗測試 → Step 2 確認 FAIL（commit `test(<crate>): 新增 <主題> 紅燈測試`）→ Step 3 實作 → Step 4 確認 PASS → Step 5 commit」，完成一個才開始下一個：
   `0 → 0b → 0c → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14 → 12b → 15a → 15 → 16 → 17 → 18 → 19 → 20 → 21a → 21 → 22 → 23`。
   因此各 Task 的 Rust 紅燈第一個命中的必然是本 Task 的符號（`not implemented: slice21 T<n> <symbol>`）；Python 黑箱紅燈在 Task 19 之前一律為「Rust daemon 啟動即 exit 101（`not implemented: slice21 T19 run`）」，指向 Python 基線時必須 PASS。
3. **每個 Task 綠燈後的累積門檻**：`cargo test -p <本 Task 涉及的 crate> --features test-fakes --test <截至本 Task 已建立的測試檔，逐一列出>`、`python3 scripts/rust/check_cfg_boundary.py`、`python3 -m pytest -q <截至本 Task 已建立的 Python 測試檔>`；因後續 Task 的紅燈測試尚未建立，`cargo test --workspace --features test-fakes` 與 `python3 -m pytest -q tests/` 在每個 Task 結束時都必須全綠（新增任何失敗即停止）。
4. 全部 Task 完成後依 master plan §1 進入雙審（sonnet max＋codex gpt-6.1-sol medium），審查者不得為該 Task 的實作者。

**Step 0（測試支援）擁有者**：各 Task 的 support 程式碼（`CoreHarness`、`ActorRig`、`ManagerRig`、`ServiceRig`、`FakeStateStore`、Python harness）由該 Task 的 **Test author model** 撰寫，與測試同一個紅燈 commit；首個使用者 Task 負責建立（`CoreHarness`：Task 11；`ActorRig`：Task 13；`FakeStateStore`：Task 12；`ManagerRig`：Task 16；`ServiceRig`：Task 18；Python `slice21/harness.py`：Task 15）。

紅燈 commit 只存在於 `wt/rust-refactor/slice21-daemon-core`；全部綠燈前不開 PR。

### 0.5 共用命令

| 用途 | 命令 |
|---|---|
| 單 crate L1 | `cargo test -p <crate> --features test-fakes --test <file_stem>` |
| 資源標記測試 | `cargo test -p sw-pal --features test-fakes -- --ignored cap_posix_pty__` |
| Windows 原生 L1 | `scripts/rust/win-test.sh --package sw-pal` ／ `scripts/rust/win-test.sh --caps windows_com --package sw-pal` |
| release artifact | `cargo build --release -p sw-daemon -p sw-cli --target x86_64-unknown-linux-gnu` |
| 黑箱（Rust） | `SERIALWRAP_DAEMON_EXE=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrapd SERIALWRAP_AB_LABEL=rust python3 -m pytest -q tests/contract_blackbox/slice21` |
| 黑箱（Python 基線） | `python3 -m pytest -q tests/contract_blackbox/slice21` |

---

## Task 0：範圍凍結、資源上限與事件延遲契約、介面骨架

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-s21-skeleton.md`（front matter `type: feat`、`scope: rust-s21`；本文：「新增 Slice 2.1 Rust daemon 範圍清單、有界資源契約與 crate 骨架（尚無行為）。」）
**BLOCKED-OWNER：** 無。

**Files:**
- Create: `tests/acceptance/slice21_scope.yaml`
- Create: `contracts/resource_limits.yaml`
- Create: `tests/test_slice21_scope.py`、`tests/test_slice21_prereqs.py`
- Create: `crates/sw-pal/src/serial.rs`、`crates/sw-pal/src/lock.rs`、`crates/sw-pal/src/device_watcher.rs`、`crates/sw-pal/src/wal_fs.rs`、`crates/sw-pal/src/signal.rs`、`crates/sw-pal/src/console_pty.rs`、`crates/sw-pal/src/fakes/{serial.rs,wal_fs.rs,device.rs,mem_stream.rs}`
- Modify（P3／P4 已建，不重建）：`crates/sw-pal/src/device_source/mod.rs`（P4 05 Task 1 的目錄模組；Rust 不可同時存在 `device_source.rs`）、`crates/sw-pal/src/fakes/clock.rs`（P3 04 Task 1／Task 9 的 `FakeClock`，Task 1 擴充）、`crates/sw-pal/src/conformance/{mod.rs,serial_suite.rs,wal_fs_suite.rs,lock_suite.rs,rpc_suite.rs}`
- Create: `crates/sw-core/src/{wal.rs,arbiter.rs,tx_writer.rs,generations.rs,mailbox.rs,session_core.rs,actor.rs,op_runner.rs,drain.rs,uart_bridge.rs,login_fsm.rs,session_auth.rs,quiet_gate.rs,lease.rs,self_test.rs,capture.rs,state_store.rs,alias_registry.rs,session_manager.rs,resources.rs,mcu_patterns.rs}`
- Create: `crates/sw-daemon/src/{daemon_main.rs,framing.rs,service.rs,health.rs}`
- Modify: `crates/sw-pal/src/traits.rs`（加入本 Phase 型別）、`crates/sw-pal/src/lib.rs`、`crates/sw-pal/Cargo.toml`（feature `test-fakes`）、`crates/sw-core/src/lib.rs`、`crates/sw-core/Cargo.toml`、`crates/sw-daemon/src/main.rs`、`crates/sw-daemon/Cargo.toml`
- Test: `crates/sw-core/tests/slice21_prereqs.rs`
- Test（P5 Task 0 雙審修正補列）：`tests/test_slice21_skeleton_guards.py`（untracked，新增）、`crates/sw-pal/tests/fakes_selftest.rs`（未設定原生元件 panic 自測）、`crates/sw-daemon/tests/launcher.rs`（`manifest_absent` 四案）、`tests/contract_blackbox/slice21/test_slice_scope.py`（§10.4 測試資產變更：延後＋未支援 method 同形狀，需獨立審閱）
- Modify（P5 Task 0 雙審修正補列）：`Cargo.lock`（骨架依賴；`scripts/rust/check_dep_direction.py` 的 `cargo metadata --locked` 需要）、`crates/sw-pal/src/clock/{posix.rs,windows.rs}`（`wall_unix_us`／`local_utc_offset_s` 必實作，T0c）、`crates/sw-pal/src/fakes/{platform.rs,mod.rs}`（五個原生元件改 `Unconfigured` 占位，builder 加 setter）、`crates/sw-proto/src/constants.rs`（`CMD_PENDING_MAX`、`DEFAULT_WAL_ROTATE_BYTES`、`CMD_HISTORY_MAX`、`PASSTHROUGH_READY_QUIET_S`）、`crates/sw-daemon/src/launcher.rs`（`LauncherError::DevBuildNoManifest`；`manifest_absent` 僅此變體為開發 build；分派判定計畫原列於 Task 19，本 Task 先行）、`crates/sw-cli/tests/{event_add_document,event_cmd,event_params,profile_checks,skill_cmd}.rs`（測試組裝點改用 `Unconfigured`；`DummyClock` 補 T0c 本體）、`docs/rust-refactor/plans/rust-architecture.md`（§4.5 補 `WalFs::read_all`；§4.6 補 `RpcServerStream` 與 `RpcListener::accept -> Box<dyn RpcServerStream>`）、`docs/rust-refactor/plans/07-slice22-console.md` 第 156 行 `CoreDeps` 副本（與本計畫 Task 11 Produces 同步）

**Interfaces:**
- Consumes：§0.3 全部。
- Produces（以下為全 Phase 共用型別，後續 Task 只補實作、不改簽章）：

```rust
// crates/sw-pal/src/traits.rs（新增部分；arch §4.2～§4.6 已定者逐字沿用）
impl PalError { pub fn io(code: i32, py_message: &str) -> PalError; pub fn from_errno(e: i32) -> PalError; pub fn cancelled() -> PalError;
                pub fn not_found(py_message: &str) -> PalError; pub fn invalid(msg: &str) -> PalError;
                pub fn py_exc_name(&self) -> &'static str; /* ENOENT→FileNotFoundError、EACCES/EPERM→PermissionError、EEXIST→FileExistsError、ETIMEDOUT→TimeoutError、其他→OSError */ }
#[derive(Clone, Debug, PartialEq, Eq, Hash)] pub struct DevicePath(pub String);
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum Parity { None, Even, Odd }
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum FlowControl { None, RtsCts }
#[derive(Clone, Debug, PartialEq)] pub struct SerialConfig { pub baud: u32, pub data_bits: u8, pub parity: Parity, pub stop_bits: u8, pub flow: FlowControl, pub xonxoff: bool }
impl SerialConfig { pub fn from_uart_profile(baud: u32, data_bits: u8, parity: &str, stop_bits: u8, flow_control: &str, xonxoff: bool) -> SerialConfig; }
// HolderScanStatus { Ok, Permission, Unknown, Unsupported }：P4 05 Task 1 已在 traits.rs 定義（ProcScan 共用），此處沿用、不重複定義
#[derive(Clone, Debug, PartialEq, Eq)] pub struct HolderReport { pub pids: Vec<u32>, pub holder_unknown: bool, pub status: HolderScanStatus }
#[derive(Debug)] pub enum OpenError { Held(HolderReport), AccessDenied { os: PalError, holder: HolderReport }, NotFound(PalError), Other(PalError) }
#[derive(Debug, PartialEq, Eq)] pub enum ReadOutcome { Data(usize), Eof, Woken, TimedOut }
#[derive(Debug, Clone, PartialEq, Eq)] pub enum TxOutcome { NotSubmitted, Completed(usize), Unknown { confirmed: usize } }
#[derive(Debug, Clone)] pub struct TxResult { pub outcome: TxOutcome, pub error: Option<PalError> }
#[derive(Clone)] pub struct CancelToken { flag: Arc<AtomicBool>, waker: Arc<dyn IoWaker> }
impl CancelToken { pub fn new(waker: Arc<dyn IoWaker>) -> CancelToken; pub fn cancel(&self); pub fn is_cancelled(&self) -> bool; }
pub struct CloseToken { inner: Box<dyn CloseImpl> }
pub trait CloseImpl: Send { fn close(self: Box<Self>) -> CloseReceipt; }
impl CloseToken { pub fn new(inner: Box<dyn CloseImpl>) -> CloseToken; pub fn close(self, r: Box<dyn SerialReader>, w: Box<dyn SerialWriter>) -> CloseReceipt; }
#[derive(Debug, Clone)] pub struct CloseReceipt { pub closed: bool, pub excl_cleared: bool, pub error: Option<PalError> }   // excl_cleared：POSIX 關 fd 前 TIOCNXCL 是否成功（arch §4.2，R0-F3）；Windows／fake 恆等於 closed
pub trait IoWaker: Send + Sync { fn wake(&self); }
pub trait SerialBackend: Send + Sync { fn open_exclusive(&self, dev: &DevicePath, cfg: &SerialConfig) -> Result<Box<dyn SerialHandle>, OpenError>; }
pub trait SerialHandle: Send {
    fn split(self: Box<Self>) -> (Box<dyn SerialReader>, Box<dyn SerialWriter>, Arc<dyn IoWaker>, CloseToken);
    fn set_baud(&self, baud: u32) -> PalResult<()>;
    fn mirror_termios_from(&self, pty: &PtySlaveRef, fallback_baud: Option<u32>) -> PalResult<()>;
    fn is_alive(&self) -> bool;
}
pub trait SerialReader: Send { fn read(&mut self, buf: &mut [u8], timeout: Option<Duration>) -> PalResult<ReadOutcome>; }
// arch §4.2 SubmitGate（R0-F1）：每次 OS 提交（POSIX 一次非阻塞 write(2)、Windows 一次 overlapped WriteFile 發出）都必須經 gate；
// gate 在 Fence 鎖內重驗後才呼叫 os_submit，被拒則不呼叫；完成等待在 gate 外。
pub trait SubmitGate { fn submit(&self, os_submit: &mut dyn FnMut() -> SubmitStep) -> Result<SubmitStep, SubmitRevoked>; }
#[derive(Debug)] pub enum SubmitStep { Accepted(usize), Pending, WouldBlock, Failed(PalError) }
#[derive(Debug, Clone, Copy, PartialEq, Eq)] pub struct SubmitRevoked;
pub struct AlwaysOpen;  impl SubmitGate for AlwaysOpen {}   // 直接呼叫 os_submit；PAL conformance／單元測試專用
pub trait SerialWriter: Send {
    fn write_gated(&mut self, data: &[u8], cancel: &CancelToken, gate: &dyn SubmitGate) -> TxResult;
    fn write(&mut self, data: &[u8], cancel: &CancelToken) -> TxResult { self.write_gated(data, cancel, &AlwaysOpen) }  // sw-core 禁用
}
// gate 撤銷：未提交任何 bytes → NotSubmitted（error None）；已確認 k≥1 後撤銷 → Completed(k)＋PalError::cancelled()；撤銷後不得再交 OS。
pub trait UartExclusive: Send + Sync { fn probe_holders(&self, real_path: &str) -> HolderReport; }
// DeviceSource／DeviceWatcher／InstanceLock／WalFs／RpcListener 照 arch §4.3～§4.6 逐字，並加下列三點（同步修訂 arch §4.5／§4.6）：
//   WalFs 新增 fn read_all(&self, path: &Path) -> PalResult<Option<Vec<u8>>>;   // 讀取 API（Task 7）用；Task 6 conformance 一併涵蓋
//   WalFs 新增 fn tail_needs_termination(&self, path: &Path) -> PalResult<bool>; // 自癒後確認 META 不會接在替換檔殘行上
//   RpcListener::accept 回 Box<dyn RpcServerStream>（Task 0c 定義；P3 RpcStream 維持 client 專用）
pub trait SignalWait: Send + Sync {
    /// 必須在 main 的第一個動作、建立任何 thread 之前呼叫：POSIX 以 pthread_sigmask(SIG_BLOCK, {SIGINT,SIGTERM}) 後啟動專屬 sigwait thread
    /// （之後建立的 thread 全部繼承 mask）；Windows 以 SetConsoleCtrlHandler 註冊。重複呼叫回 Ok(())。
    fn install(&self) -> PalResult<()>;
    fn wait_stop(&self, extra: crossbeam_channel::Receiver<()>);
}
pub struct ExternalHandle { /* FakeWalFs 外部 reader；Drop 時釋放 pending-delete */ }
// PtySlaveRef／KeepAlive／ConsoleIo／PtyPair 依 arch §4.7 逐字宣告
// DeviceBackendKind、Capabilities.native_device 依 arch §4 逐字
// ConsoleTransportFactory 依 arch §4.7 完整宣告（kind、create_pty、bind_tcp_console、open_com0com_console、held_slave_paths）；
// 本 Phase 只實作 kind 與 POSIX create_pty，其餘方法固定回 Err(PalError{kind: PalErrorKind::Unsupported, ..})，held_slave_paths 回 None（2.2 承接）。
// SerialHandle::mirror_termios_from 依 arch §4.2 宣告；本 Phase 兩平台都回 Err(Unsupported)（/dev/ttyMCU 屬 2.4）。
```

```rust
// crates/sw-core/src/lib.rs
pub type Mono = u64;                       // Clock::mono_ns()
pub const SLICE: &str = "2.1";
#[macro_export] macro_rules! s21_todo { ($t:literal, $sym:literal) => { unimplemented!(concat!("slice21 ", $t, " ", $sym)) } }

// crates/sw-core/src/session_core.rs（原本只被引用、未定義的型別，於此凍結）
pub enum ManagerNote { SessionReady { sid: String }, SessionDetached { sid: String, reason: Option<String> },
                       StickyCandidate { sid: String, by_id: String, profile: String, real_path: String },
                       InteractiveOpened { sid: String, interactive_id: String }, InteractiveClosed { interactive_id: String } }
// crates/sw-core/src/self_test.rs
pub struct SelfTestCtx { pub device_present: bool, pub holder: Option<HolderReport>, pub by_id_realpath: Option<String>,
                         pub vtty_alive: Option<bool>, pub strict_human_lock: bool, pub now: Mono }
pub struct NeedProbe { pub probe: String, pub nonce: String, pub timeout_s: f64, pub partial: PyDict /* 已決定的欄位 */ }
```

骨架中每個尚未實作的函式本體一律寫成 `s21_todo!("T<n>", "<Type>::<fn>")`（PAL 端同名巨集 `sw_pal::s21_todo!`、sw-proto 端 `sw_proto::s21_todo!`，三者格式相同；Slice 2.1 全部完成後由 Task 22 gate 以 `grep -R "s21_todo!(" crates/*/src` 除巨集定義外為 0 筆作為 `cfg_boundary` section 的附加檢查）。

- [ ] **Step 1：寫失敗測試**

`tests/test_slice21_scope.py`：

```python
"""Slice 2.1 範圍清單與資源上限契約的一致性檢查（不 import sw_core 的 Rust 部分）。"""
from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCOPE = ROOT / "tests" / "acceptance" / "slice21_scope.yaml"
LIMITS = ROOT / "contracts" / "resource_limits.yaml"

DEFERRED = {
    "session.console_attach": "2.2", "session.console_detach": "2.2", "session.console_list": "2.2",
    "event.rule_set": "2.3", "event.rule_delete": "2.3", "event.rule_list": "2.3", "event.rule_get": "2.3",
    "event.com_enable": "2.3", "event.com_disable": "2.3", "event.com_status": "2.3",
    "event.reset": "2.3", "event.reload": "2.3", "event.tail": "2.3",
    "file.push": "2.4", "file.pull": "2.4",
}


def test_scope_lists_exact_deferred_methods():
    scope = yaml.safe_load(SCOPE.read_text(encoding="utf-8"))
    got = {row["method"]: row["slice"] for row in scope["deferred_methods"]}
    assert got == DEFERRED
    assert scope["deferred_response"] == {"ok": False, "error_code": "METHOD_NOT_FOUND", "keys": ["method", "version"]}


def test_scope_served_methods_cover_rpc_catalog():
    scope = yaml.safe_load(SCOPE.read_text(encoding="utf-8"))
    served = set(scope["served_methods"])
    assert len(served) == 39
    assert served.isdisjoint(DEFERRED)
    assert {"health.ping", "health.status", "daemon.stop", "command.submit", "device.release",
            "device.attach", "session.recover", "log.tail_raw", "wal.range", "mcu.status"} <= served


def test_unsupported_profile_features_are_declared():
    scope = yaml.safe_load(SCOPE.read_text(encoding="utf-8"))
    assert scope["unsupported_profile_features"] == ["post_login_cmd", "console_unlock_env"]
    assert scope["unsupported_profile_behavior"] == {
        "auto_tx": False, "state": "ATTACHED", "last_error": None, "submit_error_code": "SESSION_NOT_READY"}
    assert scope["sentinel_console_input"] == "discard"


def test_resource_limits_contract_values():
    limits = yaml.safe_load(LIMITS.read_text(encoding="utf-8"))
    by_name = {row["name"]: row for row in limits["limits"]}
    expect = {
        "mailbox_control": 256, "mailbox_data_chunks": 1024, "mailbox_data_chunk_bytes": 8192,
        "tx_writer_jobs": 64, "cmd_pending_max": 256, "cmd_history_max": 512,
        "bg_capture_max_count": 64, "bg_capture_max_chars": 4194304, "rx_window_max_chars": 131072,
        "rx_rate_window_entries": 4096, "deferred_input_max_bytes": 262144,
        "rpc_connections": 128, "rpc_request_line_bytes": 65536, "interactive_tombstones": 16,
        "drain_timeout_ms": 3000, "mailbox_stash_max": 16,
        # R1-F8：TX 帳本只保留未結算 op；不得以 drop_oldest／evict 淘汰在途證據
        "tx_ledger_live_ops": 256, "tx_ledger_unresolved_gaps": 256, "tx_op_wal_gaps": 16, "tx_settle_queue": 256,
    }
    assert {k: by_name[k]["value"] for k in expect} == expect
    assert {k: by_name[k]["overflow"] for k in ("tx_ledger_live_ops", "tx_ledger_unresolved_gaps", "tx_op_wal_gaps", "tx_settle_queue")} == {
        "tx_ledger_live_ops": "reject_new_and_drain", "tx_ledger_unresolved_gaps": "merge_oldest",
        "tx_op_wal_gaps": "merge_oldest", "tx_settle_queue": "retain_entry"}
    for row in limits["limits"]:
        assert row["overflow"] in {"reject", "block_sender", "drop_oldest", "clear_all", "truncate_head",
                                   "evict_oldest_done", "pause_accept", "close_connection", "timeout", "merge_oldest",
                                   "reject_new_and_drain", "retain_entry"}


EVENTS = {"stop_request", "command_cancel", "lease_expiry", "drain_receipt", "release_request"}


def test_event_latency_contract_frozen_before_slice_start():
    """§5.3：各事件的受理點、完成點、最大延遲、負載與量測方法在 Slice 2.1 開工前凍結，並有獨立審閱者。"""
    lat = yaml.safe_load(LIMITS.read_text(encoding="utf-8"))["event_latency"]
    assert lat["method_revision"] and lat["reviewed_by"] and lat["reviewed_by"] != lat["owner"]
    assert lat["repeats"] == 3 and lat["samples_per_run"] == 500
    assert lat["gate_rule"] == "every_run_max_le_max_ms"           # p99 只作診斷值
    rows = {r["event"]: r for r in lat["events"]}
    assert set(rows) == EVENTS
    for r in rows.values():
        assert {"accept_point", "complete_point", "max_ms_idle", "max_ms_flood", "load", "l1_bound"} <= set(r)
        assert r["l1_bound"] == "at_most_one_data_item_before_control"
    assert lat["flood_load"] == {"bytes_per_s": 20000, "seconds": 30, "chunk_bytes": 256}


def test_sw_core_has_no_relocated_modules():
    src = ROOT / "crates" / "sw-core" / "src"
    names = {p.stem for p in src.glob("*.rs")}
    assert "state_migrate" not in names and "bench_registry" not in names
```

`crates/sw-core/tests/slice21_prereqs.rs`（§0.3 P3／P4 列**每一個**符號各一行 `use`，編譯失敗即前置未到位；以下為開頭節錄，其餘照表逐項展開，不得省略）：

```rust
#![allow(unused_imports)]
use sw_proto::{constants, pyjson::{self, PyDict, PyValue, PyInt, DumpOpts, Sep, loads, dumps, CLI_STDOUT, RPC_WIRE, RPC_EARLY, DISK_STABLE}};
use sw_proto::pycompat::{py_int_parse, py_float_parse, py_strip, py_bytes_strip, splitlines, universal_newlines,
                         decode_utf8_replace, decode_utf8_strict, py_len_chars, py_truthy, PyOsError, sha256_hex};
use sw_proto::pyfmt::{py_str, py_repr_str, py_repr_float};
use sw_proto::pyre::{compile, PyFlags, PyRegex, valid_completion_regex};
use sw_proto::config_types::{load_profiles, ProfileFs, LoadResult, ProfileTemplate, SessionProfile, UartProfile, RuntimeConfig};
use sw_proto::runtime_paths::{RuntimePaths, EnvSnapshot, NativeTraits, PathsError, resolve_config_path};
use sw_proto::endpoint::{Endpoint, EndpointError, parse_endpoint, is_loopback_tcp};
use sw_proto::{rpc_types::Method, version::{version, version_line}, error_codes::ErrorCode, session_last_errors::SessionLastError};
use sw_pal::traits::{PalError, PalErrorKind, SysEnv, Clock, EndpointSpec, RpcConnector, RpcStream};
use sw_pal::{FsOps, ProcScan, ProcessCtl};
// （P4 方法存在性以呼叫式斷言：RuntimeConfig::set_socket／to_yaml、FsOps::atomic_write(p, d, Some(0o600))）
#[test]
fn prereq_symbols_exist() {
    assert_eq!(constants::CMD_PENDING_MAX, 256);
    assert_eq!(constants::DEFAULT_WAL_ROTATE_BYTES, 67_108_864);
    assert!(!version().is_empty());
    assert_eq!(pyjson::dumps(&PyValue::Dict(PyDict::new()), &DISK_STABLE), "{}");
    assert!(valid_completion_regex(None));
    let _ = sw_core::SLICE;
}
```

`tests/test_slice21_prereqs.py`：逐項 `from ab_env import AB_ENV`、`from tests.contract_blackbox.bb_harness import repo_root, ab_label, is_rust, cli_prefix, daemon_prefix, isolated_env, run_cli, CliResult, NL`、`from tests.contract_blackbox.bb_faketarget import FakeTarget`、`from tests.contract_blackbox.bb_daemon import PyDaemon`，並斷言 `tests/acceptance/cases.lock.json`、`func-test/scenarios/l15-reject-zero-tx.yaml` 存在、`func-test/runner.py --help` 含 `--mode` 與 `--scenarios`。

「本 Phase 補齊」列的符號另由 Task 0c 的 `crates/sw-proto/tests/slice21_additions.rs` 逐項 `use`（Task 0c 前為紅燈）。

- [ ] **Step 2：執行並確認 FAIL**

Run：`python3 -m pytest -q tests/test_slice21_scope.py`
Expected：FAIL，`FileNotFoundError: .../tests/acceptance/slice21_scope.yaml`。
Run：`cargo test -p sw-core --test slice21_prereqs`
Expected：FAIL（`sw_core::SLICE` 未定義的編譯錯誤）。

- [ ] **Step 3：實作**

`tests/acceptance/slice21_scope.yaml`：

```yaml
schema_version: 1
slice: "2.1"
served_methods: [health.ping, health.status, daemon.stop, mcu.patterns, mcu.status, device.list, device.release,
  device.attach, session.list, session.get_state, session.activity, session.log_status, session.self_test,
  session.recover, session.clear, session.bind, session.pin, session.unpin, session.attach,
  session.interactive_open, session.interactive_send, session.interactive_status, session.interactive_close,
  session.log_start, session.log_stop, alias.list, alias.set, alias.assign, alias.unassign, command.submit,
  command.get, command.result_tail, command.cancel, result.tail, log.tail_raw, log.tail_text, wal.range,
  wal.reset, wal.current_seq]
deferred_response: {ok: false, error_code: METHOD_NOT_FOUND, keys: [method, version]}
deferred_methods:
  - {method: session.console_attach, slice: "2.2"}
  - {method: session.console_detach, slice: "2.2"}
  - {method: session.console_list, slice: "2.2"}
  - {method: event.rule_set, slice: "2.3"}
  - {method: event.rule_delete, slice: "2.3"}
  - {method: event.rule_list, slice: "2.3"}
  - {method: event.rule_get, slice: "2.3"}
  - {method: event.com_enable, slice: "2.3"}
  - {method: event.com_disable, slice: "2.3"}
  - {method: event.com_status, slice: "2.3"}
  - {method: event.reset, slice: "2.3"}
  - {method: event.reload, slice: "2.3"}
  - {method: event.tail, slice: "2.3"}
  - {method: file.push, slice: "2.4"}
  - {method: file.pull, slice: "2.4"}
unsupported_profile_features: [post_login_cmd, console_unlock_env]
unsupported_profile_behavior: {auto_tx: false, state: ATTACHED, last_error: null, submit_error_code: SESSION_NOT_READY}
sentinel_console_input: discard        # POSIX 哨兵 PTY 收到的 human bytes 在 2.1 丟棄（2.2 承接 Raw／Line）
session_public_deferred_fields:
  - {field: console_endpoint, platform: windows, value_in_2_1: null, slice: "2.2"}
cross_slice_cases:
  - {suite: tests/test_human_agent_coexist.py, items: [t1, t5, t6, t7, t8], slice: "2.2"}
  - {suite: func-test, items: [co-01-line-buffering, cowork-01-agent-bad-cmd, ha-01-human-then-agent, hk-01-special-keys-raw, hk-02-agent-preempt-human, hk-03-deferred-input-flush, hk-04-minicom-exit-cleanup, rc-01-interactive-mode-switch], slice: "2.2"}
  - {suite: func-test, items: [brcm-01-login-flow, ev-01-spawn-handler], slice: "2.3"}
  - {suite: regression, items: [F5, F6, F8], slice: "2.2"}
  - {suite: regression, items: [F9, F10, F11], slice: "2.3"}
  - {suite: regression, items: [F7], slice: "2.4"}
early_evidence_cases:                  # cases.lock 屬較晚切片、本 Phase 已能提供證據者；不計入 slice_2.1 分母
  - {case_id: f4-interactive-line-cmd-defined, lock_slice: "2.2", reason: "agent-only lease 已於 Task 14 承接"}
```

`contracts/resource_limits.yaml`：
- `limits`：每列 `{name, value, unit, overflow, source}`，值與 `test_resource_limits_contract_values` 相同；`overflow` 依 arch §5.10（`mailbox_control: block_sender`（RPC 送端逾時回 `timeout`）、`mailbox_data_chunks: block_sender`、`tx_writer_jobs: block_sender`、`cmd_pending_max: reject`、`cmd_history_max: evict_oldest_done`、`bg_capture_max_chars: truncate_head`、`bg_capture_max_count: evict_oldest_done`、`rx_window_max_chars: truncate_head`、`rx_rate_window_entries: merge_oldest`（合併最舊兩筆、保留 bytes 總和，flood 下不低估 RX_FLOOD 判定）、`deferred_input_max_bytes: clear_all`、`rpc_connections: pause_accept`、`rpc_request_line_bytes: close_connection`、`interactive_tombstones: drop_oldest`、`drain_timeout_ms: timeout`、`mailbox_stash_max: block_sender`、`tx_ledger_live_ops: reject_new_and_drain`（新 op 不提交、不建項目，送 `UartIoError{reason:"TX_LEDGER_SATURATED"}` 以 IoError drain 收斂；不淘汰既有項目）、`tx_ledger_unresolved_gaps: merge_oldest`／`tx_op_wal_gaps: merge_oldest`（合併進 `GapOverflow{gaps, bytes, first_op, last_op}`，計數與 bytes 不丟）、`tx_settle_queue: retain_entry`（settle try_send 滿時該 op 留在帳本，`settle_dropped` 計數），R1-F8）；`source` 寫 arch 章節。
- `event_latency`（§5.3，開工前凍結；`owner`＝PAL／Actor 實作負責人 `codex-gpt-6-luna-max`，`reviewed_by`＝獨立測試審閱者，兩者不同；`method_revision`＝`"2026-10-15.1"`）：`repeats: 3`、`samples_per_run: 500`、`gate_rule: every_run_max_le_max_ms`（每輪樣本最大值都 ≤ 上限才通過；p99 另記為診斷值、不作判定）、`flood_load: {bytes_per_s: 20000, seconds: 30, chunk_bytes: 256}`（= 規格門檻 20000 B／10 s 的 10 倍）、`host_load: "量測期間記錄 /proc/loadavg 與 CPU 數；loadavg 1min > CPU 數時該輪作廢重跑"`。`events`：

| event | accept_point | complete_point | max_ms_idle | max_ms_flood |
|---|---|---|---|---|
| `stop_request` | `daemon.stop` 回應 `stopping:true` 寫出 | actor 收到 `Shutdown` 並送出 `BeginDrain` | 50 | 100 |
| `command_cancel` | `command.cancel` RPC 收到 | 在途命令的 `CancelToken::cancel` 被呼叫 | 50 | 100 |
| `lease_expiry` | lease 期限到達（mono） | lease 移入 tombstone | 1100 | 1200 |
| `drain_receipt` | finisher 送出 `WorkerStopped` | actor 處理該 receipt 完成 | 50 | 100 |
| `release_request` | `device.release` RPC 收到 | actor 送出 `BeginDrain` | 50 | 100 |

  每列另有 `load: idle|flood`（兩種皆量）與 `l1_bound: at_most_one_data_item_before_control`（L1 決定性條件：control lane 有事件時，actor 最多再處理一個 data item 就處理它）。`lease_expiry` 上限含 1 s tick 粒度。事件處理延遲與 3000 ms Drain 完成時限分別判定，不得互相代用。

Rust 骨架：依**本計畫 Task 0～19（含 0c、12b）各自 Interfaces 的 Produces 區塊**與 §0.3「本 Phase 補齊」列建立所有檔案、型別、trait 與函式簽章（含 Task 11 的 `ControlEvent`／`DataEvent`／`Effect`，讓 Task 10 等先於 Task 11 的紅燈測試可以編譯；含 Task 0c 的 `PyErr`、`Params`、`Clock::wall_unix_us` 等，讓骨架本身可編譯）；函式本體 `s21_todo!("T<該型別所屬 Task 編號>", "<Type>::<fn>")`（0c 的符號用 `"T0c"`）。`crates/sw-pal/Cargo.toml` 加 `[features] test-fakes = []`；`crates/sw-core/Cargo.toml`、`crates/sw-daemon/Cargo.toml` 的 dev-dependencies 以 `sw-pal = { path = "../sw-pal", features = ["test-fakes"] }` 啟用 fakes，並宣告 `[features] test-fakes = ["sw-pal/test-fakes"]`。`crates/sw-core/src/lib.rs` 宣告全部模組與 `SLICE`、`Mono`、`s21_todo!`。

- P5 Task 0 雙審修正（Step 3 補充，已實作）：
  - `PalError::{from_errno, cancelled, not_found, invalid, py_exc_name}` 於 Task 0 實作為純建構子（不做 I/O；`from_errno` 沿用 `posix_errno`／`windows_crt_error`），以 `pal_error_ctor_tests` 單元測試覆蓋；Task 1 與 Task 7 不再依賴 T2 stub。
  - `Clock::{wall_unix_us, local_utc_offset_s}` 無預設實作，所有 `impl Clock`（NativeClock、FakeClock、`Unconfigured`、sw-cli 測試 dummy）以 `s21_todo!("T0c", …)` 明確覆寫。`local_utc_offset_s` 依本計畫 Produces 簽章為 `(&self) -> i32`；arch §4.1 寫為 `(&self, unix_s: i64) -> i32`，兩者不一致，待 Task 0c 裁定（Task 0 未擅自改 arch §4.1）。
  - 替身組裝點（`fakes::platform::platform()`、`fakes::fake_platform()`）的 `devices`、`instance_lock`、`rpc_listen`、`console`、`signals` 一律以 `Unconfigured` 占位（呼叫即 panic `fake component not configured: <name>`），不得接原生 PAL 元件；`fakes_selftest.rs::unconfigured_native_components_panic_not_real_os` 守此條。
  - `sw_proto::constants` 補 `CMD_HISTORY_MAX`（sw-core `resources` 改 `pub use`）與 `PASSTHROUGH_READY_QUIET_S = 30.0`（arch §5.12）；`prereq_symbols_exist` 斷言兩者。
  - `tests/test_slice21_skeleton_guards.py`：`resource_limits.yaml` 的 20 列與 Rust 鏡像雙向完全相同（`cmd_pending_max` 納入）；`.write(…cancel…)` 守衛改整檔比對（容許 rustfmt 換行與 `cancel_token` 類變數名），並以自我測試確認樣式有效。
  - `manifest_absent`（launcher 分派判定，Task 19 前先行）：僅「`current-release.json` 不存在且版本 manifest 讀取為 NotFound」為開發 build（`LauncherError::DevBuildNoManifest`）；指標存在但目標缺失、manifest 不可讀、realpath 失敗一律 fail-closed（exit 2）。
- **待擁有者裁決（開工前，未解）**：`contracts/resource_limits.yaml` 的 `rx_rate_window_entries` 標 `owner_decision: NC-P5-01`（overflow `merge_oldest` 與 Python 基線「丟棄最舊」衝突；反例見該列）。裁決前不得作為 Task 10／20 A/B 比對基準；§5.3 凍結測試因此維持 skip（INSUFFICIENT_EVIDENCE）。

- [ ] **Step 4：執行並確認 PASS**

Run：`python3 -m pytest -q tests/test_slice21_scope.py tests/test_slice21_prereqs.py tests/test_slice21_skeleton_guards.py tests/test_resource_markers.py && cargo test -p sw-core --test slice21_prereqs && cargo build --workspace --features test-fakes && python3 scripts/rust/check_cfg_boundary.py`（P5 Task 0 雙審修正：補入標記稽核與骨架守衛；`test_resource_markers.py` 為既有 pytest 標記稽核，新增測試檔必須通過它）
Expected：PASS（`test_slice21_scope.py` 的範圍與資源契約測試、`test_slice21_prereqs.py` 全部 import 與檔案存在檢查、`test_slice21_skeleton_guards.py`）；`test_event_latency_contract_frozen_before_slice_start` 在 §5.3 獨立審閱紀錄與 NC-P5-01 裁決前為 **skip（INSUFFICIENT_EVIDENCE）**，不計為 PASS；`check_cfg_boundary.py` exit 0。

- [ ] **Step 5：Commit**

```bash
git add tests/acceptance/slice21_scope.yaml contracts/resource_limits.yaml tests/test_slice21_scope.py tests/test_slice21_prereqs.py tests/test_slice21_skeleton_guards.py tests/contract_blackbox/slice21/test_slice_scope.py Cargo.lock crates changelog.d/rust-s21-skeleton.md docs/rust-refactor/plans/rust-architecture.md
git commit -m "chore(rust): 建立 Slice 2.1 範圍清單、資源上限契約與 crate 骨架" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

P5 Task 0 雙審修正（staging）：`crates` 僅限本 Task 0 骨架與修正檔（其他 Task 的中途實作由 orchestrator 依 per-task staging 分開處理）。`docs/rust-refactor/plans/00-master-plan.md`、`08-slice23-login-events.md`、`spec-reconciliation.md` 的文字修訂，以及 `07-slice22-console.md` 中 CoreDeps 以外的修改，不屬 Task 0，應另行提交或併入 Task 0b。

---

## Task 0b：Slice 2.1 必要案例集合凍結與承接率斷路器（§11.3、§12.2、OD-02、OD-13）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-s21-case-freeze.md`（`type: feat`、`scope: tests`；本文：「凍結 Slice 2.1 必要案例集合、分母與觀測窗口起點，並新增承接率斷路器判定器。」）
**BLOCKED-OWNER：** 無。

**Files:**
- Create: `scripts/rust/freeze_slice_cases.py`、`scripts/rust/slice_breaker.py`、`tests/acceptance/breaker/slice_2.1.json`、`tests/acceptance/breaker/slice_2.1.snapshots.ndjson`（空檔）
- Modify: `tests/test_slice21_scope.py`（加兩個測試）
- Test: `tests/test_slice21_case_freeze.py`

**Interfaces:**
- Consumes：P2 `tests/acceptance/cases.yaml`、`cases.lock.json`（欄位 `daemon_slice`、`necessity`）。
- Produces：
  - `scripts/rust/freeze_slice_cases.py --slice 2.1 --date YYYY-MM-DD`：寫 `tests/acceptance/breaker/slice_2.1.json`（`DISK_STABLE`）＝`{"schema_version":1,"slice":"2.1","frozen_at":<date>,"window":{"start":<date>,"weeks":3},"cases_revision":<cases.yaml revision>,"cases_lock_sha256":<cases.lock.json 位元組 sha256>,"cases_yaml_sha256":<cases.yaml 位元組 sha256>,"denominator_set":"slice_2.1","required_case_ids":<lock["sets"]["slice_2.1"] 原序>,"denominator":<len>,"stage2_started_at":<date>}`；已存在時拒絕覆寫（exit 1，分母不得事後改）。
  - `scripts/rust/slice_breaker.py`：介面與 `09-slice24-transfer-flash.md` 斷路器段相同（`acceptance_rate(lock, results, *, set_name)`、`evaluate(freeze, snapshots, *, today, stage2_estimate_weeks)`、`main`）；本 Phase 為首個建立者，09 計畫改為沿用（下游對齊）。有效 PASS 只採 Task 22 gate 判定為 `PASS` 的案例證據；SKIP／INSUFFICIENT_EVIDENCE／EVIDENCE_MISSING 不計入分子。

- [ ] **Step 1：寫失敗測試**

`tests/test_slice21_case_freeze.py`：

| 測試名 | 精確斷言 |
|---|---|
| `test_freeze_matches_lock` | `slice_2.1.json.required_case_ids == lock["sets"]["slice_2.1"]`、`denominator == len(...)`、`cases_lock_sha256` 等於現行檔案 sha256 |
| `test_freeze_precedes_any_gate_run` | `frozen_at <= ` 任何 `target/slice21-gate/*/report.json` 的 `started_at` 日期（無報告時 PASS） |
| `test_freeze_refuses_overwrite` | 以暫存目錄執行兩次 `freeze_slice_cases.py` → 第二次 exit 1、檔案不變 |
| `test_breaker_trips_on_flat_rate` | 三週 snapshot rate `0.40, 0.44, 0.48`（累計 +8 pp）→ `tripped True` |
| `test_breaker_counts_only_valid_pass` | results 含 `PASS`／`SKIP`／`EVIDENCE_MISSING` 各一 → `acceptance_rate == 1/denominator` |

`tests/test_slice21_scope.py` 加：

| 測試名 | 精確斷言 |
|---|---|
| `test_scope_matches_cases_yaml` | `slice21_scope.yaml.cross_slice_cases` 中 `func-test`／`regression` 的 items 集合 == `cases.yaml` 中同 suite、`daemon_slice > "2.1"` 的 case（regression 以 family 比對）；`early_evidence_cases` 每筆在 lock 屬 > 2.1 |
| `test_l15_and_windows_required_listed` | lock `slice_2.1` 中 `suite ∈ {l15-scenario, windows-native}` 的 15 個 id（`l15-delayed-reply`、`l15-half-prompt`、`l15-slow-reader-backpressure`、`l15-transport-disconnect`、`l15-multi-source-contention-cli`、`l15-stale-event-delay`、`l15-wal-interference`、`l15-reject-zero-tx`、`win-com-open-exclusive`、`win-com-txrx-bridge`、`win-com-cancel-close`、`win-wal-av-rotate`、`win-wal-av-unlink`、`win-wal-av-reader-handle`、`win-wal-f12-progress`）全部出現在 Task 21 的執行清單或 BLOCKED-OWNER 總表的 `case_ids` 欄 |

- [ ] **Step 2：執行並確認 FAIL**

Run：`python3 -m pytest -q tests/test_slice21_case_freeze.py tests/test_slice21_scope.py`
Expected：FAIL，`FileNotFoundError: .../tests/acceptance/breaker/slice_2.1.json`。

- [ ] **Step 3：實作**

依 Produces 寫兩支腳本（stdlib＋PyYAML）；以開工日執行 `python3 scripts/rust/freeze_slice_cases.py --slice 2.1 --date <開工日>` 產生凍結檔並 check-in。每週一以 Task 22 最近一次 gate 報告計算 rate，append 一行到 `slice_2.1.snapshots.ndjson`（`{"date","rate","results_sha256"}`）。

- [ ] **Step 4：執行並確認 PASS**

Run：同 Step 2
Expected：13 項 PASS（`test_slice21_case_freeze.py` 5、`test_slice21_scope.py` 8）。

- [ ] **Step 5：Commit**

```bash
git add scripts/rust/freeze_slice_cases.py scripts/rust/slice_breaker.py tests/acceptance/breaker tests/test_slice21_case_freeze.py tests/test_slice21_scope.py changelog.d/rust-s21-case-freeze.md
git commit -m "chore(tests): 凍結 Slice 2.1 必要案例分母與承接率斷路器" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 0c：上游缺漏介面補齊（sw-proto pycompat／Params／RuntimeConfig accessor、sw-pal Clock wall、RpcServerStream）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max（本 Task 含 PAL native `Clock::local_utc_offset_s`／`RpcServerStream`，master plan §2 PAL 歸實作 B）
**Changelog fragment:** `changelog.d/rust-s21-proto-additions.md`（`type: feat`、`scope: sw-proto`；本文：「補齊 daemon 需要的 Python 相容 helper（時間格式、uuid、可列印化、shlex、base64、round、RPC 布林轉型）、RPC 參數轉型、config accessor、wall clock 與 server 端 stream 介面，皆以 Python 產生的 golden 驗證。」）
**BLOCKED-OWNER：** 無。

**Files:**
- Modify: `crates/sw-proto/src/{pycompat.rs,rpc_types.rs,config_types.rs,runtime_paths.rs,wal_types.rs}`、`crates/sw-pal/src/traits.rs`（`Clock` 擴充、`RpcServerStream`）、`crates/sw-pal/src/{clock_native.rs}`、`docs/rust-refactor/plans/rust-architecture.md`（§1.2 `crc32fast` 用途、§4.1 `Clock`、§4.6 server stream）
- Create: `scripts/rust/goldens/gen_pycompat_s21.py`、`contracts/golden/pycompat_s21.tsv`（P3 04 Task 4 已有 `pycompat.tsv`，不得覆寫）
- Test: `crates/sw-proto/tests/slice21_additions.rs`

**Interfaces:**
- Consumes：P3 `pycompat`、`pyfmt`、`yaml11`、`config_types`、`runtime_paths`；Python `sw_core` 對應函式（只在 golden 產生器內 import）。
- Produces：§0.3「本 Phase 補齊」列全部符號；`PyErr { pub message: String }`（`PyErr::value_error(msg)`、`Display`＝message），framing 轉 `EXCEPTION` 時 `message` 即 `str(exc)`。

- [ ] **Step 1：寫失敗測試**

`crates/sw-proto/tests/slice21_additions.rs`：逐項 `use` §0.3「本 Phase 補齊」列所有符號；`pycompat_golden` 讀 `contracts/golden/pycompat_s21.tsv`（每行 `op\tinput_b64\texpected_b64`），op 涵蓋：`iso_utc_now`（含 `.000000` 省略、負 offset 不適用）、`strftime_utc`／`strftime_local`（`%Y%m%d-%H%M%S`、`%y%m%d-%H%M%S`）、`to_printable`（WAL mirror 規則）、`clean_text`、`strip_ansi`、`a2b_base64_lenient`（非 strict）、`shlex_split`（含引號錯誤 → `Err(ShlexError)`）、`shlex_quote`（09 §4.3 逐字）、`b2a_base64`、`py_round`（banker's rounding、`round(x, 1)`）、`coerce_rpc_bool`（`"yes"`、`"0"`、`0`、`null`）、`uuid4_hex`／`uuid4_dashed`（以固定 rng bytes）；`params_coercion_golden`（`Params::str_or/int_or/float_or/truthy/selector_chain` 照 02 §5.2 範例）；`runtime_config_accessors`（`windows.exclude_coms` 非 list → 空；`windows.test_device_allowlist` 非 list 或含非字串 → `Err(())`）；`daemon_dirs_defaults`（未設 env → `/dev/serial/by-id`、`/dev/serial/by-path`，與 `sw_core.constants` 相同）；`crc32_hex_matches_zlib`（`b"hello\r\n\x1b[0m\x0b\x7f\xff"` → `a81bc28c`）。

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-proto --test slice21_additions`
Expected：FAIL，`not implemented: slice21 T0c iso_utc_now`（Task 0 骨架已宣告簽章）。

- [ ] **Step 3：實作**

`gen_pycompat_s21.py` 以 Python 基線（P1 後）函式產生 golden，至少 300 行；Rust 端照 golden 實作，不得只為通過 golden 加特例。`Clock::wall_unix_us` native 用 `SystemTime`；`local_utc_offset_s` POSIX 用 `localtime_r` 的 `tm_gmtoff`、Windows 用 `GetTimeZoneInformation`；P3 既有 fake clock 與 Task 1 `FakeClock` 一併實作。`RpcServerStream` POSIX 為 `UnixStream`／`TcpStream` 包裝（`shutdown_read` = `shutdown(Read)`）。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-proto --test slice21_additions && cargo test -p sw-core --test slice21_prereqs`
Expected：PASS。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-proto crates/sw-pal/src/traits.rs crates/sw-pal/src/clock_native.rs scripts/rust/goldens/gen_pycompat_s21.py contracts/golden/pycompat_s21.tsv docs/rust-refactor/plans/rust-architecture.md changelog.d/rust-s21-proto-additions.md
git commit -m "feat(sw-proto): 補齊 daemon 所需 Python 相容 helper 與介面" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 1：PAL test-fakes（FakeClock／FakeSerial／FakeWalFs／FakeDeviceSource／MemStream）

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** 無（`test-fakes` feature 只供測試；不動 `code_paths` 的產品行為，與 Task 2 同 PR 時併入 `changelog.d/rust-s21-pal-serial.md`）。
**BLOCKED-OWNER：** 無。

**Files:**
- Modify: `crates/sw-pal/src/fakes/{mod.rs,clock.rs,serial.rs,wal_fs.rs,device.rs,mem_stream.rs}`
- Test: `crates/sw-pal/tests/fakes_selftest.rs`

**Interfaces:**
- Consumes：Task 0 traits。
- Produces：

```rust
// clock.rs（擴充 P3 04 Task 9 的同名型別，不另建第二個 FakeClock：P3 公開欄位 `now_ns`／`sleeps`、`advance`、`sleeps_s` 與 P4 05 Task 1 的 `new() -> Arc<Self>`／`elapsed()` 一律保留；
//   `new()` 建出的實例維持 P3 語意「sleep 立即推進虛擬時間並記錄」（CLI 單執行緒測試依賴）；`at()` 建出的實例為 threaded 模式，sleep 阻塞到另一 thread advance）
pub struct FakeClock { /* P3 欄位 + wall_us、local_offset_s、mode: AutoAdvance|Threaded、Condvar */ }
impl FakeClock {
    pub fn at(mono_ns: u64, wall_unix_us: i64) -> Arc<FakeClock>;   // threaded 模式；P5～P8 的 actor／IoWorker 測試一律用它
    pub fn advance(&self, d: Duration);                // mono 與 wall 一起前進，喚醒 sleep 中的等待者
    pub fn set_wall_us(&self, wall_unix_us: i64);      // 只動 wall（Review Focus 4）
    pub fn set_local_offset_s(&self, s: i32);
    pub fn subscribe(&self) -> crossbeam_channel::Receiver<()>;   // 每次 advance 送一個 ()（容量 1、滿則略過）；actor 以此在 FakeClock 下即時處理到期 timer
}
impl Clock for FakeClock { /* threaded：sleep(d) 阻塞到另一 thread advance 超過 d；auto-advance（P3/P4 `new()`）：sleep 立即推進；wall_unix_us／local_utc_offset_s（Task 0c 擴充） */ }

// serial.rs
#[derive(Debug, Clone)]
pub enum FakeWriteStep {
    Accept(usize),                         // 接受前 n bytes 後回 Completed(累計)
    AcceptThenError(usize, PalError),      // 接受 n bytes 後回 Completed(n)+error
    Unknown { confirmed: usize, error: PalError }, // 回 TxOutcome::Unknown { confirmed } + error
    BlockUntilCancel { accept_before_cancel: usize }, // 經 gate 提交後（已交 OS、in_flight 已設）於 gate 外阻塞直到 CancelToken::cancel，回 Completed(accept_before_cancel)
    BlockForever,                          // 經 gate 提交後於 gate 外阻塞直到 FakeDevice::release_block（模擬 D-state；不持 Fence 鎖）
    Error(PalError),                       // 0 byte + error
    // R0-F1 線性化測試用（arch §5.4 第 3 點）：
    PauseBeforeSubmit,                     // write_gated 進入後、第一次 gate.submit 之前暫停（不持鎖），等 release_pause 後才提交全部 bytes
    PauseInsideSubmit,                     // gate 已重驗通過、os_submit 閉包內（持 Fence 鎖）暫停，等 release_pause 後接受全部 bytes
    AcceptThenWouldBlock(usize),           // 第一次 gate.submit 接受 n bytes，隨後回 WouldBlock 並於 gate 外暫停；release_pause 後經 gate 提交剩餘
}
pub struct FakeSerialBackend { /* Mutex<HashMap<String, Arc<FakeDeviceInner>>> */ }
impl FakeSerialBackend {
    pub fn new() -> Arc<FakeSerialBackend>;
    pub fn add_device(&self, path: &str) -> FakeDevice;
    pub fn hold_externally(&self, path: &str, pids: Vec<u32>);   // 下次 open_exclusive → Held(pids, holder_unknown=pids.is_empty())
    pub fn deny_access(&self, path: &str);                       // → AccessDenied{holder: pids [] / holder_unknown true / Unsupported}
    pub fn remove_device(&self, path: &str);                     // → NotFound；已開 handle 的 read 回 Err(kind=Io, "[Errno 5] Input/output error")
}
#[derive(Clone)] pub struct FakeDevice { /* Arc<FakeDeviceInner> */ }
impl FakeDevice {
    pub fn push_rx(&self, bytes: &[u8]);
    pub fn take_tx(&self) -> Vec<u8>;                 // 只含已「確認寫出」的 bytes
    pub fn script_write(&self, steps: Vec<FakeWriteStep>);  // 依序消耗；耗盡後恢復 Accept(全部)
    pub fn release_block(&self);
    pub fn fail_close(&self, err: PalError);
    pub fn open_count(&self) -> usize;
    pub fn close_count(&self) -> usize;
    pub fn is_open(&self) -> bool;
    pub fn write_calls(&self) -> usize;               // writer.write／write_gated 被呼叫次數（TxWriter 出列快速拒絕的零 TX 斷言用）
    pub fn os_submits(&self) -> usize;                // os_submit 閉包實際執行（bytes 交給假 OS）的次數；gate 撤銷不計。零 TX 以此值與 take_tx 為準
    pub fn on_os_submit(&self, hook: Box<dyn Fn() + Send + Sync>);   // 每次 os_submit 閉包內（持 Fence 鎖時）呼叫；鎖持有斷言用
    pub fn wait_paused(&self, timeout: Duration) -> bool;            // 等 writer 抵達 Pause*／AcceptThenWouldBlock 的暫停點
    pub fn release_pause(&self);
    pub fn on_tx_reply(&self, trigger: &[u8], reply: &[u8]);                 // 已確認 TX 累積緩衝出現 trigger 時推一次 reply
    pub fn on_tx_match(&self, pattern: &str, reply: Box<dyn Fn(&[u8]) -> Vec<u8> + Send + Sync>); // pattern 以 sw_proto::pyre 編譯，對每個已確認 TX 行比對；reply 取得命中的行（random nonce probe 用：回顯 nonce＋prompt）
}
impl SerialBackend for FakeSerialBackend {}
pub struct FakeUartExclusive { pub report: Mutex<HashMap<String, HolderReport>> }
impl UartExclusive for FakeUartExclusive {}

// wal_fs.rs
pub struct FakeWalFs { /* 記憶體檔案系統，path → (identity u128, bytes, delete_pending, open_handles) */ }
impl FakeWalFs {
    pub fn new() -> Arc<FakeWalFs>;
    pub fn contents(&self, path: &Path) -> Option<Vec<u8>>;
    pub fn tail_needs_termination(&self, path: &Path) -> bool;       // 空檔／換行結尾回 false；否則 true
    pub fn list_dir(&self, dir: &Path) -> Vec<PathBuf>;            // 排序後
    pub fn external_unlink(&self, path: &Path);                    // POSIX 語意：路徑消失、handle 存活、unlinked()=true
    pub fn external_replace(&self, path: &Path, bytes: &[u8]);     // 新 identity 佔用路徑
    pub fn hold_external_reader(&self, path: &Path) -> ExternalHandle; // Windows pending-delete 語意：unlink 後路徑保留到 handle drop
    pub fn fail_next_writes(&self, path: &Path, n: usize, err: PalError);   // 接下來 n 次 write_all_flush 不寫任何 byte，回 Err(WalWriteError{sent:0, error:err})
    pub fn fail_next_write_partial(&self, path: &Path, accept: usize, err: PalError); // 下一次 write_all_flush 只附加前 min(accept, len-1) bytes，回 Err(WalWriteError{sent:<實際附加數>, error:err})（R2-F3）；與 fail_next_writes 依呼叫順序排入同一佇列
    pub fn write_calls(&self, path: &Path) -> Vec<Vec<u8>>;        // 依序記錄每次 write_all_flush 收到的 bytes（斷言「只補寫剩餘部分」用）
    pub fn open_calls(&self, path: &Path) -> usize;                // open_append 成功次數（斷言 sent==0 分支有重開）
    pub fn fail_next_renames(&self, n: usize, err: PalError);
    pub fn fail_next_opens(&self, path: &Path, n: usize, err: PalError);
    pub fn sync_calls(&self, path: &Path) -> usize;
    pub fn set_dir_writable(&self, dir: &Path, writable: bool);
}
impl WalFs for FakeWalFs {}

// device.rs
pub struct FakeDeviceSource { /* Mutex<VecDeque<PalResult<Vec<(String, DeviceInfo)>>>> + last */ }
impl FakeDeviceSource { pub fn new(initial: Vec<(String, DeviceInfo)>) -> (FakeDeviceSource, FakeDeviceScript); }
pub struct FakeDeviceScript { /* Arc */ }
impl FakeDeviceScript { pub fn set(&self, snapshot: Vec<(String, DeviceInfo)>); pub fn fail_next(&self, err: PalError); }

// mem_stream.rs
pub struct MemStream { /* 雙向記憶體 pipe */ }
impl MemStream { pub fn pair() -> (MemStream, MemStream); pub fn close_write(&mut self); }
impl RpcServerStream for MemStream {}           // Read＋Write＋shutdown_read＋set_read_timeout（Task 0c）
```

- [ ] **Step 1：寫失敗測試**

`crates/sw-pal/tests/fakes_selftest.rs`：

```rust
#![cfg(feature = "test-fakes")]
use std::{sync::Arc, thread, time::Duration};
use sw_pal::fakes::{clock::FakeClock, serial::*};
use sw_pal::*;

fn cfg() -> SerialConfig { SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false) }

#[test]
fn partial_write_then_error_reports_confirmed_bytes_only() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device("/dev/ttyFAKE0");
    dev.script_write(vec![FakeWriteStep::AcceptThenError(3, PalError::io(5, "[Errno 5] Input/output error"))]);
    let h = be.open_exclusive(&DevicePath("/dev/ttyFAKE0".into()), &cfg()).unwrap();
    let (_r, mut w, waker, _close) = h.split();
    let res = w.write(b"0123456789", &CancelToken::new(waker));
    assert_eq!(res.outcome, TxOutcome::Completed(3));
    assert!(res.error.is_some());
    assert_eq!(dev.take_tx(), b"012");
}

#[test]
fn block_until_cancel_returns_bytes_before_cancel() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device("/dev/ttyFAKE0");
    dev.script_write(vec![FakeWriteStep::BlockUntilCancel { accept_before_cancel: 4 }]);
    let h = be.open_exclusive(&DevicePath("/dev/ttyFAKE0".into()), &cfg()).unwrap();
    let (_r, mut w, waker, _close) = h.split();
    let tok = CancelToken::new(waker);
    let t2 = tok.clone();
    let j = thread::spawn(move || w.write(b"abcdefgh", &t2));
    thread::sleep(Duration::from_millis(20));
    tok.cancel();
    let res = j.join().unwrap();
    assert_eq!(res.outcome, TxOutcome::Completed(4));
    assert_eq!(res.error.unwrap().kind, PalErrorKind::Cancelled);
}

#[test]
fn second_open_while_held_is_held_with_unknown_holder() {
    let be = FakeSerialBackend::new();
    be.add_device("/dev/ttyFAKE0");
    let _h = be.open_exclusive(&DevicePath("/dev/ttyFAKE0".into()), &cfg()).unwrap();
    match be.open_exclusive(&DevicePath("/dev/ttyFAKE0".into()), &cfg()) {
        Err(OpenError::Held(rep)) => { assert!(rep.pids.is_empty()); assert!(rep.holder_unknown); }
        other => panic!("expected Held, got {other:?}"),
    }
}

#[test]
fn close_requires_reader_and_writer_and_counts_once() {
    let be = FakeSerialBackend::new();
    let dev = be.add_device("/dev/ttyFAKE0");
    let h = be.open_exclusive(&DevicePath("/dev/ttyFAKE0".into()), &cfg()).unwrap();
    let (r, w, _waker, close) = h.split();
    let rc = close.close(r, w);
    assert!(rc.closed && rc.excl_cleared && rc.error.is_none());   // fake 不模擬殘留獨占旗標；殘留情境由 Task 2 原生 PTY 測試驗證（R0-F3）
    assert_eq!((dev.open_count(), dev.close_count(), dev.is_open()), (1, 1, false));
}

#[test]
fn fake_clock_sleep_wakes_on_advance_and_wall_jump_keeps_mono() {
    let c = FakeClock::at(1_000, 1_760_000_000_000_000);
    let c2: Arc<dyn Clock> = c.clone();
    let j = thread::spawn(move || { c2.sleep(Duration::from_millis(500)); c2.mono_ns() });
    thread::sleep(Duration::from_millis(10));
    c.set_wall_us(1_759_996_400_000_000);
    c.advance(Duration::from_millis(500));
    assert_eq!(j.join().unwrap(), 1_000 + 500_000_000);
}
```

另以表格補齊（每列一個 `#[test]`）：

| 測試名 | 精確斷言 |
|---|---|
| `unknown_step_reports_unknown` | `Unknown { confirmed: 0, error: e }` → `outcome == TxOutcome::Unknown { confirmed: 0 }`、`take_tx()` 為空 |
| `deny_access_maps_to_access_denied` | `Err(OpenError::AccessDenied{holder})`，`holder == HolderReport{pids: vec![], holder_unknown: true, status: HolderScanStatus::Unsupported}` |
| `removed_device_read_errors` | `remove_device` 後 `read` 回 `Err(e)` 且 `e.py_message == "[Errno 5] Input/output error"` |
| `reader_woken_by_waker` | 另一 thread `waker.wake()` 後 `read(.., Some(1s))` 回 `Ok(ReadOutcome::Woken)` |
| `fail_close_reports_error` | `fail_close(e)` 後 `CloseReceipt{closed:false, excl_cleared:false, error:Some(_)}` |
| `wal_fs_external_unlink_keeps_handle` | `open_append` → `external_unlink` → `file.unlinked() == Ok(true)`、`path_identity == Ok(None)` |
| `wal_fs_pending_delete_with_external_reader` | `hold_external_reader` 後 `external_unlink`：`path_identity` 仍回 `Some(old)`、`open_append` 回 `Err(kind=AccessDenied)` 直到 handle drop |
| `wal_fs_partial_write_reports_sent_and_keeps_prefix`（R2-F3） | `fail_next_write_partial(p, 4, PalError::io(28, "[Errno 28] No space left on device"))`；`file.write_all_flush(b"abcdefgh\n")` → `Err(e)`、`e.sent == 4`、`e.error.py_message == "[Errno 28] No space left on device"`、`contents(p) == Some(b"abcd")`；再 `write_all_flush(b"efgh\n")` → `Ok(())`、`contents(p) == Some(b"abcdefgh\n")`、`write_calls(p) == [b"abcdefgh\n", b"efgh\n"]`；`fail_next_writes(p, 1, e)` 後寫入 → `Err(e2)`、`e2.sent == 0`、內容不變 |
| `mem_stream_roundtrip` | 一端寫 `b"x\n"`，另一端讀到相同 bytes；`close_write` 後讀回 0 |
| `on_tx_reply_pushes_rx_once` | `on_tx_reply(b"echo hi\n", b"hi\r\n# ")` → 寫 `b"echo hi\n"` 後 `read` 讀到 reply；再寫一次不再推 |
| `on_tx_match_echoes_nonce` | `on_tx_match(r"^echo __READY__([0-9a-f]{8})$", |l| …)` → 寫 `b"echo __READY__ab12cd34\n"` 後讀到含 `__READY__ab12cd34` 的回覆 |
| `fake_clock_subscribe_notified_on_advance` | `subscribe()` 後 `advance(1ms)` → `recv_timeout(100ms)` 成功 |
| `fake_writer_revoking_gate_never_submits`（R0-F1） | 以永遠回 `Err(SubmitRevoked)` 的 gate 呼叫 `write_gated(b"abc", ..)` → `outcome == NotSubmitted`、`error.is_none()`、`os_submits()==0`、`take_tx()` 為空、`write_calls()==1` |
| `fake_revoke_after_partial_stops_tail`（R0-F1） | `AcceptThenWouldBlock(2)`；gate 第一次放行、第二次回 `Err(SubmitRevoked)`；`wait_paused` 後 `release_pause` → `outcome == Completed(2)`、`error.kind == Cancelled`、`take_tx()==b"ab"`、`os_submits()==1` |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-pal --features test-fakes --test fakes_selftest`
Expected：FAIL，`not implemented: slice21 T1 FakeSerialBackend::add_device`。

- [ ] **Step 3：實作**

- `FakeDeviceInner { rx: Mutex<VecDeque<u8>>, tx: Mutex<Vec<u8>>, steps: Mutex<VecDeque<FakeWriteStep>>, cond: Condvar, open: AtomicBool, opens: AtomicUsize, closes: AtomicUsize, writes: AtomicUsize, blocked_release: AtomicBool, close_err: Mutex<Option<PalError>>, removed: AtomicBool }`。
- `open_exclusive`：`removed` → `NotFound(PalError::not_found("[Errno 2] No such file or directory"))`；`deny_access` 標記 → `AccessDenied`；外部 hold → `Held(HolderReport{pids, holder_unknown: pids.is_empty(), status: Ok})`；已 open → `Held(HolderReport{pids: vec![], holder_unknown: true, status: HolderScanStatus::Ok})`（與 POSIX flock 自身持有、排除自身 pid 後為空一致）；否則 `open=true`、`opens+=1`。
- `split`：reader/writer 共用 inner；waker 以 `Condvar::notify_all` 加 `woken` 旗標實作。
- writer.write_gated：`writes+=1`；`cancel.is_cancelled()` 已為真 → `TxResult{outcome: NotSubmitted, error: Some(Cancelled)}`（**只有** fake 在呼叫前取消時才回 NotSubmitted；真實 PAL 由 TxWriter 在呼叫前決定）；依 steps 處理。**所有**「接受 bytes」的動作都放在傳給 `gate.submit` 的 `os_submit` 閉包內（閉包內 `os_submits+=1`、呼叫 `on_os_submit` hook、把 bytes 推入 `tx`）；`gate.submit` 回 `Err(SubmitRevoked)` 時依 arch §4.2 回 `NotSubmitted` 或 `Completed(k)`＋`cancelled`，不再提交。`BlockUntilCancel`／`BlockForever` 先經 gate 提交 `accept_before_cancel`／全部 bytes，再於**閉包外**以 `cond.wait_while(!cancelled)`／等 `blocked_release`（不持 Fence 鎖）；`PauseBeforeSubmit` 在第一次 `gate.submit` 前、`PauseInsideSubmit` 在閉包內、`AcceptThenWouldBlock(n)` 在閉包回 `WouldBlock` 後於閉包外等待 `release_pause`。`write`（無 gate）＝`write_gated(.., &AlwaysOpen)`，供既有 PAL 自測沿用。
- reader.read：有資料 → 複製最多 `buf.len()`；`woken` → 清旗標回 `Woken`；逾時回 `TimedOut`。
- FakeWalFs identity 以遞增 `u128` 產生，`FileIdentity(1, id)`。
- FakeWalFs 寫入失敗佇列（per path，`fail_next_writes` 與 `fail_next_write_partial` 依呼叫順序排入）：每次 `write_all_flush` 先記入 `write_calls`，佇列首項為 `Full(err)` → 不附加、回 `WalWriteError{sent:0, error}`；`Partial(accept, err)` → 附加 `bytes[..min(accept, len-1)]`、回 `WalWriteError{sent:<附加數>, error}`；佇列空 → 全部附加、回 `Ok(())`。`sent` 必須等於實際附加到檔案的 bytes 數（arch §4.5 `WalWriteError`）。
- `sw_pal::fakes` 只在 `feature = "test-fakes"` 編譯（`#[cfg(feature = "test-fakes")] pub mod fakes;`）。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-pal --features test-fakes --test fakes_selftest`
Expected：19 passed。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-pal/src/fakes crates/sw-pal/tests/fakes_selftest.rs
git commit -m "test(sw-pal): 實作 Slice 2.1 記憶體替身（序列埠、時鐘、WAL 檔案、裝置來源）" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 2：PAL POSIX serial＋UartExclusive＋哨兵 PTY

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-pal-serial.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust PAL POSIX 序列埠：flock＋TIOCEXCL 獨占開啟（關閉與失敗路徑先 TIOCNXCL）、可喚醒讀取、可取消寫入與三態 TX 結果、/proc holder 診斷（BC-04、BC-03 前提）。」）
**BLOCKED-OWNER：** (1) 實體 USB-UART（CH340／FTDI）上的 `TIOCEXCL`＋`flock` 行為（含 `CloseToken` 的 TIOCNXCL 後外部工具立即可開、daemon 被 SIGKILL 後 tty 旗標隨最後一個 handle 重置）與 RTS/CTS 卡住時的可取消寫入（`cap_physical_uart__*`，需擁有者 bench）；(2) 外部 minicom 先開埠後 daemon `open_exclusive` 得到 `Held` 的實機證據（L3 裝置衝突 P1）。

**Files:**
- Create: `crates/sw-pal/src/serial/posix.rs`、`crates/sw-pal/src/console_pty/posix.rs`、`crates/sw-pal/src/proc_holders/posix.rs`
- Modify: `crates/sw-pal/src/serial.rs`、`crates/sw-pal/src/console_pty.rs`、`crates/sw-pal/src/conformance/serial_suite.rs`、`crates/sw-pal/src/lib.rs`（`native()` 組裝）
- Test: `crates/sw-pal/tests/serial_posix.rs`、`crates/sw-pal/tests/serial_posix_process.rs`

**Interfaces:**
- Consumes：Task 0 traits、Task 1 fakes（conformance 在 fakes 上同跑）。
- Produces：`sw_pal::native().serial: Arc<dyn SerialBackend>`、`native().uart_excl: Arc<dyn UartExclusive>`、`native().console: Arc<dyn ConsoleTransportFactory>`（POSIX `kind()==ConsoleKind::Pty`，`create_pty` 可用）；`pub fn sw_pal::conformance::serial_suite(backend: Arc<dyn SerialBackend>, pair: &dyn Fn() -> (DevicePath, Box<dyn PeerEnd>))`，`pub trait PeerEnd: Send { fn read_available(&mut self, timeout: Duration) -> Vec<u8>; fn write(&mut self, b: &[u8]); fn stop_reading(&mut self); fn resume_reading(&mut self); }`；`PtyPair { master: Box<dyn ConsoleIo>, slave_keepalive: KeepAlive, slave_path: String, slave_ref: PtySlaveRef }`（arch §4.7）。

- [ ] **Step 1：寫失敗測試**

`crates/sw-pal/tests/serial_posix.rs`（`posix_pty` 資源；Linux 上才編譯）：

```rust
// sw-pal 是唯一允許平台 cfg 的 crate
#![cfg(all(unix, feature = "test-fakes"))]
use std::{thread, time::{Duration, Instant}};
use sw_pal::{conformance::posix_pty_pair, *};

fn cfg() -> SerialConfig { SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false) }

#[test] #[ignore = "cap:posix_pty"]
fn cap_posix_pty__second_open_in_process_is_held() {
    let (dev, _peer) = posix_pty_pair();
    let pal = sw_pal::native();
    let _h = pal.serial.open_exclusive(&dev, &cfg()).expect("first open");
    match pal.serial.open_exclusive(&dev, &cfg()) {
        Err(OpenError::Held(rep)) => assert!(rep.holder_unknown, "self pid excluded → pids empty → holder_unknown"),
        other => panic!("expected Held, got {other:?}"),
    }
}

#[test] #[ignore = "cap:posix_pty"]
fn cap_posix_pty__external_plain_open_after_tiocexcl_is_ebusy() {
    if sw_pal::conformance::has_cap_sys_admin() { eprintln!("EVIDENCE_MISSING: TIOCEXCL bypassed under CAP_SYS_ADMIN"); return; } // 報告計為證據不足
    let (dev, _peer) = posix_pty_pair();
    let _h = sw_pal::native().serial.open_exclusive(&dev, &cfg()).unwrap();
    let err = std::fs::OpenOptions::new().read(true).write(true).open(&dev.0).unwrap_err();
    assert_eq!(err.raw_os_error(), Some(libc::EBUSY));
}

/// 規格 §6.2「拒絕已識別的外部衝突」：外部程式先以普通 open（不 flock、不設 TIOCEXCL）持有，
/// 本端 open→flock→TIOCEXCL 都會成功，必須靠鎖後 holder 重掃拒絕；被拒時不得改 termios、不得送任何 byte。
#[test] #[ignore = "cap:posix_pty"]
fn cap_posix_pty__external_plain_open_before_daemon_is_held() {
    let (dev, mut peer) = posix_pty_pair();
    let child = sw_pal::conformance::spawn_plain_holder(&dev);          // 子程序 open(O_RDWR|O_NOCTTY) 後 sleep；不 flock、不 TIOCEXCL
    let before = sw_pal::conformance::tcgetattr_snapshot(&dev);
    match sw_pal::native().serial.open_exclusive(&dev, &cfg()) {
        Err(OpenError::Held(rep)) => {
            assert_eq!(rep.pids, vec![child.id()]);
            assert!(!rep.holder_unknown);
        }
        other => panic!("expected Held for plain external holder, got {other:?}"),
    }
    assert_eq!(sw_pal::conformance::tcgetattr_snapshot(&dev), before, "被拒時不得改寫外部工具的 termios");
    assert_eq!(peer.read_available(Duration::from_millis(500)), Vec::<u8>::new(), "被拒時 UART 零 TX");
    drop(child);                                                        // kill + wait
    // peer（PTY master）仍開啟：被拒路徑若只 close(fd) 未 TIOCNXCL，旗標殘留，此處會得 Held(EBUSY)（R0-F3）
    let _h = sw_pal::native().serial.open_exclusive(&dev, &cfg()).expect("holder 結束後可取得");
}

/// R0-F3：TIOCEXCL 是 tty 物件旗標，不隨 fd 關閉清除；PTY master 持續開啟時，CloseToken 必須先 TIOCNXCL 才關 fd，
/// 否則 PROBE→close→正式 open 與 release→外部工具 open 都會 EBUSY。本測試在修正前必定 FAIL（第一次 reopen 即 EBUSY）。
#[test] #[ignore = "cap:posix_pty"]
fn cap_posix_pty__probe_close_reopen_with_master_open() {
    if sw_pal::conformance::has_cap_sys_admin() { eprintln!("EVIDENCE_MISSING: TIOCEXCL bypassed under CAP_SYS_ADMIN"); return; }
    let (dev, mut peer) = posix_pty_pair();                                 // peer 持有 master 直到測試結束
    for round in 0..3 {                                                     // 模擬 PROBE bridge → 正式 bridge → 再一次 recover
        let h = sw_pal::native().serial.open_exclusive(&dev, &cfg()).unwrap_or_else(|e| panic!("round {round}: reopen failed {e:?}"));
        let (r, w, _waker, close) = h.split();
        let rc = close.close(r, w);
        assert!(rc.closed && rc.excl_cleared && rc.error.is_none(), "round {round}: {rc:?}");
        // 外部一般 open（不經 serialwrap）也必須成功：證明旗標已解除，而不只是同 backend 的路徑碰巧可用
        let ext = std::fs::OpenOptions::new().read(true).write(true).open(&dev.0)
            .unwrap_or_else(|e| panic!("round {round}: plain open after close must not be EBUSY, got {:?}", e.raw_os_error()));
        drop(ext);
    }
    assert_eq!(peer.read_available(Duration::from_millis(100)), Vec::<u8>::new(), "open／close 循環不得寫出任何 byte");
}

/// R0-F3：open_exclusive 在 TIOCEXCL 成功之後失敗（termios／eventfd），失敗返回前必須 TIOCNXCL。
#[test] #[ignore = "cap:posix_pty"]
fn cap_posix_pty__open_failure_after_tiocexcl_clears_excl() {
    if sw_pal::conformance::has_cap_sys_admin() { eprintln!("EVIDENCE_MISSING: TIOCEXCL bypassed under CAP_SYS_ADMIN"); return; }
    let (dev, _peer) = posix_pty_pair();
    let be = sw_pal::serial::posix::PosixSerialBackend::with_post_excl_fault(PalError::io(5, "injected tcsetattr failure")); // test-fakes only
    match be.open_exclusive(&dev, &cfg()) { Err(OpenError::Other(e)) => assert_eq!(e.kind, PalErrorKind::Io), other => panic!("{other:?}") }
    match std::fs::OpenOptions::new().read(true).write(true).open(&dev.0) {
        Ok(f) => drop(f),
        Err(e) => panic!("失敗路徑殘留 TIOCEXCL：errno={:?}", e.raw_os_error()),
    }
    let _h = sw_pal::native().serial.open_exclusive(&dev, &cfg()).expect("失敗後可重新取得");
}

#[test] #[ignore = "cap:posix_pty"]
fn cap_posix_pty__blocked_write_cancel_returns_partial_completed() {
    let (dev, mut peer) = posix_pty_pair();
    peer.stop_reading();                                   // 讓 PTY buffer 塞滿
    let h = sw_pal::native().serial.open_exclusive(&dev, &cfg()).unwrap();
    let (_r, mut w, waker, _c) = h.split();
    let tok = CancelToken::new(waker);
    let t2 = tok.clone();
    let payload = vec![b'A'; 1 << 20];
    let j = thread::spawn(move || w.write(&payload, &t2));
    thread::sleep(Duration::from_millis(200));
    let t0 = Instant::now();
    tok.cancel();
    let res = j.join().unwrap();
    assert!(t0.elapsed() < Duration::from_millis(100), "cancel must wake poll(POLLOUT)");
    match res.outcome { TxOutcome::Completed(n) => assert!(n > 0 && n < (1 << 20)), o => panic!("{o:?}") }
    assert_eq!(res.error.unwrap().kind, PalErrorKind::Cancelled);
    peer.resume_reading();
}

#[test] #[ignore = "cap:posix_pty"]
fn cap_posix_pty__reader_woken_within_50ms() {
    let (dev, _peer) = posix_pty_pair();
    let h = sw_pal::native().serial.open_exclusive(&dev, &cfg()).unwrap();
    let (mut r, _w, waker, _c) = h.split();
    let j = thread::spawn(move || { let mut b = [0u8; 64]; r.read(&mut b, None) });
    thread::sleep(Duration::from_millis(50));
    let t0 = Instant::now();
    waker.wake();
    assert_eq!(j.join().unwrap().unwrap(), ReadOutcome::Woken);
    assert!(t0.elapsed() < Duration::from_millis(50));
}

#[test] #[ignore = "cap:posix_pty"]
fn cap_posix_pty__conformance_suite() {
    sw_pal::conformance::serial_suite(sw_pal::native().serial.clone(), &|| {
        let (d, p) = posix_pty_pair(); (d, Box::new(p) as Box<dyn PeerEnd>)
    });
}

#[test]
fn fakes_conformance_suite() {
    let be = sw_pal::fakes::serial::FakeSerialBackend::new();
    sw_pal::conformance::serial_suite(be.clone(), &sw_pal::conformance::fake_pair_factory(be.clone()));
}
```

conformance `serial_suite` 內含下列斷言（每列一個子檢查，失敗訊息帶列名）：

| 子檢查 | 精確斷言 |
|---|---|
| `roundtrip_tx` | `write(b"hello\n")` → `Completed(6)`、`error None`；peer 讀到 `b"hello\n"` |
| `roundtrip_rx` | peer 寫 `b"\x00\xff\r\n"` → `read` 回 `Data(4)` 且內容相同 |
| `read_timeout` | 無資料 `read(Some(200ms))` 回 `TimedOut`，耗時 ∈ [180, 400] ms |
| `close_once` | `CloseToken::close(r, w)` 回 `closed:true`、`excl_cleared:true`、`error:None`；**peer 端（PTY master／COM pair 對端）全程保持開啟**下，之後同一路徑可再次 `open_exclusive` 成功（POSIX 若關閉前未 TIOCNXCL，此處得 `Held`，R0-F3） |
| `eof_not_bridge_down` | peer 關閉後 `read` 回 `Ok(Eof)`（呼叫端不得當成 I/O error，UIO-12.10-d KEEP） |
| `cancel_before_write` | 寫前已 cancel：`write` 回 `Completed(0)` 且 `error.kind == Cancelled`，peer 讀不到任何 byte |
| `gate_revoked_never_reaches_os`（R0-F1） | 以永遠回 `Err(SubmitRevoked)` 的計數 gate 呼叫 `write_gated(b"zz\n", ..)` → `NotSubmitted`、`error None`、gate 被呼叫恰 1 次、peer 於 300 ms 內讀不到任何 byte |
| `gate_wraps_every_os_submit`（R0-F1） | 計數 gate（放行並記錄閉包執行次數）寫 64 KiB → `Completed(65536)`；閉包執行次數 ≥ 1 且等於 PAL 實際 syscall／`WriteFile` 發出次數（PAL 內 debug 計數器）；peer 讀到完整 bytes |

POSIX 單元測試（不需資源，`crates/sw-pal/src/serial/posix.rs` 內 `#[cfg(test)]`）：

| 測試名 | 精確斷言 |
|---|---|
| `termios_bits_8n1_rtscts` | `build_termios(cfg rtscts)`（輸入的 tcgetattr 結果預置 `HUPCL|CS7|PARENB` 與 `c_iflag=ICRNL`）：`c_iflag == 0`、`c_oflag == 0`、`c_lflag == 0`、`c_cflag & !CBAUD == CREAD|CLOCAL|CS8|CRTSCTS`（**完全相等**：Python `attrs[2] = cflag` 是覆寫，HUPCL 被清除，避免每次 close 掉 DTR 重置板子）、`c_cc` 與輸入相同 |
| `termios_bits_7e2` | `data_bits=7, parity="E", stop_bits=2` → `c_cflag & !CBAUD == CREAD|CLOCAL|CS7|PARENB|CSTOPB` |
| `termios_odd` | `parity="O"` → `c_cflag & !CBAUD == CREAD|CLOCAL|CS8|PARENB|PARODD` |
| `xonxoff_ignored_on_posix` | `xonxoff=true` 時 `c_iflag == 0`（UIO-12.1-e KEEP） |
| `unknown_baud_falls_back_115200` | `baud=1234` → speed `B115200`；`set_baud(1234)` 回 `Ok(())` 且不呼叫 `tcsetattr` |
| `vmin_vtime_untouched` | `build_termios` 不改 `c_cc[VMIN]`／`c_cc[VTIME]`（UIO-12.10-g KEEP） |
| `holder_scan_excludes_self_and_matches_rdev` | 以 tempdir 假 `/proc`（`proc_root` 參數）建 `123/fd/3 -> /dev/pts/9`、自身 pid fd → 同路徑：`probe_holders_in(root, "/dev/pts/9").pids == vec![123]`、`status == Ok` |
| `holder_scan_unreadable_proc_is_unknown` | `proc_root` 不存在 → `HolderReport{pids: [], holder_unknown: true, status: Unknown}` |
| `holder_scan_permission`（root 下 skip，報告「證據不足」） | fd 目錄 `chmod 000` → `status == Permission`、`holder_unknown == true` |
| `holder_unknown_only_when_no_pids` | `probe_holders` 回 `pids:[123]`、`status: Permission`（部分目錄不可讀）→ `holders_or_unknown` 的 `holder_unknown == false`（P1 T10：`pids` 非空 → false，空 → true，與 status 無關） |
| `close_sequence_order_nxcl_before_close` | 以錄製替身 `TtyCloseOps`（`close_sequence(&mut ops)` 為 `CloseToken::close` 的純邏輯核心）：呼叫序恰為 `[Nxcl, FlockUnlock, CloseFd, CloseWakeR, CloseWakeW]`；回 `CloseReceipt{closed:true, excl_cleared:true, error:None}` |
| `close_sequence_nxcl_hangup_errno_is_cleared` | 參數化 `Nxcl` 回 `EIO`／`ENXIO`／`ENODEV`（tty 已 hangup／裝置拔除）→ 呼叫序不變、`closed:true`、`excl_cleared:true`、`error:None`、無 WARNING |
| `close_sequence_nxcl_other_errno_still_closes` | `Nxcl` 回 `ENOTTY` → 仍呼叫 `FlockUnlock, CloseFd, CloseWakeR, CloseWakeW`（不得保留 fd）；`closed:true`、`excl_cleared:false`、`error.py_message == "[Errno 25] Inappropriate ioctl for device"`；WARNING 恰一筆，前綴 `serialwrapd: TIOCNXCL failed on ` |
| `close_sequence_close_fd_failure` | `CloseFd` 回 `EIO` → `closed:false`、`excl_cleared:true`、`error.kind == Io`；wake fd 仍關閉 |
| `open_failure_after_excl_runs_nxcl_before_close` | 錄製替身注入 `apply_termios` 失敗（`open_sequence(&mut ops)` 純邏輯核心）→ 呼叫序尾段恰為 `[Excl, Termios(Err), Nxcl, CloseFd]`；鎖後重掃 `Reject` 時尾段恰為 `[Excl, ProbeHolders, Nxcl, CloseFd]`（不得出現 `Termios`）；`Excl` 之前（open／flock）失敗時序列中**不得**出現 `Nxcl` |
| `post_lock_holder_verdict_matrix` | `{pids:[123], status:Ok}` → `Reject`；`{pids:[123], status:Permission}` → `Reject`（已識別即拒絕，不論 status）；`{pids:[], status:Ok}` → `Accept`；`{pids:[], status:Unknown}`、`{pids:[], status:Permission}` → `AcceptUnverified`（不得回 `Accept`） |

跨程序與獨占必測情境（§6.2「兩個 daemon 競爭開埠、symlink 指向同一裝置、程序退出、handle 繼承」；`crates/sw-pal/tests/serial_posix_process.rs`，子程序以 `std::env::current_exe()` 加 `--exact <name>` 與 `S21_CHILD` env 重入本測試二進位）：

| 測試名 | 資源 | 精確斷言 |
|---|---|---|
| `cap_posix_pty__second_process_open_is_held` | posix_pty | 子程序持有 `open_exclusive`（不同 lock／socket 路徑，模擬第二個 daemon）→ 父程序 `open_exclusive` 回 `Held`，`pids` 含子程序 pid、`holder_unknown:false` |
| `cap_posix_pty__two_symlinks_same_tty_is_held` | posix_pty | 以兩個不同 symlink（`by-id/a`、`by-path/b`）指向同一 slave：經 a 開成功後經 b 開 → `Held`（`flock`／`TIOCEXCL` 作用在 inode） |
| `cap_posix_pty__holder_exit_releases_flock_excl_persists_while_master_open` | posix_pty（非 root；`CAP_SYS_ADMIN` 下 skip 記「證據不足」） | （更正：舊名 `holder_exit_releases_lock_and_excl` 的「SIGKILL 後 100 ms 內 `open_exclusive` 成功」與 kernel 行為不符——TIOCEXCL 只在 tty 物件釋放時重置，父程序持有 PTY master 時旗標存活。）父程序先以一般 open 取得保活 fd `keep`（不 flock）→ 子程序以 `PosixSerialBackend::with_uart_exclusive(NoHolders)`（test-fakes only；鎖後重掃固定回 `{pids:[], status:Ok}`——否則父程序的 `keep` 會被鎖後重掃識別為外部 holder 而 `Held`，R0-F2）`open_exclusive` 成功（真 open／flock／TIOCEXCL）後被 `SIGKILL` → 100 ms 內 `flock(keep, LOCK_EX\|LOCK_NB) == 0`（flock 隨程序結束釋放）；一般 open 回 `EBUSY`；`open_exclusive` 回 `Held{pids:[], holder_unknown:true}`；`ioctl(keep, TIOCNXCL)` 後 `open_exclusive` 成功。實體 UART 程序退出時 tty 物件隨最後一個 handle 釋放、旗標重置，屬 BO-S21-04 實機證據 |
| `cap_posix_pty__release_then_external_plain_open_succeeds` | posix_pty（非 root） | peer（PTY master）全程開啟；父程序 `open_exclusive` → `split` → `CloseToken::close` 回 `excl_cleared:true` → 子程序（模擬外部燒錄工具）以一般 `open(O_RDWR\|O_NOCTTY)` 開同一 slave：exit 0（子程序內 open 成功並寫 `b"X"`，peer 讀到 `b"X"`）；子程序結束後父程序 `open_exclusive` 再次成功（release→外部→attach 循環，R0-F3） |
| `cap_posix_pty__fd_not_inherited_by_child` | posix_pty | `open_exclusive` 後 spawn `sh -c 'ls -l /proc/self/fd'` → 輸出不含 slave 路徑與 eventfd（`O_CLOEXEC`／`EFD_CLOEXEC`） |
| `cap_posix_pty__concurrent_wake_reaches_reader_and_writer` | posix_pty | reader 阻塞於 `read(None)`、writer 阻塞於 `POLLOUT`（peer 停讀）時呼叫一次 `CancelToken::cancel()`＋`waker.wake()`：兩者都在 100 ms 內返回；重複 200 次無一遺失喚醒 |
| `cap_sys_admin__tiocexcl_bypass_documented` | posix_pty＋root（非 root skip） | root 下外部普通 `open` 在 TIOCEXCL 後**成功**（記錄行為，報告標「§6.2 例外，需權限管控」） |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-pal --features test-fakes --test serial_posix -- --include-ignored`
Expected：FAIL，`not implemented: slice21 T2 PosixSerialBackend::open_exclusive`（`fakes_conformance_suite` 會 PASS，因 Task 1 已實作）。

- [ ] **Step 3：實作**

`serial/posix.rs` 開啟序列（06 §10.2 加 BC-04）：

```rust
fn open_exclusive(&self, dev: &DevicePath, cfg: &SerialConfig) -> Result<Box<dyn SerialHandle>, OpenError> {
    let c = CString::new(dev.0.as_bytes()).map_err(|_| OpenError::Other(PalError::invalid("path contains NUL")))?;
    let fd = unsafe { libc::open(c.as_ptr(), libc::O_RDWR | libc::O_NOCTTY | libc::O_NONBLOCK | libc::O_CLOEXEC) };
    if fd < 0 {
        let e = errno();
        return Err(match e {
            libc::ENOENT | libc::ENODEV | libc::ENXIO => OpenError::NotFound(PalError::from_errno(e)),
            libc::EBUSY => OpenError::Held(self.holders_or_unknown(&dev.0)),       // 他人已 TIOCEXCL
            libc::EACCES | libc::EPERM => OpenError::AccessDenied { os: PalError::from_errno(e), holder: self.holders_or_unknown(&dev.0) },
            _ => OpenError::Other(PalError::from_errno(e)),
        });
    }
    let guard = FdGuard(fd);                                                         // 任何後續失敗都 close
    if unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        let e = errno();
        return Err(if e == libc::EWOULDBLOCK { OpenError::Held(self.holders_or_unknown(&dev.0)) } else { OpenError::Other(PalError::from_errno(e)) });
    }
    if unsafe { libc::ioctl(fd, libc::TIOCEXCL) } != 0 { return Err(OpenError::Other(PalError::from_errno(errno()))); }
    // R0-F3：TIOCEXCL 成功後改由 ExclGuard 持有；之後任何 `return Err`／`?` 在 drop 時先 ioctl(TIOCNXCL)（失敗依 close_sequence 的 errno 規則只記 WARNING）再 close(fd)。
    // 旗標掛在 tty 物件上、不隨 fd 關閉清除；PTY master 仍開啟或外部 holder 仍持有時，漏清會讓之後所有一般 open 都 EBUSY。
    let guard = ExclGuard::arm(guard);
    #[cfg(feature = "test-fakes")] if let Some(e) = self.post_excl_fault.clone() { return Err(OpenError::Other(e)); } // with_post_excl_fault：只供 open_failure_after_tiocexcl_clears_excl
    // 鎖後重掃（§6.2）：TIOCEXCL 不撤銷既有 handle，未取 flock 的外部程式（minicom）先開時前三步都成功
    let rep = self.excl.probe_holders(&dev.0);                                       // 排除自身 pid
    match post_lock_holder_verdict(&rep) {
        PostLockVerdict::Reject => return Err(OpenError::Held(HolderReport { holder_unknown: false, ..rep })), // ExclGuard drop → TIOCNXCL → close(fd)；termios 未動、零 TX
        PostLockVerdict::AcceptUnverified => log::warn!("serialwrap: 無法確認 {} 的外部 holder（status={:?}），BC-04 僅保證 serialwrap 實例互斥", dev.0, rep.status),
        PostLockVerdict::Accept => {}
    }
    apply_termios(fd, cfg).map_err(OpenError::Other)?;                               // tcgetattr → build_termios → tcsetattr(TCSANOW)
    let wake_r = EventFd::new().map_err(OpenError::Other)?;                          // reader 專用；Linux eventfd(EFD_NONBLOCK|EFD_CLOEXEC)，其他 unix self-pipe
    let wake_w = EventFd::new().map_err(OpenError::Other)?;                          // writer 專用（避免 reader 讀乾後 writer 漏醒）
    Ok(Box::new(PosixSerialHandle::new(guard.disarm_into_fd(), wake_r, wake_w)))   // 成功：解除獨占的責任移交給 CloseToken
}
fn holders_or_unknown(&self, path: &str) -> HolderReport {
    let mut r = self.excl.probe_holders(path);
    r.holder_unknown = r.pids.is_empty();          // P1 T10 凍結規則；status 只作診斷
    r
}
```

- reader：`poll([{fd, POLLIN}, {wake_fd, POLLIN}], timeout_ms)`；wake 可讀 → 讀乾 eventfd 回 `Woken`；fd `POLLIN` → `read(fd, buf)`：`>0` → `Data(n)`、`0` → `Eof`、`EAGAIN` → 回到 poll、其他 errno → `Err(PalError::from_errno)`（py_message `"[Errno N] <strerror>"`）；`POLLHUP|POLLERR` 無資料時讀一次取 errno。`timeout=None` 時 poll 不設上限。
- writer：

```rust
fn write_gated(&mut self, data: &[u8], cancel: &CancelToken, gate: &dyn SubmitGate) -> TxResult {
    let mut sent = 0usize;
    loop {
        if cancel.is_cancelled() { return TxResult { outcome: TxOutcome::Completed(sent), error: Some(PalError::cancelled()) }; }
        if sent == data.len() { return TxResult { outcome: TxOutcome::Completed(sent), error: None }; }
        // R0-F1：每一次 write(2) 都在 gate（Fence 鎖）內、重驗後才執行；fd 為 O_NONBLOCK，臨界區只含這一個不阻塞的 syscall
        let fd = self.fd;
        let step = gate.submit(&mut || {
            let n = unsafe { libc::write(fd, data[sent..].as_ptr().cast(), data.len() - sent) };
            if n > 0 { return SubmitStep::Accepted(n as usize); }
            let e = errno();
            if n < 0 && (e == libc::EAGAIN || e == libc::EWOULDBLOCK) { SubmitStep::WouldBlock }
            else if n == 0 { SubmitStep::Failed(PalError::io(0, "write returned 0")) } else { SubmitStep::Failed(PalError::from_errno(e)) }
        });
        match step {
            Err(SubmitRevoked) if sent == 0 => return TxResult { outcome: TxOutcome::NotSubmitted, error: None },
            Err(SubmitRevoked) => return TxResult { outcome: TxOutcome::Completed(sent), error: Some(PalError::cancelled()) }, // 尾段不交 OS
            Ok(SubmitStep::Accepted(n)) => { sent += n; continue; }
            Ok(SubmitStep::WouldBlock) => {
                // gate 外 poll(POLLOUT, wake_fd)，無逾時；wake → 下一輪檢查 cancel（UIO-12.2-b：不再 sleep(0.01) 無限重試）；
                // 下一段一律再經 gate 重驗（失效發生在等待期間 → 尾段不送）
                match poll_out_or_wake(self.fd, self.wake_fd) { Ok(_) => continue, Err(pe) => return TxResult { outcome: TxOutcome::Completed(sent), error: Some(pe) } }
            }
            Ok(SubmitStep::Failed(pe)) => return TxResult { outcome: TxOutcome::Completed(sent), error: Some(pe) },
            Ok(SubmitStep::Pending) => unreachable!("POSIX 不產生 Pending"),
        }
    }
}
```

POSIX writer 不產生 `TxOutcome::Unknown { confirmed }`（每次 syscall 回傳確切長度）；`Unknown` 只由 Windows 與 fake 產生。
- `IoWaker::wake` 對 `wake_r` 與 `wake_w` 各寫一次；reader 只 poll／讀乾 `wake_r`，writer 只 poll／讀乾 `wake_w`（兩個等待者各自消耗自己的喚醒，不會互相吃掉）。`CancelToken::cancel` 設旗標後呼叫同一個 waker。
- `CloseToken::close`（arch §4.2 關閉序，R0-F3）：drop reader/writer（I/O 已收斂）後經 `close_sequence(&mut LibcTtyCloseOps)`：`ioctl(fd, TIOCNXCL)` → `flock(LOCK_UN)` → `close(fd)` → `close(wake_r)` → `close(wake_w)`。`TIOCNXCL` 回 `EIO`／`ENXIO`／`ENODEV`（已 hangup／拔除）視為已解除；其他 errno → 照序繼續關閉、`excl_cleared:false`、`error:Some`、stderr WARNING `serialwrapd: TIOCNXCL failed on <dev>: <py_message>`；`close(fd)` 失敗 → `CloseReceipt{closed:false, error}`。`ExclGuard`（open 失敗路徑）的 drop 共用同一個 `close_sequence`（無 wake fd 時略過末兩步）。`PosixSerialBackend::with_post_excl_fault(PalError)` 只在 `feature = "test-fakes"` 編譯，於 TIOCEXCL 成功後立即回該錯誤（模擬 termios／eventfd 失敗）。
- `apply_termios`：`tcgetattr` → `c_iflag=c_oflag=c_lflag=0`、`c_cflag = CREAD|CLOCAL|CSx[|PARENB[|PARODD]][|CSTOPB][|CRTSCTS]`（**覆寫**，不 OR 到既有值；速度以 `cfsetispeed`／`cfsetospeed` 設定）、`c_cc` 不動 → `tcsetattr(TCSANOW)`。
- `probe_holders`：照 06 §9.5 `_probe_external_holder`（排除自身 pid；`readlink == realpath(path) || readlink == path` 或 `st_rdev` 相同且非 0；pid 升冪）；`/proc` 不可列 → `Unknown`；任一 fd 目錄 EACCES → `Permission`（保留已收集的 pids）。函式內部用 `probe_holders_in(proc_root: &Path, path: &str)` 供測試注入。
- `console_pty/posix.rs` `create_pty`：`openpty` → master `O_NONBLOCK` → slave termios `iflag=oflag=lflag=0`、`cflag=CREAD|CLOCAL|CS8`、`VMIN=1`、`VTIME=0`、`TCSANOW` → `slave_path = ttyname(slave)`；daemon 同持 master 與 slave（KeepAlive）。
- `conformance::posix_pty_pair()`：`openpty`，把 slave 路徑當 `DevicePath`、master 包成 `PeerEnd`（`stop_reading` 只是停止讀 master）。
- `conformance::spawn_plain_holder(dev: &DevicePath) -> PlainHolder`：以 `current_exe()` 重入（`S21_CHILD=plain_holder`）的子程序 `open(O_RDWR|O_NOCTTY)` 該路徑後回報 ready 並 sleep，不 `flock`、不 `TIOCEXCL`（模擬 minicom）；`PlainHolder::id() -> u32`，`Drop` 時 `kill`＋`wait`。`conformance::tcgetattr_snapshot(dev) -> Vec<u8>`：另開 `O_RDONLY|O_NOCTTY|O_NONBLOCK` 取 `termios` 位元組後關閉（只讀，供「未被改寫」比對）。
- 鎖後 holder 重掃（規格 §6.2，arch §4.2）：`TIOCEXCL` 成功後、`apply_termios` 之前呼叫 `self.excl.probe_holders(&dev.0)`（排除自身 pid）；`pids` 非空 → `ExclGuard` drop（`TIOCNXCL` → close fd，R0-F3）並回 `OpenError::Held(HolderReport{pids, holder_unknown:false, status})`；`pids` 為空且 `status ∈ {Unknown, Permission}` → 照常開啟並 `log::warn!("serialwrap: 無法確認 {} 的外部 holder（status={:?}），BC-04 僅保證 serialwrap 實例互斥")`（不得當安全證明）。純函式 `post_lock_holder_verdict(&HolderReport) -> PostLockVerdict::{Reject, Accept, AcceptUnverified}` 供單元測試。此檢查在 backend 內，呼叫端（含 `precheck_holders:false` 的 force 與自動 attach）無法略過。`PosixSerialBackend::with_uart_exclusive(Arc<dyn UartExclusive>)` 只在 `feature = "test-fakes"` 編譯，替換鎖後重掃所用的 holder 掃描（供 `cap_posix_pty__holder_exit_releases_flock_excl_persists_while_master_open` 這類測試本身須預持 fd 的情境），正式路徑不得使用。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-pal --features test-fakes --test serial_posix && cargo test -p sw-pal --features test-fakes --test serial_posix --test serial_posix_process -- --ignored cap_posix_pty__ && cargo test -p sw-pal --lib serial::posix`
Expected：全部 PASS；ignored 測試在 `--ignored` 下實際執行（報告不得把 ignored 計為 pass）。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-pal changelog.d/rust-s21-pal-serial.md
git commit -m "feat(sw-pal): POSIX 序列埠獨占開啟、可取消寫入與 holder 診斷" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 3：PAL Windows serial（overlapped、CancelIoEx）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-pal-serial-windows.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust PAL Windows 序列埠：CreateFileW 獨占（dwShareMode=0）、overlapped 讀寫、CancelIoEx 後 GetOverlappedResult 收斂、ACCESS_DENIED 以 holder_unknown 表示。」）
**擁有者 host 可執行 gate（OD-15，原 BLOCKED-OWNER）：** 原生 Windows COM handle gate（OD-08）：`SW_TEST_COM_PAIR=COM20:COM21`（Spike 1 已通過的 com0com DUT-side pair）下的 `cap_windows_com__*` 全部項目，由 orchestrator 經 WSL interop 在擁有者 host 執行（Step 5 第二段），結果寫入 `target/win-test-report/<run_id>.json`。

**BLOCKED-OWNER：** (1) 實體 USB-UART 直連 Windows 的同一批項目（僅在 com0com 結果不足以判定時補做）；(2) 外部 TeraTerm／PuTTY 先開 COM 時 daemon 得到 `AccessDenied → DEVICE_STILL_HELD{pids:[],holder_unknown:true}` 的實機證據；(3) USB 拔除時 overlapped 在途 I/O 的完成碼（`ERROR_DEVICE_REMOVED`／`ERROR_OPERATION_ABORTED`）實測。

**Files:**
- Create: `crates/sw-pal/src/serial/windows.rs`
- Modify: `crates/sw-pal/src/serial.rs`、`crates/sw-pal/src/lib.rs`
- Test: `crates/sw-pal/tests/serial_windows.rs`

**Interfaces:**
- Consumes：Task 0 traits、Task 2 `conformance::serial_suite`。
- Produces：Windows 上 `native().serial`；純函式（平台無關，便於 Linux 上也能跑的單元測試，放 `serial/windows_model.rs`，無 `windows-sys` 依賴）：

```rust
pub struct DcbModel { pub baud: u32, pub byte_size: u8, pub parity: u8 /*0 NOPARITY,1 ODD,2 EVEN*/, pub stop_bits: u8 /*0 ONE,2 TWO*/,
                      pub f_binary: bool, pub f_outx_cts_flow: bool, pub rts_control: u8 /*1 ENABLE,2 HANDSHAKE*/,
                      pub dtr_control: u8 /*1 ENABLE*/, pub f_outx: bool, pub f_inx: bool, pub f_dsr_sensitivity: bool, pub f_outx_dsr_flow: bool }
pub fn dcb_model(cfg: &SerialConfig) -> DcbModel;
pub struct CommTimeoutsModel { pub read_interval: u32, pub read_total_multiplier: u32, pub read_total_constant: u32, pub write_total_multiplier: u32, pub write_total_constant: u32 }
pub fn comm_timeouts_model() -> CommTimeoutsModel;
pub fn win_device_name(path: &str) -> String;                         // _win_device_name
pub fn map_open_error(win32: u32, py_message: String) -> OpenErrorKind; // 純映射
pub enum OpenErrorKind { NotFound, AccessDenied, Other }
pub fn map_overlapped_completion(get_result_ok: bool, transferred: u32, win32: u32) -> (TxOutcome, Option<PalErrorKind>);
```

- [ ] **Step 1：寫失敗測試**

`crates/sw-pal/tests/serial_windows.rs`：

```rust
#![cfg(feature = "test-fakes")]
use sw_pal::serial::windows_model::*;
use sw_pal::*;

#[test]
fn dcb_matches_pyserial_defaults_rtscts() {
    let m = dcb_model(&SerialConfig::from_uart_profile(115200, 8, "N", 1, "rtscts", false));
    assert_eq!((m.baud, m.byte_size, m.parity, m.stop_bits), (115200, 8, 0, 0));
    assert!(m.f_binary && m.f_outx_cts_flow);
    assert_eq!(m.rts_control, 2);   // RTS_CONTROL_HANDSHAKE
    assert_eq!(m.dtr_control, 1);   // DTR_CONTROL_ENABLE（pyserial dsrdtr=False）
    assert!(!m.f_outx && !m.f_inx && !m.f_dsr_sensitivity && !m.f_outx_dsr_flow);
}

#[test]
fn comm_timeouts_follow_pyserial_constants() {
    let t = comm_timeouts_model();
    assert_eq!((t.read_interval, t.read_total_multiplier, t.read_total_constant), (u32::MAX, u32::MAX, 200));
    assert_eq!((t.write_total_multiplier, t.write_total_constant), (0, 5000));
}

#[test]
fn device_name_normalization() {
    assert_eq!(win_device_name("com3"), r"\\.\COM3");
    assert_eq!(win_device_name(r"\\.\COM12"), r"\\.\COM12");
    assert_eq!(win_device_name("/dev/ttyUSB0"), "/dev/ttyUSB0");
}

#[test]
fn access_denied_is_not_a_holder_claim() {
    assert!(matches!(map_open_error(5, "[WinError 5] Access is denied.".into()), OpenErrorKind::AccessDenied));
    assert!(matches!(map_open_error(2, "[WinError 2] x".into()), OpenErrorKind::NotFound));
    assert!(matches!(map_open_error(3, "[WinError 3] x".into()), OpenErrorKind::NotFound));
    assert!(matches!(map_open_error(31, "[WinError 31] x".into()), OpenErrorKind::Other));
}

#[test]
fn overlapped_completion_mapping() {
    assert_eq!(map_overlapped_completion(true, 10, 0), (TxOutcome::Completed(10), None));
    assert_eq!(map_overlapped_completion(false, 4, 995), (TxOutcome::Completed(4), Some(PalErrorKind::Cancelled))); // ERROR_OPERATION_ABORTED
    assert_eq!(map_overlapped_completion(true, 3, 1460), (TxOutcome::Completed(3), Some(PalErrorKind::TimedOut))); // 寫逾時：部分完成
    assert_eq!(map_overlapped_completion(false, 0, 1617), (TxOutcome::Unknown { confirmed: 0 }, Some(PalErrorKind::Io))); // ERROR_DEVICE_REMOVED：長度不可信
    assert_eq!(map_overlapped_completion(false, 0, 22), (TxOutcome::Unknown { confirmed: 0 }, Some(PalErrorKind::Io)));   // ERROR_BAD_COMMAND
}
```

Windows 原生（`#[cfg(windows)]` 區段，經 `scripts/rust/win-test.sh` 執行）：

| 測試名 | 資源 | 精確斷言 |
|---|---|---|
| `nonexistent_com_is_not_found` | memory_only（原生） | `open_exclusive(DevicePath("COM250"))` → `Err(OpenError::NotFound(e))`，`e.py_message` 以 `"[WinError 2] "` 開頭 |
| `share_mode_zero_requested` | memory_only（原生） | 以內部 `create_file_args(&dev)` 取得旗標：`share_mode == 0`、`disposition == OPEN_EXISTING`、`flags & FILE_FLAG_OVERLAPPED != 0`、`access == GENERIC_READ|GENERIC_WRITE` |
| `cap_windows_com__second_open_access_denied` | windows_com | 已開 COMa 時再 `open_exclusive(COMa)` → `AccessDenied{holder: {pids: [], holder_unknown: true, status: Unsupported}}` |
| `cap_windows_com__cancel_pending_read_returns_woken` | windows_com | reader 等待中 `waker.wake()` → `Woken`，且同一 OVERLAPPED 於回傳前已 `GetOverlappedResult(bWait=TRUE)`（內部計數 `pending_ops == 0`） |
| `cap_windows_com__cancel_blocked_write_partial` | windows_com | 對端停止讀、RTS/CTS 流控下寫 1 MiB → cancel → `Completed(n)`，`0 < n < 1 MiB`，`error.kind == Cancelled` |
| `cap_windows_com__conformance_suite` | windows_com | `serial_suite(native().serial, com_pair_factory("SW_TEST_COM_PAIR"))` |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-pal --features test-fakes --test serial_windows` 與 `scripts/rust/win-test.sh --package sw-pal`
Expected：FAIL，`not implemented: slice21 T3 dcb_model`。

- [ ] **Step 3：實作**

- 開啟：`CreateFileW(win_device_name, GENERIC_READ|GENERIC_WRITE, 0, null, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, 0)`；失敗依 `map_open_error`：`AccessDenied` → `OpenError::AccessDenied{os, holder: HolderReport{pids: vec![], holder_unknown: true, status: Unsupported}}`；成功後 `GetCommState` → 套 `dcb_model` → `SetCommState`；`SetCommTimeouts(comm_timeouts_model())`；`SetupComm(4096, 4096)`；`PurgeComm` **不呼叫**（對齊 POSIX 不 tcflush）。
- I/O 執行緒模型（arch §1.4）：reader 與 writer 各自持有專屬 `OVERLAPPED`＋manual-reset event 與 buffer，生命週期綁在物件上，`Drop` 前保證無在途 I/O。
- reader：`ReadFile(h, buf, len, null, &ov)` → `ERROR_IO_PENDING` 時 `WaitForMultipleObjects([ov.hEvent, wake_event], FALSE, timeout)`；wake → `CancelIoEx(h, &ov)` → `GetOverlappedResult(h, &ov, &n, TRUE)` → n>0 回 `Data(n)`（已完成 bytes 不丟），否則 `Woken`；io event → `GetOverlappedResult(..., FALSE)`，`n==0` 回 `TimedOut`（COMMTIMEOUTS 200 ms 到期）。
- writer（`write_gated`）：`WriteFile(..., &ov)` 的**發出**放在 `gate.submit` 閉包內（同步完成 → `Accepted(n)`；`ERROR_IO_PENDING` → `Pending`；其他錯誤 → `Failed`），gate 撤銷時不呼叫 `WriteFile`（R0-F1）；閉包返回、放鎖後才在 gate 外等 `[ov.hEvent, wake_event]`（無逾時，寫逾時由 COMMTIMEOUTS 5000 ms 處理）；wake 且已 cancel → `CancelIoEx` → `GetOverlappedResult(bWait=TRUE)` → `map_overlapped_completion`；完成後若 `transferred < len` 且無錯 → 剩餘 bytes 以新的 `gate.submit` 重驗後再發出（撤銷則 `Completed(sent)`＋`cancelled`，累計 sent）。
- `CloseToken::close`：確認 reader／writer 已 drop（無在途 OVERLAPPED）→ `CloseHandle`；成功 → `closed:true, excl_cleared:true`（`dwShareMode=0` 的限制隨 handle 關閉解除）；失敗 → `closed:false, excl_cleared:false`。
- `probe_holders` 恆回 `{pids: [], holder_unknown: true, status: Unsupported}`。
- `set_baud`：`GetCommState`／改 `BaudRate`／`SetCommState`。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-pal --features test-fakes --test serial_windows && scripts/rust/win-test.sh --package sw-pal`
Expected：PASS；`target/win-test-report/<run_id>.json` 中 `cap_windows_com__*` 為 `ignored`（未提供 `SW_TEST_COM_PAIR`），報告欄 `caps` 標「證據不足」。

擁有者 host 第二段（OD-15，orchestrator 代跑）：同一指令加 `SW_TEST_COM_PAIR=COM20:COM21` 重跑，Expected：`cap_windows_com__*` 全部實際執行（非 `ignored`），`cap_windows_com__open_exclusive_*` 斷言第二次開啟 `ERROR_ACCESS_DENIED`；報告 `caps.windows_com` 記 `pair:"COM20:COM21"`、Windows build、HVCI 狀態。結果為 FAIL 時屬真實缺陷，不得改標「證據不足」。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-pal changelog.d/rust-s21-pal-serial-windows.md
git commit -m "feat(sw-pal): Windows 序列埠 overlapped 獨占開啟與可收斂取消" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 4：PAL InstanceLock（Tier 1）＋SignalWait

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-pal-lock.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust PAL Tier 1 實例鎖（POSIX flock＋socket 探測、Windows LockFileEx＋TCP 探測、路徑正規化 SC-10）與 daemon 停止訊號等待。」）
**BLOCKED-OWNER：** (1) Windows `CTRL_BREAK`／`CTRL_CLOSE` 送達 daemon 的測試需互動 console session（`cap_interactive_desktop__ctrl_break_stops_wait`）；(2) `windows-task` 監管下 `schtasks /End` 的停止行為屬 Spike 2（>72 h 監管驗證）範圍，本 Phase 不產出。

**Files:**
- Create: `crates/sw-pal/src/lock/posix.rs`、`crates/sw-pal/src/lock/windows.rs`、`crates/sw-pal/src/signal/posix.rs`、`crates/sw-pal/src/signal/windows.rs`、`crates/sw-pal/src/path_norm.rs`
- Modify: `crates/sw-pal/src/lock.rs`、`crates/sw-pal/src/signal.rs`、`crates/sw-pal/src/conformance/lock_suite.rs`
- Test: `crates/sw-pal/tests/lock.rs`

**Interfaces:**
- Consumes：`sw_pal::traits::EndpointSpec`（P3：`Unix(OsString) | Tcp{host, port}`；sw-pal 不依賴 sw-proto，arch §1.3——`sw_proto::endpoint::parse_endpoint` 的 `Endpoint` 只在 `daemon_main` 轉成 `EndpointSpec`）。
- Produces：`native().instance_lock: Arc<dyn InstanceLockFactory>`（arch §4.4 逐字）；`native().signals: Arc<dyn SignalWait>`（`install`＋`wait_stop`，Task 0）；`pub fn sw_pal::path_norm::normalize_for_compare(p: &str, cwd: &Path, home: Option<&Path>) -> String`（去 `unix://` 前綴 → `expanduser`（對齊 P1 `normalize_endpoint_path`）→ abspath → normpath；Windows 另轉小寫並統一 `\`）。

- [ ] **Step 1：寫失敗測試**

```rust
#![cfg(feature = "test-fakes")]
#[cfg(unix)] use std::os::unix::net::UnixListener;   // 平台 import 必須自帶 cfg，否則 Windows 編譯失敗
use std::{ffi::OsString, path::PathBuf};
use sw_pal::*;
use sw_pal::traits::EndpointSpec;

#[cfg(unix)]
#[test]
fn second_acquire_same_lock_is_already_running_and_pid_written() {
    let d = tempfile::tempdir().unwrap();
    let lock = d.path().join("run/serialwrapd.lock");
    let ep = EndpointSpec::Unix(d.path().join("run/serialwrapd.sock").into_os_string());
    let f = sw_pal::native().instance_lock;
    let g = f.acquire(&lock, &ep).expect("first");
    assert_eq!(std::fs::read_to_string(&lock).unwrap(), format!("{}\n", std::process::id()));
    assert!(matches!(f.acquire(&lock, &ep), Err(InstanceLockError::AlreadyRunning)));
    g.release();
    assert!(lock.exists(), "lock file is never deleted");
}

#[cfg(unix)]
#[test]
fn live_socket_under_other_lock_path_is_already_running_even_via_unix_url() {
    let d = tempfile::tempdir().unwrap();
    let sock = d.path().join("s.sock");
    let _l = UnixListener::bind(&sock).unwrap();
    let ep = EndpointSpec::Unix(OsString::from(format!("{}/x/../s.sock", d.path().display())));
    let r = sw_pal::native().instance_lock.acquire(&d.path().join("other.lock"), &ep);
    assert!(matches!(r, Err(InstanceLockError::AlreadyRunning)));
}

#[cfg(unix)]
#[test]
fn stale_socket_is_unlinked_then_acquired() {
    let d = tempfile::tempdir().unwrap();
    let sock = d.path().join("s.sock");
    drop(UnixListener::bind(&sock).unwrap());           // 檔案殘留、無 listener → ECONNREFUSED
    let g = sw_pal::native().instance_lock.acquire(&d.path().join("l.lock"), &EndpointSpec::Unix(sock.clone().into_os_string())).unwrap();
    assert!(!sock.exists());
    g.release();
}
```

其餘案例：

| 測試名 | 平台／資源 | 精確斷言 |
|---|---|---|
| `socket_probe_eacces_is_socket_unavailable` | unix（root 下 skip，報告「證據不足」） | socket 檔所在目錄 `chmod 000` → `Err(InstanceLockError::SocketUnavailable)`，lock fd 已關（同一 lock 立即可再 acquire） |
| `lock_file_mode_0600` | unix | 新建 lock 檔 `st_mode & 0o777 == 0o600` |
| `normalize_for_compare_cases` | 全平台（純函式） | `normalize_for_compare("unix:///a/b/../c.sock", "/", None) == "/a/c.sock"`；`normalize_for_compare("rel/x.sock", "/w", None) == "/w/rel/x.sock"`；`normalize_for_compare("~/s.sock", "/w", Some("/h")) == "/h/s.sock"` |
| `windows_live_tcp_endpoint_checked_before_lock` | windows 原生 | 先 `TcpListener::bind("127.0.0.1:0")`，以其 port 作 endpoint → `AlreadyRunning`，且 lock 檔**未被建立** |
| `windows_lockfileex_conflicts_with_python_msvcrt_holder` | windows 原生（需 Windows Python） | 以 `py -3.12 -c "import msvcrt,os,time;fd=os.open(p,os.O_RDWR|os.O_CREAT,0o600);msvcrt.locking(fd,msvcrt.LK_NBLCK,1);print('held',flush=True);time.sleep(30)"` 持鎖後 `acquire` → `AlreadyRunning`（SC-10 互斥相容） |
| `windows_second_acquire_in_process` | windows 原生 | 同 lock 第二次 → `AlreadyRunning`；`release` 後可再取得 |
| `signal_extra_channel_stops_wait` | 全平台 | `wait_stop(rx)` 於另一 thread，`tx.send(())` 後 100 ms 內返回 |
| `posix_sigterm_stops_wait_in_child` | unix | **在子程序內**執行（`current_exe --exact` 重入，`S21_CHILD=sigterm`）：子程序第一個動作 `install()`，之後才 spawn 4 個 worker thread 並 `wait_stop`；父程序對子程序送 `SIGTERM` → 子程序 exit 0（`wait_stop` 返回後正常結束，未被訊號殺死）；cargo test 主程序從不對自己送訊號 |
| `posix_threads_spawned_after_install_inherit_mask` | unix（子程序） | `install()` 後建立的 thread 呼叫 `pthread_sigmask(SIG_BLOCK, NULL, &old)` → `old` 含 `SIGTERM|SIGINT` |
| `cap_interactive_desktop__ctrl_break_stops_wait` | windows＋interactive_desktop | 子行程以 `CREATE_NEW_PROCESS_GROUP` 啟動並 `wait_stop`，父行程 `GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid)` → 子行程 exit 0 |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-pal --features test-fakes --test lock && scripts/rust/win-test.sh --package sw-pal`
Expected：FAIL，`not implemented: slice21 T4 PosixInstanceLock::acquire`。

- [ ] **Step 3：實作**

- POSIX（07 §2.1 逐步）：`create_dir_all(parent)` → `open(O_RDWR|O_CREAT|O_CLOEXEC, 0o600)` → `flock(LOCK_EX|LOCK_NB)`，`EWOULDBLOCK` → close、`AlreadyRunning` → endpoint 為 `Unix(p)` 且 `p` 存在：`UnixStream::connect_timeout` 等價（非阻塞 connect＋poll 200 ms）：成功 → close、`AlreadyRunning`；`ENOENT|ECONNREFUSED` → `unlink`（錯誤忽略）；其他（含逾時）→ close、`SocketUnavailable` → `ftruncate(0)` → 寫 `"{pid}\n"` → `fsync`。`Tcp` endpoint 在 POSIX 不探測（Python 以 `os.path.exists("tcp://…")` 恆假）。
- Windows（07 §2.2，SC-10）：endpoint 為 `Tcp` → `TcpStream::connect_timeout(200 ms)` 成功 → `AlreadyRunning`；`create_dir_all(parent)`；`CreateFileW(GENERIC_READ|GENERIC_WRITE, FILE_SHARE_READ|FILE_SHARE_WRITE, OPEN_ALWAYS)`；`LockFileEx(LOCKFILE_EXCLUSIVE_LOCK|LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, &ov offset 0)` 失敗 → `AlreadyRunning`；`SetEndOfFile` 於 offset 0、寫 pid、`FlushFileBuffers`。`release`：`UnlockFileEx` → `CloseHandle`。
- 比較用路徑一律經 `normalize_for_compare`；寫入 config 的仍是原字串（RPC-D-07）。
- SignalWait POSIX：`install()` 以 `pthread_sigmask(SIG_BLOCK, {SIGINT,SIGTERM})`（不得用 `sigprocmask`：多 thread 下語意未定義）擋下訊號後啟動專屬 thread `sigwait`，收到即送 internal channel；之後建立的所有 thread 繼承 mask，因此 `install()` 必須在 `main` 建立任何 thread 之前呼叫（Task 19）。`wait_stop` 以 `select!` 等 internal 或 `extra`。Windows：`install()` 以 `SetConsoleCtrlHandler` 處理 `CTRL_C_EVENT|CTRL_BREAK_EVENT|CTRL_CLOSE_EVENT` → 送 channel 並回 TRUE；`wait_stop` 同上。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-pal --features test-fakes --test lock && scripts/rust/win-test.sh --package sw-pal`
Expected：PASS；`cap_interactive_desktop__*` 為 ignored。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-pal changelog.d/rust-s21-pal-lock.md
git commit -m "feat(sw-pal): Tier 1 實例鎖與停止訊號等待" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 5：PAL DeviceSource／DeviceWatcher（#100 前提、SERIALCOMM、藍牙排除、OD-11）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-pal-devices.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust PAL 裝置來源：POSIX by-id／by-path 掃描（realpath 去重）、Windows SERIALCOMM＋藍牙雙重排除＋exclude_coms、OD-11 測試裝置 allowlist 過濾，以及不因單輪失敗終止的 watcher。」）
**BLOCKED-OWNER：** 藍牙 SPP 虛擬 COM 實際存在時的排除證據（需配對藍牙裝置的 Windows 機台；總表 BO-S21-15）。

**Files:**
- Create: `crates/sw-pal/src/device_source/posix.rs`、`crates/sw-pal/src/device_source/windows.rs`
- Modify: `crates/sw-pal/src/device_source/mod.rs`、`crates/sw-pal/src/device_source/windows_model.rs`（P4 05 Task 1／2 已建立並實作 `RegistryView`（`Send + Sync`）、`read_bt_ports`、`exclude_bluetooth`、`SERIALCOMM_KEY`；本 Task 沿用，只新增 `WindowsDeviceSource`）、`crates/sw-pal/src/device_watcher.rs`
- Test: `crates/sw-pal/tests/device_source.rs`

**Interfaces:**
- Consumes：Task 0 `DeviceInfo`、`DeviceSelection`、`DeviceSource`、`WatcherEvent`、`DeviceWatcher`（arch §4.3）；Task 1 `FakeDeviceSource`、`FakeClock`。
- Produces：

```rust
pub fn sw_pal::device_source::posix_scan(dirs: &[PathBuf], allowlist: Option<&[String]>) -> PalResult<Vec<(String, DeviceInfo)>>;
// 沿用 P4 05 Task 1／2（不重新定義）：RegistryView: Send + Sync、windows_model::{read_bt_ports, exclude_bluetooth, SERIALCOMM_KEY}
pub struct WindowsDeviceSource { view: Box<dyn RegistryView>, clock: Arc<dyn Clock>, exclude: HashSet<String>, allowlist: Option<Vec<String>>, bt_cache: Option<(u64, HashSet<String>)> }
impl DeviceWatcher { pub fn initial_scan(src: &mut dyn DeviceSource) -> PalResult<Vec<(String, DeviceInfo)>>; pub fn diff(prev: &[(String, DeviceInfo)], cur: &[(String, DeviceInfo)]) -> (Vec<DeviceInfo>, Vec<DeviceInfo>); }
pub struct WatcherHandle; impl WatcherHandle { pub fn stop(self); }
```

- [ ] **Step 1：寫失敗測試**

```rust
#![cfg(feature = "test-fakes")]
use std::collections::HashSet;
#[cfg(unix)] use std::os::unix::fs::symlink;          // 平台 import 自帶 cfg
use sw_pal::device_source::{posix_scan, windows_model::*};
use sw_pal::{DeviceInfo, DeviceWatcher};

#[cfg(unix)]
#[test]
fn by_id_wins_over_by_path_for_same_tty_and_sorted_names() {
    let d = tempfile::tempdir().unwrap();
    let (byid, bypath, tty) = (d.path().join("by-id"), d.path().join("by-path"), d.path().join("ttyX"));
    std::fs::create_dir_all(&byid).unwrap(); std::fs::create_dir_all(&bypath).unwrap();
    std::fs::write(&tty, b"").unwrap();
    std::fs::write(d.path().join("ttyY"), b"").unwrap();
    symlink(&tty, byid.join("usb-B")).unwrap();
    symlink(d.path().join("ttyY"), byid.join("usb-A")).unwrap();
    symlink(&tty, bypath.join("pci-0")).unwrap();                 // 同一 tty → 去重
    symlink(d.path().join("missing"), byid.join("usb-dangling")).unwrap();
    let got = posix_scan(&[byid.clone(), bypath.clone()], None).unwrap();
    let keys: Vec<_> = got.iter().map(|(k, _)| k.clone()).collect();
    assert_eq!(keys, vec![byid.join("usb-A").display().to_string(), byid.join("usb-B").display().to_string()]);
    assert_eq!(got[1].1, DeviceInfo { by_id: byid.join("usb-B").display().to_string(),
                                      real_path: std::fs::canonicalize(&tty).unwrap().display().to_string() });
}

#[test]
fn bluetooth_double_exclusion_keeps_enumeration_order() {
    let sc = vec![(r"\Device\Serial0".to_string(), "COM3".to_string()),
                  (r"\Device\BthModem0".to_string(), "COM7".to_string()),
                  (r"\Device\VCP0".to_string(), "COM10".to_string()),
                  (r"\Device\VCP1".to_string(), "COM5".to_string())];
    let bt: HashSet<String> = ["COM5".to_string()].into();
    let ex: HashSet<String> = ["COM10".to_string()].into();
    assert_eq!(exclude_bluetooth(&sc, &bt, &ex), vec!["COM3".to_string()]);
}

#[test]
fn watcher_diff_appends_changed_after_added_and_removed() {
    let a = |k: &str, r: &str| (k.to_string(), DeviceInfo { by_id: k.into(), real_path: r.into() });
    let prev = vec![a("/b/x", "/dev/ttyUSB0"), a("/b/y", "/dev/ttyUSB1")];
    let cur  = vec![a("/b/x", "/dev/ttyUSB2"), a("/b/z", "/dev/ttyUSB3")];
    let (added, removed) = DeviceWatcher::diff(&prev, &cur);
    assert_eq!(added.iter().map(|d| (&*d.by_id, &*d.real_path)).collect::<Vec<_>>(), vec![("/b/z", "/dev/ttyUSB3"), ("/b/x", "/dev/ttyUSB2")]);
    assert_eq!(removed.iter().map(|d| (&*d.by_id, &*d.real_path)).collect::<Vec<_>>(), vec![("/b/y", "/dev/ttyUSB1"), ("/b/x", "/dev/ttyUSB0")]);
}
```

| 測試名 | 精確斷言 |
|---|---|
| `scan_skips_missing_dirs_and_includes_regular_files` | 不存在的 dir 略過；一般檔案也收（07 §1.3） |
| `allowlist_filters_by_id_exactly` | `posix_scan(dirs, Some(&["<by-id>/usb-A".into()]))` 只回 `usb-A` |
| `bt_ports_walk_recursive_portname` | 假 `RegistryView`：`BTHENUM\{x}\y\Device Parameters` 有 `PortName="COM5"`、更深一層有 `COM9`、非字串值略過 → `{"COM5","COM9"}`；任一 `OpenKey` 錯誤吞掉 |
| `windows_scan_maps_real_path` | `WindowsDeviceSource.scan()` 回 `[("COM3", DeviceInfo{by_id:"COM3", real_path:r"\\.\COM3"})]` |
| `bt_cache_ttl_5s` | FakeClock：第一次 scan 讀 BTHENUM；advance 4.9 s 再 scan 不讀；advance 0.2 s 再讀（以 view 呼叫計數斷言） |
| `serialcomm_missing_key_is_empty` | `enum_values` 回 `NotFound` → scan 回 `Ok(vec![])` |
| `watcher_scan_failure_does_not_stop_thread` | FakeDeviceSource 第 2 輪 `fail_next` → 收到 `WatcherEvent::ScanFailed`，第 3 輪仍收到 `Tick`；thread 名 `serialwrap-device-watcher` |
| `watcher_emits_delta_only_when_changed` | 無變化只送 `Tick`；有變化送 `Delta{added, removed, snapshot}` 後才送 `Tick` |
| `watcher_tick_panic_isolated` | FakeDeviceSource 第 2 輪 scan panic → WARNING 一行，第 3 輪仍收到 `Tick` |
| `windows_live_serialcomm_read_ok`（windows 原生） | 真實 HKLM 唯讀列舉回 `Ok(_)`（內容不斷言） |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-pal --features test-fakes --test device_source`
Expected：FAIL，`not implemented: slice21 T5 posix_scan`。

- [ ] **Step 3：實作**

- `posix_scan`：依 dirs 順序；`read_dir` → 收集名稱 `sort()`（code point 序）→ `fs::metadata(path)`（跟隨 symlink）失敗略過 → `canonicalize` → `seen_real` 去重 → allowlist 存在時 `by_id ∉ allowlist` 略過（allowlist 在去重**之後**套用，避免 by-path 補位）。
- Windows：`HARDWARE\DEVICEMAP\SERIALCOMM` 以 `RegEnumValueW` 依序列舉 `(value_name, data)`；`read_bt_ports` 遞迴 `SYSTEM\CurrentControlSet\Enum\BTHENUM`；`exclude_bluetooth`：`com ∈ bt || com ∈ exclude || value_name.to_lowercase().contains("bthmodem")` 剔除。`exclude_coms` 由呼叫端（daemon_main 讀 `config.yaml` 的 `windows.exclude_coms`，任何錯誤視為空集合，PLT-O-16 只讀一次）傳入。
- `DeviceWatcher::start`：thread `serialwrap-device-watcher`；每輪以 `catch_unwind` 包住（panic → WARNING `device watcher tick 例外，略過本輪`、thread 續行）；每輪 `scan` → 失敗送 `ScanFailed(e)` 並 log WARNING `device watcher tick 失敗，略過本輪` → 成功時 `diff`，有變化送 `Delta` → 送 `Tick` → `clock.sleep(poll)`（可被 `WatcherHandle::stop` 打斷）。`diff` 照 07 §1.4：`added_keys = sorted(cur−prev)`、`removed_keys = sorted(prev−cur)`、`changed = sorted(both, real_path 不同)`，changed 依序把 prev 接在 removed 後、cur 接在 added 後。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-pal --features test-fakes --test device_source && scripts/rust/win-test.sh --package sw-pal`
Expected：PASS。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-pal changelog.d/rust-s21-pal-devices.md
git commit -m "feat(sw-pal): 裝置來源掃描、藍牙排除、測試裝置 allowlist 與 watcher" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 6：PAL WalFs（常駐 handle、identity、share-delete、不覆蓋 rename）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-pal-walfs.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust PAL WAL 檔案層：常駐 append handle、檔案 identity 與 unlink／DeletePending 偵測、Windows FILE_SHARE_DELETE、不覆蓋既有歸檔的 rename（DF-03）。」）
**BLOCKED-OWNER：** 無（Windows 部分在擁有者本機經 WSL interop 原生執行，屬本環境可產出；第三方 EDR 影響併入 Spike 3 結果比對，見 Task 22）。

**Files:**
- Create: `crates/sw-pal/src/wal_fs/posix.rs`、`crates/sw-pal/src/wal_fs/windows.rs`
- Modify: `crates/sw-pal/src/wal_fs.rs`、`crates/sw-pal/src/conformance/wal_fs_suite.rs`
- Test: `crates/sw-pal/tests/wal_fs.rs`

**Interfaces:**
- Consumes：arch §4.5 `WalFs`／`WalFile`／`FileIdentity`／`WalWriteError`（`write_all_flush` 失敗必帶 `sent`，R2-F3）。
- Produces：`native().wal_fs`；`pub fn sw_pal::conformance::wal_fs_suite(fs: Arc<dyn WalFs>, dir: &Path, external: &dyn ExternalFsOps)`，`pub trait ExternalFsOps { fn unlink(&self, p: &Path); fn replace_with(&self, p: &Path, bytes: &[u8]); fn hold_reader(&self, p: &Path) -> Box<dyn Send>; }`（native 版用另一個行程或 std::fs；fake 版用 `FakeWalFs`）。

- [ ] **Step 1：寫失敗測試**

```rust
#![cfg(feature = "test-fakes")]
use std::path::Path;
use sw_pal::*;

fn native_suite(dir: &Path) { sw_pal::conformance::wal_fs_suite(sw_pal::native().wal_fs, dir, &sw_pal::conformance::NativeExternalFs); }

#[test]
fn native_conformance() { let d = tempfile::tempdir().unwrap(); native_suite(d.path()); }

#[test]
fn fake_conformance() {
    let fs = sw_pal::fakes::wal_fs::FakeWalFs::new();
    sw_pal::conformance::wal_fs_suite(fs.clone(), Path::new("/w"), &sw_pal::conformance::FakeExternalFs(fs));
}
```

`wal_fs_suite` 子檢查：

| 子檢查 | 精確斷言 |
|---|---|
| `append_two_writes_in_order` | `open_append` → `write_all_flush(b"a\n")`、`write_all_flush(b"b\n")` → 外部讀檔 `== b"a\nb\n"`（不需 close 即可見） |
| `identity_stable_and_matches_path` | `file.identity() == fs.path_identity(path).unwrap().unwrap()` |
| `external_unlink_detected` | `external.unlink(path)` → `file.unlinked() == Ok(true)` 或 `fs.path_identity(path) == Ok(None)`（兩者至少其一；POSIX 兩者皆真） |
| `external_replace_detected` | `external.replace_with(path, b"x")` → `fs.path_identity(path)` 為 `Some(id2)` 且 `id2 != file.identity()` |
| `tail_needs_termination_reports_partial_line` | `contents(path)=b"partial"` → true；改成 `b"partial\n"` 或空檔 → false |
| `rename_while_open_keeps_writing_to_renamed` | `rename_noreplace_or_suffix(path, path.with_extension("ndjson.20261015-000000"))` 回該路徑；原 handle 再寫 → 內容出現在歸檔，不在新 path |
| `rename_noreplace_suffix` | 目標已存在 → 回 `<base>.1`；再一次 → `<base>.2`；既有歸檔內容不變（DF-03） |
| `size_and_missing` | `fs.size(missing) == Ok(None)`；寫 3 bytes 後 `== Ok(Some(3))` |
| `sync_is_callable` | `file.sync()` 與 `fs.sync_dir(dir)` 回 `Ok(())` |
| `read_all_sees_unflushed_free_writes` | `write_all_flush(b"a\n")` 後 `fs.read_all(path) == Ok(Some(b"a\n"))`；不存在 → `Ok(None)`（Task 0 已把 `read_all` 納入 trait） |

POSIX 原生專屬（部分寫入進度，R2-F3；規格 §15.7.1：OS 內部的 partial completion 以決定性 fault test 覆蓋）：

| 測試名 | 精確斷言 |
|---|---|
| `posix_rlimit_fsize_partial_write_reports_sent` | 以 `std::env::current_exe()` re-exec 本測試 binary 為子行程（env `SW_PAL_FSIZE_CHILD=1`，避免 rlimit 影響其他測試 thread）；子行程 `signal(SIGXFSZ, SIG_IGN)`＋`setrlimit(RLIMIT_FSIZE, 10)` 後 `open_append(p)`、`write_all_flush(&[b'x'; 64])` → `Err(e)`、`e.sent == 10`、`e.error.os_code == Some(libc::EFBIG)`、`e.error.py_message == "[Errno 27] File too large"`；檔案大小恰 10；子行程以 exit 0 回報、父行程斷言 exit status 與檔案內容 `== [b'x'; 10]` |

Windows 原生專屬：

| 測試名 | 精確斷言 |
|---|---|
| `windows_share_flags_allow_external_delete_and_rename` | 外部行程（`cmd /c del <path>`）刪除成功；外部 `ren` 成功 |
| `windows_delete_pending_with_external_reader_reported` | 外部 `py -3.12` 以 `open(p,'rb')` 持有 handle 後 `del`：`file.unlinked() == Ok(true)`（`FileStandardInfo.DeletePending`）；同名 `open_append` 在外部 handle 關閉前回 `Err(kind=AccessDenied)`，關閉後成功 |
| `windows_posix_semantics_delete` | 以 `SetFileInformationByHandle(FileDispositionInfoEx, FILE_DISPOSITION_FLAG_DELETE|FILE_DISPOSITION_FLAG_POSIX_SEMANTICS)` 刪除後，`path_identity == Ok(None)` 且可立即 `open_append` 新檔 |
| `windows_identity_uses_fileid128` | `identity()` 回 `FileIdentity(volume_serial, file_id_128)`，與 `GetFileInformationByHandleEx(FileIdInfo)` 相同 |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-pal --features test-fakes --test wal_fs`
Expected：`fake_conformance` PASS，`native_conformance` FAIL：`not implemented: slice21 T6 PosixWalFs::open_append`。

- [ ] **Step 3：實作**

- POSIX：`open(O_WRONLY|O_APPEND|O_CREAT|O_CLOEXEC, 0o666)`（umask 生效）；`write_all_flush` 迴圈 `write(bytes[sent..])` 直到全寫（EINTR 重試），無 user-space buffer；每次成功 `write` 累加 `sent`，`write` 回 0 → `WalWriteError{sent, error: PalError::io(EIO, "[Errno 5] Input/output error")}`、回 -1（非 EINTR）→ `WalWriteError{sent, error: <errno 對應 PalError>}`，**不得**丟棄已累加的 `sent`（arch §4.5，R2-F3）；`sync` = `fsync`；`identity` = `fstat (st_dev, st_ino)` → `FileIdentity(dev as u128, ino as u128)`；`unlinked` = `st_nlink == 0`；`path_identity` = `stat`（ENOENT → `None`）；`tail_needs_termination` 以唯讀 handle 檢查最後一個 byte，非空且不是 `\\n` 時回 true；`rename_noreplace_or_suffix`：Linux `renameat2(RENAME_NOREPLACE)`，`EEXIST` → 依序試 `.1`、`.2`…；`EINVAL`／`ENOSYS` → `link(from,to)` + `unlink(from)`；`sync_dir` = `open(dir, O_RDONLY)` + `fsync`；`dir_writable` = `access(W_OK|X_OK)`。
- Windows：`CreateFileW(FILE_APPEND_DATA|SYNCHRONIZE, FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL)`；`write_all_flush` 迴圈 `WriteFile`，以每次 `lpNumberOfBytesWritten` 累加 `sent`，失敗時同樣回 `WalWriteError{sent, error}`；`sync` = `FlushFileBuffers`；`identity` = `FileIdInfo`；`unlinked` = `FileStandardInfo.DeletePending`；`path_identity`：`CreateFileW(0 access, share RWD, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS)`，`ERROR_FILE_NOT_FOUND|ERROR_PATH_NOT_FOUND` → `None`，其他查詢錯誤（含 `ERROR_ACCESS_DENIED`）維持未知；`tail_needs_termination` 以可共享讀取的 handle 檢查最後一個 byte；`rename_noreplace_or_suffix` 用 `MoveFileExW(flags=0)`，`ERROR_ALREADY_EXISTS` → 下一個後綴；`sync_dir` 回 `Ok(())`；`dir_writable` = 目錄存在且無 `FILE_ATTRIBUTE_READONLY`。
- 每個 `WalFile` 的 `close` 只能呼叫一次（`self: Box<Self>`），`Drop` 未 close 時也關 handle（唯一關閉點，規格 §8.2）。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-pal --features test-fakes --test wal_fs && scripts/rust/win-test.sh --package sw-pal`
Expected：PASS（Windows 四個專屬測試原生執行通過）。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-pal changelog.d/rust-s21-pal-walfs.md
git commit -m "feat(sw-pal): WAL 常駐 handle、identity 偵測與不覆蓋歸檔 rename" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 7：sw-core WalWriter（BC-01、DF-03、DF-17）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-wal.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust WalWriter：Python 位元組相容的 WAL／mirror 紀錄、常駐 handle、外部 unlink／替換自癒並寫 WAL_RECREATED_AFTER_EXTERNAL_UNLINK、歸檔同名加後綴、mirror 失敗不重複 WAL 行、重建失敗停止自動 TX。」）
**BLOCKED-OWNER：** 無。

**Files:**
- Modify: `crates/sw-core/src/wal.rs`
- Test: `crates/sw-core/tests/wal_writer.rs`

**Interfaces:**
- Consumes：Task 6 `WalFs`；`sw_proto::pyjson`（`DISK_STABLE`）、`sw_proto::pycompat::{to_printable, iso_utc_now, strftime_utc}`（Task 0c）、`sw_proto::wal_types::{WalRecord, crc32_hex}`（`crc32fast` 只在 sw-proto，arch §1.2）；P1 T02～T05 凍結契約（`contracts/wal_meta_events.schema.json`、`contracts/golden/wal/*`、`contracts/captures/wal/*`）；Task 1 `FakeWalFs`、`FakeClock`。
- Produces：

```rust
pub struct WalConfig { pub dir: PathBuf, pub rotate_bytes: u64 }
pub trait WalHealthSink: Send + Sync { fn set_wal_unhealthy(&self, unhealthy: bool); }
/// 全域唯一 sink（WalWriter 只註冊這一個）：持有所有 session fence 的 Weak 參照；狀態變化時逐一更新，
/// `register(fence)` 時以 `WalWriter::health().healthy` 的反值初始化新 fence（新建 session 繼承目前狀態）。
pub struct WalHealthFanout { /* Mutex<(bool unhealthy, Vec<Weak<dyn WalHealthSink>>)> */ }
impl WalHealthFanout { pub fn new() -> Arc<WalHealthFanout>; pub fn register(&self, sink: Arc<dyn WalHealthSink>); pub fn current(&self) -> bool; }
impl WalHealthSink for WalHealthFanout {}
pub struct WalAppendReq<'a> { pub com: &'a str, pub dir: &'a str, pub source: &'a str, pub payload: &'a [u8],
                              pub cmd_id: Option<&'a str>, pub loss_flag: bool, pub meta: Option<PyDict> }
#[derive(Debug, Clone, PartialEq)] pub struct WalAppend { pub seq: u64, pub ok: bool, pub loss_flag: bool }
pub struct WalHealth { pub wal_dir: String, pub wal_path: String, pub wal_dir_exists: bool, pub wal_file_exists: bool,
                       pub wal_dir_writable: bool, pub current_seq: u64, pub write_failures: u64,
                       pub last_write_error: Option<String>, pub recreated_count: u64, pub healthy: bool,
                       pub unhealthy_reason: Option<&'static str> /* "recreate_failed" | "write_failed" | "dir_unwritable" */ }
impl WalHealth { pub fn to_pydict(&self) -> PyDict; }   // key 順序照 04 §1.10＋P1 T05 第 11 鍵 unhealthy_reason
pub struct WalReset { pub previous_seq: u64, pub rotated_suffix: String /* WAL 實際歸檔後綴（含 .N）；mirror 後綴各自計算 */ }
pub struct WalWriter { /* Mutex<WalInner> + Arc<dyn WalFs> + Arc<dyn Clock> + OnceLock<Arc<dyn WalHealthSink>> */ }
impl WalWriter {
    pub fn open(cfg: WalConfig, fs: Arc<dyn WalFs>, clock: Arc<dyn Clock>) -> WalWriter;  // 不回錯；失敗反映在 health
    pub fn set_health_sink(&self, sink: Arc<WalHealthFanout>);   // 只呼叫一次（SessionManager 建立時）
    pub fn append(&self, req: WalAppendReq<'_>) -> WalAppend;
    pub fn current_seq(&self) -> u64;
    pub fn health(&self) -> WalHealth;
    pub fn reset(&self) -> Result<WalReset, PalError>;
    pub fn tail_raw_with_meta(&self, from_seq: Option<i128>, com: Option<&str>, limit: i128) -> PalResult<(Vec<PyDict>, bool)>;
    pub fn available_from_seq(&self) -> Option<i128>;
    pub fn wal_path(&self) -> PathBuf;
    pub fn mirror_path(&self) -> PathBuf;
    pub fn shutdown(&self) -> PalResult<()>;
}
pub fn rows_to_text_lines(rows: &[PyDict]) -> Vec<String>;
```

- [ ] **Step 1：寫失敗測試**

`crates/sw-core/tests/wal_writer.rs`（關鍵案例的位元組 golden 取自 04 §1.3〔實跑〕）：

```rust
#![cfg(feature = "test-fakes")]
use std::{path::Path, sync::Arc, time::Duration};
use sw_core::wal::*;
use sw_pal::fakes::{clock::FakeClock, wal_fs::FakeWalFs};
use sw_proto::pyjson::{PyDict, PyValue};

fn writer(rotate: u64) -> (WalWriter, Arc<FakeWalFs>, Arc<FakeClock>) {
    let fs = FakeWalFs::new();
    let clock = FakeClock::at(5_798_622_898_976, 1_792_033_577_663_778); // 2026-10-15T03:06:17.663778Z
    (WalWriter::open(WalConfig { dir: "/w".into(), rotate_bytes: rotate }, fs.clone(), clock.clone()), fs, clock)
}
fn req<'a>(dir: &'a str, source: &'a str, payload: &'a [u8], meta: Option<PyDict>) -> WalAppendReq<'a> {
    WalAppendReq { com: "COM0", dir, source, payload, cmd_id: None, loss_flag: false, meta }
}

#[test]
fn rx_record_bytes_match_python_golden() {
    let (w, fs, _) = writer(67_108_864);
    let a = w.append(req("RX", "uart", b"hello\r\n\x1b[0m\x0b\x7f\xff", None));
    assert_eq!(a, WalAppend { seq: 1, ok: true, loss_flag: false });
    let text = String::from_utf8(fs.contents(Path::new("/w/raw.wal.ndjson")).unwrap()).unwrap();
    assert_eq!(text, "{\"cmd_id\":null,\"com\":\"COM0\",\"crc32\":\"a81bc28c\",\"dir\":\"RX\",\"len\":14,\"loss_flag\":false,\"meta\":{},\"mono_ts_ns\":5798622898976,\"payload_b64\":\"aGVsbG8NChtbMG0Lf/8=\",\"seq\":1,\"source\":\"uart\",\"wall_ts\":\"2026-10-15T03:06:17.663778+00:00\"}\n");
    assert_eq!(fs.contents(Path::new("/w/raw.mirror.log")).unwrap(), b"hello\r\n\\x1b[0m\\x0b\\x7f\\xff".to_vec());
}

#[test]
fn meta_empty_payload_and_sorted_meta_keys() {
    let (w, fs, _) = writer(67_108_864);
    let mut m = PyDict::new(); m.insert("z", PyValue::from(1)); m.insert("a", PyValue::from("中"));
    w.append(WalAppendReq { com: "COM0", dir: "TX", source: "agent:x", payload: "é".as_bytes(), cmd_id: Some("c1"), loss_flag: false, meta: Some(m) });
    w.append(req("META", "system", b"", Some({ let mut d = PyDict::new(); d.insert("event", PyValue::from("x")); d })));
    let text = String::from_utf8(fs.contents(Path::new("/w/raw.wal.ndjson")).unwrap()).unwrap();
    let lines: Vec<&str> = text.lines().collect();
    assert!(lines[0].starts_with("{\"cmd_id\":\"c1\",\"com\":\"COM0\",\"crc32\":\"0e048d3e\",\"dir\":\"TX\",\"len\":2,\"loss_flag\":false,\"meta\":{\"a\":\"中\",\"z\":1},"));
    assert!(lines[1].contains("\"crc32\":\"00000000\",\"dir\":\"META\",\"len\":0,") && lines[1].contains("\"payload_b64\":\"\","));
    assert_eq!(fs.contents(Path::new("/w/raw.mirror.log")).unwrap(), b"\\xc3\\xa9".to_vec()); // META 不寫 mirror
}

#[test]
fn external_unlink_writes_recreate_meta_then_record() {
    let (w, fs, clock) = writer(67_108_864);
    for _ in 0..3 { w.append(req("RX", "device", b"x", None)); }
    fs.external_unlink(Path::new("/w/raw.wal.ndjson"));
    clock.advance(Duration::from_millis(60));                     // 超過 50 ms 節流
    let a = w.append(req("RX", "device", b"y", None));
    assert_eq!(a.seq, 5);
    let text = String::from_utf8(fs.contents(Path::new("/w/raw.wal.ndjson")).unwrap()).unwrap();
    let first: PyValue = sw_proto::pyjson::loads(text.lines().next().unwrap()).unwrap();
    let d = first.as_dict().unwrap();
    assert_eq!(d.get("seq"), Some(&PyValue::from(4)));
    assert_eq!(d.get("dir"), Some(&PyValue::from("META")));
    assert_eq!(d.get("com"), Some(&PyValue::from("WAL")));            // P1 T05 凍結：外層 com 為 "WAL"，不出現在 COM0 的 tail
    assert_eq!(d.get("source"), Some(&PyValue::from("system")));
    assert_eq!(d.get("loss_flag"), Some(&PyValue::Bool(true)));
    let meta = d.get("meta").unwrap().as_dict().unwrap();
    assert_eq!(meta.get("event"), Some(&PyValue::from("WAL_RECREATED_AFTER_EXTERNAL_UNLINK")));
    assert_eq!(meta.get("reason"), Some(&PyValue::from("external_unlink")));
    assert_eq!(meta.get("potentially_lost_seq_range"), Some(&PyValue::List(vec![PyValue::from(1), PyValue::from(3)])));
    assert_eq!(meta.get("range_known"), Some(&PyValue::Bool(true)));
    assert_eq!(w.health().recreated_count, 1);
}

#[test]
fn reopen_failure_marks_unhealthy_and_trips_sink() {
    struct Sink(std::sync::Mutex<Vec<bool>>);
    impl WalHealthSink for Sink { fn set_wal_unhealthy(&self, u: bool) { self.0.lock().unwrap().push(u); } }
    let (w, fs, clock) = writer(67_108_864);
    let fan = WalHealthFanout::new(); w.set_health_sink(fan.clone());
    let sink = Arc::new(Sink(Default::default())); fan.register(sink.clone());
    w.append(req("RX", "device", b"x", None));
    fs.external_unlink(Path::new("/w/raw.wal.ndjson"));
    fs.fail_next_opens(Path::new("/w/raw.wal.ndjson"), 3, sw_pal::PalError::io(28, "[Errno 28] No space left on device"));
    clock.advance(Duration::from_millis(60));
    let a = w.append(req("RX", "device", b"y", None));
    assert!(!a.ok && a.loss_flag);
    let h = w.health();
    assert!(!h.healthy);
    assert_eq!(h.unhealthy_reason, Some("recreate_failed"));
    assert_eq!(h.last_write_error.as_deref(), Some("OSError: [Errno 28] No space left on device"));
    assert_eq!(*sink.0.lock().unwrap(), vec![false, true]);          // register 時初始化 false，之後轉 true
}

#[test]
fn wal_partial_write_suffix_failure_terminates_tail_next_record_parses() {   // R2-F3；對應 P1 T02 test_partial_write_suffix_failure_terminates_tail
    let (w, fs, _) = writer(67_108_864);
    let wal = Path::new("/w/raw.wal.ndjson");
    let enospc = || sw_pal::PalError::io(28, "[Errno 28] No space left on device");
    fs.fail_next_write_partial(wal, 10, enospc());                     // 第一次只寫出 JSON 前綴 10 bytes 即 ENOSPC
    fs.fail_next_writes(wal, 1, enospc());                             // 補寫剩餘部分也失敗
    let a = w.append(req("RX", "device", b"x", None));
    assert_eq!(a, WalAppend { seq: 1, ok: false, loss_flag: true });
    let calls = fs.write_calls(wal);
    let line1 = calls[0].clone();
    assert_eq!(calls, vec![line1.clone(), line1[10..].to_vec()]);      // 只補寫 data[sent..]，絕不整行重寫
    assert_eq!(fs.open_calls(wal), 1);                                  // 補寫在同一 handle，不重開
    assert_eq!(fs.contents(wal).unwrap(), line1[..10].to_vec());        // 檔案只有前綴，沒有「前綴＋完整行」
    let h = w.health();
    assert_eq!(h.write_failures, 1);
    assert!(!h.healthy);                                                // 補寫失敗不得標為健康
    assert_eq!(h.unhealthy_reason, Some("write_failed"));
    assert_eq!(h.last_write_error.as_deref(), Some("OSError: [Errno 28] No space left on device"));

    let b = w.append(req("RX", "device", b"y", None));
    assert_eq!(b, WalAppend { seq: 2, ok: true, loss_flag: false });
    let calls = fs.write_calls(wal);
    assert_eq!(calls.len(), 3);
    assert_eq!(calls[2][0], b'\n');                                     // 先終結殘行（dirty_tail，arch §5.8 第 5 點）
    let mut expect = line1[..10].to_vec(); expect.extend_from_slice(&calls[2]);
    let bytes = fs.contents(wal).unwrap();
    assert_eq!(bytes, expect);
    let text = String::from_utf8(bytes).unwrap();
    let lines: Vec<&str> = text.split_terminator('\n').collect();
    assert_eq!(lines.len(), 2);
    assert!(sw_proto::pyjson::loads(lines[0]).is_err());                // 殘行獨立成行，讀取端判為無效
    let rec = sw_proto::pyjson::loads(lines[1]).unwrap();
    assert_eq!(rec.as_dict().unwrap().get("seq"), Some(&PyValue::from(2)));
    assert!(lines.iter().all(|l| l.matches("\"seq\":").count() <= 1)); // 無串接的 NDJSON 行
    let (rows, _) = w.tail_raw_with_meta(None, None, 100).unwrap();
    assert_eq!(rows.len(), 1);                                          // 讀取 API 略過殘行、只回 seq 2
    assert_eq!(rows[0].get("seq"), Some(&PyValue::from(2)));
    let h = w.health();
    assert!(h.healthy);
    assert_eq!(h.unhealthy_reason, None);
}
```

其餘案例：

| 測試名 | 精確斷言 |
|---|---|
| `wall_ts_omits_zero_microseconds` | wall `…:17.000000` → `"wall_ts":"2026-10-15T03:06:17+00:00"` |
| `rotate_before_write_at_threshold_utc_suffix` | `rotate_bytes=200`；寫到 size≥200 後下一筆 append 前把 `raw.wal.ndjson` 改名為 `raw.wal.ndjson.20261015-030617`（UTC `%Y%m%d-%H%M%S`），新檔第一筆 `seq` = 舊檔最後 seq+1；mirror 依自身大小獨立判斷 |
| `rotate_collision_adds_numeric_suffix` | 同一秒內兩次 rotate → 第二個歸檔 `raw.wal.ndjson.20261015-030617.1`；兩份內容都在（DF-03） |
| `rotate_rename_failure_marks_record` | `fail_next_renames(1, EACCES)` → 該筆紀錄含 `"rotation_failed":true`，重新開啟原路徑後繼續附加；log WARNING `WAL 輪替失敗（best-effort 續用既有檔案，不殺 reader）：/w/raw.wal.ndjson：[Errno 13] Permission denied` |
| `mirror_failure_retries_only_mirror` | `fail_next_writes(mirror, 1, EACCES)` → WAL 檔只有一行 seq=1（不重複，DF-17）；mirror 經一次重試後有內容 |
| `mirror_retry_failure_counts_write_failure` | `fail_next_writes(mirror, 2, EACCES)` → WAL 仍寫入、`write_failures == 1`、`last_write_error == "PermissionError: [Errno 13] Permission denied"`（P1：mirror 重試失敗也計入） |
| `mirror_identity_reopened_without_meta` | `external_unlink(mirror)`、advance 60 ms、append → mirror 重建並含新內容；WAL 無 META、`recreated_count` 不變（P1 T05 第 1 點） |
| `identity_check_precedes_rotation_when_rotation_is_due` | 同一筆 append 同時滿足 rotate 門檻與 identity 變更 → 先做失效分類並保存原遺失範圍，再依 `>= R` 輪替替換檔、於現行檔寫 META；以 `FakeWalFs` 操作紀錄斷言順序 |
| `identity_query_error_at_rotate_threshold_keeps_current_file` | path／handle identity 或 `unlinked` 查詢失敗且檔案達門檻 → 該路徑保留現行 handle、略過本次輪替並正常 append，不寫 META、不設 unhealthy |
| `replacement_at_rotate_threshold_keeps_meta_current` | 替換檔大小恰為／高於 `R` → 替換證據在輪替前確認；META 與原紀錄寫入新現行檔，遺失範圍仍從前檔 `file_first_seq` 開始 |
| `replacement_unterminated_tail_keeps_meta_parseable` | 替換檔尾端為未換行殘行 → 先終結殘行；`tail_raw` 略過殘行且仍讀得到獨立 META 與後續紀錄 |
| `second_unlink_range_starts_at_first_meta_seq` | 連續兩次 unlink → 第二筆 META 範圍從第一筆 META seq 起算 |
| `zero_identity_and_unlinked_handle_classify_external_unlink` | path identity `(0,0)` sentinel 或 identity 相同但 `unlinked()==true` → `reason:"external_unlink"` |
| `recreate_retry_schedule_uses_monotonic_gaps` | 依序驗證 10／50／200 ms、1／2／5／5 s 間隔；未到期限不開檔，到期 append 最多嘗試一次 |
| `lazy_open_idle_file_absent` | `WalWriter::open` 後尚未 append → `fs.contents(wal) == None`、`health().wal_file_exists == false`、`healthy == true`（P1 開檔為 lazy） |
| `enospc_on_open_handle_trips_all_fences`（Review Focus 3） | fanout 註冊三個 fence；已開 handle 上 `fail_next_writes(wal, 2, ENOSPC)`（含重試共兩次）→ 三個 fence `wal_unhealthy == true`、`unhealthy_reason == "write_failed"`；下一筆成功 → 三者恢復 false、`unhealthy_reason == None` |
| `new_session_inherits_wal_unhealthy` | unhealthy 期間 `fan.register(new_fence)` → 新 fence 立即 `wal_unhealthy == true` |
| `identity_throttle_ignores_wall_jump`（Review Focus 4） | `set_wall_us` 倒退 1 h 後 append → 不因 wall 倒退而跳過或重複 identity 檢查（節流只看 mono） |
| `meta_not_in_com0_tail` | 重建 META 後 `tail_raw_with_meta(None, Some("COM0"), 100)` 不含 META；`com=None` 時含 |
| `wal_contract_golden_matches_p1` | 讀 P1 `contracts/golden/wal/*.json`（Python 擷取的紀錄位元組）逐案以同輸入重放，輸出位元組相同 |
| `wal_write_failure_twice_sets_loss_flag_and_consumes_seq` | WAL 寫入連續失敗 2 次 → 回 `WalAppend{seq:1, ok:false, loss_flag:true}`、`write_failures==1`；下一筆成功 `seq==2`（seq 缺口保留，04 §1.7） |
| `wal_partial_write_then_failure_completes_suffix_only`（R2-F3；對應 P1 `test_partial_write_then_eio_completes_suffix_only`） | `fail_next_write_partial(wal, 10, PalError::io(5, "[Errno 5] Input/output error"))`、補寫成功 → `WalAppend{seq:1, ok:true, loss_flag:false}`；`write_calls(wal) == [line, line[10..]]`；`open_calls(wal) == 1`（同 handle 補寫、不重開）；`contents(wal) == line`（恰一行、`loads` 成功、全檔 `"seq":1` 只出現一次，且不含 `line[..10] ++ line`）；`write_failures == 0`、`healthy == true` |
| `wal_zero_sent_failure_rewrites_whole_line_after_reopen`（R2-F3） | `fail_next_writes(wal, 1, EIO)`（`sent == 0`）→ `ok:true`；`write_calls(wal) == [line, line]`、`open_calls(wal) == 2`（關閉→`ensure_dir`→重開後整行重寫一次）；`contents(wal) == line`；`write_failures == 0` |
| `wal_reopen_retry_partial_sets_dirty_tail`（R2-F3） | `fail_next_writes(wal, 1, EIO)` 後接 `fail_next_write_partial(wal, 7, EIO)`（整行重寫時又只寫 7 bytes）→ `WalAppend{seq:1, ok:false, loss_flag:true}`；`write_calls(wal) == [line1, line1]`（不做第三次嘗試）、`contents(wal) == line1[..7]`；下一筆 append → `write_calls(wal)[2][0] == b'\n'`、檔案依 `\n` 分成兩行：第一行 `loads` 失敗、第二行 `seq == 2` 可解析 |
| `mirror_partial_write_completes_suffix_only`（R2-F3） | `fail_next_write_partial(mirror, 3, EACCES)`、補寫成功 → `contents(mirror) == to_printable(payload)`（無重複前綴）、`write_calls(mirror) == [p, p[3..]]`；WAL 只一行 seq=1（DF-17）；`write_failures == 0` |
| `startup_seq_is_max_int_seq_in_current_file` | 預置檔含 seq 5、9、7、壞行、`{"seq":"x"}` → `current_seq()==9`；下一筆 `seq==10`；`available_from_seq()==Some(5)` |
| `reset_renames_nonempty_files_independent_suffix` | `reset()` → `WalReset{previous_seq:N, rotated_suffix:<WAL 實際歸檔後綴>}`；預置同秒歸檔 `raw.wal.ndjson.20261015-030617` 時回 `"20261015-030617.1"`，mirror 依自身既有歸檔獨立取後綴；空檔不改名；下一筆 seq=1 |
| `reset_rename_error_propagates_and_keeps_seq` | rename 失敗 → `Err(_)`，`current_seq()` 不變（呼叫端轉 `EXCEPTION`） |
| `health_keys_order` | `to_pydict()` key 依序 `wal_dir, wal_path, wal_dir_exists, wal_file_exists, wal_dir_writable, current_seq, write_failures, last_write_error, recreated_count, healthy, unhealthy_reason`（11 鍵，P1 T05） |
| `health_missing_file_after_writes_unhealthy` | seq>0 且檔案被刪（尚未 append 觸發重建）→ `healthy==false`、`wal_file_exists==false` |
| `open_failure_at_startup_marks_unhealthy`（Review Focus 3） | `set_dir_writable("/w", false)` → `WalWriter::open` 不 panic、不開檔（lazy），但於啟動時評估 `dir_writable` → `health().healthy==false`、`unhealthy_reason=="dir_unwritable"`、fanout 註冊的 fence 收到 `true`；append 回 `loss_flag:true`、`write_failures` +1，記憶體不保留未寫紀錄（`fs.contents` 仍為 None、內部無 queue） |
| `tail_latest_and_range_semantics` | 檔含 seq 1..5：`(None, None, 2)` → seq [4,5]、truncated true；`(Some(2), None, 2)` → [3,4]、truncated true；`(Some(4), None, 2)` → [5]、false；`limit=0` latest → `([], true)`；com 過濾只回該 com |
| `rows_to_text_lines_keeps_crlf_quirk` | rows payload `b"hello\r\n\x1b[0m\x0b\x7f\xff"` 與 `"é"` → `["hello", "\\x1b[0m\\x0b\\x7f\\xff\\xc3\\xa9", "9"]`（WAL-1.11 KEEP） |
| `shutdown_syncs_then_closes` | `shutdown()` 後 `fs.sync_calls(wal) >= 1` 且之後 append 回 `ok:false`（SC-11） |
| `recreate_meta_matches_contract_schema` | 讀 `contracts/wal_meta_events.schema.json`（P1 T05），以手寫最小驗證（`required` 鍵集合、`reason` enum、`previous_identity` pattern、`additionalProperties:false`）通過 |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-core --features test-fakes --test wal_writer`
Expected：FAIL，`not implemented: slice21 T7 WalWriter::open`。

- [ ] **Step 3：實作**

```rust
pub fn append(&self, r: WalAppendReq<'_>) -> WalAppend {
    let b64 = base64_std(r.payload);                      // wal.rs 私有 helper：標準字母、含 '='、不換行（= Python b64encode）
    let crc = sw_proto::wal_types::crc32_hex(r.payload);
    let printable = sw_proto::pycompat::to_printable(r.payload);
    let mut g = self.inner.lock().unwrap();
    let now = self.clock.mono_ns();
    let rotation_due = self.rotation_candidate_locked(&g); // 用目前 size 判斷是否可能輪替
    let identity_check = self.check_identity_locked(&mut g, now, rotation_due); // 平時 ≥50 ms mono 節流；輪替前分類並保存範圍；查詢未知時回傳該路徑 skip
    let rotation_failed = self.rotate_if_needed_locked(&mut g, &identity_check.rotation_skip_paths);
    self.finish_recreate_if_pending_locked(&mut g);        // WAL 命中 → 先輪替達門檻的替換檔，再重開、終結殘行並寫 META（com:"WAL"）；mirror 命中只重開
    self.ensure_open_locked(&mut g);                      // lazy open：第一次 append 才開檔
    g.seq += 1;
    let rec = WalRecord { seq: g.seq, mono_ts_ns: now, wall_ts: sw_proto::pycompat::iso_utc_now(self.clock.wall_unix_us()), com: r.com.into(),
        dir: r.dir.into(), source: r.source.into(), cmd_id: r.cmd_id.map(Into::into), len: r.payload.len() as u64,
        crc32_hex: crc, payload_b64: b64, loss_flag: r.loss_flag, meta: r.meta.unwrap_or_default(),
        rotation_failed: rotation_failed.then_some(true) };
    let line = rec.to_line();                             // DISK_STABLE + "\n"
    let wal_ok = self.write_target_locked(&mut g, Target::Wal, line.as_bytes());      // sent／dirty_tail 協定（arch §5.8 第 5 點，R2-F3）
    let mir_ok = if r.payload.is_empty() { true } else { self.write_target_locked(&mut g, Target::Mirror, printable.as_bytes()) };
    let ok = wal_ok && mir_ok;
    if !wal_ok { g.write_failures += 1; }
    if !mir_ok { g.write_failures += 1; }                 // P1：mirror 重試失敗同樣計入並更新 last_write_error
    self.update_health_locked(&mut g, wal_ok);            // WAL 兩次嘗試都失敗 → unhealthy("write_failed")＋fanout(true)；下一次成功 → 清除＋fanout(false)
    WalAppend { seq: g.seq, ok, loss_flag: r.loss_flag || !wal_ok }
}
```

- `write_target_locked(g, t, data) -> bool`（取代原「失敗就整行重寫一次」的 `write_with_one_retry`，R2-F3；逐條對齊 P1 T02 第 3 點 `PartialWriteError.sent`／`_dirty_tail` 協定）：只重試失敗的那個檔（DF-17），每檔最多一次重試；以 `WalWriteError.sent` 承接已寫進度：
  1. `g.dirty_tail[t]` 為真 → 本次資料改為 `buf = b"\n" ++ data`、`term = 1`；否則 `buf = data`、`term = 0`。handle 為 None 時先 `open_append`（開檔失敗視為 `sent == 0`）。
  2. 第一次 `write_all_flush(buf)`：`Ok` → 清 `dirty_tail[t]`、回 true（之後任何錯誤都不得重寫本筆）。
  3. `Err(WalWriteError{sent: 0, ..})` → 關閉該 handle → `ensure_dir` → 重開（ENOENT 類 → 記 `reason:"dir_missing"` META 於下一次成功寫入前）→ **整段** `buf` 重寫一次。
  4. `Err(WalWriteError{sent: k, ..})`，`0 < k < buf.len()` → **不得重寫整段、不得重開**；同一 handle 只補寫 `buf[k..]` 一次。
  5. 重試結果：成功 → 清 `dirty_tail[t]`、回 true。失敗 → 以「本次已確認總長 `c`」（步驟 3 為重寫那次的 `sent`；步驟 4 為 `k + 補寫的 sent`）判定：`term < c < buf.len()` → `dirty_tail[t] = true`（殘行留待下一筆先以 `\n` 終結）；`c <= term` → 未產生新殘行，`dirty_tail[t] = (c < term)`（終結 `\n` 尚未寫出才保留旗標）；回 false。不做第三次嘗試。
  6. 旗標以 WAL／mirror 為單位（與 P1 `_dirty_tail[which]` 相同）；rotate／自癒重建後的新檔若旗標仍為真，首次寫入同樣先寫 `\n`（空行由讀取端略過，與 P1 位元組一致）。
  `last_write_error = format!("{}: {}", e.error.py_exc_name(), e.error.py_message)`（取最後一次錯誤；`PalError::py_exc_name`，Task 0）。WAL 回 false（含部分寫入後補寫失敗）即「首次＋重試皆失敗」，由 `update_health_locked` 設 `unhealthy_reason="write_failed"`；下一筆完整寫出才清除。
- 自癒：每筆 append 檢查目錄與 WAL 名稱；identity／DeletePending 平時以 50 ms mono 節流，輪替將清除 handle／identity 時先分類。分類順序為目錄不存在、path identity 為 `None` 或 `(0,0)`、path identity 與 `file.identity()` 不同、identity 相同但 `file.unlinked()` 為真；identity 查詢的 `OSError` 視為未知，不因查詢失敗自癒，且該路徑若達輪替門檻則保留 handle 並略過本次輪替。WAL 命中後保存舊 identity 與範圍 `[file_first_seq, seq]`（範圍起點未知時 `null`、`range_known:false`，重試期間新消耗的 seq 納入終點），關閉自身 handle；若替換檔達 `>= R`，先輪替替換檔，再重建現行檔。到期 append 每次最多嘗試一次開檔；前三次失敗後間隔 10／50／200 ms，之後 1／2／5／5… 秒（上限 5 秒），不睡眠。重開後呼叫 `WalFs::tail_needs_termination`；若既存檔非空且尾端未換行，先補換行，然後寫 META（外層 `dir:"META"`、`source:"system"`、`cmd_id:null`、`com:"WAL"`、`loss_flag:true`，`seq` 消耗一號），再寫原紀錄；尾端檢查、終結或 META 寫入失敗皆保持不健康並依退避重試，成功後 `file_first_seq = META seq`。重建成功沿用既有 WAL recreated 診斷與日誌。失敗 → `unhealthy_reason="recreate_failed"`、fanout `set_wal_unhealthy(true)`；成功後清除並 fanout `false`。mirror 的 identity 檢查同節流、只重開不寫 META。
- rotate／reset 照 arch §5.8（同一鎖內 flush、`sync()`、關閉自身 handle 後執行 `rename_noreplace_or_suffix`；失敗時重開原路徑，成功後 `sync_dir`，下次寫入才開新檔；size 以「開檔時 `fs.size()` ＋ 已寫 bytes」追蹤）。
- `health`：不取主鎖，以 `AtomicU64`／`ArcSwap` 等價快照；`healthy = dir_exists && dir_writable && !(seq>0 && !file_exists) && unhealthy_reason.is_none()`。`WalWriter::open` 不開檔（lazy，P1）但評估 `fs.dir_writable(dir)`：不可寫 → `unhealthy_reason="dir_unwritable"` 並在 `set_health_sink` 時立即 fanout `true`。
- 讀取 API 用 `WalFs::read_all`（Task 0 已納入 trait、Task 6 實作與 conformance）。逐行 `decode_utf8_replace` → strip → 空行／非 JSON／非 dict／`seq` 非 int 略過。
- `reset`：WAL 與 mirror 各自以 `rename_noreplace_or_suffix` 取得實際歸檔路徑，回應的 `rotated_suffix` 取 WAL 的實際後綴（含 `.N`）；`WalHealthFanout` 由 Task 16 `SessionManager::new` 建立並以 `set_health_sink` 註冊一次，每個 session 建立 fence 時 `register`。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-core --features test-fakes --test wal_writer && cargo test -p sw-pal --features test-fakes --test wal_fs`
Expected：`wal_writer` PASS（34 項：5 個程式碼案例＋29 列表格；含 R2-F3 的部分寫入 4 列＋1 程式碼案例）。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-core/src/wal.rs crates/sw-core/tests/wal_writer.rs crates/sw-pal changelog.d/rust-s21-wal.md
git commit -m "feat(sw-core): WalWriter 常駐 handle、外部刪檔自癒與歸檔不覆蓋" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 8：sw-core CommandRegistry／SessionQueue（admission、出列、history、SC-06）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-arbiter.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust 命令仲裁：Python 相容的 admission 順序與回應、優先序＋FIFO 出列、512 筆 history 淘汰、pending 上限 256 納入已取消但仍在執行者（SC-06）。」）
**BLOCKED-OWNER：** 無。

**Files:**
- Modify: `crates/sw-core/src/arbiter.rs`
- Test: `crates/sw-core/tests/arbiter.rs`

**Interfaces:**
- Consumes：`sw_proto::constants::{CMD_PENDING_MAX, CMD_HISTORY_MAX, CMD_WARN_BYTES, CMD_REJECT_BYTES}`、`sw_proto::pyjson::PyDict`、`pydict!`；`sw_proto::pyre::valid_completion_regex`（P3 已交付並有 Python golden；`sw_core::quiet_gate::valid_completion_regex` 只是 `pub use` 重新匯出，無前向依賴）。
- Produces：

```rust
#[derive(Clone, Debug)] pub struct SubmitReq { pub session_id: String, pub command: String, pub source: String, pub mode: String,
    pub timeout_s: f64, pub priority: i128, pub expected_duration_s: Option<f64>, pub completion_regex: Option<String> }
#[derive(Clone, Debug)] pub struct QueuedCommand { pub sort_key: (i128, u64), pub cmd_id: String, pub req: SubmitReq }
pub struct Admitted { pub response: PyDict, pub queued: QueuedCommand }
pub enum RunDecision { Run, Skip }
#[derive(Clone, Debug, PartialEq)] pub enum AbortClass { InFlight, NotSubmitted, Unknown }
pub enum CmdOutcome { Result(PyDict), SendFailed, Aborted(AbortClass) }
pub struct CommandRegistry { /* Mutex<RegInner{records: IndexMap<String, PyDict>, counter: u64, in_flight: HashMap<String, String>}> */ }
impl CommandRegistry {
    pub fn new() -> Arc<CommandRegistry>;
    pub fn admit(&self, req: SubmitReq, queue_registered: bool, cmd_id: String, now_iso: &str) -> Result<Admitted, PyDict>;
    pub fn mark_running(&self, cmd_id: &str, now_iso: &str) -> RunDecision;
    pub fn set_in_flight(&self, session_id: &str, cmd_id: Option<&str>);
    pub fn complete(&self, cmd_id: &str, outcome: CmdOutcome, now_iso: &str);
    pub fn cancel(&self, cmd_id: &str, now_iso: &str) -> PyDict;
    pub fn flush_session(&self, session_id: &str, code: &str, now_iso: &str) -> Vec<String>;
    pub fn get(&self, cmd_id: &str) -> Option<PyDict>;
    pub fn len(&self) -> usize;
    pub fn pending(&self, session_id: &str) -> usize;
}
pub struct SessionQueue { /* BinaryHeap<Reverse<(i128,u64,String)>> + HashMap<String, QueuedCommand> */ }
impl SessionQueue { pub fn new() -> SessionQueue; pub fn push(&mut self, q: QueuedCommand, reg: &CommandRegistry); pub fn pop(&mut self) -> Option<QueuedCommand>;
    pub fn remove(&mut self, cmd_id: &str) -> bool; pub fn len(&self) -> usize; pub fn physical_len(&self) -> usize; pub fn drain_all(&mut self) -> Vec<QueuedCommand>; }
// 實體上限：push 前若 physical_len() >= CMD_PENDING_MAX，先 compact（丟棄 registry 中已非 accepted 的項目）；
// 因 admission 只在 pending < 256 時成功，compact 後 physical_len() <= CMD_PENDING_MAX 恆成立。actor 收到 command.cancel 的通知（ManagerNote 無關，經 registry 回傳 session_id 後 route 一個 CancelQueued 事件）時直接 remove。
```

- [ ] **Step 1：寫失敗測試**

```rust
use sw_core::arbiter::*;
use sw_proto::pyjson::{dumps, PyValue, RPC_WIRE};

fn req(cmd: &str) -> SubmitReq {
    SubmitReq { session_id: "p:COM0".into(), command: cmd.into(), source: "agent".into(), mode: "line".into(),
                timeout_s: 10.0, priority: 10, expected_duration_s: None, completion_regex: None }
}
const T: &str = "2026-10-15T03:06:17.663778+00:00";

#[test]
fn too_long_checked_before_newline_with_exact_response() {
    let r = CommandRegistry::new();
    let mut long = "a".repeat(16_385); long.push('\n');
    let e = r.admit(req(&long), true, "c1".into(), T).err().unwrap();
    assert_eq!(dumps(&PyValue::Dict(e), &RPC_WIRE),
        "{\"ok\":false,\"error_code\":\"CMD_TOO_LONG\",\"cmd_length\":16386,\"limit\":16384,\"hint\":\"Command exceeds 16 KB hard limit. Use file-based injection or split into smaller commands.\"}\n");
}

#[test]
fn queue_full_counts_canceled_but_running() {
    let r = CommandRegistry::new();
    let mut q = SessionQueue::new();
    for i in 0..256 { let a = r.admit(req("x"), true, format!("c{i}"), T).ok().unwrap(); q.push(a.queued, &r); }
    let head = q.pop().unwrap();
    assert!(matches!(r.mark_running(&head.cmd_id, T), RunDecision::Run));
    r.set_in_flight("p:COM0", Some(&head.cmd_id));
    r.cancel(&head.cmd_id, T);                                      // 標 canceled、設 done_at
    let e = r.admit(req("y"), true, "late".into(), T).err().unwrap();
    assert_eq!(e.get("error_code"), Some(&PyValue::from("SESSION_QUEUE_FULL")));
    assert_eq!(e.get("pending"), Some(&PyValue::from(256)));       // 255 accepted + 1 canceled-running（SC-06）
}

#[test]
fn record_key_order_matches_python() {
    let r = CommandRegistry::new();
    r.admit(req("ls"), true, "c1".into(), T).ok().unwrap();
    let keys: Vec<String> = r.get("c1").unwrap().keys().cloned().collect();
    assert_eq!(keys, ["cmd_id","session_id","command","source","mode","execution_mode","timeout_s","expected_duration_s",
        "completion_regex","priority","status","created_at","accepted_at","started_at","done_at","error_code","stdout",
        "partial","background_capture_id","interactive_session_id","recovery_action"]);
}
```

| 測試名 | 精確斷言 |
|---|---|
| `invalid_completion_regex_first` | `completion_regex=Some("")` 且命令含 `\n` → `{"ok":false,"error_code":"INVALID_COMPLETION_REGEX"}`（無其他鍵） |
| `warning_above_4096` | 4097 bytes → `ok:true`，回應末鍵 `warning == {"code":"CMD_LENGTH_WARNING","cmd_length":4097,"soft_limit":4096,"hint":"Command exceeds 4 KB soft limit. Long commands may cause UART buffer overflow or prompt timeout."}` |
| `length_counts_utf8_replace_bytes` | `"中"×5462`（16386 bytes）→ `CMD_TOO_LONG`，`cmd_length==16386` |
| `newline_rejected_cr_allowed_empty_allowed` | `"a\nb"` → `CMD_CONTAINS_NEWLINE` 回應 `{"ok":false,"error_code":"CMD_CONTAINS_NEWLINE","hint":"Command must not contain embedded newline characters. Split into multiple independent submissions."}`；`"a\rb"` 與 `""` → ok |
| `unregistered_queue_not_ready` | `queue_registered=false` → `{"ok":false,"error_code":"SESSION_NOT_READY","session_id":"p:COM0"}` |
| `accepted_response_shape` | `{"ok":true,"cmd_id":"c1","status":"accepted","session_id":"p:COM0"}` |
| `priority_then_fifo` | priority 20,5,5,10 依序 admit → pop 順序 `c1(5)`,`c2(5)`,`c3(10)`,`c0(20)` |
| `execution_mode_alias` | mode `fg`→`line`、`bg`→`background`、`weird`→`weird` |
| `mark_running_skips_canceled_or_done` | cancel 後 `mark_running` → `Skip`；正常 → `status:"running"`、`started_at==T` |
| `complete_ok_true_drops_error_code` | `Result({"ok":true,"error_code":"PROMPT_TIMEOUT_RECOVERED","recovery_action":"CTRL_C","stdout":"o","partial":true})` → record `status:"done"`、`error_code:null`、`recovery_action:"CTRL_C"`、`stdout:"o"`、`partial:true` |
| `complete_ok_false` | `{"ok":false,"error_code":"PROMPT_TIMEOUT"}` → `status:"error"`、`error_code:"PROMPT_TIMEOUT"`；無碼 → `COMMAND_FAILED` |
| `complete_interactive_status` | `{"ok":true,"status":"interactive","interactive_session_id":"i"}` → `status:"interactive"` |
| `complete_on_canceled_keeps_canceled` | cancel 後 complete → record 不變 |
| `send_failed` | `CmdOutcome::SendFailed` → `status:"error"`、`error_code:"SEND_FAILED"` |
| `aborted_mapping_bc03` | `Aborted(InFlight)` → `error_code:"COMMAND_ABORTED_IN_FLIGHT"`；`Aborted(NotSubmitted)` → `FLUSHED_BY_RECOVERY`；`Aborted(Unknown)` → `SEND_FAILED`；三者 `status:"error"` 且 `done_at==T` |
| `cancel_semantics` | 不存在 → `{"ok":false,"error_code":"CMD_NOT_FOUND","cmd_id":"x"}`；done/error → `CMD_NOT_CANCELABLE`；accepted/running/interactive/canceled → `{"ok":true,"cmd_id":..,"status":"canceled"}` |
| `flush_only_accepted` | running 與 interactive 不動；accepted → `status:"error"`、`error_code:<code>`、`done_at`；`FLUSHED_BY_SHUTDOWN` 同理；回傳被 flush 的 cmd_id 依 admit 順序 |
| `history_evicts_oldest_done_by_done_at_string` | 600 筆完成（done_at 字串遞增）+ 1 筆 running → `len() <= 512`，被淘汰的是 done_at 最小者；running 永不淘汰；flush 不觸發淘汰 |
| `history_never_evicts_in_flight_canceled` | 前景 running 的命令被 cancel（有 `done_at`、`status:"canceled"`、`in_flight` 指向它）且 done_at 最舊 → 淘汰時跳過該筆（§5.4「不淘汰仍在途命令以隱藏負載」），`pending` 仍計 +1（SC-06） |
| `submit_cancel_loop_keeps_queue_bounded` | 前景永遠忙；重複 10000 次「admit → push → cancel」→ `queue.physical_len() <= 256`、`registry.len() <= 512 + 1`（含在途） |
| `cancel_queued_removes_from_heap` | admit＋push 後 cancel 並 `queue.remove(id)` → `physical_len()` 減 1，`pop()` 不會回傳該筆 |
| `pending_excludes_interactive` | record `status:"interactive"`（有 done_at）不計入 `pending` |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-core --features test-fakes --test arbiter`
Expected：FAIL，`not implemented: slice21 T8 CommandRegistry::admit`。

- [ ] **Step 3：實作**

依 05 §4.3～§4.7、02 §6.3 逐條。`complete`／`cancel` 不改 queue；queue 實體上限與 `remove` 見 Produces 註解。evict 跳過 `in_flight` 指向的紀錄。`admit` 順序：completion regex → UTF-8（replace）長度 > 16384 → >4096 warning → `\n` → `!queue_registered` → `pending(sid) >= 256` → counter+1、建 record（`created_at == accepted_at == now_iso`）、evict → 回 `(response, QueuedCommand{sort_key:(priority, counter)})`。`pending(sid)` = 該 sid 無 `done_at` 的筆數 ＋（`in_flight[sid]` 指向的 record 為 `canceled` 時 +1）。`complete` 依 05 §4.5；`Aborted` 依 arch §5.7 映射；完成後 `in_flight` 若指向該 cmd 則清除並 evict。evict：`len > 512` 時取所有有 `done_at` 者依 `done_at` 字串升冪淘汰直到 `len <= 512`。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-core --features test-fakes --test arbiter`
Expected：PASS（25 項：3 個程式碼案例＋22 列表格）。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-core/src/arbiter.rs crates/sw-core/tests/arbiter.rs changelog.d/rust-s21-arbiter.md
git commit -m "feat(sw-core): 命令 admission、出列與 history 上限" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 9：sw-core TxWriter＋Fence（送出前檢查、BC-03 三態、SC-07）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-txwriter.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust 單一 TX 寫入者：每次 OS 提交都在 fence 鎖內重驗 epoch／凍結／Quiet／Reconfirm／WAL 健康／世代／取消後才交 OS（授權與提交同一臨界區，鎖不跨完成等待），TX WAL 只記 OS 確認的 bytes 且順序等於實際寫入順序；TX 帳本只保留未結算 op，終態 op 經 actor 結算後退休，證據缺口另有上限與合併計數。」）
**BLOCKED-OWNER：** 無。

**Files:**
- Modify: `crates/sw-core/src/tx_writer.rs`
- Test: `crates/sw-core/tests/tx_writer.rs`

**Interfaces:**
- Consumes：Task 7 `WalWriter`、`WalHealthSink`；Task 1 `FakeSerialBackend`；arch §5.4 型別。
- Produces：

```rust
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)] pub struct OpId(pub u64);
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum TxClass { Flash, FlashProbe, LeaseOwner, HumanRaw, HumanReplay, LoginProbe,
    ReadyReconfirm, Command, FileTransfer, SystemRecoverKey, SelfTestProbe, InteractiveOpenCmd }
#[derive(Clone, Debug)] pub struct TxPermit { pub op_id: OpId, pub epoch: u64, pub owner_gen: u64, pub fsm_gen: u64, pub class: TxClass,
    pub source: String, pub cmd_id: Option<String>, pub log: bool }
// 世代規則（Fence::admit 第 6 步）：LeaseOwner／InteractiveOpenCmd／HumanRaw／HumanReplay 需 permit.owner_gen == st.owner_gen；
// LoginProbe／ReadyReconfirm 需 permit.fsm_gen == st.fsm_gen。lease close／expiry、quiet arm、reboot、drain Phase 1 皆遞增 owner_gen；
// quiet arm、reboot、新 epoch 皆遞增 fsm_gen（07 計畫消費 TxPermit 時須帶 fsm_gen，下游對齊）。
// SelfTestProbe（R5-F2，DF-19）不比對 fsm_gen（self-test 不是 login run，不得被進行中的登入世代取代，SES-16.3b）：
// 只受 epoch 與 quiet_active 擋——quiet_active 在 arm 的同一次 Fence::with 內設定（任何 profile），與 actor 內 self-test
// pre-check（classify_pre_probe 的 AUTOBOOT_QUIET 分支）同義，故「pre-check 通過→banner arm→OS 提交」必為零 TX。
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum FlashMode { Off, Probe(u64), Flashing }
#[derive(Debug)] pub struct InFlight { pub op_id: OpId, pub len: usize }
#[derive(Debug)] pub struct FenceState { pub active_epoch: Option<u64>, pub owner_gen: u64, pub fsm_gen: u64, pub frozen: bool, pub quiet_active: bool,
    pub reconfirm_pending: bool, pub flash_mode: FlashMode, pub wal_unhealthy: bool, pub cancelled: HashSet<OpId>, pub in_flight: Option<InFlight> }
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum FenceReject { StaleEpoch, Frozen, Quiet, ReconfirmPending, WalUnhealthy, Flash, OwnerMismatch, FsmMismatch, Cancelled }
pub struct Fence(Mutex<FenceState>);
impl Fence { pub fn new() -> Arc<Fence>; pub fn with<R>(&self, f: impl FnOnce(&mut FenceState) -> R) -> R; pub fn admit(st: &FenceState, p: &TxPermit) -> Result<(), FenceReject>;
             pub fn try_with<R>(&self, f: impl FnOnce(&mut FenceState) -> R) -> Option<R>; /* try_lock；鎖被持有時回 None（鎖持有斷言用） */ }
impl WalHealthSink for Fence {}                               // set_wal_unhealthy → state.wal_unhealthy
/// arch §4.2／§5.4 第 3 點（R0-F1）：授權與 OS 提交的唯一序列化點。submit：取 Fence 鎖 → Fence::admit(permit) →
/// Err(r) 時記 last_reject=Some(r) 並回 Err(SubmitRevoked)（不呼叫 os_submit）→ Ok 時仍持鎖呼叫 os_submit →
/// Accepted(n>0)／Pending 時同一臨界區設 in_flight=Some(InFlight{op_id, len}) → 放鎖。鎖只跨非阻塞提交，不跨完成等待。
pub struct FenceSubmitGate<'a> { pub fence: &'a Fence, pub permit: &'a TxPermit, pub last_reject: Cell<Option<FenceReject>> }
impl SubmitGate for FenceSubmitGate<'_> {}
pub struct TxJob { pub permit: TxPermit, pub payload: Vec<u8>, pub reply: crossbeam_channel::Sender<TxReport> }
#[derive(Debug, Clone)] pub struct TxReport { pub op_id: OpId, pub epoch: u64, pub result: TxResult, pub wal: Option<WalAppend> /* TX WAL 寫入結果（seq／ok／loss_flag）*/, pub reject: Option<FenceReject> }
/// epoch 範圍的 TX 帳本（R1-F8：只保留**未結算** op）：TxWriter 對每個 op 累計；actor 判定某 op 終態且證據已收斂後，
/// 經 `TxWriterHandle::settle` 送出結算，writer 才移除該 op（結算／退休協定見下方註解）。結束時把剩餘帳本交回，
/// finisher 原樣放進 WorkerStoppedReceipt（BC-03 映射的唯一來源）。**帳本缺項＝該 op 在本 epoch 沒有任何已提交 bytes 的證據**，
/// 映射同 `confirmed_bytes:0, unknown:false`（→ `FLUSHED_BY_RECOVERY`）；因此只有「確定不再被 BC-03 查詢」的 op 可以移除。
pub const TX_LEDGER_LIVE_MAX: usize = 256;            // 未結算 op 上限；滿時新 op 一律不提交（見 saturation）
pub const TX_LEDGER_UNRESOLVED_GAPS_MAX: usize = 256; // 已結算 op 遺留的 WAL 證據缺口上限；溢位合併最舊者進 gaps_overflow
pub const TX_OP_WAL_GAPS_MAX: usize = 16;             // 單一 op 的 wal_gaps 上限；溢位合併進該 op 的 wal_gaps_overflow
pub const TX_SETTLE_QUEUE_MAX: usize = 256;           // settle channel 容量
#[derive(Debug, Clone, Default, PartialEq, Eq)] pub struct GapOverflow { pub gaps: u64, pub bytes: u64, pub first_op: Option<OpId>, pub last_op: Option<OpId> }
#[derive(Debug, Clone, Default)] pub struct TxOpLedger { pub confirmed_bytes: usize, pub unknown: bool, pub last_error: Option<PalError>,
    pub wal_ok: bool, pub wal_gaps: Vec<EvidenceGap> /* 已寫出但 TX WAL 失敗；≤ TX_OP_WAL_GAPS_MAX */, pub wal_gaps_overflow: GapOverflow,
    pub reply_lost: bool, pub not_submitted: usize }
#[derive(Debug, Clone, Default)] pub struct TxLedger { pub epoch: u64, pub ops: IndexMap<OpId, TxOpLedger> /* 只含未結算 op，≤ TX_LEDGER_LIVE_MAX */,
    pub unresolved_gaps: VecDeque<EvidenceGap> /* 已結算 op 的 wal_gaps 移入此處，≤ TX_LEDGER_UNRESOLVED_GAPS_MAX，FIFO 合併 */,
    pub gaps_overflow: GapOverflow /* 被合併的缺口計數與 bytes 總和（證據不靜默丟棄，規格 §5.4） */,
    pub settled_total: u64, pub saturated: bool }
/// writer thread 每次變更帳本後以 Release 寫入；供測試與 daemon status 診斷讀取（不經 writer thread）。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)] pub struct TxLedgerGauge { pub live_ops: usize, pub unresolved_gaps: usize,
    pub gaps_overflow: u64, pub settled_total: u64, pub settle_dropped: u64, pub saturated: bool }
#[derive(Clone)] pub struct TxWriterHandle { pub jobs: crossbeam_channel::Sender<TxJob> /* bounded(64)；可被 OpCtx clone 持有 */,
    settle: crossbeam_channel::Sender<OpId> /* bounded(TX_SETTLE_QUEUE_MAX) */, gauge: Arc<LedgerGaugeCells> }
impl TxWriterHandle {
    /// 非阻塞（actor executor 呼叫，不得阻塞 actor）：try_send；Full 時回 false 並 settle_dropped+=1——該 op 留在帳本
    /// （過度保留、不失證據；最壞情況由 saturation 收斂），Disconnected（writer 已結束）回 false 且不計數。
    pub fn settle(&self, op_id: OpId) -> bool;
    pub fn ledger_gauge(&self) -> TxLedgerGauge;
}
pub struct TxWriterStop(crossbeam_channel::Sender<()>);     // 只有 IoWorker 持有；send 後 writer 不再接新 job
pub fn spawn_tx_writer(com: String, writer: Box<dyn SerialWriter>, cancel: CancelToken, fence: Arc<Fence>, wal: Arc<WalWriter>, epoch: u64,
                       control: crossbeam_channel::Sender<ControlEvent> /* saturation 通知；阻塞送（內部事件） */)
    -> (TxWriterHandle, TxWriterStop, std::thread::JoinHandle<(Box<dyn SerialWriter>, TxLedger)>);
// 結束條件與 Sender 數量無關：收到 TxWriterStop（或所有 Sender drop）即結束；結束前先套用 settle 佇列中已到達的結算，
// 再把仍在 jobs 佇列的 job 逐一以 NotSubmitted＋Frozen 回報並記入帳本（不呼叫 OS write）。
//
// 結算／退休協定（R1-F8，arch §5.7）：
// (1) 只有 actor 能發起結算：SessionCore 在某 op 的**終態處理點**（命令紀錄已 registry.complete 或該 op 的完成事件已套用）
//     產生 Effect::SettleTx{epoch, op_id}；executor 只在 epoch == 現行 worker epoch 時呼叫 worker.tx().settle(op_id)，
//     否則丟棄（舊 epoch 的帳本會整本進 receipt，舊 op 若已終態，stale receipt 路徑本就不再碰它）。
// (2) actor **不得**結算：stop.aborted_op（drain 進行中其 OpFinished 被忽略，結果只由 receipt 帳本決定）、任何尚未終態的 op。
//     因此結算永遠不會移除 BC-03 仍需查詢的證據——不論 settle 與 TxWriterStop 誰先到達 writer。
// (3) writer 單執行緒套用結算（select_biased：stop > settle > jobs），套用時該 op 不可能有 job 正在 write_gated；
//     移除 ops[op_id]，其 wal_gaps／wal_gaps_overflow 移入 unresolved_gaps／gaps_overflow（不丟棄），reply_lost 隨之清除
//     （actor 已拿到終態）；op 不在帳本（無 TX 或已結算）時為 no-op。結算後同 op 再到達的 job 屬協定違反：照常處理並重建項目，
//     debug_assert 失敗、release build 記 WARNING（該項目留到 epoch 結束，計入 live 上限）。
// (4) saturation：新 op_id 的 job 到達時若 ops.len() >= TX_LEDGER_LIVE_MAX，**不淘汰任何既有項目**：該 job 不呼叫 PAL，
//     回 NotSubmitted＋Frozen 且不建立項目（缺項＝未提交，BC-03 映射正確）；第一次發生時設 saturated=true 並送
//     ControlEvent::UartIoError{epoch, reason:"TX_LEDGER_SATURATED"}，actor 以既有 IoError drain 收斂（新 epoch 帳本歸零；
//     不新增 error_code，last_error 依既有 IoError 規則）。既有 op 的後續 job 照常處理，在途證據不被截斷。
```

- [ ] **Step 1：寫失敗測試**

```rust
#![cfg(feature = "test-fakes")]
use std::{sync::{Arc, Mutex}, time::Duration};
use sw_core::{mailbox::ControlEvent, tx_writer::*, wal::*};
use sw_pal::{fakes::{clock::FakeClock, serial::*, wal_fs::FakeWalFs}, *};

struct Rig { fence: Arc<Fence>, wal: Arc<WalWriter>, dev: FakeDevice, h: TxWriterHandle, wfs: Arc<FakeWalFs>, cancel: CancelToken,
             stop: Option<TxWriterStop>, join: Option<std::thread::JoinHandle<(Box<dyn SerialWriter>, TxLedger)>>,
             ctl: Arc<Mutex<Vec<ControlEvent>>> /* drainer thread 收集 writer 送出的 control 事件，避免 bounded(256) 塞住 writer */ }
fn wait_gauge(r: &Rig, pred: impl Fn(&TxLedgerGauge) -> bool) -> TxLedgerGauge {   // 每 2 ms 輪詢，2 s 未滿足即 panic 並印出最後 gauge
    let t0 = std::time::Instant::now();
    loop { let g = r.h.ledger_gauge(); if pred(&g) { return g } assert!(t0.elapsed() < Duration::from_secs(2), "gauge {g:?}"); std::thread::sleep(Duration::from_millis(2)); }
}
fn rig() -> Rig {
    let be = FakeSerialBackend::new(); let dev = be.add_device("/dev/f");
    let h = be.open_exclusive(&DevicePath("/dev/f".into()), &SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false)).unwrap();
    let (_r, w, waker, _c) = h.split();
    let wfs = FakeWalFs::new();
    let wal = Arc::new(WalWriter::open(WalConfig { dir: "/w".into(), rotate_bytes: 1 << 26 }, wfs.clone(), FakeClock::at(1, 1)));
    let fence = Fence::new(); fence.with(|s| s.active_epoch = Some(7));
    let cancel = CancelToken::new(waker);
    let (ctl_tx, ctl_rx) = crossbeam_channel::bounded::<ControlEvent>(256);
    let ctl = Arc::new(Mutex::new(Vec::new()));
    { let ctl = ctl.clone(); std::thread::spawn(move || for ev in ctl_rx.iter() { ctl.lock().unwrap().push(ev) }); }
    let (h, stop, j) = spawn_tx_writer("COM0".into(), w, cancel.clone(), fence.clone(), wal.clone(), 7, ctl_tx);
    Rig { fence, wal, dev, h, wfs, cancel, stop: Some(stop), join: Some(j), ctl }
}
fn send(r: &Rig, class: TxClass, op: u64, epoch: u64, payload: &[u8]) -> TxReport {
    let (tx, rx) = crossbeam_channel::bounded(1);
    r.h.jobs.send(TxJob { permit: TxPermit { op_id: OpId(op), epoch, owner_gen: 0, fsm_gen: 0, class, source: "agent:x".into(), cmd_id: Some("c".into()), log: true },
                          payload: payload.to_vec(), reply: tx }).unwrap();
    rx.recv_timeout(Duration::from_secs(2)).unwrap()
}
fn tx_lens(r: &Rig) -> Vec<String> {
    String::from_utf8(r.wfs.contents("/w/raw.wal.ndjson".as_ref()).unwrap_or_default()).unwrap()
        .lines().filter(|l| l.contains("\"dir\":\"TX\"")).map(|l| l.to_string()).collect()
}

#[test]
fn stale_epoch_is_not_submitted_and_never_calls_os_write() {
    let r = rig();
    let rep = send(&r, TxClass::Command, 1, 6, b"ls\n");
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::StaleEpoch)));
    assert_eq!(r.dev.write_calls(), 0);
    assert!(tx_lens(&r).is_empty());
}

#[test]
fn partial_write_records_only_confirmed_prefix() {
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::AcceptThenError(3, PalError::io(5, "[Errno 5] Input/output error"))]);
    let rep = send(&r, TxClass::Command, 1, 7, b"0123456789");
    assert_eq!(rep.result.outcome, TxOutcome::Completed(3));
    let lines = tx_lens(&r);
    assert_eq!(lines.len(), 1);
    assert!(lines[0].contains("\"len\":3,") && lines[0].contains("\"payload_b64\":\"MDEy\""));
}

#[test]
fn freeze_during_in_flight_counts_as_committed_and_next_is_rejected() {
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::BlockUntilCancel { accept_before_cancel: 2 }]);
    let (tx, rx) = crossbeam_channel::bounded(1);
    r.h.jobs.send(TxJob { permit: TxPermit { op_id: OpId(1), epoch: 7, owner_gen: 0, fsm_gen: 0, class: TxClass::Command, source: "a".into(), cmd_id: None, log: true },
                          payload: b"abcdef".to_vec(), reply: tx }).unwrap();
    std::thread::sleep(Duration::from_millis(30));
    r.fence.with(|s| { assert!(s.in_flight.is_some()); s.frozen = true; s.active_epoch = None; });
    r.cancel.cancel();
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(rep.result.outcome, TxOutcome::Completed(2));
    assert_eq!(tx_lens(&r).len(), 1);
    let rep2 = send(&r, TxClass::Command, 2, 7, b"x");
    assert_eq!(rep2.result.outcome, TxOutcome::NotSubmitted);
}

// ---- R0-F1：授權與 OS 提交的序列化（arch §5.4 第 3 點、規格 §5.2.2）----
fn send_async(r: &Rig, class: TxClass, op: u64, owner_gen: u64, payload: &[u8]) -> crossbeam_channel::Receiver<TxReport> {
    let (tx, rx) = crossbeam_channel::bounded(1);
    r.h.jobs.send(TxJob { permit: TxPermit { op_id: OpId(op), epoch: 7, owner_gen, fsm_gen: 0, class, source: "agent:x".into(), cmd_id: Some("c".into()), log: true },
                          payload: payload.to_vec(), reply: tx }).unwrap();
    rx
}

#[test]
fn authorized_then_quiet_before_os_submit_is_zero_tx() {        // 舊設計（鎖內驗證→放鎖→write）會在此寫出 bytes
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let rx = send_async(&r, TxClass::Command, 1, 0, b"reboot\n");
    assert!(r.dev.wait_paused(Duration::from_secs(2)));           // 出列快速檢查已通過、尚未提交 OS
    r.fence.with(|s| s.quiet_active = true);                      // 不得阻塞：writer 此時不持鎖
    r.dev.release_pause();
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::Quiet)));
    assert!(rep.result.error.is_none());
    assert_eq!(r.dev.os_submits(), 0);
    assert!(r.dev.take_tx().is_empty());
    assert!(tx_lens(&r).is_empty());
    assert!(r.fence.with(|s| s.in_flight.is_none()));
}

#[test]
fn authorized_then_drain_freeze_before_os_submit_is_zero_tx() {
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let rx = send_async(&r, TxClass::Command, 1, 0, b"ls\n");
    assert!(r.dev.wait_paused(Duration::from_secs(2)));
    r.fence.with(|s| { s.frozen = true; s.active_epoch = None; });   // Phase 1（換手／release）
    r.dev.release_pause();
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::Frozen)));
    assert_eq!(r.dev.os_submits(), 0);
    assert!(r.dev.take_tx().is_empty() && tx_lens(&r).is_empty());
}

#[test]
fn authorized_then_lease_owner_gen_bump_before_os_submit_is_zero_tx() {   // LeaseOwner 世代在提交臨界區重驗
    let r = rig();
    r.fence.with(|s| s.owner_gen = 3);
    r.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let rx = send_async(&r, TxClass::LeaseOwner, 1, 3, b"\x03");
    assert!(r.dev.wait_paused(Duration::from_secs(2)));
    r.fence.with(|s| s.owner_gen = 4);                            // lease close／expiry／換手
    r.dev.release_pause();
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::NotSubmitted, Some(FenceReject::OwnerMismatch)));
    assert_eq!(r.dev.os_submits(), 0);
    assert!(r.dev.take_tx().is_empty() && tx_lens(&r).is_empty());
}

#[test]
fn quiet_during_os_submit_waits_for_submit_and_counts_committed() {
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::PauseInsideSubmit]);
    let rx = send_async(&r, TxClass::Command, 1, 0, b"abc");
    assert!(r.dev.wait_paused(Duration::from_secs(2)));           // 已重驗通過、持 Fence 鎖、os_submit 進行中
    let (done_tx, done_rx) = crossbeam_channel::bounded(1);
    let f = r.fence.clone();
    let inv = std::thread::spawn(move || { f.with(|s| s.quiet_active = true); done_tx.send(()).unwrap(); });
    assert!(done_rx.recv_timeout(Duration::from_millis(100)).is_err(), "失效操作必須等到 OS 提交返回");
    assert!(r.dev.take_tx().is_empty(), "暫停點在提交之前，bytes 尚未交 OS");
    r.dev.release_pause();
    inv.join().unwrap();
    assert!(done_rx.recv_timeout(Duration::from_secs(1)).is_ok());
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::Completed(3), None));
    assert_eq!(r.dev.take_tx(), b"abc".to_vec());
    assert_eq!(tx_lens(&r).len(), 1);
    let rep2 = send(&r, TxClass::Command, 2, 7, b"x");
    assert_eq!((rep2.result.outcome, rep2.reject), (TxOutcome::NotSubmitted, Some(FenceReject::Quiet)));
    assert_eq!(r.dev.os_submits(), 1);
}

#[test]
fn quiet_between_partial_submits_stops_tail() {
    let r = rig();
    r.dev.script_write(vec![FakeWriteStep::AcceptThenWouldBlock(2)]);
    let rx = send_async(&r, TxClass::Command, 1, 0, b"abcdef");
    assert!(r.dev.wait_paused(Duration::from_secs(2)));           // 已提交 "ab"，於 gate 外等 POLLOUT
    r.fence.with(|s| { assert!(s.in_flight.is_some()); s.quiet_active = true; });   // 不得阻塞
    r.dev.release_pause();
    let rep = rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!((rep.result.outcome, rep.reject), (TxOutcome::Completed(2), Some(FenceReject::Quiet)));
    assert_eq!(rep.result.error.as_ref().unwrap().kind, PalErrorKind::Cancelled);
    assert_eq!(r.dev.take_tx(), b"ab".to_vec());                 // 尾段 "cdef" 永不交 OS
    assert_eq!(r.dev.os_submits(), 1);
    let lines = tx_lens(&r);
    assert_eq!(lines.len(), 1);
    assert!(lines[0].contains("\"len\":2,"));
}

#[test]
fn os_submit_always_inside_fence_critical_section() {
    let r = rig();
    let f = r.fence.clone();
    let unlocked = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let u2 = unlocked.clone();
    r.dev.on_os_submit(Box::new(move || { if f.try_with(|_| ()).is_some() { u2.fetch_add(1, std::sync::atomic::Ordering::SeqCst); } }));
    r.dev.script_write(vec![FakeWriteStep::AcceptThenWouldBlock(1)]);
    let rx = send_async(&r, TxClass::Command, 1, 0, b"xyz");
    assert!(r.dev.wait_paused(Duration::from_secs(2)));
    r.dev.release_pause();
    assert_eq!(rx.recv_timeout(Duration::from_secs(2)).unwrap().result.outcome, TxOutcome::Completed(3));
    for i in 0..20u64 { send(&r, TxClass::Command, 10 + i, 7, b"q"); }
    assert!(r.dev.os_submits() >= 22);
    assert_eq!(unlocked.load(std::sync::atomic::Ordering::SeqCst), 0, "每次 OS 提交時 Fence 鎖必為持有");
}

#[test]
fn ledger_retires_completed_ops_without_reconnect() {     // R1-F8：同一 epoch 長跑、不重連，已結算 op 不得累積
    let mut r = rig();
    for i in 0..600u64 {                                     // 超過 CMD_HISTORY_MAX(512)：history 淘汰後帳本也不得殘留
        assert_eq!(send(&r, TxClass::Command, 1000 + i, 7, b"ok\n").result.outcome, TxOutcome::Completed(3));
        assert!(r.h.settle(OpId(1000 + i)));                 // 等同 actor 終態處理點的 Effect::SettleTx
    }
    let g = wait_gauge(&r, |g| g.settled_total == 600);
    assert_eq!((g.live_ops, g.unresolved_gaps, g.gaps_overflow, g.settle_dropped, g.saturated), (0, 0, 0, 0, false));
    assert_eq!(send(&r, TxClass::Command, 5000, 7, b"tail").result.outcome, TxOutcome::Completed(4));   // 未結算 op
    wait_gauge(&r, |g| g.live_ops == 1);
    r.stop.as_ref().unwrap().send();
    let (_w, ledger) = r.join.take().unwrap().join().unwrap();
    assert_eq!(ledger.ops.keys().copied().collect::<Vec<_>>(), vec![OpId(5000)]);   // 只剩未結算者
    assert_eq!((ledger.ops[&OpId(5000)].confirmed_bytes, ledger.settled_total), (4, 600));
    assert!(ledger.unresolved_gaps.is_empty() && ledger.gaps_overflow == GapOverflow::default());
    assert_eq!(tx_lens(&r).len(), 601);                      // 結算只動記憶體帳本，不動 WAL 證據
}

#[test]
fn ledger_retire_racing_drain_preserves_bc03() {          // R1-F8：結算與 drain 競速時，在途 op 的 BC-03 證據不得被淘汰
    for settle_after_stop in [false, true] {
        let mut r = rig();
        assert_eq!(send(&r, TxClass::Command, 1, 7, b"done\n").result.outcome, TxOutcome::Completed(5));   // 已終態的舊命令
        r.dev.script_write(vec![FakeWriteStep::BlockUntilCancel { accept_before_cancel: 2 }]);
        let rx2 = send_async(&r, TxClass::Command, 2, 0, b"abcdef");                                       // 前景在途＝aborted_op
        let t0 = std::time::Instant::now();
        while r.fence.with(|s| s.in_flight.as_ref().map(|f| f.op_id)) != Some(OpId(2)) {
            assert!(t0.elapsed() < Duration::from_secs(2)); std::thread::sleep(Duration::from_millis(1));
        }
        if !settle_after_stop { assert!(r.h.settle(OpId(1))); }        // writer 卡在 op 2 的完成等待，settle 先排隊
        r.fence.with(|s| { s.frozen = true; s.active_epoch = None; }); // drain Phase 1 線性化點
        r.stop.as_ref().unwrap().send();
        if settle_after_stop { let _ = r.h.settle(OpId(1)); }           // 可能被 stop 後的 settle 收尾套用，也可能 Disconnected
        r.cancel.cancel();
        assert_eq!(rx2.recv_timeout(Duration::from_secs(2)).unwrap().result.outcome, TxOutcome::Completed(2));
        let queued = send_async(&r, TxClass::Command, 3, 0, b"zz");     // stop 後才到：不得寫 OS
        let (_w, ledger) = r.join.take().unwrap().join().unwrap();
        let op2 = &ledger.ops[&OpId(2)];                                // aborted_op 永不被結算，帳本必有其證據
        assert_eq!((op2.confirmed_bytes, op2.unknown), (2, false), "settle_after_stop={settle_after_stop}");
        if !settle_after_stop { assert!(!ledger.ops.contains_key(&OpId(1))); }   // stop 前已排隊的 settle 必在交帳前套用
        if let Some(op1) = ledger.ops.get(&OpId(1)) { assert_eq!(op1.confirmed_bytes, 5); }   // 未套用時證據完整、不被部分改寫
        if let Ok(rep) = queued.recv_timeout(Duration::from_millis(200)) { assert_eq!(rep.result.outcome, TxOutcome::NotSubmitted); }
        assert_eq!(r.dev.take_tx(), b"done\nab".to_vec());              // 結算不改變任何已寫出／未寫出的事實
    }
}
```

Gate 表（`gate_rules` 參數化測試；每列以 `Fence::admit(&state, &permit)` 斷言）：

| class | 狀態 | 預期 |
|---|---|---|
| `Command` | `quiet_active` | `Err(Quiet)` |
| `Command` | `reconfirm_pending` | `Err(ReconfirmPending)` |
| `FileTransfer` | `quiet_active` | `Err(Quiet)` |
| `LoginProbe`／`ReadyReconfirm`／`SystemRecoverKey`／`SelfTestProbe` | `quiet_active` | `Err(Quiet)` |
| `LoginProbe`／`ReadyReconfirm`／`SystemRecoverKey`／`SelfTestProbe` | `wal_unhealthy` | `Err(WalUnhealthy)` |
| `SelfTestProbe` | `reconfirm_pending` 且 `quiet_active=false` | `Ok(())`（R5-F2：self-test 是 `READY_UNCONFIRMED` 的建議出口，不受 reconfirm 擋） |
| `SelfTestProbe` | `permit.fsm_gen != st.fsm_gen`、`quiet_active=false` | `Ok(())`（R5-F2：不比對 fsm_gen） |
| `Command` | `wal_unhealthy` | `Ok(())`（agent 命令不屬自動 TX） |
| `LeaseOwner`／`InteractiveOpenCmd` | `quiet_active` | `Ok(())`（05 §7 不被 gate） |
| `Command` | `reconfirm_pending` 且 `quiet_active=false`，`SystemRecoverKey` 同狀態 | `Err(ReconfirmPending)`／`Ok(())` |
| 任一非 Flash class | `flash_mode=Flashing` | `Err(Flash)` |
| `Flash` | `flash_mode=Off` | `Err(Flash)` |
| 任一 | `frozen` | `Err(Frozen)` |
| 任一 | `epoch != active_epoch` 或 `active_epoch=None` | `Err(StaleEpoch)` |
| 任一 | `op_id ∈ cancelled` | `Err(Cancelled)` |

其他：

| 測試名 | 精確斷言 |
|---|---|
| `unknown_outcome_records_no_tx_and_reports_unknown` | `Unknown { confirmed: 0, error: e }` → `TxReport.result.outcome == Unknown { confirmed: 0 }`、`wal_seq == None`、無 TX 行 |
| `log_false_secret_not_in_wal` | `permit.log=false` → OS 收到 bytes、WAL 無 TX 行（UIO-12.3-c） |
| `wal_order_equals_write_order` | 連續 50 個 job（payload `i`）→ WAL TX 行的 payload 序 == `dev.take_tx()` 序 |
| `fence_sink_sets_wal_unhealthy` | `WalHealthSink::set_wal_unhealthy(&*fence, true)` → `fence.with(|s| s.wal_unhealthy) == true` |
| `job_queue_bounded_64` | writer `BlockForever` 時第 65 個 `try_send` 回 `Full` |
| `writer_returned_on_shutdown` | `stop` 後 join 回 `(writer, ledger)`；無 job 時 `ledger.ops` 為空 |
| `stop_ends_writer_while_other_senders_alive` | 另 clone 一個 `TxWriterHandle`（模擬 OpCtx 持有）且不 drop → `TxWriterStop::send` 後 join 於 100 ms 內返回（不依賴 Sender 全數 drop） |
| `queued_jobs_after_stop_are_not_submitted_in_ledger` | writer 卡在 `BlockUntilCancel`、佇列另有 2 個 job → stop＋cancel → 兩個 job 回 `NotSubmitted`＋`Frozen`，`dev.write_calls()` 只增加 1；ledger 對三個 op 皆有紀錄 |
| `ledger_records_partial_and_unknown` | op1 `AcceptThenError(3)`、op2 `Unknown` → `ledger.ops[op1].confirmed_bytes==3`、`ledger.ops[op2].unknown==true` |
| `reply_receiver_gone_is_kept_in_ledger` | job 的 reply receiver 先 drop → writer 不 panic，`ledger.ops[op].reply_lost == true`，結果仍在帳本 |
| `tx_wal_failure_reported_and_gap_listed` | `wfs.fail_next_writes(wal, 2, ENOSPC)` → `TxReport.wal == Some(WalAppend{ok:false, loss_flag:true, ..})`、`ledger.ops[op].wal_gaps.len()==1`（bytes 已寫出、WAL 未落盤） |
| `settled_op_wal_gaps_move_to_unresolved_set`（R1-F8） | op 9 一個 job 遇 TX WAL 失敗（同上）後 `settle(OpId(9))` → `wait_gauge(live_ops==0 && unresolved_gaps==1)`；stop 後 `ledger.ops` 不含 op 9、`ledger.unresolved_gaps.len()==1` 且 `unresolved_gaps[0].op_id==Some(OpId(9))`、`len==` 該 job 確認 bytes（證據隨結算移轉，不丟棄） |
| `unresolved_gaps_capped_with_overflow_summary`（R1-F8） | WAL 持續失敗下送 300 個 op（各 1 job、payload 2 bytes）並逐一 settle → `ledger.unresolved_gaps.len()==TX_LEDGER_UNRESOLVED_GAPS_MAX(256)`、保留的是最新 256 筆（`front().op_id==Some(第 45 個 op)`）、`gaps_overflow == GapOverflow{gaps:44, bytes:88, first_op:Some(第 1 個 op), last_op:Some(第 44 個 op)}`；gauge `gaps_overflow==44` |
| `per_op_wal_gaps_capped`（R1-F8） | WAL 持續失敗下同一 op 送 40 個 job（各 1 byte）且不 settle → `ledger.ops[op].wal_gaps.len()==TX_OP_WAL_GAPS_MAX(16)`、`wal_gaps_overflow.gaps==24`、`wal_gaps_overflow.bytes==24`；`confirmed_bytes==40`（計數不受缺口合併影響） |
| `ledger_saturation_never_evicts_live_ops`（R1-F8） | 送 256 個不同 op 各 1 job 且都不 settle → 第 257 個新 op 的 job 回 `NotSubmitted`＋`reject==Some(Frozen)`、`dev.write_calls()` 不增、`ledger` 無 op 257 項目；`r.ctl` 恰一個 `UartIoError{epoch:7, reason:"TX_LEDGER_SATURATED"}`（第 258 個新 op 不再重送）；op 1 再送一個 job 仍 `Completed`（既有 op 不受限）；stop 後 `ledger.ops.len()==256`、`ledger.saturated==true`，op 1～256 的 `confirmed_bytes` 全保留 |
| `settle_unknown_or_already_settled_is_noop`（R1-F8） | `settle(OpId(77))`（無 TX）與對同一已結算 op 重複 settle → `live_ops`、`unresolved_gaps` 不變、不 panic；`settled_total` 只計實際移除的項目 |
| `settle_queue_full_retains_entry`（R1-F8） | op 1 先完成（不 settle）；writer 卡在 op 99 的 `BlockUntilCancel{accept_before_cancel:1}` 時對 `OpId(1000+i)`（i∈0..256，帳本無此項）settle 256 次皆回 `true`，再 `settle(OpId(1))` 回 `false`、gauge `settle_dropped==1` → stop＋cancel 後 `ledger.ops` 仍含 op 1（`confirmed_bytes` 不變）與 op 99（`confirmed_bytes==1`）：佇列滿只造成過度保留，不失證據 |
| `lease_owner_job_after_owner_gen_bump_rejected` | `LeaseOwner` permit `owner_gen=3`，送出前 `fence.with(|s| s.owner_gen = 4)`（lease close／expiry）→ `NotSubmitted`＋`OwnerMismatch`，`write_calls()` 不變 |
| `interactive_open_cmd_after_owner_gen_bump_rejected` | 同上，class `InteractiveOpenCmd` |
| `login_probe_after_fsm_gen_bump_rejected` | `LoginProbe` permit `fsm_gen=1`、fence `fsm_gen=2`（quiet re-arm）→ `FsmMismatch` |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-core --features test-fakes --test tx_writer`
Expected：FAIL，`not implemented: slice21 T9 spawn_tx_writer`。

- [ ] **Step 3：實作**

```rust
fn run(mut self) -> (Box<dyn SerialWriter>, TxLedger) {
    let mut ledger = TxLedger { epoch: self.epoch, ..Default::default() };
    loop {
        let job = crossbeam_channel::select_biased! {
            recv(self.stop) -> _ => break,                                  // 與 Sender 數量無關（OpCtx 持有 clone 也能結束）
            recv(self.settle) -> s => { if let Ok(op) = s { ledger.settle(op); self.publish_gauge(&ledger); } continue }   // R1-F8 結算
            recv(self.jobs) -> j => match j { Ok(j) => j, Err(_) => break },
        };
        // R1-F8 saturation：新 op 且未結算項目已達上限 → 不淘汰既有項目、不呼叫 PAL、不建項目（缺項＝未提交）
        if !ledger.ops.contains_key(&job.permit.op_id) && ledger.ops.len() >= TX_LEDGER_LIVE_MAX {
            if !ledger.saturated { ledger.saturated = true;
                let _ = self.control.send(ControlEvent::UartIoError { epoch: self.epoch, reason: "TX_LEDGER_SATURATED".into() }); }
            let _ = job.reply.send(TxReport { op_id: job.permit.op_id, epoch: self.epoch,
                result: TxResult { outcome: TxOutcome::NotSubmitted, error: None }, wal: None, reject: Some(FenceReject::Frozen) });
            self.publish_gauge(&ledger); continue;
        }
        // 出列快速拒絕（不呼叫 PAL）；通過**不是**授權——授權只發生在 PAL 每次 OS 提交的 FenceSubmitGate 臨界區（R0-F1）
        let pre = self.fence.with(|s| Fence::admit(s, &job.permit));
        let (result, reject) = match pre {
            Err(r) => (TxResult { outcome: TxOutcome::NotSubmitted, error: None }, Some(r)),
            Ok(()) => {
                let gate = FenceSubmitGate { fence: &self.fence, permit: &job.permit, last_reject: Cell::new(None) };
                let res = self.writer.write_gated(&job.payload, &self.cancel, &gate);   // 鎖只在 gate.submit 內持有
                self.fence.with(|s| if s.in_flight.as_ref().map(|f| f.op_id) == Some(job.permit.op_id) { s.in_flight = None });
                (res, gate.last_reject.get())                                     // Completed(k)+cancelled 時亦帶撤銷原因
            }
        };
        let wal = match (&result.outcome, job.permit.log) {
            (TxOutcome::Completed(n), true) if *n > 0 => Some(self.wal.append(WalAppendReq { com: &self.com, dir: "TX",
                source: &job.permit.source, payload: &job.payload[..*n], cmd_id: job.permit.cmd_id.as_deref(), loss_flag: false, meta: None })),
            _ => None,
        };
        let e = ledger.ops.entry(job.permit.op_id).or_default();
        e.record(&result, reject, wal.as_ref(), &job.payload);              // 累計 confirmed_bytes／unknown／last_error／wal_ok；wal_gaps 超過 TX_OP_WAL_GAPS_MAX 合併進 wal_gaps_overflow；not_submitted
        if job.reply.send(TxReport { op_id: job.permit.op_id, epoch: self.epoch, result, wal, reject }).is_err() { e.reply_lost = true; }
        self.publish_gauge(&ledger);
    }
    for op in self.settle.try_iter() { ledger.settle(op); }                   // stop 前已到達的結算先套用（R1-F8；aborted_op 依協定不會在其中）
    for job in self.jobs.try_iter() {                                         // stop 後仍在佇列者：不寫 OS，回 NotSubmitted＋Frozen
        let known = ledger.ops.contains_key(&job.permit.op_id);
        if known || ledger.ops.len() < TX_LEDGER_LIVE_MAX {                   // 滿載時新 op 不建項目（缺項＝未提交）
            ledger.ops.entry(job.permit.op_id).or_default().not_submitted += 1;
        }
        if job.reply.send(TxReport { op_id: job.permit.op_id, epoch: self.epoch,
            result: TxResult { outcome: TxOutcome::NotSubmitted, error: None }, wal: None, reject: Some(FenceReject::Frozen) }).is_err() {
            if let Some(e) = ledger.ops.get_mut(&job.permit.op_id) { e.reply_lost = true; }
        }
    }
    self.publish_gauge(&ledger);
    (self.writer, ledger)
}

impl TxLedger {
    /// R1-F8 結算：移除未結算項目，證據缺口移入有界集合（FIFO，溢位合併最舊者進 gaps_overflow，不丟計數與 bytes）。
    fn settle(&mut self, op: OpId) {
        let Some(e) = self.ops.shift_remove(&op) else { return };            // 未知或已結算：no-op
        self.settled_total += 1;
        self.gaps_overflow.absorb(&e.wal_gaps_overflow);
        for g in e.wal_gaps {
            if self.unresolved_gaps.len() == TX_LEDGER_UNRESOLVED_GAPS_MAX {
                let old = self.unresolved_gaps.pop_front().unwrap(); self.gaps_overflow.absorb_gap(&old);
            }
            self.unresolved_gaps.push_back(g);
        }
    }
}
// GapOverflow::absorb_gap：gaps+=1、bytes+=len、first_op 取最早、last_op 取最新；absorb 合併另一個 GapOverflow。
// publish_gauge：把 ops.len()／unresolved_gaps.len()／gaps_overflow.gaps／settled_total／saturated 以 Release 寫入 LedgerGaugeCells
// （settle_dropped 由 TxWriterHandle::settle 自行遞增）。
```

帳本有界性（R1-F8，arch §5.7／§5.10）：`ops.len() ≤ TX_LEDGER_LIVE_MAX`、`unresolved_gaps.len() ≤ TX_LEDGER_UNRESOLVED_GAPS_MAX`、每個 op 的 `wal_gaps.len() ≤ TX_OP_WAL_GAPS_MAX`，因此一個 epoch 的帳本記憶體與該 epoch 送過多少 op 無關；長跑（48 h `lr-mixed` 不重連）下帳本對 RSS 斜率的貢獻為常數。

`Fence::admit` 依 gate 表的檢查順序：`frozen` → `epoch` → `cancelled` → flash 規則 → class 規則（quiet → reconfirm → wal_unhealthy）→ 世代規則（`owner_gen`：`LeaseOwner`／`InteractiveOpenCmd`／`HumanRaw`／`HumanReplay`；`fsm_gen`：`LoginProbe`／`ReadyReconfirm`；`SelfTestProbe` 不比對，R5-F2）。drain 時 actor 在**同一把鎖**內設 `frozen=true`、`active_epoch=None`（Task 12），形成 arch §5.4 的線性化邊界。`FenceSubmitGate::submit`：`let mut st = fence.0.lock(); if let Err(r) = Fence::admit(&st, permit) { self.last_reject.set(Some(r)); return Err(SubmitRevoked) } let step = os_submit(); if matches!(step, SubmitStep::Accepted(n) if n > 0) || matches!(step, SubmitStep::Pending) { st.in_flight = Some(InFlight { op_id: permit.op_id, len }) } drop(st); Ok(step)`——`os_submit` 必須是非阻塞提交（Task 2 POSIX 單次 `write(2)`、Task 3 Windows `WriteFile` 發出），任何完成等待都不得放進閉包；code review 以 `grep -n "poll_out_or_wake\|GetOverlappedResult\|WaitForMultipleObjects" crates/sw-pal/src/serial/` 確認這些呼叫都不在 `gate.submit(` 閉包內。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-core --features test-fakes --test tx_writer`
Expected：PASS。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-core/src/tx_writer.rs crates/sw-core/tests/tx_writer.rs changelog.d/rust-s21-txwriter.md
git commit -m "feat(sw-core): 單一 TX 寫入者、fence 線性化與確認長度 TX WAL" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 10：sw-core IoWorker reader＋RxWindow＋哨兵 fan-out

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-reader.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust UART reader：RX 先落 WAL（精確 seq，DF-15）再更新絕對偏移 RX 視窗、哨兵 console 與 actor；讀取錯誤以 SERIAL_READ:<errno> 回報，EOF 不視為斷線。」）
**BLOCKED-OWNER：** 無。

**Files:**
- Modify: `crates/sw-core/src/uart_bridge.rs`
- Test: `crates/sw-core/tests/rx_reader.rs`

**Interfaces:**
- Consumes：Task 7 `WalWriter::append`；Task 2 `ConsoleIo`（哨兵）；Task 11 `DataEvent`、`ControlEvent`（Task 0 骨架已有型別）；`sw_proto::pyre::PyRegex`、`sw_proto::pycompat::decode_utf8_replace`。
- Produces：

```rust
pub struct RxShared { /* Mutex<RxWindow> + Condvar */ }
impl RxShared {
    pub fn new(max_chars: usize, created_mono: u64) -> Arc<RxShared>;      // _last_rx_mono = 建立時刻
    pub fn append(&self, data: &[u8], now: Mono, epoch: u64, rx_seq: u64); // 06 §4.1 step 2；同一把鎖內更新 watermark (epoch, rx_seq)
    pub fn watermark(&self) -> (u64 /*epoch*/, u64 /*rx_seq*/);
    pub fn clear(&self);                                                   // trimmed += len(text)；不動 total/window/last_rx
    pub fn snapshot_len(&self) -> u64;                                     // trimmed + chars(text)
    pub fn text_from(&self, from: u64) -> String;                          // rel<=0 回全窗
    pub fn tail(&self, max_chars: usize) -> String;                        // max_chars==0 回全窗（Python text[-0:] quirk）
    pub fn wait_for_regex(&self, re: &PyRegex, timeout: Duration, clock: &dyn Clock) -> Option<RxMatch>;
    pub fn wait_for_regex_from(&self, re: &PyRegex, from: u64, timeout: Duration, clock: &dyn Clock) -> Option<RxMatch>;
    pub fn rx_stats(&self, now: Mono) -> (u64, u64);                       // (rx_bytes_last_10s, rx_rate_bps)
    pub fn total_bytes(&self) -> u64;
    pub fn idle_s(&self, now: Mono) -> f64;
    pub fn last_rx_mono(&self) -> u64;
    pub fn trimmed(&self) -> u64;
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub struct RxMatch { pub epoch: u64, pub rx_seq: u64 /* 命中當下（同一把鎖）的 watermark；OpFinished／LoginFinished 的 after_rx_seq 一律取此值 */ }
pub struct ReaderCtx { pub com: String, pub epoch: u64, pub wal: Arc<WalWriter>, pub rx: Arc<RxShared>,
    pub sentinel: Option<Box<dyn ConsoleIo>>, pub data: crossbeam_channel::Sender<DataEvent>,
    pub control: crossbeam_channel::Sender<ControlEvent>, pub clock: Arc<dyn Clock> }
pub struct ReaderExit { pub reader: Box<dyn SerialReader>, pub sentinel: Option<Box<dyn ConsoleIo>>, pub error: Option<PalError>, pub rx_seq: u64 }
pub fn spawn_reader(reader: Box<dyn SerialReader>, ctx: ReaderCtx, stop: Arc<AtomicBool>) -> std::thread::JoinHandle<ReaderExit>;
```

- [ ] **Step 1：寫失敗測試**

```rust
#![cfg(feature = "test-fakes")]
use std::{sync::{atomic::AtomicBool, Arc}, time::Duration};
use sw_core::{mailbox::*, uart_bridge::*, wal::*};
use sw_pal::{fakes::{clock::FakeClock, serial::*, wal_fs::FakeWalFs}, *};

#[test]
fn rx_is_in_wal_before_actor_sees_it_with_exact_seq() {
    let be = FakeSerialBackend::new(); let dev = be.add_device("/dev/f");
    let h = be.open_exclusive(&DevicePath("/dev/f".into()), &SerialConfig::from_uart_profile(115200, 8, "N", 1, "none", false)).unwrap();
    let (r, _w, _k, _c) = h.split();
    let wfs = FakeWalFs::new(); let clock = FakeClock::at(1, 1);
    let wal = Arc::new(WalWriter::open(WalConfig { dir: "/w".into(), rotate_bytes: 1 << 26 }, wfs.clone(), clock.clone()));
    wal.append(WalAppendReq { com: "COM1", dir: "TX", source: "x", payload: b"z", cmd_id: None, loss_flag: false, meta: None }); // 其他 session 先推進 seq
    let (dtx, drx) = crossbeam_channel::bounded(1024); let (ctx_, _crx) = crossbeam_channel::bounded(256);
    let rx = RxShared::new(131_072, 1);
    let _j = spawn_reader(r, ReaderCtx { com: "COM0".into(), epoch: 3, wal: wal.clone(), rx: rx.clone(), sentinel: None,
                                         data: dtx, control: ctx_, clock: clock.clone() }, Arc::new(AtomicBool::new(false)));
    dev.push_rx(b"login: ");
    match drx.recv_timeout(Duration::from_secs(1)).unwrap() {
        DataEvent::UartRxChunk { epoch, rx_seq, wal_seq, bytes } => {
            assert_eq!((epoch, rx_seq, wal_seq, bytes.as_slice()), (3, 1, 2, &b"login: "[..]));
            let text = String::from_utf8(wfs.contents("/w/raw.wal.ndjson".as_ref()).unwrap()).unwrap();
            assert!(text.lines().nth(1).unwrap().contains("\"source\":\"device\""));
        }
    }
    assert_eq!(rx.snapshot_len(), 7);
}

#[test]
fn rx_window_trims_head_and_keeps_absolute_offsets() {
    let rx = RxShared::new(10, 0);
    rx.append(b"0123456789", 1, 1, 1);                       // (data, now, epoch, rx_seq)
    let off = rx.snapshot_len();
    rx.append(b"abcde", 2, 1, 2);
    assert_eq!((rx.trimmed(), rx.snapshot_len()), (5, 15));
    assert_eq!(rx.text_from(off), "abcde");
    assert_eq!(rx.text_from(0), "56789abcde");                // 頭段已修剪 → 降級回全窗
    rx.clear();
    assert_eq!((rx.trimmed(), rx.snapshot_len(), rx.total_bytes()), (15, 15, 15));
}
```

| 測試名 | 精確斷言 |
|---|---|
| `per_chunk_utf8_decode_replaces_split_multibyte` | `append(b"\xe4\xb8")`、`append(b"\xad")` → `tail(0) == "\u{FFFD}\u{FFFD}"`（逐 chunk 解碼，UIO-12.10-b KEEP；`b"\xe4\xb8"` 為一個 maximal subpart → 一個 U+FFFD） |
| `rx_stats_window_prunes_strictly_older_than_10s` | t=0 收 100 B、t=10.0 s 收 50 B → `rx_stats(10.0s) == (150, 15)`；`rx_stats(10.000001s)` → `(50, 5)` |
| `idle_counts_from_creation` | `RxShared::new(_, created=5s)`、無 RX → `idle_s(8s) == 3.0` |
| `wait_for_regex_zero_timeout_is_false` | 視窗已含 match 但 `timeout=0` → `None`（Python `while now < deadline` 不進迴圈） |
| `wait_for_regex_from_wakes_on_append` | 另一 thread 50 ms 後 `append("# ", epoch 3, rx_seq 7)` → `wait_for_regex_from(r"# $", off, 1s)` 於 < 200 ms 回 `Some(RxMatch{epoch:3, rx_seq:7})`（condvar，非 50 ms 輪詢） |
| `match_watermark_is_atomic_with_text` | 兩個 thread 交錯 append（rx_seq 1..1000）與 wait：每次命中回傳的 `rx_seq` 都 ≥ 含有命中文字的那個 chunk 的 rx_seq（watermark 與文字同鎖更新） |
| `completion_cannot_overtake_its_rx_chunk` | reader 已 `rx.append(rx_seq=5)` 但 data lane 的 Data(5) 尚未被 actor 處理時，OpRunner 回 `OpFinished{after_rx_seq: Some(5)}` → actor 先處理 Data(5) 再處理 completion（與 Task 11 `control_waits_for_dependent_rx` 串接） |
| `rx_rate_window_capped_4096` | 1 ms 內 append 10000 個 1-byte chunk → 內部 rate deque 長度 ≤ 4096（`resource_limits.rx_rate_window_entries`，`merge_oldest`）；`rx_stats(now).0` 不小於未設上限時的值（合併 bucket 保留 bytes 總和並取較新時間戳，只可能延後剔除，flood 下不低估） |
| `rx_handler_panic_skips_chunk_keeps_reader` | 哨兵 `try_write` panic（測試注入）→ stderr WARNING `uart reader RX handler 例外（com=COM0），略過此塊、reader 續行`，下一 chunk 照常送 actor（#79 STA-1） |
| `wait_for_regex_from_recomputes_rel_after_trim` | 等待中發生修剪，仍只在 `from` 之後的文字中搜尋 |
| `read_error_reports_serial_read_errno` | fake `remove_device` → control 收到 `ControlEvent::UartIoError{epoch:3, reason:"SERIAL_READ:5"}`，reader thread 結束並交回 reader（`ReaderExit.error` 為 `Some`） |
| `eof_does_not_report_bridge_down` | reader 回 `Eof` 連續 5 次 → 無 `UartIoError`，期間每次 `Eof` 後等待 200 ms（FakeClock 推進）不空轉 |
| `woken_and_stop_flag_exits` | `stop=true` 後 `waker.wake()` → thread 結束，`ReaderExit.error == None` |
| `sentinel_receives_raw_bytes_and_eagain_drops` | 哨兵 `ConsoleIo::try_write` 回 `Ok(0)` 時 RX 不阻塞、下一 chunk 照常送 actor |
| `data_lane_backpressure_blocks_reader_after_wal` | data channel 容量 1 且不消費：第 2 個 chunk 已寫入 WAL（`current_seq` 前進）但 reader 阻塞於 send（arch §5.2：背壓不丟 RX 證據） |
| `sentinel_input_discarded` | 哨兵 master 有 human 輸入 bytes → reader 讀出後丟棄、不寫 WAL、不送 UART（`dev.take_tx()` 為空） |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-core --features test-fakes --test rx_reader`
Expected：FAIL，`not implemented: slice21 T10 RxShared::new`。

- [ ] **Step 3：實作**

reader 迴圈（每次 `read(buf=8192, timeout=Some(200 ms))`）：

```rust
loop {
    if stop.load(Acquire) { break exit(None); }
    match reader.read(&mut buf, Some(IO_POLL)) {
        Ok(ReadOutcome::Data(n)) => {
            let data = &buf[..n];
            let a = ctx.wal.append(WalAppendReq { com: &ctx.com, dir: "RX", source: "device", payload: data, cmd_id: None, loss_flag: false, meta: None });
            let now = ctx.clock.mono_ns();
            rx_seq += 1;
            ctx.rx.append(data, now, ctx.epoch, rx_seq);                                      // watermark 與文字同鎖
            if let Some(s) = sentinel.as_mut() {
                if std::panic::catch_unwind(AssertUnwindSafe(|| s.try_write(data))).is_err() { warn_rx_handler_panic(&ctx.com); }
            }
            if ctx.data.send(DataEvent::UartRxChunk { epoch: ctx.epoch, rx_seq, wal_seq: a.seq, bytes: data.to_vec() }).is_err() { break exit(None); }
        }
        Ok(ReadOutcome::Eof) => { ctx.clock.sleep(IO_POLL); }                       // UIO-12.10-d：不視為斷線
        Ok(ReadOutcome::Woken) | Ok(ReadOutcome::TimedOut) => { drain_sentinel_input(&mut sentinel); }
        Err(e) => { let _ = ctx.control.send(ControlEvent::UartIoError { epoch: ctx.epoch, reason: format!("SERIAL_READ:{}", e.os_code.map(|c| c.to_string()).unwrap_or_else(|| e.py_exc_name().to_string())) }); break exit(Some(e)); }
    }
}
```

`RxShared::append`：鎖內 `total_bytes += n`、`last_rx_mono = now`、`watermark = (epoch, rx_seq)`、window push `(now, n)` 後剔除 `now - t > 10 s`，長度超過 4096 時把最舊兩個 entry 合併（保留 bytes 總和與較新的時間戳），`text.push_str(&decode_utf8_replace(data))`，`chars(text) > max` 時截頭並 `trimmed += overflow`（以 code point 計），`cond.notify_all()`。wait 系列：`deadline = clock.mono_ns() + timeout`；`while clock.mono_ns() < deadline { 檢查; cond.wait_timeout(剩餘) }`（FakeClock 的 advance 也 notify 同一 condvar：RxShared 註冊為 FakeClock waiter，透過 `Clock::sleep` 等價機制；實作上以 `cond.wait_timeout(min(剩餘, 50 ms))` 搭配重算 deadline，確保 FakeClock 下可前進）。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-core --features test-fakes --test rx_reader`
Expected：PASS（18 項）。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-core/src/uart_bridge.rs crates/sw-core/tests/rx_reader.rs changelog.d/rust-s21-reader.md
git commit -m "feat(sw-core): UART reader 先落 WAL 再分送與絕對偏移 RX 視窗" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 11：sw-core SessionCore／Mailbox／Generations／Actor（§5.3 的 2.1 列）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-session-actor.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust SessionActor：sans-IO 狀態機、control 優先且不越過依賴 RX 的有界 mailbox、conn_epoch／fsm_gen／owner_gen 世代隔離，RECOVERING／FLASHING 下的提交改回 SESSION_RECOVERING／FLASHING_BUSY（SC-01、SC-02）；passthrough 不登入、ATTACHED 連續 30 s 無 RX 由 tick 轉 READY，未滿時 submit 回 SESSION_NOT_READY＋passthrough_ready_in_s（SC-35）。」）
**BLOCKED-OWNER：** 無（延遲上限的外部量測在 Task 21）。

**Files:**
- Modify: `crates/sw-core/src/{session_core.rs,mailbox.rs,generations.rs,actor.rs,resources.rs}`
- Create: `crates/sw-core/tests/support/mod.rs`（Step 0 建立 `CoreHarness`；Tasks 12～16 共用，之後的 Task 只追加）
- Test: `crates/sw-core/tests/session_core.rs`、`crates/sw-core/tests/mailbox.rs`

**Interfaces:**
- Consumes：Tasks 7～10。
- Produces：

```rust
// generations.rs
pub struct EpochAllocator(AtomicU64);                  // 全域單調；open_exclusive 成功才 next()
impl EpochAllocator { pub fn new() -> Arc<EpochAllocator>; pub fn next(&self) -> u64; }
#[derive(Clone, Debug, Default)] pub struct Generations { pub conn_epoch: Option<u64>, pub fsm_gen: u64, pub owner_gen: u64, pub bridge_generation: u64 }

// mailbox.rs
pub type Reply = crossbeam_channel::Sender<Result<PyDict, PyErr>>;
pub enum DataEvent { UartRxChunk { epoch: u64, rx_seq: u64, wal_seq: u64, bytes: Vec<u8> } }
pub enum ControlEvent {
    Request { req: SessionRequest, reply: Reply },
    DeviceAttached { by_id: String, real_path: String },
    DeviceDetached { by_id: String, reason: DetachReason },
    BridgeOpened { epoch_hint: u64, result: Result<OpenedBridge, OpenFailure> },
    UartIoError { epoch: u64, reason: String },
    UartTxCompleted { epoch: u64, op_id: OpId, confirmed_bytes: usize, unknown: bool },   // arch §5.4 step 3；TxWriter 每個 job 後送出（內部事件，阻塞送）；actor 用於 last_tx_*，BC-03 映射仍以 receipt 帳本為準
    WorkerStopped(WorkerStoppedReceipt),
    WorkerDrainTimeout { epoch: u64, stop_id: StopId },
    OpFinished { op_id: OpId, epoch: u64, outcome: OpOutcome, after_rx_seq: Option<u64> },
    LoginFinished { epoch: u64, fsm_gen: u64, result: LoginOutcome, after_rx_seq: Option<u64> },
    Timer(TimerEvent),
    StateCommitted { commit_id: u64, result: Result<(), PalError> },
    Tick,
    Shutdown { reply: Reply },
}
pub enum TimerEvent { BootQuietExpired { epoch: u64, fsm_gen: u64 }, LeaseExpiry { lease_id: String, deadline: Mono }, DrainTimeout { epoch: u64, stop_id: StopId } }
pub enum DetachReason { Removed, Rebound }                                  // → DEVICE_REMOVED／DEVICE_REBOUND_REQUIRED
pub struct Mailbox { /* control: bounded(256), data: bounded(1024), stash: ArrayVec<ControlEvent, 16>（mailbox_stash_max） */ }
impl Mailbox {
    pub fn new(control_cap: usize, data_cap: usize) -> Mailbox;
    pub fn control_sender(&self) -> crossbeam_channel::Sender<ControlEvent>;
    pub fn data_sender(&self) -> crossbeam_channel::Sender<DataEvent>;
    /// deadline 以 Mono 表示；實作以 recv_timeout(min(deadline - clock.mono_ns(), 50 ms)) 迴圈並在每次醒來重讀 clock，
    /// 另 select FakeClock::subscribe（test-fakes）使 advance 立即喚醒。current_epoch 用於丟棄舊 epoch 依賴。
    pub fn recv_next(&mut self, processed: (u64 /*epoch*/, u64 /*rx_seq*/), current_epoch: Option<u64>, deadline: Option<Mono>, clock: &dyn Clock) -> Option<MailboxItem>;
}
pub enum MailboxItem { Control(ControlEvent), Data(DataEvent) }

// session_core.rs
#[derive(Clone, Copy, Debug, PartialEq, Eq)] pub enum SessionState { Detached, Attaching, Attached, Ready, Recovering, Released, Flashing }
impl SessionState { pub fn as_str(&self) -> &'static str; }
pub enum SessionRequest {
    GetState, Activity, Submit(SubmitReq),
    Release { source: String, reason: PyValue }, DeviceAttach { force: bool }, SessionAttach,
    Recover { timeout_s: f64, force: bool }, Clear, Bind { by_id: String },
    InteractiveOpen { owner: String, timeout_s: f64, command: String, allow_attached: bool },
    InteractiveSend { interactive_id: String, data: String, encoding: String },
    InteractiveStatus { interactive_id: String, screen_chars: i128 }, InteractiveClose { interactive_id: String },
    SelfTest { timeout_s: f64, strict_human_lock: bool },
    LogStart, LogStop, LogStatus, ResultTail { cmd_id: String, from_chunk: i128, limit: i128 },
    CancelQueued { cmd_id: String },              // command.cancel 成功後 best-effort 通知，actor 自 queue remove（Task 8）
}
pub enum Effect {
    Reply(Reply, Result<PyDict, PyErr>),
    OpenBridge { dev: DevicePath, cfg: SerialConfig, precheck_holders: bool },
    BeginDrain { stop_id: StopId, epoch: u64 },
    StartLogin { epoch: u64, fsm_gen: u64, plan: LoginPlan },
    StartOp { grant: OpGrant, spec: OpSpec },
    Schedule { at: Mono, ev: TimerEvent },
    WalMeta { cmd_id: Option<String>, meta: PyDict },
    Persist { commit_id: u64, delta: StateDelta },
    Notify(ManagerNote),
    Warn(String),                                       // daemon stderr WARNING（GAP-G-01 模板）
    SettleTx { epoch: u64, op_id: OpId },               // R1-F8：op 終態處理點產生；executor 只對現行 worker epoch 呼叫 tx().settle（非阻塞），其他 epoch 丟棄
}
pub struct Effects(pub Vec<Effect>);
pub struct OpGrant { pub op_id: OpId, pub epoch: u64, pub owner_gen: u64, pub fsm_gen: u64, pub cmd_id: Option<String> }
pub struct CoreDeps { pub fence: Arc<Fence>, pub registry: Arc<CommandRegistry>, pub wal: Arc<WalWriter>, pub rx: Arc<RxShared>,
    pub epochs: Arc<EpochAllocator>, pub wall_iso: Arc<dyn Fn() -> String + Send + Sync>, pub rng: Arc<dyn Fn(&mut [u8]) + Send + Sync>,
    pub env: Arc<dyn SysEnv>, pub read_env_file: Arc<dyn Fn(&str) -> Option<std::io::Result<String>> + Send + Sync> }
    // P5 Task 0 雙審修正（補記，07 計畫副本同步）：env／read_env_file 供 Task 13 `resolve_session_auth(p, env, read)`（本計畫 Task 13 Interfaces）於 attach／recover 時讀取；`read_env_file` 回 None＝檔案不存在。
pub struct PublicCtx { pub console_count: i128, pub vtty: Option<String>, pub console_endpoint: Option<Option<String>> }
pub struct SessionCore { /* 欄位見本 Task Step 3 第一點（05 §1.6 SessionRuntime 對應＋queue／foreground／stop／quiet／leases／queue_registered） */ }
impl SessionCore {
    pub fn new(profile: SessionProfile, profile_source: &str, deps: CoreDeps) -> SessionCore;
    pub fn on_event(&mut self, ev: MailboxItem, now: Mono) -> Effects;
    pub fn public(&self, now: Mono, ctx: &PublicCtx) -> PyDict;          // 02 §6.1 順序＋SC-04 兩欄
    pub fn state(&self) -> SessionState;
    pub fn session_id(&self) -> &str;
    pub fn generations(&self) -> &Generations;
}

// actor.rs
pub struct ActorHandle { pub control: crossbeam_channel::Sender<ControlEvent>, pub data: crossbeam_channel::Sender<DataEvent>, pub join: std::thread::JoinHandle<()> }
impl ActorHandle { pub fn request(&self, req: SessionRequest, timeout: Duration) -> Result<PyDict, PyErr>; }
pub trait EffectExecutor: Send { fn execute(&mut self, e: Effect, tx: &crossbeam_channel::Sender<ControlEvent>); }
pub fn spawn_actor(core: SessionCore, mailbox: Mailbox, exec: Box<dyn EffectExecutor>, clock: Arc<dyn Clock>) -> ActorHandle;   // thread "serialwrap-session-<COM>"，1 s Tick
```

`CoreHarness`（`crates/sw-core/tests/support/mod.rs`）：

```rust
pub struct CoreHarness { pub core: SessionCore, pub clock: Arc<FakeClock>, pub fence: Arc<Fence>, pub reg: Arc<CommandRegistry>,
                         pub wal: Arc<WalWriter>, pub wfs: Arc<FakeWalFs>, pub rx: Arc<RxShared>, pub effects: Vec<Effect> }
impl CoreHarness {
    pub fn new(profile_yaml: &str) -> CoreHarness;                     // 解析單一 target 的 profiles YAML
    pub fn to_ready(&mut self) -> &mut Self;                          // DeviceAttached → BridgeOpened(ok) → LoginFinished(Ok) → READY
    pub fn to_attached(&mut self, last_error: Option<&str>) -> &mut Self;
    pub fn rpc(&mut self, req: SessionRequest) -> Result<PyDict, PyErr>; // 回傳 Effect::Reply 的內容（未立即回覆時 panic）
    pub fn rpc_deferred(&mut self, req: SessionRequest) -> crossbeam_channel::Receiver<Result<PyDict, PyErr>>;
    pub fn feed(&mut self, ev: ControlEvent) -> Vec<Effect>;
    pub fn rx(&mut self, bytes: &[u8]) -> Vec<Effect>;                // 同 epoch 的 UartRxChunk，rx_seq 自動遞增
    pub fn advance(&mut self, d: Duration) -> Vec<Effect>;            // FakeClock 推進並投遞到期 timer 與 Tick
    pub fn public(&self) -> PyDict;
    pub fn force_state(&mut self, s: SessionState);                  // 只供碼映射測試（例如 FLASHING）
}
pub fn agent_submit(cmd: &str) -> SubmitReq;                         // source "agent"、mode "line"、timeout 10.0、priority 10
pub const SHELL_PROFILE: &str = "profiles:\n  t:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: t, act_no: 1, alias: t+1, device_by_id: /s/by-id/usb-A}\n";
pub const PASSTHROUGH_PROFILE: &str = "profiles:\n  p:\n    platform: passthrough\n    ready_probe: ''\ntargets:\n  - {com: COM0, profile: p, act_no: 1, alias: p+1, device_by_id: /s/by-id/usb-A}\n";
// SC-35：others-template 等價（prompt_regex '.*' 不可用 → 完成規則 3）；uboot 類（ready_probe 非空、prompt 可用 → 規則 2，且仍不 probe）；
// 非 passthrough 的空 ready_probe（維持 PROFILE_NOT_COMMAND_CAPABLE）
pub const OTHERS_PASSTHROUGH_PROFILE: &str = "profiles:\n  o:\n    platform: passthrough\n    prompt_regex: '.*'\n    login_regex: '$^'\n    password_regex: '$^'\n    ready_probe: ''\ntargets:\n  - {com: COM0, profile: o, act_no: 1, alias: o+1, device_by_id: /s/by-id/usb-A}\n";
pub const UBOOT_PASSTHROUGH_PROFILE: &str = "profiles:\n  u:\n    platform: passthrough\n    prompt_regex: '(?mi)^(=>|u-boot>|cfe>) $'\n    login_regex: '$^'\n    password_regex: '$^'\n    ready_probe: 'echo __READY__${nonce}'\ntargets:\n  - {com: COM0, profile: u, act_no: 1, alias: u+1, device_by_id: /s/by-id/usb-A}\n";
pub const SHELL_NO_PROBE_PROFILE: &str = "profiles:\n  n:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    ready_probe: ''\ntargets:\n  - {com: COM0, profile: n, act_no: 1, alias: n+1, device_by_id: /s/by-id/usb-A}\n";
pub const CRED_PROFILE: &str = "profiles:\n  c:\n    platform: shell\n    prompt_regex: '(?m)^root@t:.*# '\n    login_regex: 'login: ?$'\n    password_regex: 'Password: ?$'\n    user_env: SW_T_U\n    pass_env: SW_T_P\n    env_file: /nonexistent/s21.env\n    timeout_s: 2.0\ntargets:\n  - {com: COM0, profile: c, act_no: 1, alias: c+1, device_by_id: /s/by-id/usb-A}\n";
// 上述 YAML 的鍵名照 P3 load_profiles 接受的格式；Step 0 以 load_profiles 實際載入並斷言 targets.len()==1 再交給測試
```

- [ ] **Step 0：測試支援**（Test author model 撰寫，與 Step 1 同一 commit）：建立 `crates/sw-core/tests/support/mod.rs` 的 `CoreHarness`、`agent_submit`、六個 profile 常數（含 SC-35 的 `OTHERS_PASSTHROUGH_PROFILE`、`UBOOT_PASSTHROUGH_PROFILE` 與 `SHELL_NO_PROBE_PROFILE`）；`CoreHarness::new` 以 P3 `load_profiles` 載入 YAML（不得手刻 `SessionProfile`），`to_ready` 只以注入 `BridgeOpened`／`LoginFinished(Ready)` 達成（不依賴 Task 13 login）。

- [ ] **Step 1：寫失敗測試**

```rust
#![cfg(feature = "test-fakes")]
mod support;
use std::time::Duration;
use support::*;
use sw_core::{arbiter::SubmitReq, session_core::*};
use sw_proto::pyjson::PyValue;

fn submit(cmd: &str) -> SessionRequest {
    SessionRequest::Submit(SubmitReq { session_id: String::new(), command: cmd.into(), source: "agent".into(), mode: "line".into(),
        timeout_s: 10.0, priority: 10, expected_duration_s: None, completion_regex: None })
}
fn code(r: &Result<sw_proto::pyjson::PyDict, sw_proto::pycompat::PyErr>) -> String {
    r.as_ref().unwrap().get("error_code").and_then(|v| v.as_str()).unwrap_or("").to_string()
}

#[test]
fn recovering_submit_is_session_recovering_sc01() {
    let mut h = CoreHarness::new(SHELL_PROFILE); h.to_ready();
    h.feed(sw_core::mailbox::ControlEvent::UartIoError { epoch: h.core.generations().conn_epoch.unwrap(), reason: "SERIAL_READ:5".into() });
    assert_eq!(h.core.state(), SessionState::Recovering);
    assert_eq!(code(&h.rpc(submit("ls"))), "SESSION_RECOVERING");
}

#[test]
fn stale_epoch_io_error_is_ignored() {
    let mut h = CoreHarness::new(SHELL_PROFILE); h.to_ready();
    let old = h.core.generations().conn_epoch.unwrap() - 1;
    assert!(h.feed(sw_core::mailbox::ControlEvent::UartIoError { epoch: old, reason: "SERIAL_READ:5".into() }).is_empty());
    assert_eq!(h.core.state(), SessionState::Ready);
}

#[test]
fn wall_clock_jump_does_not_move_deadlines() {            // Review Focus 4
    let mut h = CoreHarness::new(SHELL_PROFILE); h.to_ready();
    h.rx(b"U-Boot 2024.01\r\n");
    let q0 = h.public().get("boot_quiet_remaining_s").cloned();
    h.clock.set_wall_us(0);                                  // wall 倒退到 1970
    assert_eq!(h.public().get("boot_quiet_remaining_s").cloned(), q0);
    h.advance(Duration::from_secs(181));
    assert_eq!(h.public().get("boot_quiet_remaining_s"), Some(&PyValue::Null));
}
```

§5.3（2.1 適用列）狀態表，每列一個 `#[test]`（檔案 `session_core.rs`）：

| 測試名 | 起始 → 事件 | 精確斷言 |
|---|---|---|
| `detached_device_attached_opens_and_attaching` | DETACHED＋`DeviceAttached` | 產生 `Effect::OpenBridge{precheck_holders:false}`；state `ATTACHING`、`last_error:null` |
| `detached_submit_not_ready` | DETACHED＋Submit | `{"ok":false,"error_code":"SESSION_NOT_READY","session":{…}}`（02 §5.4 形狀） |
| `attaching_submit_not_ready` | ATTACHING＋Submit | 同上 |
| `bridge_opened_allocates_epoch_once` | ATTACHING＋`BridgeOpened(ok)` | `conn_epoch` 由 `None` 變 `Some(e)`，`bridge_generation` 尚不變（於 bridge 被採用時才 +1） |
| `login_ok_ready_and_bridge_generation_plus_one` | ATTACHING＋`LoginFinished(Ready)` 同 epoch/fsm_gen | state `READY`、`bridge_generation==1`、`last_ready_at` 非 null、`Notify(SessionReady)` |
| `login_not_ready_attached_also_bumps_bridge_generation` | ATTACHING＋`LoginFinished(NotReady("PROMPT_UNAVAILABLE"))` | state `ATTACHED`、`bridge_generation==1`（Python `_attach_by_id` L2708／`_attach_by_id_dynamic` L3132：bridge 採用即遞增，READY／ATTACHED 皆然） |
| `passthrough_and_unresolved_bump_on_adoption` | passthrough（`LoginPlan::None`）與 `CredentialsUnresolved` 進 `ATTACHED` | 兩者 `bridge_generation==1` |
| `login_failed_detach_does_not_bump` | `LoginFinished(Failed("LOGIN_PROMPT_TIMEOUT"))` → drain → `DETACHED` | `bridge_generation==0` |
| `login_finished_stale_fsm_gen_ignored` | `fsm_gen` 不符 | 無狀態變更 |
| `attached_non_capable_submit_profile_not_command_capable` | **非 passthrough**（`SHELL_NO_PROBE_PROFILE`：`platform: shell`、`ready_probe: ''`）ATTACHED＋Submit | `error_code:"PROFILE_NOT_COMMAND_CAPABLE"`＋`hint`＋`session`（非 passthrough 的空 ready_probe profile 維持基線） |
| `attached_passthrough_submit_not_ready_with_ready_in`（SC-35） | `PASSTHROUGH_PROFILE` 進 ATTACHED 後 `advance(12 s)`（期間無 RX）＋Submit | 回應 key 依序 `["ok","error_code","passthrough_ready_in_s","session"]`；`ok:false`、`error_code:"SESSION_NOT_READY"`、`passthrough_ready_in_s == 18.0`（`py_round(30.0 - 12.0, 1)`）；Effects 不含 `StartOp`；**不得**為 `PROFILE_NOT_COMMAND_CAPABLE` |
| `passthrough_attach_no_login_no_probe`（SC-35） | `PASSTHROUGH_PROFILE` 與 `UBOOT_PASSTHROUGH_PROFILE`（ready_probe 非空）各自 `DeviceAttached`→`BridgeOpened(ok)` | 兩者皆直接 `ATTACHED`、`last_error:null`；Effects 不含 `StartLogin`、無任何 `Tx` 類效果（零 TX）；`bridge_generation==1` |
| `passthrough_quiet_tick_ready_at_30s`（SC-35） | ATTACHED 後只推進時間（無 RX）：`advance(29.9 s)` 再 `advance(0.1 s)`＋下一個 1 s Tick | 29.9 s 時 state 仍 `ATTACHED`；達 30 s 後第一個 Tick state 轉 `READY`、`last_error:null`、`Notify(SessionReady)`、`queue_registered==true`；整段 Effects 無 `Tx`／`StartLogin`（零 TX） |
| `passthrough_rx_restarts_quiet_window`（SC-35） | ATTACHED t=0；t=20 s 注入 `rx(b"log\r\n")`；推進至 t=49.9 s 與 t=50 s | t=49.9 s 仍 `ATTACHED`；t ≥ 50 s 的 Tick 才轉 `READY`（`quiet_since = max(進入 ATTACHED, 最後 RX)`）；TX（例如人類 raw bytes）不重算視窗 |
| `passthrough_quiet_blocked_by_drain_flash_released`（SC-35；參數化） | ATTACHED 安靜 30 s 前分別：(a) `DeviceRelease` 進 drain（pending stop）、(b) `force_state(Flashing)`、(c) release 完成為 `RELEASED` | 推進 60 s 後 state 分別仍為 `RECOVERING`／`FLASHING`／`RELEASED`，皆不轉 `READY`、無 `Notify(SessionReady)` |
| `passthrough_ready_not_demoted_by_rx_or_banner`（SC-35） | passthrough 已 READY；注入 `rx(b"U-Boot 2023.01\r\nHit any key to stop autoboot:  3\r\n")` | state 仍 `READY`；`boot_quiet_remaining_s` 為 null、`ready_reconfirm_pending:false`（passthrough 不 arm quiet／reconfirm）；之後 Submit 回 `{"ok":true,…,"status":"accepted"}`（不回 `AUTOBOOT_QUIET`） |
| `passthrough_io_error_recovers_and_requiets`（SC-35） | passthrough READY＋`UartIoError` → drain 回條 → `DeviceAttached`→`BridgeOpened(ok)` | 依序 `RECOVERING`→`ATTACHING`→`ATTACHED`（無 probe、零 TX）；再安靜 30 s 才回 `READY` |
| `attached_capable_submit_not_ready` | shell ATTACHED＋Submit | `SESSION_NOT_READY` |
| `ready_submit_admitted` | READY＋Submit | `{"ok":true,"cmd_id":…,"status":"accepted","session_id":"t:COM0"}`，並產生 `StartOp`（前景空閒時） |
| `queue_full_at_256` | READY、前景忙、已 256 筆 | 第 257 筆 `SESSION_QUEUE_FULL` |
| `released_submit_not_ready` | RELEASED＋Submit | `SESSION_NOT_READY` |
| `released_device_attached_is_noop` | RELEASED＋`DeviceAttached` | 無 `OpenBridge`；state 仍 `RELEASED` |
| `flashing_submit_flashing_busy_sc02` | 以 `CoreHarness::force_state(Flashing)` 設定＋Submit | `FLASHING_BUSY`（2.1 不會自然進入 FLASHING，此列驗證碼映射） |
| `recovering_interactive_open_session_recovering` | RECOVERING＋InteractiveOpen | `SESSION_RECOVERING`（SC-01） |
| `device_detached_starts_drain_removed` | READY＋`DeviceDetached(Removed)` | `BeginDrain` 效果、state `RECOVERING`、fence `frozen==true && active_epoch==None` |
| `stale_timer_ignored` | `Timer(BootQuietExpired{fsm_gen: 舊})` | 無變更、無 TX 效果 |
| `stale_rx_chunk_no_state_effect` | 舊 epoch 的 `UartRxChunk` 含 `U-Boot` | quiet 不啟動、`last_rx_at` 不變（WAL 已由 reader 寫入） |
| `public_dict_key_order_with_sc04_fields` | READY 時 `public()` | key 依 02 §6.1 順序，`capture` 之後依序為 `drain_stalled`（false）、`drain_timeout_epoch`（null）；POSIX ctx 不含 `console_endpoint` |
| `public_console_endpoint_null_when_ctx_requests` | `PublicCtx{console_endpoint: Some(None)}` | 末尾出現 `"console_endpoint":null`（Windows 2.1 範圍清單） |
| `activity_classification_thresholds` | `max(last_rx, last_tx)` 距今 4.9 s／5.0 s／59.9 s／60 s；只有 TX 沒有 RX（以 TX 時間計）；RX 與 TX 都從未發生；非 READY/ATTACHED | `active`／`idle-healthy`／`idle-healthy`／`quiet-suspicious`／依 TX 時間分類／`newly-attached`／`offline`（`SessionRuntime.classify_activity`：idle 取 `max(last_rx_mono, last_tx_mono)`，兩者皆 0 才 `newly-attached`） |
| `last_error_change_clears_detail` | 設 detail 後改 last_error | `last_error_detail` 變 null |
| `tick_expires_reconfirm_without_tx` | READY＋pending，advance 301 s | `ready_reconfirm_failed:true`、`ready_reconfirm_pending:false`，Effects 不含 `StartOp`／`StartLogin` |
| `wall_clock_jump_param`（Review Focus 4；參數化 wall `-3600 s`／`+3600 s`） | 分別建立 quiet、reconfirm、lease（20 s）、drain（3000 ms）、命令逾時（10 s）五種期限後跳 wall | 每種期限的剩餘值不變；只推進 mono 到期限前 1 ms 不觸發、到期限後觸發；`*_at` 字串反映新 wall |
| `actor_event_panic_isolated` | 注入會 panic 的 `Request` 處理 | 該請求收 `Err(PyErr("internal error: <panic msg>"))`（framing 轉 `EXCEPTION`）、stderr WARNING `session actor 事件處理例外（com=COM0），略過此事件`、actor 續行；後續 `GetState` 正常 |

`mailbox.rs` 測試：

| 測試名 | 精確斷言 |
|---|---|
| `control_first_when_independent` | data 已排 1024 筆後送 `Request(GetState)` → `recv_next` 第一個回 `Control` |
| `control_waits_for_dependent_rx` | `LoginFinished{after_rx_seq: Some(3)}`、data rx_seq 1..5 已排、`processed=(e,0)` → 依序回 Data(1)、Data(2)、Data(3)、Control、Data(4) |
| `independent_control_served_while_dependent_stashed` | stash 有 `LoginFinished{after_rx_seq: Some(9)}`、data 只到 rx_seq 3、control lane 再來 `Request(Release)` → `recv_next` 先回 Data(1..3) 之一後立即回 `Release`（不等 rx_seq 9） |
| `stale_epoch_dependency_released_immediately` | stash 項目 `epoch` 已非 `current_epoch` → 立即回該 control（舊 epoch 的 data 不再等待） |
| `dependency_released_on_worker_stopped` | 依賴的 rx_seq 永遠不會到（reader 已結束）時收到 `WorkerStopped` → stash 全部依序釋放在 receipt 之前 |
| `stash_bounded_16` | 第 17 個需 stash 的 control → 送端阻塞（`mailbox_stash_max: block_sender`），不丟棄 |
| `fake_clock_advance_wakes_timer_promptly` | `spawn_actor`＋FakeClock：排 `DrainTimeout` 於 +3000 ms；`advance(3001 ms)` 後 200 ms（實時）內處理（不等下一個 1 s Tick） |
| `control_lane_bounded_256` | 第 257 個 `try_send` 回 `Full` |
| `data_lane_bounded_1024` | 第 1025 個 `try_send` 回 `Full` |
| `request_timeout_returns_internal_timeout` | executor 永不回覆的請求 → `request(_, 100ms)` 回 `Err(PyErr)`，message `"internal timeout"` |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-core --features test-fakes --test session_core --test mailbox`
Expected：FAIL，`not implemented: slice21 T11 SessionCore::new`。

- [ ] **Step 3：實作**

- `SessionCore` 欄位對應 05 §1.6 `SessionRuntime`（除 bridge 物件改為 `conn_epoch`），外加 `queue: SessionQueue`、`foreground: Option<OpGrant>`、`stop: Option<StopOp>`（Task 12）、`quiet: QuietState`（Task 13）、`leases`（Task 14）、`queue_registered: bool`（READY 時設 true；只在 detach／drain 收尾時清除，對應 Python `on_ready`／`on_detached`）。
- Submit 處理順序（submit-time，05 §5.1，加 SC-01／SC-02／DF-02）：
  1. state `RECOVERING` → `SESSION_RECOVERING`＋`session`；`FLASHING` → `FLASHING_BUSY`＋`session`；
  2. state ≠ READY：ATTACHED 且 `platform == "passthrough"` → `{"ok":false,"error_code":"SESSION_NOT_READY","passthrough_ready_in_s":py_round(max(0, PASSTHROUGH_READY_QUIET_S - (now - quiet_since)), 1),"session":…}`（SC-35）；ATTACHED 且 `!command_capable`（非 passthrough）→ `PROFILE_NOT_COMMAND_CAPABLE`；其餘 `SESSION_NOT_READY`；
  3. gate（**不看 `human:` 前綴**，DF-02；`platform == "passthrough"` 整段略過——passthrough 不 arm quiet、不跑 reconfirm，SC-35）：`ready_reconfirm_failed` → `READY_UNCONFIRMED`；`boot_quiet_remaining_s` 非 null → `AUTOBOOT_QUIET`＋`retry_after_s=<remaining>`；pending → `AUTOBOOT_QUIET`＋`retry_after_s=5.0`（hint 字串逐字照 02 §5.4）；
  4. `registry.admit(req, queue_registered, uuid4_hex, wall_iso())`；成功 push 進 `queue`，前景空閒則出列（見 Task 14）。
- Passthrough 安靜升 READY（SC-35，arch §5.12）：`SessionCore` 記 `quiet_since: Option<MonoNs>`；進入 `ATTACHED`（`platform == "passthrough"` 的 `BridgeOpened(ok)` 直接採用 bridge、`LoginPlan::None`、不產生 `StartLogin`／probe TX）時設為 `now`，同 epoch 的每個 `UartRxChunk` 設為 `now`（TX 不更新）。`ControlEvent::Tick` 處理：state `ATTACHED`、`conn_epoch` 有值、`stop.is_none()`、非 RELEASED、非 flash 臨界（Probe／FLASHING）且 `now - quiet_since >= PASSTHROUGH_READY_QUIET_S`（`sw_proto::constants::PASSTHROUGH_READY_QUIET_S = 30.0`）→ 轉 `READY`（`last_error=None`、`queue_registered=true`、`Notify(SessionReady)`，零 TX）。passthrough 的 RX 不觸發 boot banner quiet 與 READY reconfirm；離開 READY 只經 drain 路徑，回條後依一般恢復回 `ATTACHED` 並重新累計。不新增錯誤碼、不新增 WAL META。
- `public()`：依 02 §6.1 逐欄產生；`boot_quiet_remaining_s` 等用 `py_round(x, 1)`；`next_reprobe_at` 輸出 monotonic 秒原值（2.1 恆 null，因無 reprobe）；`console_count`／`vtty` 由 `PublicCtx` 提供（哨兵存在時 1 與 `/dev/pts/N`）。
- Mailbox：`recv_next` 先看 stash（`epoch != current_epoch` 或 `(epoch, after_rx_seq) <= processed` 者依序可出）→ `select_biased!{ recv(control) → 帶 after_rx_seq 且尚未滿足者入 stash（滿 16 則阻塞送端），其餘 control 立即回傳；recv(data) }`；stash 非空時仍持續 select 兩條 lane，獨立 control（release／cancel／stop／lease expiry／receipt）不受依賴阻擋。`WorkerStopped` 到達時先釋放 stash 中同 epoch 的全部項目再回 receipt。
- actor 迴圈：`recv_next(deadline=下一個 timer 或 1 s Tick（Mono）)` → `catch_unwind(core.on_event(item, clock.mono_ns()))`（panic → 對該事件的 reply 回 `Err(PyErr("internal error: …"))`、WARNING、續行；同一事件型別連續 panic 3 次 → 以 `StopKind::IoError` 進 drain（`last_error` 沿用既有 IoError 規則，不新增任何 `error_code`／`last_error` 值））→ 逐一 `exec.execute(effect)`；`Effect::Reply` 以 `try_send` 送出（reply channel 容量 1，接收端已離開時丟棄）。actor thread 本身結束（不應發生）時 `ActorHandle::request` 回 `Err(PyErr("session actor stopped"))`。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-core --features test-fakes --test session_core --test mailbox`
Expected：PASS。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-core changelog.d/rust-s21-session-actor.md
git commit -m "feat(sw-core): SessionActor 狀態機、世代隔離與有界 mailbox" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 12：sw-core Two-Phase Drain 核心＋IoWorker 收尾＋Release／Attach（sans-IO 與 IoWorker 層）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-drain-release.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust 兩階段 drain：停止授權、取消在途 I/O、收到 handle 關閉回條才完成 release；3000 ms 未收斂標 drain_stalled，device.release 回 DRAIN_STALLED（OD-14 #4），drain 中的 attach／clear／bind／recover 仍回 SESSION_RECOVERING；部分寫出中止回 COMMAND_ABORTED_IN_FLIGHT 並寫 command_aborted_in_flight META；device.attach 同步重新取得獨占；動態 session 的 RELEASED 跨多次重啟保留（SC-04、SC-05、BC-03、DF-04）。」）
**BLOCKED-OWNER：** (1) 雙平台實機「外部燒錄工具接手→`device attach` 收回」交接證據（L3 P1 裝置交接，需 bench）；(2) Windows 在途 overlapped write 被 drain 取消的 3000 ms 收斂實測（需 COM pair，見 Task 3）。

本 Task 只含**不需要登入與命令執行**即可驗證的部分（`CoreHarness` 注入 receipt／timer／`BridgeOpened`，以及 `IoWorker` 對 FakeSerial 的收尾）；需要真 actor＋login＋命令執行的端到端案例移到 Task 12b（排在 Task 14 之後）。

**Files:**
- Modify: `crates/sw-core/src/drain.rs`、`crates/sw-core/src/session_core.rs`、`crates/sw-core/src/uart_bridge.rs`（`IoWorker`、finisher）、`crates/sw-core/src/actor.rs`（executor 的 `OpenBridge`／`BeginDrain`）
- Modify: `crates/sw-core/tests/support/mod.rs`（Step 0：`FakeStateStore`）
- Test: `crates/sw-core/tests/drain_release.rs`、`crates/sw-core/tests/io_worker.rs`

**Interfaces:**
- Consumes：Tasks 7～11；Task 15 `StateDelta`（Task 0 骨架型別；本 Task 起統一為 `StateDelta::Released { sid: String, row: ReleasedRow }`）。
- Produces：

```rust
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)] pub struct StopId(pub u64);
#[derive(Clone, Debug, PartialEq, Eq)] pub enum StopKind { Release, Recover, IoError, Removed, Rebound, Clear, Bind, Shutdown, LoginFailed }
pub struct StopOp { pub id: StopId, pub kind: StopKind, pub epoch: u64, pub waiters: Vec<Reply>, pub started: Mono, pub stalled: bool,
                    pub aborted_op: Option<(OpId, String /*cmd_id*/)> /* Phase 1 當下的前景命令 */, pub post_kind: Option<StopKind> /* drain 中到達的 Removed／Rebound／IoError */ }
#[derive(Debug, Clone)] pub struct EvidenceGap { pub op_id: Option<OpId>, pub dir: &'static str, pub len: usize, pub error: PalError }
#[derive(Debug)] pub struct WorkerStoppedReceipt { pub epoch: u64, pub stop_id: StopId, pub ledger: TxLedger /* Task 9；BC-03 映射唯一來源；只含未結算 op，缺項＝未提交（R1-F8） */,
    pub unrecorded: Vec<EvidenceGap>, pub wal_error: Option<PalError>, pub handle_closed: bool, pub close_error: Option<PalError> }
pub struct OpenedBridge { pub epoch: u64, pub worker: IoWorker, pub real_path: String }
pub enum OpenFailure { Held(HolderReport), AccessDenied(HolderReport), NotFound, Other(PalError) }
pub struct IoWorker { /* reader JoinHandle、TxWriterHandle、TxWriterStop、writer JoinHandle、CloseToken、waker、cancel */ }
impl IoWorker {
    pub fn start(handle: Box<dyn SerialHandle>, args: IoWorkerArgs) -> IoWorker;
    pub fn tx(&self) -> &TxWriterHandle;
    pub fn begin_stop(&self);                                    // reader stop flag＋TxWriterStop::send＋cancel.cancel()＋waker.wake()
    pub fn finish_async(self, stop_id: StopId, control: crossbeam_channel::Sender<ControlEvent>);  // thread：join writer（取 TxLedger）→ join reader → close → WorkerStopped
}
pub struct IoWorkerArgs { pub com: String, pub epoch: u64, pub wal: Arc<WalWriter>, pub rx: Arc<RxShared>, pub fence: Arc<Fence>,
    pub sentinel: Option<Box<dyn ConsoleIo>>, pub data: crossbeam_channel::Sender<DataEvent>, pub control: crossbeam_channel::Sender<ControlEvent>, pub clock: Arc<dyn Clock> }
pub fn drain_in_progress(&self) -> bool;                         // SessionCore；manager 用於 open 前檢查
```

`FakeStateStore`（Step 0，加入 `tests/support/mod.rs`；Tasks 12～16 共用）：`new() -> FakeStateStore`、`last_released() -> IndexMap<String, PyDict>`、`commits() -> usize`、`fail_next_commit(err: PalError)`；`CoreHarness` 以它處理 `Effect::Persist`（同步回 `StateCommitted`）。

- [ ] **Step 0：測試支援**：建立 `FakeStateStore` 並接到 `CoreHarness`（欄位 `state_store`）；新增 `CoreHarness::{start_foreground_command(cmd) -> StartedCmd{op_id, cmd_id}（admit＋出列並回傳 `StartOp` 的 grant）, pending_stop() -> (u64 epoch, StopId)（取最後一個 `BeginDrain`）, receipt(epoch, stop_id) -> ReceiptBuilder（`.confirmed(op, n)`、`.unknown(op)`、`.handle_closed(bool)`、`.gap(..)`、`.feed()`）, wal_meta_events() -> Vec<PyDict>（`Effect::WalMeta` 與 WAL 內 META 合併）}`。

- [ ] **Step 1：寫失敗測試**

`crates/sw-core/tests/drain_release.rs`（全部以 `CoreHarness`，無 thread）：

```rust
#![cfg(feature = "test-fakes")]
mod support;
use std::time::Duration;
use support::*;
use sw_core::{mailbox::ControlEvent, session_core::*};
use sw_proto::pyjson::PyValue;

#[test]
fn release_waits_for_receipt_then_persists_and_maps_bc03() {
    let mut h = CoreHarness::new(SHELL_PROFILE); h.to_ready();
    let cmd = h.start_foreground_command("sleep 20");                   // 注入 StartOp 已發生（OpGrant 取自 effect）
    let rx = h.rpc_deferred(SessionRequest::Release { source: "agent:t".into(), reason: PyValue::from("r") });
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    assert!(h.fence.with(|s| s.frozen && s.active_epoch.is_none()));
    assert!(rx.try_recv().is_err(), "no reply before receipt");
    let (epoch, stop) = h.pending_stop();
    h.receipt(epoch, stop).confirmed(cmd.op_id, 4).handle_closed(true).feed();
    let resp = rx.try_recv().unwrap().unwrap();                         // StateCommitted 已由 FakeStateStore 同步回
    assert_eq!((resp["ok"].clone(), resp["aborted_cmd"].clone()), (PyValue::Bool(true), PyValue::Bool(true)));
    let rec = h.reg.get(&cmd.cmd_id).unwrap();
    assert_eq!((rec["status"].clone(), rec["error_code"].clone()), (PyValue::from("error"), PyValue::from("COMMAND_ABORTED_IN_FLIGHT")));
    let meta = h.wal_meta_events().into_iter().find(|m| m["event"] == PyValue::from("command_aborted_in_flight")).unwrap();
    assert_eq!((meta["confirmed_tx_bytes"].clone(), meta["tx_outcome"].clone()), (PyValue::from(4), PyValue::from("partial")));   // P1 T08
    assert!(h.state_store.last_released().contains_key("t:COM0"));
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
}

#[test]
fn drain_timeout_marks_stalled_and_late_receipt_completes() {
    let mut h = CoreHarness::new(SHELL_PROFILE); h.to_ready();
    let rx = h.rpc_deferred(SessionRequest::Release { source: "cli".into(), reason: PyValue::Null });
    let (epoch, stop) = h.pending_stop();
    h.advance(Duration::from_millis(3001));
    let resp = rx.try_recv().unwrap().unwrap();
    assert_eq!(resp.keys().collect::<Vec<_>>(), vec!["ok", "error_code", "drain_stalled", "drain_timeout_epoch", "session"]);
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(false)));
    assert_eq!(resp.get("error_code"), Some(&PyValue::from("DRAIN_STALLED")));   // OD-14 #4，取代 SESSION_RECOVERING
    assert_eq!(resp.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert_eq!(resp.get("drain_timeout_epoch"), Some(&PyValue::from(1)));   // bridge_generation
    assert_eq!(h.public()["state"], PyValue::from("RECOVERING"));
    assert!(!h.effects.iter().any(|e| matches!(e, Effect::OpenBridge { .. })), "no reopen while stalled");
    let still = h.rpc(SessionRequest::Release { source: "cli".into(), reason: PyValue::Null }).unwrap();   // 仍 stalled 再 release
    assert_eq!(still.get("error_code"), Some(&PyValue::from("DRAIN_STALLED")));
    let att = h.rpc(SessionRequest::DeviceAttach { force: false }).unwrap();   // drain 中 attach 維持 SESSION_RECOVERING
    assert_eq!(att.get("error_code"), Some(&PyValue::from("SESSION_RECOVERING")));
    h.receipt(epoch, stop).handle_closed(true).feed();                     // 回條遲到
    assert_eq!(h.public()["state"], PyValue::from("RELEASED"));
    assert_eq!(h.public()["drain_stalled"], PyValue::Bool(false));
    assert_eq!(h.public()["drain_timeout_epoch"], PyValue::from(1));      // P1 T08：完成後保留最後一次逾時值
    let again = h.rpc(SessionRequest::Release { source: "cli".into(), reason: PyValue::Null }).unwrap();
    assert_eq!(again.get("already_released"), Some(&PyValue::Bool(true)));
}
```

| 測試名 | 精確斷言 |
|---|---|
| `release_success_response_shape` | 無在途命令：`{"ok":true,"session":{…state:"RELEASED"…},"closed_consoles":1,"aborted_cmd":false}`（POSIX 哨兵算 1） |
| `release_flushes_accepted_with_flushed_by_recovery` | 前景忙時排隊的 3 筆 → `status:"error"`、`error_code:"FLUSHED_BY_RECOVERY"` |
| `in_flight_zero_bytes_is_flushed_by_recovery` | receipt 帳本該 op `confirmed_bytes:0` → 該命令 `FLUSHED_BY_RECOVERY`（arch §5.7），無 `command_aborted_in_flight` META |
| `in_flight_unknown_is_send_failed` | 帳本 `unknown:true` → `SEND_FAILED`，無自動重送（Effects 無新 `StartOp`） |
| `full_write_aborted_meta_complete` | 帳本 `confirmed_bytes == payload len` → `COMMAND_ABORTED_IN_FLIGHT`＋META `tx_outcome:"complete"` |
| `abort_mapping_independent_of_opfinished_order` | Phase 1 後、receipt 前先到達同 op 的 `OpFinished(SendFailed)` 與 `OpFinished(CommandResult)` → 兩者都不呼叫 `registry.complete`（只保留 stdout 供診斷）；receipt 到達後才以帳本映射，最終 `COMMAND_ABORTED_IN_FLIGHT`；把順序對調（receipt 先、OpFinished 後）結果相同 |
| `duplicate_release_joins_same_stop_op` | drain 中（尚未逾時）第二次 release → 加入同一 StopOp、共用同一 3000 ms 期限；兩個 waiter 收到相同結果（回條及時 → 皆 ok；逾時 → 皆 `DRAIN_STALLED`）；只有一個 `BeginDrain` |
| `cross_kind_request_during_drain_is_session_recovering` | Release drain 中收到 `Clear`／`Bind`／`Recover{force:false}` → 各自立即回 `{"ok":false,"error_code":"SESSION_RECOVERING","drain_stalled":<bool>,"drain_timeout_epoch":<…>,"session":…}`，不加入 release waiters（§5.2.5 去重只限同 kind） |
| `internal_events_during_drain_recorded_as_post_kind` | Release drain 中收到 `DeviceDetached(Removed)` → 不另起 drain；receipt 後仍 `RELEASED`（Release 優先）；Recover drain 中收到 Removed → receipt 後 `DETACHED`＋`DEVICE_REMOVED`；`Shutdown` 一律加入 waiters |
| `stale_receipt_after_reattach_keeps_evidence_only` | release 完成並 `DeviceAttach` 進新 epoch 後，**明確注入**一個舊 `epoch`／`stop_id` 的 receipt（帳本 op `confirmed_bytes:2`）→ 新 session state、fence、registry 中新命令皆不變；只對舊 op 的命令紀錄套 BC-03（若尚未終態）（Review Focus 1、§5.2.4） |
| `settle_emitted_once_at_terminal_point`（R1-F8） | READY；命令 A `start_foreground_command` → 注入 `OpFinished{op_id:A, outcome: CommandResult(ok)}` → effects 恰一個 `Effect::SettleTx{epoch: conn_epoch, op_id: A}`，且排在 `registry.complete(A)` 之後（`registry.get(A).done_at` 已設）；同一 op 重送 `OpFinished`（重複事件）不再產生 `SettleTx`；`LoginFinished(Ready)` 對 login run 的 op 同樣恰一個 `SettleTx` |
| `aborted_op_is_never_settled_during_drain`（R1-F8；與 T9 `ledger_retire_racing_drain_preserves_bc03` 成對） | 前景 B 進行中、已終態的 A 尚未處理其 `OpFinished`；`Release` → Phase 1（`aborted_op==B`）→ 依序注入 A 的 `OpFinished`、B 的 `OpFinished(CommandResult)` → effects 含 `SettleTx{op_id:A}`、**不含**任何 `SettleTx{op_id:B}`；receipt 以兩種帳本各跑一次（`B:confirmed 3`＋A 缺項〔settle 已套用〕；`B:confirmed 3`＋`A:confirmed 5`〔settle 被 executor 因 worker 已交 finisher 而丟棄〕）→ 兩次皆 B `COMMAND_ABORTED_IN_FLIGHT`＋`command_aborted_in_flight` META（`confirmed_tx_bytes:3`）、A 維持其原終態不被改寫（已終態者不再套 BC-03）；receipt 之後仍無 `SettleTx{B}`（該 epoch 已結束，帳本隨 receipt 丟棄） |
| `missing_ledger_entry_maps_to_flushed`（R1-F8） | 前景命令 C 的 receipt 帳本無 C 項目（saturation 下未建項目，或從未提交）且 `aborted_op==C` → `FLUSHED_BY_RECOVERY`（等同 `confirmed_bytes:0, unknown:false`），無 `command_aborted_in_flight` META |
| `tx_ledger_saturated_drains_as_io_error`（R1-F8） | READY＋`UartIoError{epoch: conn_epoch, reason:"TX_LEDGER_SATURATED"}` → 與 `SERIAL_READ:5` 相同的 IoError drain 路徑（RECOVERING → receipt → 裝置在 → `ATTACHING`＋`OpenBridge`），`Effect::Warn` 一則含 `TX_LEDGER_SATURATED`；無新 `error_code` |
| `wal_gap_lists_unrecorded_and_still_completes` | receipt `unrecorded` 非空、`handle_closed:true` → release 完成；stderr WARNING 含 `未落盤`；`health().healthy` 由 WalWriter 自身決定（本列不另改） |
| `close_failure_keeps_recovering_and_no_success` | receipt `handle_closed:false` → state `RECOVERING`、`drain_stalled:true`、release waiter 收 `{"ok":false,"error_code":"DRAIN_STALLED","drain_stalled":true,"drain_timeout_epoch":<bridge_generation>,"session":…}`（OD-14 #4）；`DeviceAttached` 不觸發 open；此時 `DeviceAttach` 回 `SESSION_RECOVERING` |
| `release_commit_failure_is_exception_but_memory_released` | `state_store.fail_next_commit(EIO)` → reply `Err(PyErr)`（framing 轉 `EXCEPTION`），state `RELEASED`，`DeviceAttached` 不觸發 open（RPC-H-11、§6.2「提交失敗時不得回覆成功，維持禁止自動 attach」） |
| `attach_precheck_holder_still_held` | RELEASED＋`DeviceAttach{force:false}` → `Effect::OpenBridge{precheck_holders:true}`；注入 `BridgeOpened(Err(Held{pids:[4242]}))`（executor 預檢結果）→ `{"ok":false,"error_code":"DEVICE_STILL_HELD","pids":[4242],"holder_unknown":false,"selector":…}`、仍 `RELEASED`、`state_store.commits()` 不變 |
| `attach_open_held_reports_holder_unknown` | `BridgeOpened(Err(Held{pids:[]}))` → `DEVICE_STILL_HELD`、`pids:[]`、`holder_unknown:true` |
| `attach_access_denied_reports_holder_unknown` | `BridgeOpened(Err(AccessDenied{pids:[]}))` → 同上（Windows 語意） |
| `attach_not_found_device_not_present` | `BridgeOpened(Err(NotFound))` → `{"ok":false,"error_code":"DEVICE_NOT_PRESENT","selector":…,"device_by_id":…}`，仍 `RELEASED` |
| `attach_other_open_error_detached_attach_failed` | `BridgeOpened(Err(Other(EIO)))` → `{"ok":true,"session":…}`、state `DETACHED`、`last_error:"ATTACH_FAILED:OSError"`（P1 T10 結果表、arch §5.9 步驟 3）；`state.json` 的 released 列已清（attach 已被受理）——以 P1 golden `contracts/golden/rpc/device_attach_*.json` 為準 |
| `hotplug_held_is_detached_attach_failed` | 非 RELEASED 的動態 session 收 `DeviceAttached` → `BridgeOpened(Err(Held{..}))` → `DETACHED`、`last_error:"ATTACH_FAILED:DeviceHeldError"` |
| `attach_success_commits_then_attaching` | `BridgeOpened(Ok)` → `Persist(Unreleased)` → `StateCommitted(Ok)` 後才回 `{"ok":true,"session":{state:"ATTACHING"}}`；`last_released()` 已不含該 sid；`StartLogin` 在回覆之後 |
| `attach_during_drain_is_session_recovering` | release 卡在 drain 時 `DeviceAttach` → `SESSION_RECOVERING`＋`drain_stalled`，無 `OpenBridge` |
| `attach_force_does_not_bypass_identified_external_holder`（取代原 `attach_force_skips_precheck_not_exclusivity`；規格 §6.2） | RELEASED＋`DeviceAttach{force:true}` → `Effect::OpenBridge{precheck_holders:false}`；注入 `BridgeOpened(Err(Held{pids:[4242], holder_unknown:false}))`（`open_exclusive` 鎖後重掃識別到未取 flock 的外部 holder）→ 回應**完全等於** `{"ok":false,"error_code":"DEVICE_STILL_HELD","pids":[4242],"holder_unknown":false,"selector":"COM0"}`；仍 `RELEASED`、`state_store.commits()` 不變；effects 不含 `StartLogin`、`StartOp`、`Persist{delta: StateDelta::Unreleased{..}}`（UART 零 TX）；`conn_epoch` 仍 `None` |
| `auto_attach_plain_external_holder_no_io` | DETACHED 動態 session（初次啟動）＋`DeviceAttached` → `OpenBridge{precheck_holders:false}`；注入 `BridgeOpened(Err(Held{pids:[4242], holder_unknown:false}))` → `DETACHED`、`last_error:"ATTACH_FAILED:DeviceHeldError"`；effects 無 `StartLogin`／`StartOp`；`conn_epoch` 仍 `None` |
| `already_attached` | 有 bridge 且非 RELEASED → `{"ok":true,"already_attached":true,"session":…}` |
| `missing_device_tick_keeps_released` | RELEASED 時裝置離線 → 仍 `RELEASED`（DF-04；`_mark_missing_devices` 不降級） |
| `io_error_drain_then_reattach_when_present` | READY＋`UartIoError` → RECOVERING → receipt → 裝置在 → `ATTACHING`＋`OpenBridge`；裝置不在 → `DETACHED` |
| `rebound_receipt_sets_device_rebound_required_then_reopens` | `DeviceDetached(Rebound)` → receipt 後 `last_error:"DEVICE_REBOUND_REQUIRED"`，**之後**才出現 `OpenBridge`（新 real_path） |
| `login_failure_detaches_after_receipt` | `LoginFinished(Failed("LOGIN_PROMPT_TIMEOUT"))` → drain → receipt → `DETACHED`、`last_error:"LOGIN_PROMPT_TIMEOUT"`（05 T5） |
| `shutdown_flushes_with_flushed_by_shutdown` | `Shutdown` → accepted 命令 `FLUSHED_BY_SHUTDOWN`；回覆在 receipt 之後 |
| `stale_reply_context_rejected_after_release` | release 前取得的 lease id 與 OpGrant：release 完成後以舊 `owner_gen`／`epoch` 的 permit 送 job → fence 拒絕（`StaleEpoch`／`OwnerMismatch`），`InteractiveSend` 舊 id 回 `INTERACTIVE_NOT_FOUND`（§11.3 不回放失效上下文） |

`crates/sw-core/tests/io_worker.rs`（`IoWorker`＋FakeSerial，無 login）：

| 測試名 | 精確斷言 |
|---|---|
| `idle_worker_finishes_with_receipt` | 無任何 TX 時 `begin_stop`＋`finish_async` → 100 ms 內收到 `WorkerStopped{handle_closed:true}`（即使另有 `TxWriterHandle` clone 存活，模擬 OpCtx） |
| `drain_completes_while_opctx_sender_alive` | OpCtx 持有的 `TxWriterHandle` clone 未 drop、writer 卡 `BlockUntilCancel{4}` → receipt 帳本 `confirmed_bytes:4`，`close_count()==1` |
| `blocked_forever_writer_never_sends_receipt` | `BlockForever` → 3 s 內無 receipt（finisher 不設逾時；逾時由 actor 的 DrainTimeout 處理）；`release_block()` 後收到 |
| `begin_stop_not_blocked_by_inflight_completion_wait`（R3-F3；對照 P1 T20 `test_windows_guarded_write_stall_reports_drain_stalled_on_time`） | writer 以 `BlockForever` 卡在 gate 外的完成等待（已交 OS、`in_flight` 已設）；等待期間另一 thread `fence.try_with(|_| ()).is_some()`（Fence 鎖未跨完成等待）；Phase 1 的 `fence.with(|st| st.frozen = true)` 加 `worker.begin_stop()` 在 50 ms 內返回、之後 `fence.with(|st| st.frozen) == true`；`release_block()` 前 `close_count() == 0`、無 receipt（不提前關閉）；`release_block()` 後收到 receipt 且 `close_count() == 1`。期限起點由 core 的 `begin_stop(kind, waiter, now)` 以停止請求當下的 `now` 排 `DrainTimeout`（`drain_timeout_marks_stalled_and_late_receipt_completes` 已釘 3001 ms 觸發） |
| `wal_unwritable_during_drain_lists_gap` | drain 期間 `fail_next_writes(wal, ∞)` → receipt `handle_closed:true`、`unrecorded` 非空 |
| `close_failure_in_receipt` | `fail_close(e)` → receipt `handle_closed:false`、`close_error` 有值 |
| `receipt_ledger_holds_only_unsettled_ops`（R1-F8） | 經 `worker.tx()` 送 op 1～600 各 1 job 並逐一 `worker.tx().settle(op)`，op 601 送 1 job 不 settle；其中 op 3 的 TX WAL 失敗（`fail_next_writes(wal, 2, ENOSPC)`）→ `begin_stop`＋`finish_async` → receipt `ledger.ops` 鍵集合 `== {601}`、`ledger.settled_total==600`、`unrecorded` 恰含一筆 `op_id==Some(OpId(3))` 的缺口（已結算 op 的證據缺口仍隨 receipt 交回） |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-core --features test-fakes --test drain_release --test io_worker`
Expected：FAIL，`not implemented: slice21 T12 SessionCore::begin_stop`（`io_worker`：`not implemented: slice21 T12 IoWorker::start`）。

- [ ] **Step 3：實作**

Phase 1（`SessionCore::begin_stop(kind, waiter, now)`，arch §5.5）：

```rust
fn begin_stop(&mut self, kind: StopKind, waiter: Option<Reply>, now: Mono) -> Effects {
    if let Some(op) = self.stop.as_mut() {
        if op.kind == kind || kind == StopKind::Shutdown { if let Some(w) = waiter { op.waiters.push(w); } return Effects(vec![]); } // 同 kind 去重
        return match (kind, waiter) {
            (StopKind::Removed | StopKind::Rebound | StopKind::IoError, _) => { op.post_kind = Some(kind); Effects(vec![]) }
            (_, Some(w)) => Effects(vec![Effect::Reply(w, Ok(self.session_recovering_reply()))]),   // 跨 kind 的 RPC 請求
            (_, None) => Effects(vec![]),
        };
    }
    let epoch = match self.gens.conn_epoch { Some(e) => e, None => return self.finish_stop_without_worker(kind, waiter) };
    let id = StopId(self.next_stop_id()); let mut fx = Vec::new();
    self.deps.fence.with(|s| { s.active_epoch = None; s.frozen = true; s.owner_gen += 1; s.fsm_gen += 1; });  // 同一把鎖：線性化點
    self.set_state(SessionState::Recovering);                                         // SC-04（release 亦同）
    let aborted_op = self.foreground.as_ref().and_then(|g| g.cmd_id.clone().map(|c| (g.op_id, c)));
    for c in self.queue.drain_all() { let _ = c; }                                    // 佇列清空；紀錄由 registry flush
    let code = if kind == StopKind::Shutdown { "FLUSHED_BY_SHUTDOWN" } else { "FLUSHED_BY_RECOVERY" };
    self.deps.registry.flush_session(&self.sid, code, &(self.deps.wall_iso)());
    self.gens.owner_gen += 1;                                                         // BC-02：舊輸入上下文失效
    self.close_leases_and_capture();                                                  // _detach_session_locked 契約（05 §3.1 step 5–7）
    fx.push(Effect::BeginDrain { stop_id: id, epoch });
    fx.push(Effect::Schedule { at: now + 3_000_000_000, ev: TimerEvent::DrainTimeout { epoch, stop_id: id } });
    self.stop = Some(StopOp { id, kind, epoch, waiters: waiter.into_iter().collect(), started: now, stalled: false, aborted_op, post_kind: None });
    Effects(fx)
}
```

- executor 的 `BeginDrain`：`worker.begin_stop()` 後 `worker.finish_async(stop_id, control)`；finisher thread（不設逾時）：join writer thread 取得 `(writer, TxLedger)` → join reader thread 取得 `ReaderExit` → WAL 失敗彙整為 `unrecorded`（reader 缺口＋`ledger.unresolved_gaps`＋仍在 `ledger.ops` 的各 op `wal_gaps`；合併計數留在 `ledger.gaps_overflow` 與各 op `wal_gaps_overflow`，隨 receipt 交回，R1-F8）→ `CloseToken::close(reader, writer)` → 送 `WorkerStopped(receipt{ledger, ..})`。`TxWriter` 結束不依賴 Sender 數量（Task 9 `TxWriterStop`）。
- `OpFinished` 優先序：`stop` 進行中且 `OpFinished.epoch == stop.epoch` 且 op 為 `stop.aborted_op` → 不呼叫 `registry.complete`（保留 stdout 於 debug log），最終結果只由 receipt 帳本決定；其他 op 照常處理。
- TX 帳本結算（R1-F8，Task 9 結算／退休協定）：`SessionCore::settle_tx(op_id, fx)` 是唯一產生 `Effect::SettleTx{epoch: conn_epoch, op_id}` 的地方，每個 op 恰呼叫一次，位置是該 op 的終態處理點且在 `registry.complete` 之後——`OpFinished`（非 aborted_op 時）、`LoginFinished`（login run 的 op）、lease close／expiry（`LeaseOwner`／`InteractiveOpenCmd` op）、self-test 的 `OpFinished(SelfTest)`；`stop.aborted_op` 與尚未終態的 op 一律不結算（其證據只隨 receipt 帳本交回）。`conn_epoch` 為 None 時不產生。executor：`SettleTx.epoch` 等於現行 worker epoch 才呼叫 `worker.tx().settle(op_id)`（非阻塞），否則丟棄。`UartIoError{reason:"TX_LEDGER_SATURATED"}` 走既有 IoError drain，另發 `Effect::Warn("session <sid> TX 帳本未結算 op 達 256 上限，以 IoError drain 收斂（R1-F8）")`。後續 Phase 新增 op 種類（07 human TX、08 reprobe／reconfirm、09 transfer／flash）須在各自終態處理點呼叫 `settle_tx`。
- 收到 receipt：`epoch`／`stop_id` 不符 → 只對帳本中仍未終態的舊 op 命令紀錄套 BC-03，不改狀態；相符且 `handle_closed` → 對 `aborted_op` 依帳本 `registry.complete(cmd, CmdOutcome::Aborted(class))`（`confirmed_bytes ≥ 1`→InFlight、`0` 且非 unknown→NotSubmitted、`unknown`→Unknown），InFlight 另產生 `Effect::WalMeta{cmd_id, meta:{"event":"command_aborted_in_flight","confirmed_tx_bytes":N,"tx_outcome":"complete"|"partial"}}`（`complete`＝確認 bytes 等於 payload 長度；P1 T08）→ `conn_epoch=None`、`queue_registered=false`、`Notify(SessionDetached)` → 依 kind（`post_kind` 只在 kind 非 Release 時覆寫裝置存在判斷）：Release → `Persist{delta: StateDelta::Released{sid, row: ReleasedRow{by_id, released_by, released_at, reason}}}`，等 `StateCommitted` 成功才 `RELEASED` 並回覆所有 waiters；失敗 → 狀態仍設 `RELEASED`、waiters 收 `Err(PyErr("state.json commit failed: …"))`；Recover／IoError → 裝置在 `ATTACHING`＋`OpenBridge`，否則 `DETACHED`；Removed → `DETACHED`、`last_error:"DEVICE_REMOVED"`；Rebound → `last_error:"DEVICE_REBOUND_REQUIRED"` 後 `OpenBridge`（Task 16）；Shutdown → 回覆 waiter。`handle_closed:false` → `stalled=true`、維持 RECOVERING、Release waiters 收 `DRAIN_STALLED` 形狀（下列逾時同形；OD-14 #4）。收尾時 `drain_stalled=false`，`drain_timeout_epoch` 保留最後一次逾時值（P1 T08 凍結）。
- `DrainTimeout` 且 StopOp 未完成：`stalled=true`、對外 `drain_stalled:true`、`drain_timeout_epoch=bridge_generation`；Release 的 waiters 收 `{"ok":false,"error_code":"DRAIN_STALLED","drain_stalled":true,"drain_timeout_epoch":N,"session":…}`（OD-14 #4，鍵序如列；waiters 清空，之後遲到回條照樣完成 release）；`stalled` 期間再收到 Release → 立即回同形 `DRAIN_STALLED`（不加入 waiters、不另起 drain）；Clear／Bind／Recover／DeviceAttach 仍回 `SESSION_RECOVERING`＋drain 欄位。
- Release 回應 `closed_consoles` = 進入 Phase 1 時的 console 數（Python `len(bridge.list_consoles())`；2.1 POSIX 哨兵 = 1、Windows 0）、`aborted_cmd` = 進入 Phase 1 時前景是否忙。收尾時 `last_error` 依 kind 設定（05 §3.1）：Release→`RELEASED`、Clear→`CLEARED`、Bind→`REBOUND`、Removed→`DEVICE_REMOVED`、Rebound→`DEVICE_REBOUND_REQUIRED`、IoError→裝置仍在時覆寫為 `null`（T20）。
- SC-05 attach：`drain_in_progress` → `SESSION_RECOVERING`；`OpenBridge{precheck_holders: !force}` 由 attach worker thread 執行：預檢 `uart_excl.probe_holders(real_path)` 的 `pids` 非空 → `BridgeOpened(Err(Held{pids}))`（不呼叫 `open_exclusive`），否則同步 `open_exclusive`（RPC reply 延後到 `BridgeOpened`）；`precheck_holders:false`（force、自動 attach）只省略開檔前快照，`open_exclusive` 內的鎖後重掃（Task 2）照常執行，識別到外部 holder 一律 `Err(Held)`——`force` 不得略過已識別的外部衝突（規格 §6.2）；結果映射照 P1 T10 結果表（含「其他 OSError → `ok:true`＋`DETACHED`＋`ATTACH_FAILED:<ExcType>`」、熱插 `Held` → `ATTACH_FAILED:DeviceHeldError`）；成功 → `Persist(StateDelta::Unreleased{sid})` → commit 後 `ATTACHING` 並回 ok，`StartLogin` 非同步。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-core --features test-fakes --test drain_release --test io_worker`
Expected：PASS（`drain_release` 36 項：2 個程式碼案例＋34 列，含 R1-F8 四列；`io_worker` 6 項）。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-core changelog.d/rust-s21-drain-release.md
git commit -m "feat(sw-core): 兩階段 drain、release 回條與獨占收回" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 13：sw-core 最小 Login＋帳密＋Quiet Gate＋Profile 能力閘

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-login-quiet.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust 最小登入（shell／prpl；passthrough 不登入、不 probe、不 arm quiet，SC-35）、帳密未解析終態 CREDENTIALS_UNRESOLVED（不送空帳密）、boot banner／reboot 命令觸發的 Quiet Gate 與 Ready Reconfirm 期限；RPC 帶入的 human: source 一律以 agent 對待（DF-02）；Slice 2.1 不支援的 profile 能力不自動送 TX。」）
**BLOCKED-OWNER：** (1) 實機 prplOS／OpenWrt shell 板的登入→READY 與 autoboot quiet 行為（L2 F9 屬 2.3，但 2.1 的 quiet 最小防護需 L3 P0 reboot 相關核心案例，需 bench）。

**Files:**
- Modify: `crates/sw-core/src/{login_fsm.rs,session_auth.rs,quiet_gate.rs,session_core.rs,op_runner.rs}`
- Test: `crates/sw-core/tests/login_quiet.rs`

**Interfaces:**
- Consumes：Tasks 9～12；`sw_proto::config_types::SessionProfile.template: ProfileTemplate`（欄位：`platform`、`prompt_regex`、`login_regex`、`password_regex`、`user_env`、`pass_env`、`env_file`、`username`、`post_login_cmd`、`ready_probe`、`timeout_s`、`console_unlock_env`、`bootloader_prompts`；`ProfileTemplate::command_capable()`）；`sw_proto::pycompat::shlex_split`（Task 0c，`is_reboot_command` 用）、`sw_proto::pyre::valid_completion_regex`；`sw_pal::SysEnv::var`。
- Produces：

```rust
// session_auth.rs（05 §9 逐字）
#[derive(Clone, Default)] pub struct SessionAuth { pub username: Option<String>, pub password: Option<String>, pub console_unlock: Option<String> }
#[derive(Clone, Debug, PartialEq)] pub struct AuthResolution { pub reason: &'static str, pub env_file_path: Option<String> }
pub fn parse_env_file(text: &str) -> IndexMap<String, String>;
pub fn resolve_session_auth(p: &SessionProfile, env: &dyn SysEnv, read: &dyn Fn(&str) -> Option<std::io::Result<String>>) -> (SessionAuth, AuthResolution);
pub fn credentials_declared_but_unresolved(p: &SessionProfile, r: &AuthResolution) -> bool;

// quiet_gate.rs（05 §7）
pub const BOOT_BANNER_PATTERNS: [&str; 3] = ["U-Boot", "Hit any key to stop autoboot", "CFE version "];
#[derive(Default, Debug, Clone)] pub struct QuietState { pub boot_quiet_until: Mono, pub boot_banner_tail: String, pub pending: bool, pub deadline: Mono, pub attempts: u32, pub failed: bool }
impl QuietState {
    pub fn arm(&mut self, now: Mono);                       // until = now+180 s；tail=""；未 pending → pending、deadline=now+300 s、attempts=0、failed=false
    pub fn clear_quiet(&mut self);                          // 只清 until/tail
    pub fn confirm_ready(&mut self);
    pub fn expire(&mut self, now: Mono, force: bool) -> bool;
    pub fn quiet_active(&self, now: Mono) -> bool;
    pub fn remaining_s(&self, now: Mono) -> Option<f64>;    // py_round(…,1)
    pub fn reconfirm_remaining_s(&self, now: Mono) -> Option<f64>;
    pub fn on_rx_text(&mut self, chunk: &str, profile: &SessionProfile, now: Mono) -> QuietTransition;  // Armed | Cleared | None
}
pub enum QuietTransition { Armed, Cleared, None }
pub fn detect_boot_banner(text: &str) -> bool;
pub fn is_reboot_command(cmd: &str) -> bool;                // 05 §5.2 step 2
pub use sw_proto::pyre::valid_completion_regex;            // 05 §0；P3 已交付，此處只重新匯出
pub fn slice21_unsupported_feature(p: &SessionProfile) -> Option<&'static str>; // "post_login_cmd" | "console_unlock_env"

// login_fsm.rs
pub enum LoginPlan { None, Probe, EnsureReady { auth: SessionAuth }, CredentialsUnresolved { reason: &'static str, env_file: String } }
pub fn choose_login_plan(p: &SessionProfile, pending_auto_login: bool, auth: (SessionAuth, AuthResolution), quiet_blocks: bool) -> (LoginPlan, Option<&'static str> /*合成 last_error*/);
#[derive(Debug, Clone, PartialEq)] pub enum LoginOutcome { Ready, NotReady(String /* last_error */), Failed(String) }
pub struct LoginIo<'a> { pub rx: &'a RxShared, pub tx: &'a dyn Fn(&[u8], TxClass, &str, bool) -> TxResult, pub clock: &'a dyn Clock, pub nonce: &'a dyn Fn() -> String }
pub fn probe_ready(io: &LoginIo, p: &SessionProfile) -> LoginOutcome;
pub fn ensure_ready(io: &LoginIo, p: &SessionProfile, auth: &SessionAuth, env: &dyn SysEnv) -> LoginOutcome;
pub fn prompt_timeout_error(platform: &str) -> &'static str;   // bcm→BCM_PROMPT_TIMEOUT、shell→SHELL_PROMPT_TIMEOUT、其他→PRPL_PROMPT_TIMEOUT
pub const FLOOD_MASKABLE_ERRORS: [&str; 5] = ["PROMPT_UNAVAILABLE", "READY_NONCE_TIMEOUT", "READY_PROMPT_TIMEOUT", "POST_LOGIN_CMD_TIMEOUT", "LOGIN_PROMPT_TIMEOUT"]; // login_fsm.py _FLOOD_MASKABLE_ERRORS 逐字
pub const LOGIN_FSM_DETAIL_ERRORS: [&str; 12] = ["USER_ENV_MISSING", "PASS_ENV_REQUIRED", "PASS_ENV_MISSING", "LOGIN_USER_REQUIRED", "LOGIN_PROMPT_TIMEOUT",
    "BCM_PROMPT_TIMEOUT", "SHELL_PROMPT_TIMEOUT", "PRPL_PROMPT_TIMEOUT", "POST_LOGIN_CMD_TIMEOUT", "READY_NONCE_TIMEOUT", "READY_PROMPT_TIMEOUT", "LOGIN_REQUIRED"]; // #174
pub use quiet_gate::detect_boot_banner;                       // 下游（07 計畫）以 login_fsm::detect_boot_banner 取用
```

- [ ] **Step 0：測試支援**（本 Task 為 `ActorRig` 的第一個使用者，Test author model 撰寫、與 Step 1 同 commit）：`ActorRig`（加入 `tests/support/mod.rs`，Tasks 13、14、12b 共用；以真 actor thread＋`IoWorker`＋FakeSerial＋FakeWalFs＋FakeClock＋`FakeStateStore` 組成）：`new(yaml: &str) -> ActorRig`、`attach(self) -> ActorRig`（只送 `DeviceAttached`，不等 READY）、`ready(self) -> ActorRig`（attach 並以 `FakeDevice::on_tx_match` 回 prompt／nonce 直到 READY；本 Task 實作 login 後才可用，Task 14／12b 沿用）、`submit_ok(&self, cmd: &str) -> String`、`submit_with(&self, cmd: &str, f: impl FnOnce(&mut SubmitReq)) -> String`、`wait_running(&self, cmd_id: &str)`、`wait_done(&self, cmd_id: &str) -> PyDict`（等紀錄有 `done_at`，回 `command` dict）、`request(&self, req: SessionRequest) -> PyDict`、`request_async(&self, req: SessionRequest) -> Receiver<Result<PyDict, PyErr>>`、`get_state(&self) -> PyDict`（回 `session` 子 dict）、`command_get(&self, id: &str) -> PyDict`、`wal_tx_payloads(&self) -> Vec<Vec<u8>>`、`wal_meta_events(&self) -> Vec<PyDict>`、`stderr_lines(&self) -> Vec<String>`（`Effect::Warn` 收集）、`wait_state(&self, s: &str)`；欄位 `dev: FakeDevice`、`clock: Arc<FakeClock>`、`state_store: FakeStateStore`。`submit_ok`／`wait_running`／`wait_done` 需 Task 14 命令執行，本 Task 測試只用 `attach`／`ready`／`get_state`／`wal_meta_events`／`stderr_lines`。

- [ ] **Step 1：寫失敗測試**

```rust
#![cfg(feature = "test-fakes")]
mod support;
use std::time::Duration;
use support::*;
use sw_core::{quiet_gate::*, session_core::*};
use sw_proto::pyjson::PyValue;

#[test]
fn credentials_unresolved_is_terminal_attached_and_sends_no_credentials() {
    let mut h = ActorRig::new(CRED_PROFILE).attach();             // env_file 不存在、env 無 SW_T_U/SW_T_P
    h.wait_state("ATTACHED");
    let s = h.get_state();
    assert_eq!(s["last_error"], PyValue::from("CREDENTIALS_UNRESOLVED"));
    assert_eq!(h.dev.write_calls(), 0, "no probe, no empty username");
    let metas = h.wal_meta_events();
    assert_eq!(metas.iter().filter(|m| m["event"] == PyValue::from("credentials_unresolved")).count(), 1);
    assert_eq!(metas[0]["reason"], PyValue::from("env_file_missing"));
    assert_eq!(h.stderr_lines().iter().filter(|l| l.contains("帳密解析未完成")).count(), 1);
    h.clock.advance(Duration::from_secs(60));
    assert_eq!(h.dev.write_calls(), 0, "no automatic reprobe");
}

#[test]
fn banner_split_across_chunks_arms_quiet_and_blocks_agent_with_retry_after() {
    let mut h = CoreHarness::new(SHELL_PROFILE); h.to_ready();
    h.rx(b"\r\nU-Bo"); h.rx(b"ot 2024.01 (Jan 01)\r\n");
    let r = h.rpc(SessionRequest::Submit(agent_submit("ls")) ).unwrap();
    assert_eq!(r.get("error_code"), Some(&PyValue::from("AUTOBOOT_QUIET")));
    assert_eq!(r.get("retry_after_s"), Some(&PyValue::Float(180.0)));
    h.rx(b"root@t:/# ");                                          // prompt 命中 → 只清 quiet、保留 pending
    let r2 = h.rpc(SessionRequest::Submit(agent_submit("ls"))).unwrap();
    assert_eq!(r2.get("retry_after_s"), Some(&PyValue::Float(5.0)));
    assert_eq!(h.public()["ready_reconfirm_pending"], PyValue::Bool(true));
}

#[test]
fn human_prefixed_rpc_source_is_gated_like_agent_df02() {
    let mut h = CoreHarness::new(SHELL_PROFILE); h.to_ready();
    h.rx(b"Hit any key to stop autoboot: 3\r\n");
    let mut req = agent_submit("ls"); req.source = "human:spoof".into();
    assert_eq!(h.rpc(SessionRequest::Submit(req)).unwrap().get("error_code"), Some(&PyValue::from("AUTOBOOT_QUIET")));
}
```

（`agent_submit(cmd) -> SubmitReq` 已由 Task 11 Step 0 建立。）

| 測試名 | 精確斷言 |
|---|---|
| `rearm_does_not_extend_reconfirm_deadline` | t=0 arm、t=100 s 再 arm → `reconfirm_remaining_s(t=100) == Some(200.0)`；quiet `remaining_s == Some(180.0)` |
| `bootloader_prompt_tail_prevents_clear` | quiet 中 tail 尾行 `=> ` 命中 `bootloader_prompts`（不用 fallback）→ 不清；`prompt_regex` 命中 → 清 |
| `reconfirm_exhausts_after_300s_ready_unconfirmed` | advance 300 s → Submit 回 `{"ok":false,"error_code":"READY_UNCONFIRMED","hint":"READY 已逾 300s／5 次未能經 nonce probe 再確認（可能卡在 bootloader）；請 session self-test 取分類後 interactive-open 處理","recommended_action":"self_test","session":…}`，無 `retry_after_s` |
| `reconfirm_exhausts_after_5_attempts` | `attempts=5`（self-test probe 失敗 5 次）→ `failed:true` |
| `execute_time_gate_has_retry_after_sc03` | 命令已排隊後 banner 到達 → 出列時命令紀錄 `error_code:"AUTOBOOT_QUIET"`，且 OpRunner 未呼叫 writer（`write_calls` 不變）；`command.get` 不含 retry（REC）；直接 `execute` 回應含 `retry_after_s`（SC-03） |
| `quiet_blocks_login_probe_zero_tx` | quiet 中觸發 attach → `LoginPlan` 不啟動、`last_error:"PROMPT_UNAVAILABLE"`、`write_calls()==0` |
| `reboot_command_arms_quiet_at_dequeue` | 送 `reboot`（及 `echo b > /proc/sysrq-trigger`、`systemctl reboot`、`shutdown -r now`、`/sbin/reboot`）→ 該命令 TX 寫出後 fence `quiet_active==true`、後續 Submit 回 `AUTOBOOT_QUIET` |
| `is_reboot_command_negative` | `"rebootx"`、`"echo b"`、`"shutdown -h now"` → false |
| `probe_ready_sends_lf_and_matches_whole_window` | `probe_ready`：先 `clear` RX、送 `b"\n"`（source `system`、class `LoginProbe`）、prompt 在 clear 後出現即成功；nonce 探針 `echo __READY__ab12cd34` 由 `nonce()`＝`"ab12cd34"`，命令回顯含 nonce 即滿足（SES-16.6-05 KEEP） |
| `ensure_ready_login_sequence` | RX 依序 `login: `→送 user→`Password: `→送 secret（WAL 無此 TX，`log=false`）→prompt→nonce → `Ready`；WAL TX 只有 `\n`、user、nonce 命令 |
| `prompt_timeout_codes_by_platform` | 純函式 `prompt_timeout_error`：`shell`→`SHELL_PROMPT_TIMEOUT`、`prpl`→`PRPL_PROMPT_TIMEOUT`、`passthrough`→`PRPL_PROMPT_TIMEOUT`（函式層回歸；SC-35 起 passthrough session 不進 Login FSM，實際不會產生此值） |
| `rx_flood_reclassifies` | 失敗碼 `PROMPT_UNAVAILABLE` 且 `rx_stats().0 >= 20000` → `RX_FLOOD` |
| `login_failure_code_table` | 參數化，每列一個情境：probe 失敗且視窗可見 `login:`（`matches_login_or_password`）→ `LOGIN_REQUIRED`；nonce 未回 → `READY_NONCE_TIMEOUT`；nonce 後無 prompt → `READY_PROMPT_TIMEOUT`；`login:` 未出現 → `LOGIN_PROMPT_TIMEOUT`；需 user 但 `username`／`user_env` 皆無 → `LOGIN_USER_REQUIRED`；`user_env` 宣告但 env 無值 → `USER_ENV_MISSING`；需密碼但 `pass_env` 未宣告 → `PASS_ENV_REQUIRED`；`pass_env` 宣告但 env 無值 → `PASS_ENV_MISSING`。每列斷言 `last_error`，並經 `SessionAttach` 回應可見：`error_code` 固定 `"SESSION_NOT_READY"`，頂層 `last_error` 等於該列值、`last_error_detail` 等於 session 內同名欄位（SC-34） |
| `flood_masking_exact_set` | 對 `FLOOD_MASKABLE_ERRORS` 五碼，`rx_stats().0 >= 20000` 時改為 `RX_FLOOD`；`LOGIN_REQUIRED`、`USER_ENV_MISSING`、`SHELL_PROMPT_TIMEOUT` 在同條件下**不**被遮蔽 |
| `login_error_detail_carries_rx_tail` | `last_error ∈ LOGIN_FSM_DETAIL_ERRORS` 時 `last_error_detail` 為失敗當下 RX tail（截尾、`strip_ansi`＋去控制碼，#174）；其他碼 `last_error_detail:null` |
| `disk_full_blocks_login_probe_but_not_agent_cmd`（Review Focus 3） | READY 後 WAL `fail_next_writes(∞, ENOSPC)` 觸發 unhealthy → `SessionAttach` 的 probe 被 fence `WalUnhealthy` 拒絕（`write_calls` 不增、`last_error:"PROMPT_UNAVAILABLE"`）；同時 agent `echo x` 照常寫出、TX WAL 回 `loss_flag:true` |
| `passthrough_no_ready_probe_no_tx` | others-template：attach → `ATTACHED`、`last_error:null`、`write_calls()==0`；`choose_login_plan` 回 `LoginPlan::None` |
| `uboot_template_no_probe_quiet_ready`（取代 `uboot_template_probes_and_can_ready`；SC-35） | passthrough＋ready_probe 預設（`echo __READY__${nonce}`）：attach → `ATTACHED`、`write_calls()==0`（不送 `\n`、不送 nonce 探針）；無 RX 推進 30 s → Tick 轉 `READY`，整段 `write_calls()==0` |
| `passthrough_banner_does_not_arm_quiet`（SC-35） | passthrough（ATTACHED 與 READY 各一次）收到 `U-Boot` banner RX → `on_rx_text` 不 arm（fence `quiet_active==false`、`reconfirm_pending==false`、`boot_quiet_remaining_s` null）；同一 RX 只重設 passthrough 安靜視窗 |
| `unsupported_profile_no_auto_tx` | `post_login_cmd:"sh"` 的 explicit target → `ATTACHED`、`last_error:null`、`write_calls()==0`、stderr 一次 `slice-2.1 不支援的 profile 能力（brcm-template: post_login_cmd）；不自動登入`；Submit → `SESSION_NOT_READY` |
| `parse_env_file_rules` | `"export A='x'\n# c\nB = \"y\"\n=z\nC\n"` → `{A:"x", B:"y"}` |
| `auth_reasons` | env_file 缺 → `env_file_missing`；不可讀 → `env_file_unreadable`；檔在但缺 key → `key_absent`；未宣告 → `not_configured`；env 有值 → `ok` |
| `attach_and_recover_reset_credentials_warned` | 補上 env 後 `SessionAttach` → 重新解析並登入成功 `READY`；再次失敗時再告警一次 |
| `valid_completion_regex_rules` | `None`→true；`""`→false；>1024 bytes→false；`"(?"`→false；`"x*"`（可匹配空串）→false；`"END$"`→true |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-core --features test-fakes --test login_quiet`
Expected：FAIL，`not implemented: slice21 T13 QuietState::arm`。

- [ ] **Step 3：實作**

- `on_rx_text`（05 §7 arm／clear 點；`platform == "passthrough"` 的 session 不呼叫，只更新 `quiet_since`，SC-35）：`window = tail[-256 chars:] + chunk`；`detect_boot_banner(window)` → `arm(now)`、回 `Armed`；否則 `tail = window[-256:]`；quiet 啟動中：尾行命中 `profile.bootloader_prompts` → `None`；`login_regex` 或 `prompt_regex` 對 tail `search` 命中 → `clear_quiet()` 回 `Cleared`。actor 在 `Armed` 時 `fsm_gen += 1`、`owner_gen += 1`、fence `quiet_active=true`、`reconfirm_pending=true`、排 `BootQuietExpired`；`Cleared` 時 fence `quiet_active=false`（`reconfirm_pending` 保留）；`confirm_ready` 時兩者皆 false。
- `choose_login_plan`（05 §8.2＋`_attach_by_id`）：`platform == "passthrough"` → `None`（last_error None，不論 ready_probe 是否為空；不送任何 probe／login TX，SC-35）；unsupported → `None`；`!command_capable` → `None`（last_error None）；`quiet_blocks` → `None`＋合成 `PROMPT_UNAVAILABLE`；`require_login = pending_auto_login || login_regex 非空 || console_unlock_env 非空`：declared-but-unresolved → `CredentialsUnresolved`；`username && password` → `EnsureReady`；否則 `Probe`；無 require_login → `Probe`。
- login 在 OpRunner thread 執行，TX 經 `TxWriter`（class `LoginProbe`；secret `log=false`），結果以 `LoginFinished{epoch, fsm_gen, result, after_rx_seq}` 回 actor。`EnsureReady` 失敗 → `begin_stop(LoginFailed)`（05 T5）；`Probe` 失敗 → `ATTACHED`＋last_error（T4）。
- 失敗碼由 `refine_probe_failure`（照 `session_manager._refine_probe_failure`）決定：先 `LOGIN_REQUIRED` 判定，再 `FLOOD_MASKABLE_ERRORS` 的 RX_FLOOD 遮蔽，最後 `LOGIN_FSM_DETAIL_ERRORS` 補 `last_error_detail`。
- 每步逾時 `profile.template.timeout_s`；`_maybe_login`／`_finalize_ready` 照 05 §8.3（不含 `post_login_cmd`、console unlock）；nonce = `uuid4_hex(rng)[..8]`；pattern 為**裸 nonce 字串**（`pyre` 以 escape 後的字面量）。
- `CredentialsUnresolved`：`Effect::WalMeta{meta:{"event":"credentials_unresolved","reason":<AuthResolution.reason>,"env_file":<env_file_path 或 "">}}`、`Effect::Warn("session <sid> 帳密解析未完成（reason=<r>, env_file=<p>）：不對 login prompt 送空帳密，標記 CREDENTIALS_UNRESOLVED 終態，請補齊帳密後手動 attach/recover")`，以 `credentials_warned` 去重。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-core --features test-fakes --test login_quiet`
Expected：PASS（27 項：3 個程式碼案例＋24 列；含 SC-35 的 `uboot_template_no_probe_quiet_ready`、`passthrough_banner_does_not_arm_quiet`）。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-core changelog.d/rust-s21-login-quiet.md
git commit -m "feat(sw-core): 最小登入、帳密未解析終態與 boot quiet gate" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 14：sw-core 命令執行、agent Interactive Lease、self-test、capture

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-command-exec.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust 命令執行：line／background／completion_regex、逾時後 CTRL_C／CTRL_D 救援並清空佇列、reboot 命令進 RECOVERING 與重開機恢復、agent interactive lease（1 s tick 主動到期，SC-13）、self-test 分類與 background／agent log capture；passthrough 命令依 completion_regex／可用 prompt／timeout 三規則完成，未命中不送 CTRL_C／CTRL_D、不降級，self-test 零 TX（SC-35）；recover REPROBE 未恢復回 SESSION_NOT_READY＋頂層 last_error（SC-34）。」）
**BLOCKED-OWNER：** 無。

**Files:**
- Modify: `crates/sw-core/src/{op_runner.rs,lease.rs,self_test.rs,capture.rs,session_core.rs}`
- Test: `crates/sw-core/tests/command_exec.rs`、`crates/sw-core/tests/lease_selftest.rs`

**Interfaces:**
- Consumes：Tasks 8～13；`sw_proto::pycompat::{clean_text, splitlines, a2b_base64_lenient, uuid4_dashed, strftime_local}`、`sw_pal::FsOps`（log 檔 append）。
- Produces：

```rust
// op_runner.rs
pub enum OpSpec { Command(QueuedCommand), Recover { timeout_s: f64, force: bool }, SelfTestProbe { timeout_s: f64, probe: String, nonce: String },
                  InteractiveOpenCmd { command: String, owner: String }, InteractiveSend { payload: Vec<u8>, owner: String },
                  RebootRecovery { hard_timeout_s: f64 }, DetectTemplate { templates: Vec<ProfileTemplate> } }
pub enum OpOutcome { CommandResult(PyDict), SendFailed, Recovered { key: &'static str, stdout: String }, PromptTimeout { stdout: String },
                     ToAttached { reason: &'static str }, RebootExpected, RebootRecoveryDone(LoginOutcome), SelfTest(PyDict),
                     SelfTestRefused(FenceReject) /* R5-F2：nonce probe 的 TxReport.reject == Some(Quiet)；不等 nonce，由 actor 組 AUTOBOOT_QUIET 回應 */,
                     Sent { bytes: usize }, Detected(Option<String>) }
pub struct OpRunner { /* thread pool size 1 per session（ForegroundSlot） */ }
impl OpRunner { pub fn spawn(ctx: OpCtx) -> OpRunner; pub fn run(&self, grant: OpGrant, spec: OpSpec); }
pub struct OpCtx { pub com: String, pub sid: String, pub profile: SessionProfile, pub rx: Arc<RxShared>, pub tx: TxWriterHandle,
                   pub wal: Arc<WalWriter>, pub clock: Arc<dyn Clock>, pub control: crossbeam_channel::Sender<ControlEvent>, pub rng: Arc<dyn Fn(&mut [u8]) + Send + Sync> }
pub fn extract_command_stdout(raw: &str, command: &str, prompt_regex: &PyRegex, completion: Option<&PyRegex>) -> String;  // 05 §5.2
pub fn encode_interactive_payload(data: &str, encoding: &str) -> Result<Vec<u8>, PyErr>;

// lease.rs
pub struct InteractiveLease { pub interactive_id: String, pub owner: String, pub created_at: String, pub timeout_s: f64, pub last_activity: Mono, pub status: &'static str, pub recovery_mode: bool }
impl InteractiveLease { pub fn touch(&mut self, now: Mono); pub fn expired(&self, now: Mono) -> bool; /* now - last > timeout（嚴格大於） */ }
pub struct Tombstones { /* VecDeque<(String id, &'static str status)> cap 16 */ }

// capture.rs
pub struct BackgroundCapture { /* 05 §1.1 欄位 */ }
impl BackgroundCapture { pub fn add_chunk(&mut self, chunk: &str, max_chars: usize); pub fn maybe_finalize(&mut self, now: Mono); pub fn result(&mut self, from_chunk: i128, limit: i128, now: Mono) -> Result<PyDict, PyErr>; }
pub struct AgentLogCapture { /* capture_id(uuid 含 -)、log_path、started_at、line_count、byte_count、status */ }

// self_test.rs
pub fn classify_pre_probe(core: &SessionCore, ctx: &SelfTestCtx) -> Result<PyDict, NeedProbe>;  // 05 §11 順序 0–15
```

- [ ] **Step 1：寫失敗測試**

```rust
#![cfg(feature = "test-fakes")]
mod support;
use std::time::Duration;
use support::*;
use sw_core::session_core::*;
use sw_proto::pyjson::PyValue;

#[test]
fn line_command_stdout_extraction_and_done() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"echo hi\n", b"echo hi\r\nhi\r\nroot@t:/# ");     // FakeDevice 腳本：收到指定 TX 後推 RX
    let id = h.submit_ok("echo hi");
    let rec = h.wait_done(&id);
    assert_eq!((rec["status"].clone(), rec["stdout"].clone(), rec["partial"].clone()), (PyValue::from("done"), PyValue::from("hi"), PyValue::Bool(false)));
}

#[test]
fn timeout_sends_ctrl_c_then_recovers_and_flushes_queue() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.on_tx_reply(b"\x03", b"^C\r\nroot@t:/# ");
    let slow = h.submit_with("sleep 20", |r| r.timeout_s = 1.0);
    let queued = h.submit_ok("echo later");
    h.clock.advance(Duration::from_millis(1100));
    let rec = h.wait_done(&slow);
    assert_eq!((rec["status"].clone(), rec["error_code"].clone(), rec["recovery_action"].clone()),
               (PyValue::from("done"), PyValue::Null, PyValue::from("CTRL_C")));       // ok:true 的碼不入紀錄
    assert_eq!(h.command_get(&queued)["error_code"], PyValue::from("FLUSHED_BY_RECOVERY"));
    assert_eq!(h.get_state()["state"], PyValue::from("READY"));
    assert!(h.wal_tx_payloads().contains(&b"\x03".to_vec()));
}

#[test]
fn submit_during_agent_lease_is_rejected_at_execute_time() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    let open = h.request(SessionRequest::InteractiveOpen { owner: "agent:f4".into(), timeout_s: 20.0, command: String::new(), allow_attached: false });
    let iid = open["interactive_id"].as_str().unwrap().to_string();
    let id = h.submit_ok("echo during_lease");
    let rec = h.wait_done(&id);
    assert_eq!(rec["error_code"], PyValue::from("SESSION_INTERACTIVE_BUSY"));        // f4-interactive-line-cmd-defined 合法終態 (a)
    let close = h.request(SessionRequest::InteractiveClose { interactive_id: iid });
    assert_eq!(close["ok"], PyValue::Bool(true));
}
```

（`FakeDevice::on_tx_reply`／`on_tx_match` 由 Task 1 提供；`ActorRig::{submit_ok, submit_with, wait_running, wait_done}` 已於 Task 13 Step 0 宣告，本 Task 實作命令執行後即可用。）

`command_exec.rs` 其他：

| 測試名 | 精確斷言 |
|---|---|
| `silence_breaks_wait_loop` | 命令送出後 `min(timeout,30)` 內完全無 RX → 視為靜默結束 → 進救援流程（05 §5.2 step 7） |
| `rx_keeps_waiting_until_timeout` | 有持續 RX 但無 prompt → 等到 `max(timeout_s, expected_duration_s)` 才救援 |
| `ctrl_d_after_ctrl_c_fails` | `\x03` 無回應、`\x04` 後 prompt → `recovery_action:"CTRL_D"` |
| `both_keys_fail_prompt_timeout_to_attached` | 皆無 prompt → 紀錄 `error_code:"PROMPT_TIMEOUT"`、`partial:true`；session `ATTACHED`、`last_error:"PROMPT_TIMEOUT"`、佇列 `FLUSHED_BY_RECOVERY` |
| `quiet_rechecked_before_each_recover_key` | `\x03` 送出後 RX 出現 banner → `\x04` 不送（fence `Quiet` 拒絕，`write_calls` 不增） |
| `completion_regex_meta_and_extraction` | `completion_regex:"^RC=0$"` → WAL META `{"event":"command_completion","completion_regex":"^RC=0$"}`（cmd_id 帶入）；stdout 保留完成標記、裁掉其後 prompt |
| `echo_line_removed_repeatedly` | raw `"ls\r\nls\r\na b\r\n# "` → stdout `"a b"` |
| `background_capture_lifecycle` | mode `bg` → 回 `execution_mode:"background"`、`background_capture_id==cmd_id`；`result_tail` 欄位 `cmd_id,status,error_code,from_seq,last_seq,from_chunk,next_chunk,dropped_chunks,lost,chunks` 依序；quiet_window 2.0 s 後 `maybe_finalize` → `done` |
| `background_capture_truncates_head_keeps_one` | 單 chunk 超過 4194304 字元仍保留該段；`dropped_chunks` 累計、`lost` 依 `from_chunk < dropped_chunks` |
| `bg_capture_count_evicts_finished_over_64` | 第 65 個 capture 建立時淘汰最舊已結束者 |
| `result_tail_fallback_from_registry` | capture 不存在且紀錄為 accepted background → `{"ok":true,"cmd_id","status":"accepted","error_code":null,"from_seq":0,"last_seq":0,"from_chunk":k,"next_chunk":k,"chunks":[]}`（無 `dropped_chunks`） |
| `result_tail_negative_limit_exception` | `islice` 語意：`start = max(from_chunk - dropped_chunks, 0)`，`start + limit < 0` 時 `Err(PyErr)`（例：`from_chunk=0, limit=-1`）；`from_chunk=3, limit=-1`（start 3、stop 2）→ `ok:true`、`chunks:[]`、`next_chunk==3` |
| `reboot_agent_enters_recovering_expect_reboot` | `reboot` 後 2 s 內無 prompt → 回 `{"ok":true,"execution_mode":"line","stdout":"","partial":true,"status":"recovering","recovery_action":"EXPECT_REBOOT"}`、state `RECOVERING`、`pending_auto_login:true` |
| `reboot_recovery_waits_quiet_then_login_ready` | RECOVERING 中 RX 開機訊息、quiet 期間不送 probe；quiet 清除後 `ensure_ready` 成功 → `READY` |
| `reboot_recovery_timeout_to_attached` | 硬上限（`deadline_cap = start+180+timeout`）到期 → `ATTACHED`、`last_error:"RECOVERY_TIMEOUT"` |
| `recover_ready_path_returns_prompt_timeout_recovered` | `Recover{timeout_s:2.0, force:false}` 於 READY → `{"ok":true,"error_code":"PROMPT_TIMEOUT_RECOVERED","stdout":…,"partial":true,"recovery_action":"CTRL_C"}` |
| `recover_force_timeout_after_10s` | force 且 10 次×1 s 未 READY → `{"ok":false,"error_code":"FORCE_RECOVER_TIMEOUT","action":"FORCE_CLEAR_REATTACH","recovered":false,"session":…}` |
| `recover_released` | RELEASED → `{"ok":true,"released":true,"recommended_action":"device_attach","session":…}` |
| `recover_no_bridge_reattach` | 無 bridge 裝置在 → `{"ok":true,"recovering":false,"action":"REATTACH","session":…}` |
| `recover_attached_reprobe_not_recovered_last_error_field`（取代 `recover_attached_reprobe_not_recovered_passthrough_error_code`；SC-34） | shell ATTACHED probe 失敗（`last_error:"READY_NONCE_TIMEOUT"`）→ 回應 key 依序 `["ok","recovering","action","recovered","session","error_code","last_error","last_error_detail"]`；`ok:true`、`recovered:false`、`error_code:"SESSION_NOT_READY"`（**不得**等於 `"READY_NONCE_TIMEOUT"`）、`last_error:"READY_NONCE_TIMEOUT"`、`last_error_detail` 等於 `session.last_error_detail`；`last_error` 為 None 的情境 → `last_error:null`、`last_error_detail:null` |
| `recover_attached_reprobe_bootloader_classification` | ATTACHED reprobe 失敗且 RX tail 尾行命中 bootloader prompt，或 `last_error == "BOOTLOADER_STUCK"`（SC-34 起看 `last_error`，不看 `error_code`）→ `error_code:"SESSION_NOT_READY"`，其後依序另含 `classification:"BOOTLOADER"`、`recommended_action:"recover_interactive"`、命中時 `matched_prompt`（`session_manager.py` L4651） |
| `recover_ready_path_force_fallbacks` | READY `Recover{force:true}`：救援鍵後 prompt 回來 → `PROMPT_TIMEOUT_FORCE_RECOVERED`；10 次未 READY → `FORCE_CLEAR_REATTACH` 路徑（L3921 附近逐字對照） |
| `reboot_prompt_returns_quickly_ok_not_partial` | `reboot` 後 `min(timeout, 2 s)` 內 prompt 回來 → `{"ok":true,"partial":false,…}`、quiet 清除但 `ready_reconfirm_pending:true` 保留（#162） |
| `reboot_flushes_commands_queued_behind_it` | `reboot` 後排隊的 2 筆 → `FLUSHED_BY_RECOVERY`，queue 解除註冊（`queue_registered:false`）直到重新 READY |
| `recover_keys_wal_source_system_recover` | 救援送出的 `\x03`／`\x04` 的 TX WAL `source == "system:recover"`；ensure_ready 的密碼 TX 為 `source:"system:secret"` 且 `log=false`（WAL 無該行）；self-test probe TX 為 `source:"system:self_test"` |
| `passthrough_completion_regex_producer`（SC-35 規則 1） | `OTHERS_PASSTHROUGH_PROFILE`（others-template，`prompt_regex: ".*"`）安靜 30 s 進 READY；submit `cmd:"run"`、`completion_regex:"(?m)^__RC__=0\\r*$"`；FakeDevice 回 `b"run\r\nout\r\n__RC__=0\r\n"` | 紀錄 `status:"done"`、`partial:false`、`stdout` 為 `clean_text(pre_offset 起至完成標記結尾的 RX)`（含 `__RC__=0`，不經 prompt 裁切）；`write_calls` 只有 `b"run\n"` |
| `passthrough_usable_prompt_completion`（SC-35 規則 2） | `UBOOT_PASSTHROUGH_PROFILE`（`prompt_regex: "(?mi)^(=>|u-boot>|cfe>) $"`）安靜 30 s 進 READY；submit `printenv`，回 `b"printenv\r\nbootcmd=x\r\n=> "` | `status:"done"`、`partial:false`、`stdout:"bootcmd=x"`（既有 prompt 完成與 stdout 擷取） |
| `passthrough_unusable_prompt_timeout_no_recovery`（SC-35 規則 3） | `OTHERS_PASSTHROUGH_PROFILE` READY、submit `ls`（`timeout_s:1.0`，無 completion_regex）；FakeDevice 回 `b"ls\r\nfoo\r\n"` 後靜默；advance 1.1 s | 紀錄 `error_code:"PROMPT_TIMEOUT"`、`partial:true`、`recovery_action:"NONE"`，capture chunks＝`["ls\nfoo"]` 的 `clean_text` 結果；`write_calls()` 只含 `b"ls\n"`——**不含** `b"\x03"`、`b"\x04"`；state 仍 `READY`、`last_error:null`；之後排隊命令照常執行（不 `FLUSHED_BY_RECOVERY`）；無 force recover |
| `passthrough_reboot_is_plain_line`（SC-35） | passthrough READY submit `reboot` | 不 arm quiet、不進 `RECOVERING`、無 `EXPECT_REBOOT`；依規則 1～3 完成（others-template → `PROMPT_TIMEOUT`、`recovery_action:"NONE"`），state 仍 `READY` |
| `passthrough_recover_no_probe`（SC-35＋SC-34） | passthrough ATTACHED（安靜 10 s）`Recover{timeout_s:2.0, force:false}` | `write_calls()==0`；回 `{"ok":true,"recovering":false,"action":"REPROBE","recovered":false,"session":…,"error_code":"SESSION_NOT_READY","last_error":null,"last_error_detail":null}`；READY 時同請求 → `recovered:true`、無 `error_code` 鍵、零 TX |
| `tx_ledger_bounded_over_long_session_without_reconnect`（R1-F8；`ActorRig` 新增 accessor `tx_ledger_gauge() -> TxLedgerGauge`，讀現行 worker 的 `tx().ledger_gauge()`） | `ActorRig::new(SHELL).ready()` 後同一 epoch 依序 `submit_ok`＋`wait_done` 700 筆 `echo i`（> history 512），其間穿插 20 次 `InteractiveOpen`／`InteractiveSend`／`InteractiveClose`、5 次 `SelfTest`、1 次 `\x03` 救援路徑（`recover_keys_wal_source_system_recover` 的觸發法）→ 全程 `conn_epoch` 不變（未重連）；結束後 `wait` 至 `tx_ledger_gauge().live_ops == 0`（2 s 內），且全程取樣的 `live_ops` 最大值 ≤ 2、`saturated == false`、`settle_dropped == 0`——任一終態處理點漏呼叫 `settle_tx` 都會使本測試失敗 |

`lease_selftest.rs`：

| 測試名 | 精確斷言 |
|---|---|
| `open_ready_shape` | `{"ok":true,"interactive_id":<32 hex>,"session":…,"recovery_mode":false}`；`command` 非空時以 `InteractiveOpenCmd` class 送出（quiet 中也送） |
| `open_busy_foreground` | 前景忙 → `{"ok":false,"error_code":"SESSION_BUSY","selector":"COM0"}` |
| `open_existing_agent_lease` | `{"ok":false,"error_code":"SESSION_INTERACTIVE_BUSY","interactive_session_id":…}` |
| `open_attached_rescue_banner` | ATTACHED＋`allow_attached`、RX tail 含 `Hit any key to stop autoboot` → `recovery_mode:true`、`boot_interrupt:true`；timeout 夾到 `min(t,120)` |
| `open_attached_not_bootloader` | → `{"ok":false,"error_code":"SESSION_NOT_READY","selector":…,"error_detail":"NOT_BOOTLOADER"}` |
| `open_attached_rescue_three_arms` | ATTACHED＋`allow_attached` 三種 RX tail：bootloader prompt 命中 → `recovery_mode:true` 且**無** `boot_interrupt`／`login_required` 鍵；banner → `boot_interrupt:true`；login／password prompt → `login_required:true`（#174，CREDENTIALS_UNRESOLVED session 的救回路徑） |
| `lease_send_after_close_is_zero_tx` | lease close 後以舊 `interactive_id` `InteractiveSend` → `INTERACTIVE_NOT_FOUND`；已排入 TxWriter 但未寫出的舊 `LeaseOwner` job 被 fence `OwnerMismatch` 拒絕（`write_calls` 不增，§11.3 不回放失效上下文） |
| `lease_send_after_expiry_or_quiet_is_zero_tx` | lease 到期或 banner 觸發 quiet re-arm 後，先前已排入的 `LeaseOwner` job → `OwnerMismatch`，零 TX |
| `rescue_lease_send_during_quiet_reaches_uart`（R2-F2，規格 §5.2.1「有效 Lease 可中斷 autoboot」） | ATTACHED、RX `Hit any key to stop autoboot:  3` 觸發 quiet（`fence.quiet_active == true`）；`InteractiveOpen{allow_attached:true}` → `recovery_mode:true`、`boot_interrupt:true`；之後 `InteractiveSend{data:"ctrl-c",encoding:"key"}`（permit 於 send 當下以現行 `owner_gen` 建構）→ `{"ok":true,"interactive_id":…,"bytes":1}`、`write_calls() == [b"\x03"]`、TX WAL 恰 1 行 `source == "agent:…"`；送出後 `quiet_active` 仍為 true。對照：另一 rig 為 READY 且 banner 已觸發 quiet、同時持有效 agent lease——`CmdSubmit` → `AUTOBOOT_QUIET`＋`retry_after_s`、`write_calls()` 不增，而該 lease 的 `InteractiveSend` 照常寫出 1 次；lease 到期後同一 `InteractiveSend` → `INTERACTIVE_EXPIRED`、`write_calls()` 不增 |
| `send_encodings` | `plain` UTF-8 replace；`base64` 非 strict（`"aGk=\n!"` → `b"hi"`）；`key` 的 9 個鍵值；未知 → `Err(PyErr("INVALID_INTERACTIVE_ENCODING"))` |
| `send_expired_closes` | `timeout_s=1` advance 1.001 s → `{"ok":false,"error_code":"INTERACTIVE_EXPIRED","interactive_id":…}`；之後 `INTERACTIVE_NOT_FOUND` |
| `status_touches_and_returns_screen` | `status` 延長期限；回 `{"ok":true,"interactive_id","owner","status":"active","recovery_mode":false,"screen":<clean_text(tail(2048))>,"session"}` |
| `tick_expires_lease_sc13` | 無存取 advance 超過 timeout → 1 s tick 後 lease 移除並留 tombstone；`status` 回 `INTERACTIVE_NOT_FOUND` |
| `lease_expiry_checks_current_deadline` | 排定的 `LeaseExpiry{deadline: 舊}` 在 touch 之後到達 → 不關閉 |
| `selftest_order_table` | 依序建構 RELEASED／recovering／DEVICE_MISSING／BRIDGE_DOWN／PASSTHROUGH／LOGIN_REQUIRED／BOOTLOADER（含 fallback prompts）／RX_FLOOD／ATTACHED_NOT_READY／AUTOBOOT_QUIET → 各自 `classification` 與 `recommended_action` 照 05 §11；所有分支含 `command_capable` 與 4 個 LeaseContext 鍵 |
| `selftest_passthrough_zero_tx`（SC-35） | passthrough READY → `classification:"PASSTHROUGH"`、`recommended_action:"none"`、無 `probe_ok`；passthrough ATTACHED（安靜 12 s）→ 既有 PASSTHROUGH 分支（`recommended_action:"console_attach"`，fallback 且 RX 命中時 `pin_profile`＋`suggested_profile`）並含 `passthrough_ready_in_s: 18.0`；兩者 `write_calls()==0`（即使 profile 的 ready_probe 非空） |
| `selftest_ok_confirms_ready` | READY＋pending：nonce 回 → `classification:"OK"`、`probe_ok:true`、`ready_reconfirm_pending:false` |
| `selftest_quiet_armed_after_precheck_before_submit_zero_tx`（R5-F2，DF-19；「pre-check 後才擋或只在出列擋」的實作必紅） | `ActorRig::new(SHELL_PROFILE).ready()`，前置 `fence.quiet_active == false`；`h.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit])`；送 `SessionRequest::SelfTest{timeout_s:2.0, strict_human_lock:false}`（pre-check 通過、`StartOp(SelfTestProbe)`）；`assert!(h.dev.wait_paused(2s))`（TxWriter 出列快速檢查已過、尚未進 `FenceSubmitGate`）；推 RX `b"U-Boot 2024.04 (Oct 01 2024)\r\n"` 並等 actor 處理（`fence.quiet_active == true`，此步不得阻塞）；`h.dev.release_pause()` → 不推進 FakeClock 即收到回覆（不等 2 s nonce）；回覆鍵序 `["ok","classification","session","recommended_action",<lease ctx 4 鍵>,"command_capable"]`（與 `selftest_order_table` 的 AUTOBOOT_QUIET 列相同）且 `classification == "AUTOBOOT_QUIET"`、`recommended_action == "wait"`、無 `probe_ok`；probe job 已進 PAL（`h.dev.write_calls() == 1`）但被 `FenceSubmitGate` 以 `Quiet` 拒絕：`h.dev.os_submits() == 0`、`h.dev.take_tx().is_empty()`、WAL 無 `source == "system:self_test"` 的 TX；`ready_reconfirm_pending == true`（未 `confirm_ready`）、state 仍 `READY`；之後立即再送 `SelfTest` → 走 pre-check 分支，回覆與前者鍵序及 `ok`／`classification`／`recommended_action` 相同、`os_submits()` 仍為 0 |
| `selftest_quiet_armed_during_submit_counts_committed`（R5-F2，線性化另一側） | 同上，但 `FakeWriteStep::PauseInsideSubmit`（持 Fence 鎖）；另一 thread 送 banner RX，斷言 actor 的 arm 在 0.2 s 內未完成（等 Fence 鎖）；放行 → probe bytes 恰 1 次 OS 提交、TX WAL 1 行 `source == "system:self_test"`；advance 2 s → `TARGET_UNRESPONSIVE`、`probe_ok:false`；`quiet_active == true` |
| `selftest_probe_other_reject_zero_tx_matches_python`（R5 審查後續） | 對 `Frozen`（drain 起始）、`StaleEpoch`（release 後）、`Cancelled`、`WalUnhealthy` 各一案：pre-check 通過後、OS 提交前注入該狀態 → `h.dev.writes()` 無 probe bytes、`clock().elapsed() < timeout_s`（不等 nonce）、`session.ready_reconfirm_*` 不變；回應 `error_code` 逐案等於 `contracts/golden/selftest_refused.json` 的 Python 值；`selftest_probe_drain_race_golden`：drain 與 probe 提交競態 1000 次，結果只有「probe 已提交且計入 committed（回 nonce 結果）」或「零 TX＋golden 碼」兩種 |
| `selftest_during_reconfirm_pending_not_fenced`（R5-F2，回歸防護） | READY＋`reconfirm_pending=true`、`quiet_active=false` → `SelfTest` 的 probe 寫出 1 次、nonce 回 → `OK`；同狀態 `CmdSubmit` → `READY_UNCONFIRMED`、`write_calls()` 只多 probe 那 1 次 |
| `selftest_unresponsive_counts_attempt` | READY＋pending、nonce 不回 → `TARGET_UNRESPONSIVE`、`ready_reconfirm_attempts` +1 |
| `selftest_released_external_holder` | RELEASED、holder pids [] 且 `holder_unknown:true` → `external_holder:"none"`、`reclaimable:false`、`recommended_action:"wait_external_flash"`（BC-04：不明不得當安全證明） |
| `selftest_device_rebound_required` | by-id 現在指向的 realpath ≠ `attached_real_path` → `classification:"DEVICE_REBOUND_REQUIRED"`（L4366） |
| `selftest_vtty_stale` | 哨兵 PTY slave 已不存在 → `classification:"VTTY_STALE"`（L4396） |
| `log_capture_start_stop_status` | `LogStart` → 檔名 `{log_dir}/COM0_{%y%m%d-%H%M%S 本地}.log`、`capture_id` 含 `-`；前景執行中 RX 不寫入 log（EVT-D-L1）；`LogStop` 回 `line_count`、`byte_count`、`started_at`；重複 start → `already_active:true` |
| `log_capture_open_failure` | log_dir 存在但檔案無法建立 → `{"ok":false,"error_code":"LOG_OPEN_FAILED","detail":…}` |
| `log_capture_dir_not_creatable_is_exception` | log_dir 的父目錄不可寫（`makedirs` 失敗，Python 在 try 之外）→ `Err(PyErr)`（framing 轉 `EXCEPTION`），不是 `LOG_OPEN_FAILED` |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-core --features test-fakes --test command_exec --test lease_selftest`
Expected：FAIL，`not implemented: slice21 T14 OpRunner::spawn`。

- [ ] **Step 3：實作**

- 出列（第二檢查點）：前景空閒時 `queue.pop()` → `registry.mark_running` → 依 05 §5.1 execute-time 順序（completion regex → mode 正規化 → `FLASHING_BUSY` → state≠READY `SESSION_NOT_READY` → gate（含 SC-03 `retry_after_s`）→ `recovering` → `SESSION_RECOVERING` → interactive 且前景忙 → `SESSION_BUSY` → 有 lease 且 mode≠interactive → `SESSION_INTERACTIVE_BUSY`（2.1 無 human lease，故不 suspend））→ 不通過直接 `registry.complete(Result(err_dict))`；通過 → `StartOp{grant, Command}`、`registry.set_in_flight`。
- 命令執行（OpRunner）：照 05 §5.2 line／background／interactive 流程；TX 以 `TxPermit{class: Command, log: true, cmd_id}`；送出前 `Effect` 已更新 `last_tx_*`（`_mark_session_tx` 等價，經 `OpFinished` 前的 `OpStarted` 事件無需額外 RPC：actor 在 `StartOp` 當下更新）；完成以 `OpFinished{outcome}` 回 actor，actor `registry.complete` 並出列下一筆。
- Passthrough 命令路徑（SC-35，arch §5.12）：execute-time gate 對 `platform == "passthrough"` 略過 quiet／reconfirm 兩項；完成條件依序：(1) `completion_regex` 有值 → #230 producer 完成；(2) 否則 `valid_completion_regex(Some(prompt_regex))` 為真 → 既有 prompt 完成；(3) 否則無完成條件，沿用既有等待迴圈（有 RX 就續等、連續 `min(timeout_s, 30)` 秒無新 RX 或 effective timeout 到即停）並回 `{"ok":false,"error_code":"PROMPT_TIMEOUT","partial":true,"recovery_action":"NONE"}`、capture chunks＝`clean_text` 後 RX。passthrough 未命中時一律**不**進下方救援（不送 CTRL_C／CTRL_D、不轉 ATTACHED、不 force recover），session 維持 READY、佇列不 flush；規則 1 命中而 prompt 不可用時 stdout＝`clean_text(pre_offset 起至完成標記結尾)`；`is_reboot_command` 特例不套用；interactive 模式不變。self-test 對 passthrough 不送 nonce probe；recover 的 REPROBE 對 passthrough 不 probe。
- 救援 `_recover_after_failure`（05 §5.3，人類來源分支不適用，DF-02；passthrough 不進入，SC-35）：`\x03`、`\x04` 各以 `SystemRecoverKey` class、`source:"system:recover"` 送出、等 `min(timeout_s, 2.0)`；fence 在每次寫入前重驗 quiet（取代 Python 的逐 byte 鎖內檢查）。密碼 TX 為 `source:"system:secret"`、`log=false`；self-test probe 為 `source:"system:self_test"`。
- reboot：`is_reboot_command` 為真時 actor 在出列時 `quiet.arm(now)`（core 狀態，admission 立即拒絕後續命令），TX 完成後的 `OpFinished` 才設 fence `quiet_active=true`（同一前景槽內不會有其他 Command TX 交錯）。
- Lease 1 s tick（SC-13）：`Tick` 時 `expired(now)` → `status="expired"`、移入 tombstone（容量 16，FIFO）；`LeaseExpiry` timer 只在 `deadline == lease.last_activity + timeout` 時生效。
- self-test 依 05 §11 順序；nonce probe 以 `SelfTestProbe` class；`/proc` holder 掃描在 actor 外的 executor thread 進行，再以 `OpFinished(SelfTest)` 回覆。
- self-test nonce probe 的提交點重驗（R5-F2，DF-19）：actor 在同一次 `on_event` 內完成 `classify_pre_probe`（含 AUTOBOOT_QUIET 分支）並發出 `StartOp(SelfTestProbe)`，permit 帶當下 `epoch`（`fsm_gen` 不參與比對）；pre-check 之後 RX banner 觸發的 arm 在同一次 `Fence::with` 內設 `quiet_active=true`，TxWriter 出列檢查或 `FenceSubmitGate` 以 `Quiet` 拒絕、零 TX。OpRunner 收到 probe 的 `TxReport.reject == Some(FenceReject::Quiet)` 時不呼叫 nonce 等待，直接回 `OpFinished(SelfTestRefused(Quiet))`；actor 以 `classify_pre_probe` 的 AUTOBOOT_QUIET 分支同一組裝函式產生回應（鍵序相同、`session` 取處理當下的 public dict、無 `probe_ok`），不 `confirm_ready`、不動 `ready_reconfirm_*`，human suspend 照常在 `OpFinished` 結束。`reject == Some(Flash)` 維持既有語意：照等 nonce、逾時回 `TARGET_UNRESPONSIVE`（RPC-H-07 KEEP）；其餘 reject（`Frozen`／`StaleEpoch`／`Cancelled`／`WalUnhealthy`／`OwnerMismatch`，R5 審查後續，原寫「不在本條範圍」而與 Python 例外穿出路徑形成雙軌差異）：同樣零 TX（gate 已擋）、**不**等 nonce、不 `confirm_ready`、不動 `ready_reconfirm_*`，OpRunner 回 `OpFinished(SelfTestRefused(r))`，actor 以 `rpc_error_for_fence_reject(r)` 產生與 Python 例外穿出 manager 時相同的 RPC 錯誤（02 T20 第 6 點「其他拒絕原因照 T06／T07／T05 既有路徑」：drain／frozen → 該路徑的碼、stale／cancelled → T07 的碼、WAL unhealthy → T05 `WalUnavailableError` 的碼；對映表與 Python 共用 `contracts/golden/selftest_refused.json`，由 P1 T20 以 Python 實跑產生）；human suspend 照常在 `OpFinished` 結束。Python 對應 02 T20 行為規格第 6 點（`self_test_probe`）。
- Agent log capture：檔案以 `FsOps` append 開啟，RX 經 `clean_text` 寫入並每 chunk flush；`byte_count += utf8 長度`、`line_count += '\n' 數`。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-core --features test-fakes --test command_exec --test lease_selftest`
Expected：PASS。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-core changelog.d/rust-s21-command-exec.md
git commit -m "feat(sw-core): 命令執行與救援、agent lease、self-test 與 capture" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 12b：Drain／Release 端到端（真 actor＋login＋命令執行）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** 併入 `changelog.d/rust-s21-drain-release.md`（同一 PR，無新增 fragment）。
**BLOCKED-OWNER：** 同 Task 12。

執行順序在 Task 14 之後（`ActorRig::ready`／`submit_ok`／`wait_running` 需要 Task 13 login 與 Task 14 命令執行）。本 Task 原則上只新增測試；若端到端案例揭露 Task 9～14 的缺陷，修正落在對應模組並在該 Task 的測試檔補重現案例。

**Files:**
- Test: `crates/sw-core/tests/drain_release_e2e.rs`
- Modify（只在發現差異時）：`crates/sw-core/src/{drain.rs,session_core.rs,uart_bridge.rs,actor.rs,tx_writer.rs,mailbox.rs}`

**Interfaces:**
- Consumes：Tasks 9～14、Task 13 `ActorRig`、Task 12 `FakeStateStore`。
- Produces：無新介面。

- [ ] **Step 1：寫失敗測試**

```rust
#![cfg(feature = "test-fakes")]
mod support;
use std::time::Duration;
use support::*;
use sw_core::session_core::*;
use sw_pal::fakes::serial::FakeWriteStep;
use sw_proto::pyjson::PyValue;

#[test]
fn release_waits_for_receipt_and_aborts_partial_in_flight() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::BlockUntilCancel { accept_before_cancel: 4 }]);
    let cmd = h.submit_ok("sleep 20");
    h.wait_running(&cmd);
    let pending = h.request_async(SessionRequest::Release { source: "agent:t".into(), reason: PyValue::from("r") });
    std::thread::sleep(Duration::from_millis(20));
    assert_eq!(h.get_state()["state"], PyValue::from("RECOVERING"));  // actor 仍可回應
    let resp = pending.recv_timeout(Duration::from_secs(3)).unwrap().unwrap();
    assert_eq!(resp.get("ok"), Some(&PyValue::Bool(true)));
    assert_eq!(resp.get("aborted_cmd"), Some(&PyValue::Bool(true)));
    assert_eq!(h.dev.close_count(), 1);
    let rec = h.command_get(&cmd);
    assert_eq!((rec["status"].clone(), rec["error_code"].clone()), (PyValue::from("error"), PyValue::from("COMMAND_ABORTED_IN_FLIGHT")));
    assert_eq!(h.wal_tx_payloads().last().unwrap(), &b"slee".to_vec());   // 只記確認的 4 bytes
    assert!(h.state_store.last_released().contains_key("t:COM0"));
    assert_eq!(h.get_state()["state"], PyValue::from("RELEASED"));
}

#[test]
fn drain_timeout_marks_stalled_and_release_reports_drain_stalled() {
    let h = ActorRig::new(SHELL_PROFILE).ready();
    h.dev.script_write(vec![FakeWriteStep::BlockForever]);
    let cmd = h.submit_ok("x"); h.wait_running(&cmd);
    let pending = h.request_async(SessionRequest::Release { source: "cli".into(), reason: PyValue::Null });
    h.clock.advance(Duration::from_millis(3001));                              // FakeClock::subscribe 立即喚醒 actor
    let resp = pending.recv_timeout(Duration::from_secs(5)).unwrap().unwrap();
    assert_eq!(resp.get("error_code"), Some(&PyValue::from("DRAIN_STALLED")));        // OD-14 #4
    assert_eq!(resp.get("drain_stalled"), Some(&PyValue::Bool(true)));
    assert_eq!(h.dev.open_count(), 1, "no reopen while stalled");
    h.dev.release_block();                                                     // 回條遲到
    h.wait_state("RELEASED");
    let again = h.request(SessionRequest::Release { source: "cli".into(), reason: PyValue::Null });
    assert_eq!(again.get("already_released"), Some(&PyValue::Bool(true)));
}
```

| 測試名 | 精確斷言 |
|---|---|
| `actor_answers_get_state_while_drain_pending` | FakeSerial `BlockForever`：觸發 release 後 50 ms 內 `request(GetState, 1s)` 回 `ok:true` 且 `session.state=="RECOVERING"`（§5.2.4 不阻塞 mailbox；原列於 Task 11，因依賴 drain 移至此） |
| `in_flight_unknown_is_send_failed_no_resend` | writer `Unknown` 時 release → `SEND_FAILED`，`dev.write_calls()` 之後不再增加 |
| `wal_unwritable_does_not_block_close_but_lists_gap` | drain 期間 `fail_next_writes(wal, ∞)` → release 完成、`health().healthy==false`、stderr WARNING 含 `未落盤` |
| `close_failure_keeps_recovering_and_no_success` | `fail_close(e)` → state `RECOVERING`、`drain_stalled:true`、release waiter 收 `DRAIN_STALLED`（OD-14 #4） |
| `release_handled_within_one_data_item_under_flood` | 以 FakeDevice 持續 push RX（data lane 保持滿載）時送 release：actor 處理 release 之前最多再處理 1 個 data item（以 actor 內 test-fakes 計數器斷言；`resource_limits.event_latency.l1_bound`）；`command.cancel` 與 lease expiry 同法各驗一次 |
| `old_epoch_tx_completion_after_reattach_keeps_evidence` | release→attach→READY 後，舊 epoch writer 晚到的 `UartTxCompleted` → 新 session state 不變；WAL 已有該 TX 紀錄（由 TxWriter 寫入） |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-core --features test-fakes --test drain_release_e2e`
Expected：若 Tasks 9～14 已正確實作，本 Task 測試可能直接 PASS——此時把 Step 2 的「FAIL」以「暫時把 `FakeStateStore::fail_next_commit` 改為永遠失敗」的突變驗證取代：確認 `release_waits_for_receipt_and_aborts_partial_in_flight` 會因 `Err(PyErr)` 而 FAIL，再還原（突變紀錄寫入 commit message）。

- [ ] **Step 3：實作**

依 Task 20 Step 3 的差異修正方式：每個失敗先在擁有該行為的 Task 測試檔加重現案例（紅燈）再修正。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-core --features test-fakes --test drain_release_e2e --test drain_release --test session_core --test mailbox --test login_quiet --test command_exec --test lease_selftest`
Expected：PASS（`drain_release_e2e` 8 項）。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-core
git commit -m "test(sw-core): drain／release 端到端案例" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 15a：黑箱 FakeTarget 擴充（測試資產變更，§10.4）

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** 無（只動 `tests/`，不屬 `code_paths`）。
**BLOCKED-OWNER：** 無。須由非實作者獨立審閱並在 PR 描述列出（§10.4）。

P3 `tests/contract_blackbox/bb_faketarget.FakeTarget` 只模擬 `login:`／`Password:`／`echo X`／`^C`；Slice 2.1 黑箱需要開機 banner、`sleep N`、大量輸出、任意注入與已寫出 bytes 查詢。以**向後相容的擴充**完成（既有參數與行為不變，P3 自測仍綠）。

**Files:**
- Modify: `tests/contract_blackbox/bb_faketarget.py`
- Test: `tests/contract_blackbox/test_bb_faketarget_s21.py`

**Interfaces:**
- Consumes：P3 `bb_faketarget.FakeTarget`。
- Produces（新增，全部 keyword-only、預設值維持舊行為）：`FakeTarget(workdir, *, prompt="root@fake:~# ", user="root", password="pw", login=True, boot_banner: bytes = b"", noise_interval_s: float | None = None)`；屬性 `slave_path: str`（`start()` 後可讀）；方法 `emit(data: bytes)`（立即從 DUT 端送出）、`written() -> bytes`（= `rx_log()` 的別名，DUT 已收到的全部 bytes）、`pause_responding(seconds)`；行內命令模型新增 `sleep N`（N 秒後送 prompt）、`yes | head -c N`（送 N 個 `y\n` 字元後 prompt）、`printf M1..M50`（分 10 個 chunk 送出 `M1`～`M50` 各一行，供 F4 背景分頁）。

- [ ] **Step 1：寫失敗測試**：`test_bb_faketarget_s21.py`：`login=False` 時開機直接送 prompt；`boot_banner` 在 prompt 前送出；`emit` 後 master 端讀得到；`sleep 1` 在 0.9～1.5 s 間送 prompt；`yes | head -c 200000` 送出恰 200000 bytes 再 prompt；`written()` 等於 DUT 收到的 bytes；舊建構子呼叫（P3 自測）行為不變。
- [ ] **Step 2：執行並確認 FAIL**：`python3 -m pytest -q tests/contract_blackbox/test_bb_faketarget_s21.py` → `TypeError: unexpected keyword argument 'login'`。
- [ ] **Step 3：實作**：擴充狀態機；不 import 產品模組（`test_bb_no_product_import.py` 仍綠）。
- [ ] **Step 4：執行並確認 PASS**：`python3 -m pytest -q tests/contract_blackbox/test_bb_faketarget_s21.py tests/contract_blackbox/test_bb_harness_selftest.py tests/contract_blackbox/test_bb_no_product_import.py` 全綠。
- [ ] **Step 5：Commit**：`test(contract): 擴充黑箱 FakeTarget 以支援 Slice 2.1 情境`（含 Co-authored-by trailer）。

---

## Task 15：sw-core StateStore＋AliasRegistry＋state.json 雙向相容

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-state-store.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust state.json 單一寫入者：與 Python 相同的 5 個 key、位元組相容的原子寫入、讀取失敗拒絕啟動、JSON 損毀改名 .corrupt；alias registry 行為逐字承接；動態 session 的 released 列跨重啟保留（DF-04）。」）
**BLOCKED-OWNER：** 無。

**Files:**
- Modify: `crates/sw-core/src/state_store.rs`、`crates/sw-core/src/alias_registry.rs`
- Create: `tests/contract_blackbox/slice21/__init__.py`、`tests/contract_blackbox/slice21/harness.py`、`tests/contract_blackbox/slice21/conftest.py`（Tasks 20～22 擴充）、`tests/contract_blackbox/slice21/test_state_file_compat.py`
- Test: `crates/sw-core/tests/state_store.rs`

**Interfaces:**
- Consumes：P4 `FsOps::atomic_write(p, data, mode: Option<u32>)`（本 Task 以 `Some(0o600)` 呼叫；Step 0 先以測試確認 P4 實作滿足 Python `_save_state` 語意：同目錄 tmp 前綴 `state.json.tmp.`、0600、fsync、replace、Windows PermissionError 6 次退避、目錄 fsync——任一不符屬上游缺陷，停止回報）、`FsRead::read_bytes`；`sw_proto::pyjson::DISK_STABLE`；P3 `tests/contract_blackbox/bb_harness.py`（`isolated_env`、`cli_prefix`、`daemon_prefix`、`run_cli`、`is_rust`）；Task 15a `FakeTarget`。
- Produces：

```rust
#[derive(Clone, Debug, PartialEq)] pub struct ReleasedRow { pub by_id: PyValue, pub released_by: PyValue, pub released_at: PyValue, pub reason: PyValue }
#[derive(Clone, Debug, Default, PartialEq)] pub struct LoadedState { pub aliases: IndexMap<String, PyDict>, pub bindings: IndexMap<String, String>,
    pub released: IndexMap<String, ReleasedRow>, pub profile_pins: IndexMap<String, String>, pub profile_detected: IndexMap<String, String> }
#[derive(Debug)] pub struct StateLoadError { pub message: String }    // Display = Python StateLoadError 訊息
pub fn load_state(path: &Path, fs: &dyn FsOps) -> Result<(LoadedState, Option<PathBuf> /* .corrupt 備份 */), StateLoadError>;
pub fn serialize_state(s: &LoadedState) -> Vec<u8>;                  // DISK_STABLE + b"\n"
pub enum StateDelta { Released { sid: String, row: ReleasedRow }, Unreleased { sid: String }, Binding { sid: String, by_id: Option<String> },
    Pin { device_key: String, profile: Option<String> }, Detected { by_id: String, profile: String }, AliasSet { alias: String, row: Option<PyDict> }, Touch }
pub struct StateModel { /* LoadedState + 已載入但未實體化的 released 列 */ }
impl StateModel { pub fn new(loaded: LoadedState) -> StateModel; pub fn apply(&mut self, d: StateDelta); pub fn snapshot(&self) -> LoadedState; pub fn released_by_ids(&self) -> HashSet<String>; }
pub struct StateStore { /* thread "serialwrap-state-store"，bounded(64) */ }
impl StateStore {
    pub fn spawn(path: PathBuf, fs: Arc<dyn FsOps>) -> StateStore;
    #[cfg(feature = "test-fakes")]
    pub fn spawn_with_hooks(path: PathBuf, fs: Arc<dyn FsOps>, before_commit: Option<Box<dyn Fn() + Send + Sync>>,
                            after_commit: Option<Box<dyn Fn() + Send + Sync>>) -> StateStore;   // Review Focus 1：hook 在 atomic_write 之前／之後於 store thread 呼叫
    pub fn commit(&self, snap: LoadedState, done: crossbeam_channel::Sender<Result<(), PalError>>);
    pub fn commit_sync(&self, snap: LoadedState) -> Result<(), PalError>;
}
pub struct AliasRegistry { /* IndexMap<String, PyDict> */ }
impl AliasRegistry { pub fn load(rows: IndexMap<String, PyDict>) -> AliasRegistry; pub fn dump(&self) -> IndexMap<String, PyDict>;
    pub fn list_alias(&self) -> Vec<PyDict>; pub fn set_for_session(&mut self, sid: &str, alias: &str, now_iso: &str);
    pub fn assign_by_id(&mut self, by_id: &str, alias: &str, profile: Option<&str>, now_iso: &str); pub fn unassign(&mut self, alias: &str) -> bool; }
```

黑箱 harness（Python，stdlib＋pytest＋PyYAML，不 import `sw_core`）：

```python
# tests/contract_blackbox/slice21/harness.py
@dataclasses.dataclass
class Slice21Daemon:
    root: pathlib.Path
    env: dict[str, str]
    endpoint: str                      # POSIX socket 路徑；Windows "tcp://127.0.0.1:<port>"
    state_dir: pathlib.Path
    wal_dir: pathlib.Path
    targets: dict[str, "FakeTarget"]
    proc: subprocess.Popen
    cli: str
    def sw(self, *args: str, timeout: float = 30.0) -> dict: ...          # bb_harness.run_cli([*cli_prefix(), "--socket", endpoint, *args])；stdout JSON＋"_rc"＋"_stderr"
    def rpc(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict: ...  # 直接 NDJSON（量測／framing 用）
    def wait_state(self, com: str, state: str, timeout: float = 20.0) -> dict: ...             # 回 session dict；逾時 AssertionError
    def wait_command_done(self, cmd_id: str, timeout: float = 20.0) -> dict: ...             # 輪詢 cmd status 直到有 done_at；回 command dict
    def wal_records(self) -> list[dict]: ...                               # 讀 wal_dir/raw.wal.ndjson
    def stop(self, timeout: float = 15.0) -> int: ...                      # daemon stop → 等行程 → 回 exit code
SHELL_TARGET_YAML: str                 # 單一 COM0、platform shell、prompt "(?m)^root@t:.*# "、by-id 指向 FakeTarget
def start_daemon(root: pathlib.Path, *, daemon: list[str] | None = None, cli: list[str] | None = None, profile_yaml: str = SHELL_TARGET_YAML,
                 prompt: str = "root@t:/# ", login: bool = False, boot_banner: bytes = b"",
                 extra_env: dict[str, str] | None = None, reuse_dirs: bool = False) -> Slice21Daemon: ...
    # daemon／cli 預設取 bb_harness.daemon_prefix()／cli_prefix()（AB_ENV 選擇器）；FakeTarget 為 Task 15a 擴充版

# tests/contract_blackbox/slice21/conftest.py
@pytest.fixture
def python_daemon_exe() -> list[str]: ...      # [<repo>/tests/contract_blackbox/bin/py-serialwrapd]（arch §9.2；P3 交付）
@pytest.fixture
def slice21_daemon_factory(tmp_path, request) -> Callable[..., Slice21Daemon]: ...   # (root, *, daemon=None, reuse_dirs=False, profile_yaml=…, prompt=…, extra_env=None)；teardown 一律 stop
@pytest.fixture
def artifact_identity() -> dict: ...           # {"cli": cli_prefix(), "daemon": daemon_prefix(), "label": ab_label()}；每個測試開始時斷言實際啟動的 daemon 行程
                                               # /proc/<pid>/exe（Windows：psutil 等價的 QueryFullProcessImageName）等於所選 artifact，否則 pytest.fail（防止 Rust 輪靜默跑 Python）
@pytest.fixture
def shell_daemon(slice21_daemon_factory, tmp_path) -> Slice21Daemon: ...     # 已 wait_state("COM0","READY")
```

- [ ] **Step 0：測試支援**（Test author model；與 Step 1 同 commit）：先寫 `tests/contract_blackbox/slice21/{__init__.py,harness.py,conftest.py}`（含 `artifact_identity` 檢查），並以 Python 基線跑一個 smoke（`start_daemon` → `wait_state("COM0","READY")` → `stop()==0`）確認 harness 本身可用；另寫 `crates/sw-core/tests/state_store.rs` 的 `p4_atomic_write_semantics` 前置檢查（tmp 前綴、0600、目錄 fsync 呼叫次數）。

- [ ] **Step 1：寫失敗測試**

```rust
use sw_core::state_store::*;
use sw_proto::pyjson::PyValue;

const PY_BYTES: &str = "{\"aliases\":{\"custom\":{\"session_id\":\"p:COM5\",\"updated_at\":\"2026-10-15T03:07:39.596535+00:00\"},\"zz\":{\"device_by_id\":\"/dev/serial/by-id/Z\",\"profile\":\"prpl-template\",\"updated_at\":\"2026-10-15T03:07:39.596600+00:00\"}},\"bindings\":{},\"profile_detected\":{},\"profile_pins\":{\"/dev/serial/by-id/X\":\"prpl-template\"},\"released\":{\"prpl-template:COM0\":{\"by_id\":\"/dev/serial/by-id/X\",\"reason\":\"r\",\"released_at\":\"t\",\"released_by\":\"cli\"}}}\n";

#[test]
fn python_bytes_roundtrip_exactly() {
    let d = tempfile::tempdir().unwrap(); let p = d.path().join("state.json");
    std::fs::write(&p, PY_BYTES).unwrap();
    let (s, backup) = load_state(&p, &*sw_pal::native().fs).unwrap();
    assert!(backup.is_none());
    assert_eq!(String::from_utf8(serialize_state(&s)).unwrap(), PY_BYTES);
    assert_eq!(s.released["prpl-template:COM0"].released_by, PyValue::from("cli"));
}

#[test]
fn unreadable_is_state_load_error_and_file_untouched() {
    let d = tempfile::tempdir().unwrap(); let p = d.path().join("state.json");
    std::fs::create_dir(&p).unwrap();                                   // read 失敗（EISDIR）
    let e = load_state(&p, &*sw_pal::native().fs).unwrap_err();
    assert!(e.message.starts_with(&format!("無法讀取既有 state.json（{}）：", p.display())));
    assert!(e.message.ends_with("；為避免以空狀態覆寫並遺失 RELEASED 交接（two-reader 風險），daemon 拒絕啟動。"));
    assert!(p.is_dir());
}
```

| 測試名 | 精確斷言 |
|---|---|
| `corrupt_json_renamed_and_empty_state` | 內容 `{"aliases":` → 回 `(LoadedState::default(), Some("<p>.corrupt"))`；原路徑不存在、`.corrupt` 內容相同；既有 `.corrupt` 被覆蓋；stderr WARNING `state.json 解析失敗（JSON 損毀），略過載入並備份至 <p>.corrupt：<msg>` |
| `non_dict_json_is_empty_without_backup` | `[1,2]` → 預設狀態、無備份 |
| `tolerant_fields` | `bindings` 的非 str 值、strip 後空字串被略過；`released` 中 meta 非 dict 略過；`by_id` 非字串不加入 `released_by_ids` |
| `unknown_keys_dropped_on_save` | 載入含 `"future":1` → `serialize_state` 輸出不含 `future`（WAL-D14） |
| `tmp_residue_ignored` | 目錄內殘留 `state.json.tmp.abc` 不影響載入；commit 後殘留 tmp 不增加 |
| `dynamic_released_rows_retained_across_snapshots` | `StateModel::new` 載入 `{"released":{"t+1:COM3":…}}`，未有 session 實體化 → `snapshot().released` 仍含該列（DF-04） |
| `unreleased_removes_row` | `apply(Unreleased{sid})` 後 snapshot 不含 |
| `commit_file_mode_0600_and_trailing_newline` | `commit_sync` 後檔案 `0o600`、最後一個 byte 為 `\n` |
| `commit_failure_returns_error` | `FsOps` fake `fail_atomic_write(EIO)` → `Err(_)` 且磁碟舊檔不變 |
| `alias_list_sorted_with_alias_key` | `list_alias()` 依 alias 字典序，每列複製並加 `"alias"`；`assign_by_id(profile=None)` 列無 `profile` 鍵 |
| `alias_unassign_missing_false` | 不存在 → `false` |

`tests/contract_blackbox/slice21/test_state_file_compat.py`（雙 artifact 交叉讀寫，Rust→Python 用於 rollback 驗證）：

```python
import json, subprocess

def test_rust_written_state_is_loaded_by_python(slice21_daemon_factory, python_daemon_exe, tmp_path):
    d = slice21_daemon_factory(tmp_path / "a")                       # 受測 artifact（Rust 或 Python）
    d.wait_state("COM0", "READY")
    d.sw("device", "release", "--selector", "COM0", "--source", "agent:t", "--reason", "x")
    d.stop()
    raw = (d.state_dir / "state.json").read_bytes()
    assert raw.endswith(b"\n")
    assert list(json.loads(raw)) == ["aliases", "bindings", "profile_detected", "profile_pins", "released"]
    p = slice21_daemon_factory(tmp_path / "a", daemon=python_daemon_exe, reuse_dirs=True)   # 換成 Python 基線讀同一份
    s = p.wait_state("COM0", "RELEASED", timeout=10)
    assert s["released_by"] == "agent:t" and s["released_reason"] == "x"
```

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-core --features test-fakes --test state_store`；`SERIALWRAP_DAEMON_EXE=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrapd python3 -m pytest -q tests/contract_blackbox/slice21/test_state_file_compat.py`；`python3 -m pytest -q tests/contract_blackbox/slice21/test_state_file_compat.py`（Python 基線）
Expected：Rust L1 FAIL `not implemented: slice21 T15 load_state`；黑箱 Rust 輪 FAIL（Rust daemon 啟動即 exit 101，Task 19 前預期）；Python 基線（未設 env）PASS。

- [ ] **Step 3：實作**

Python harness：`start_daemon` 以 P3 `bb_harness.isolated_env(root)` 為底，另設 `SERIALWRAP_BY_ID_DIR=<root>/by-id`（內含 `fake-uart0 → FakeTarget.slave_path`）、`SERIALWRAP_BY_PATH_DIR=<root>/by-path`（空）、`SERIALWRAP_PROFILE_DIR=<root>/profiles`（寫入 `profile_yaml`），以 `subprocess.Popen([daemon], env=…, stdout=DEVNULL, stderr=<root>/daemon.stderr)` 無參數啟動（與 regression ThrowawayDaemon 相同契約），等 socket／TCP 可連；`reuse_dirs=True` 時沿用同一 `root` 的 state／wal／config 並建新 FakeTarget 接到同一 by-id 名稱。Rust：依 04 §2.2～§2.7 逐條。`load_state`：不存在 → 預設；`fs` 讀取錯誤 → `StateLoadError`（訊息逐字照 Python）；`pyjson::loads` 失敗（含非 UTF-8）→ `replace(p, p+".corrupt")`（失敗則 backup None）＋ WARNING。`StateStore` thread 收 `(snapshot, done)` 依序 `atomic_write`，結果回 `done`；`StateModel::snapshot` 的 `released` = 實體化 session 中 `RELEASED` 者 ∪ 未實體化的已載入列（同 sid 時以實體化者為準）。

- [ ] **Step 4：執行並確認 PASS**

Run：同 Step 2（Rust 與兩種 artifact）
Expected：PASS。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-core tests/contract_blackbox/slice21 changelog.d/rust-s21-state-store.md
git commit -m "feat(sw-core): state.json 雙向相容的單一寫入者與 alias registry" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 16：sw-core SessionManager（裝置、rank #100、動態 session、selector、管理操作）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-session-manager.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust SessionManager：初次掃描後先排定動態 COM 再啟動 watcher（DF-14）、模板 pin／sticky／detect／fallback 四層解析、PROBE 偵測與正式開啟依序交接、selector 解析、bind／pin／clear／recover 與裝置移除／改指的 drain 後重開。」）
**BLOCKED-OWNER：** 同款 CH340 by-id 相同時以 by-path 區分的實機證據（需擁有者 bench 兩張同款板）。

**Files:**
- Modify: `crates/sw-core/src/session_manager.rs`
- Test: `crates/sw-core/tests/session_manager.rs`

**Interfaces:**
- Consumes：Tasks 5、11～15；`sw_proto::config_types::{LoadResult, ProfileTemplate, SessionProfile}`。
- Produces：

```rust
pub fn device_sort_key(by_id: &str, by_path: Option<&str>) -> (String, String);
pub struct ManagerDeps { pub serial: Arc<dyn SerialBackend>, pub uart_excl: Arc<dyn UartExclusive>, pub console: Arc<dyn ConsoleTransportFactory>,
    pub caps: Capabilities, pub wal: Arc<WalWriter>, pub registry: Arc<CommandRegistry>, pub state: StateStore, pub loaded: LoadedState,
    pub load: LoadResult, pub clock: Arc<dyn Clock>, pub env: Arc<dyn SysEnv>, pub fs: Arc<dyn FsOps>, pub warn: Arc<dyn Fn(String) + Send + Sync> }
pub struct SessionManager { /* 全域 actor thread "serialwrap-session-manager" + 各 SessionActor */ }
impl SessionManager {
    pub fn new(deps: ManagerDeps) -> Arc<SessionManager>;
    pub fn prepare_dynamic_rank(&self, by_ids: &[String]);
    pub fn update_devices(&self, snapshot: &[(String, DeviceInfo)]);
    pub fn bootstrap_attach(&self);
    pub fn resolve(&self, selector: &str) -> Option<String>;                 // session_id
    pub fn route(&self, sid: &str, req: SessionRequest, timeout: Duration) -> Result<PyDict, PyErr>;
    pub fn list_sessions(&self) -> Vec<PyDict>;                              // 依 session_id 排序
    pub fn list_devices(&self) -> Vec<PyDict>;                               // {by_id, real_path} 依 by_id 排序
    pub fn bind(&self, selector: &str, by_id: &str) -> Result<PyDict, PyErr>;      // state.json 寫入失敗 → Err（framing 轉 EXCEPTION）
    pub fn pin(&self, selector: &str, profile: &str) -> Result<PyDict, PyErr>;
    pub fn unpin(&self, selector: &str) -> Result<PyDict, PyErr>;
    pub fn alias_list(&self) -> PyDict; pub fn alias_set(&self, sid: &str, alias: &str) -> Result<PyDict, PyErr>;
    pub fn alias_assign(&self, by_id: &str, alias: &str, profile: Option<&str>) -> Result<PyDict, PyErr>; pub fn alias_unassign(&self, alias: &str) -> Result<PyDict, PyErr>;
    /// interactive_send／status／close 只帶 interactive_id（Python service.py:850-869）：以 manager 的 id→sid 索引路由；
    /// 索引由 ManagerNote::{InteractiveOpened, InteractiveClosed} 與 session drain／移除維護；查無 → {"ok":false,"error_code":"INTERACTIVE_NOT_FOUND","interactive_id":…}
    pub fn route_interactive(&self, interactive_id: &str, req: SessionRequest, timeout: Duration) -> Result<PyDict, PyErr>;
    pub fn wal_health_fanout(&self) -> Arc<WalHealthFanout>;                     // new() 時建立並 wal.set_health_sink 一次；每個 session fence 建立時 register
    pub fn on_tick(&self);
    pub fn counts(&self) -> (usize /*sessions*/, usize /*devices*/);
    pub fn attached_real_paths(&self) -> Vec<String>;
    pub fn flash_candidates(&self) -> Vec<PyDict>;                           // mcu.status 用（02 §7.2）
    pub fn watcher_sender(&self) -> crossbeam_channel::Sender<WatcherEvent>;   // DeviceWatcher → manager（Delta 轉 update_devices、Tick 轉 on_tick）
    /// SC-11 stopping gate（R1-F2）：持 manager 鎖設 `stopping=true`，冪等。之後 `update_devices`／`WatcherEvent::{Delta,Tick}` 觸發的 attach／reattach、
    /// `ManagerNote::RequestAttach`、`bootstrap_attach`、`bind`／`clear`／`recover`／`device.attach` 的重開一律**不**呼叫 `SerialOpener::open`、不建立 session 或 worker
    /// （自動路徑只記 log `shutdown 中，略過 attach <by_id>`；RPC 路徑回 `Err(PyErr("daemon stopping"))`，framing 轉 `EXCEPTION`）。
    /// gate 設下時已在 inflight set 中、已越過 gate 檢查的 attach worker 不中斷，由 `shutdown` 一併納入 drain 集合（open 尚未返回者等它返回後立即 Phase 1，零 TX）。
    pub fn begin_shutdown(&self);
    pub fn shutdown(&self, budget: Duration) -> Vec<(String, bool /* drained */)>;   // 先隱含呼叫 begin_shutdown；drain 集合＝所有有 bridge 的 session ∪ inflight attach
}
```

- [ ] **Step 1：寫失敗測試**

```rust
#![cfg(feature = "test-fakes")]
mod support;
use support::*;
use sw_core::session_manager::*;

#[test]
fn rank_assigns_com_by_sorted_by_id_before_watcher_start() {
    let m = ManagerRig::templates_only();                          // default.yaml 的 templates，無 targets
    m.devices(&["/s/by-path/pci-0", "/s/by-id/usb-B", "/s/by-id/usb-A"]);
    m.mgr.prepare_dynamic_rank(&m.device_keys());
    m.mgr.update_devices(&m.snapshot()); m.mgr.bootstrap_attach();
    m.wait_sessions(3);
    let by_com = m.com_by_id();
    assert_eq!(by_com["/s/by-id/usb-A"], "COM0");
    assert_eq!(by_com["/s/by-id/usb-B"], "COM1");
    assert_eq!(by_com["/s/by-path/pci-0"], "COM2");
}

#[test]
fn rebind_drains_before_reopen() {                                  // Review Focus 2
    let m = ManagerRig::single_target(SHELL_PROFILE).ready();
    let old = m.dev.clone();
    m.retarget_by_id("/s/by-id/usb-A", "/dev/ttyNEW");
    m.wait_state("COM0", "READY");
    assert_eq!(old.close_count(), 1);
    assert!(m.events().iter().position(|e| e == "close:/dev/ttyOLD").unwrap() < m.events().iter().position(|e| e == "open:/dev/ttyNEW").unwrap());
    assert_eq!(m.session("COM0")["attached_real_path"], "/dev/ttyNEW".into());
    assert!(m.last_error_history("COM0").contains(&"DEVICE_REBOUND_REQUIRED".to_string()));   // drain 收尾時設定（Review Focus 2）
}
```

`ManagerRig`（Step 0 加入 support；Test author model 撰寫）：`last_error_history(&self, com) -> Vec<String>`（actor 每次 last_error 變更的紀錄）、`templates_only() -> ManagerRig`、`single_target(yaml) -> ManagerRig`、`ready(self) -> ManagerRig`、`devices(&self, keys: &[&str])`、`device_keys(&self) -> Vec<String>`、`snapshot(&self) -> Vec<(String, DeviceInfo)>`、`wait_sessions(&self, n: usize)`、`com_by_id(&self) -> HashMap<String, String>`、`retarget_by_id(&self, by_id: &str, new_real: &str)`、`wait_state(&self, com: &str, state: &str)`、`events(&self) -> Vec<String>`（FakeSerial 的 open／close 時序）、`session(&self, sel: &str) -> PyDict`、`dev: FakeDevice`、`mgr: Arc<SessionManager>`。

| 測試名 | 精確斷言 |
|---|---|
| `rank_excludes_released_explicit_bound` | `released_by_ids`、YAML target 的 by_id、binding override 值不參與 rank；已用 COM 跳過 |
| `next_dynamic_com_for_hotplug` | 已有 COM0、COM2 → 熱插新裝置得 `COM1` |
| `dynamic_session_naming` | template `prpl-template` 第 3 個 session：`session_id == "prpl-template:COM2"`、`alias == "prpl-template+3"`、`act_no == 3` |
| `resolution_order_pin_sticky_detect_fallback` | pin 存在 → 用 pin（`profile_source:"pin"`）；否則 sticky（`"sticky"`）；否則 detect：PROBE 開啟 → 送 `\n` → 3.0 s 後 tail 命中第一個 prompt_regex（`"detected"`）；都不中 → 非 command_capable passthrough（`"fallback"`） |
| `detect_skips_passthrough_console_unlock_and_unsupported` | detect 不嘗試 passthrough、`console_unlock_env`、`post_login_cmd` 模板 |
| `probe_bridge_closed_with_receipt_before_real_open` | PROBE 的 `CloseReceipt{closed:true, excl_cleared:true}` 收到後才 `open(real)`（`excl_cleared:false` 時仍嘗試 open，得 `Held` 依熱插規則 `DETACHED`＋`ATTACH_FAILED:DeviceHeldError`，不得靜默吞掉；原生 PTY 下的 reopen 由 Task 2 `cap_posix_pty__probe_close_reopen_with_master_open` 釘住）；時序 `open(PROBE)`→`close(PROBE)`→`open(real)`，WAL 的 detect RX/TX `com == "PROBE"` |
| `sticky_persisted_only_when_ready_and_path_unchanged` | detected 且 READY → `state.json.profile_detected[by_id]` 寫入；READY 前 real_path 改變 → 不寫 |
| `detached_rebind_fallback_smallest_act_no` | 新 by_id 無 session、有兩個 DETACHED 且其 by_id 離線的 session → 綁到 act_no 較小者並寫 binding override |
| `max_sessions_warning` | 已 16 個 → 新裝置不建 session，WARNING `已達 max_sessions=16，忽略裝置 <by_id>` |
| `binding_override_cleanup_186` | 啟動時 override 指向另一 target 宣告的 by_id → 刪除 |
| `selector_resolution_order` | session_id 精確優先；否則依建立順序第一個 `com == sel || alias == sel` |
| `device_removed_drains_to_detached` | 裝置從快照消失 → `DETACHED`、`last_error:"DEVICE_REMOVED"`、handle 已關 |
| `bind_paths` | 空 → `INVALID_ARGS`；他 session 已綁 → `{"ok":false,"error_code":"DEVICE_ALREADY_BOUND","device_by_id":…,"session_id":…}`；同值 → `already_bound:true`；成功 → drain(REBOUND) → 裝置在 `ATTACHING`／不在 `DETACHED`＋`DEVICE_NOT_FOUND`；RELEASED 被 bind 視為離開 RELEASED |
| `pin_unpin_paths` | 未知 template → `UNKNOWN_PROFILE`；解析不到 device key → `DEVICE_NOT_FOUND`；yaml-target → `PROFILE_IS_EXPLICIT`；成功 `{"ok":true,"device_key":…,"profile":…}`，不 reattach |
| `clear_paths` | RELEASED → `{"ok":true,"released":true,"session":…}`；否則 drain(CLEARED) → 裝置在 `ATTACHING` |
| `session_attach_paths` | 無 by_id → `DEVICE_NOT_BOUND`；RELEASED → `released:true`＋`recommended_action:"device_attach"`；ATTACHED 同步 probe（passthrough 不 probe，回 `{"ok":true,"session":…}`、零 TX，SC-35）；無 bridge 裝置不在 → `DEVICE_NOT_FOUND` 並設 DETACHED；state ∉ {READY, ATTACHING, FLASHING, RECOVERING} 且 capable（含 human lease 分支、bridge 存在的 else 分支、同步 probe 之後）→ 回應 key 依序 `["ok","error_code","last_error","last_error_detail","session"]`、`ok:false`、`error_code:"SESSION_NOT_READY"`、`last_error`／`last_error_detail` 等於 `session` 內同名欄位（例：ATTACHED＋`CREDENTIALS_UNRESOLVED` → `last_error:"CREDENTIALS_UNRESOLVED"`；last_error 為 None → 兩欄皆 null，不再回 `NOT_READY`）（SC-34）；FLASHING → `FLASHING_BUSY`、RECOVERING → `SESSION_RECOVERING` 不變 |
| `alias_set_persists_but_not_applied_after_restart` | `alias_set("t:COM0","x")` → `alias.list` 含 x、重建 manager 後 `resolve("x") == None`（WAL-D15 KEEP） |
| `alias_unassign_saves_before_check` | 不存在 → `ALIAS_NOT_FOUND`，但 state.json 已重寫一次 |
| `shutdown_budget_5s` | 一個 session writer `BlockForever` → `shutdown(5s)` 於 5 s（FakeClock）內返回，該 session `drained:false`，其他 `true` |
| `begin_shutdown_blocks_hotplug_open`（R1-F2，修正前必 FAIL） | READY 的 COM0 之外，`begin_shutdown()` 後經 `watcher_sender()` 送 `Delta{added:["/s/by-id/usb-NEW"]}`、再呼叫 `bootstrap_attach()` 與 `recover("COM0")` → `FakeSerial.open_calls("/s/by-id/usb-NEW") == 0`、COM0 無新 open；`counts().0` 不變（未建動態 session）；`recover` 回 `Err` 且訊息 `daemon stopping`；log 恰含 `shutdown 中，略過 attach /s/by-id/usb-NEW` |
| `shutdown_includes_inflight_attach`（R1-F2） | 熱插 `/s/by-id/usb-NEW` 使 attach worker 卡在 `FakeSerial::block_open`（已越過 gate）→ 呼叫 `shutdown(5s)` 後放行 open → 回傳清單含該新 session 且 `drained:true`；該 handle `close_count()==1`、FakeTarget 收到 0 bytes（無登入 probe TX）；`shutdown` 返回後不再有任何 open |
| `released_survives_two_restarts_dynamic` | 動態 session release → 以同一 `state.json` 重建 manager 兩次 → 兩次都 `RELEASED`、`DeviceAttached` 不 open（DF-04；原列於 Task 12，需 StateModel＋manager 故移至此） |
| `interactive_routes_by_id_without_selector` | `interactive_open` 回 id 後，`route_interactive(id, InteractiveStatus)` 不帶 selector 即到達正確 session；未知 id → `INTERACTIVE_NOT_FOUND`；session drain 後索引移除 |
| `fanout_registers_every_session_fence` | 建立 3 個 session 後 WAL 轉 unhealthy → 三個 fence 皆 `wal_unhealthy`；熱插第 4 個 → 建立即 `wal_unhealthy == true` |
| `management_ops_commit_failure_is_err` | `FakeStateStore::fail_next_commit` 時 `bind`／`pin`／`alias_set` 回 `Err(PyErr)` |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-core --features test-fakes --test session_manager`
Expected：FAIL，`not implemented: slice21 T16 SessionManager::new`。

- [ ] **Step 3：實作**

依 05 §2、§14 與 07 §1.5：`prepare_dynamic_rank` 在 manager 鎖內；`device_sort_key` 回 `(by_id, by_path.unwrap_or(""))`（`DeviceInfo` 無 by_path，故第二欄恆空，與 Python 同）；`update_devices` 計算 added／removed／changed，removed → actor `DeviceDetached(Removed)`、changed → `DeviceDetached(Rebound)`（actor 收到回條後才以新 real_path `OpenBridge`）、added → `DeviceAttached`（或建立動態 session）；attach worker per by_id 去重（inflight set）。`begin_shutdown` 與 inflight set 共用 manager 鎖：每個 attach worker 在呼叫 `SerialOpener::open` 前持鎖檢查 `stopping`（為真即放棄、自 inflight 移除），`shutdown` 持鎖取「有 bridge 的 session ∪ inflight」為 drain 集合，故不存在「已越過 gate 卻不在 drain 集合」的 worker（R1-F2）。detect 用 `UartProfile` 預設值開 PROBE handle，`com="PROBE"`，`detect_template` 照 05 §8.3（`sleep(3.0)` 用 `Clock`）；PROBE handle 經 `CloseToken::close` 取得 `closed:true` 後才開正式 handle。管理操作的 `state.json` 寫入都經 `StateModel::apply` → `StateStore::commit_sync`（alias／pin／bind 為同步，失敗回 `Err(PyErr)`）。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-core --features test-fakes --test session_manager`
Expected：PASS。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-core changelog.d/rust-s21-session-manager.md
git commit -m "feat(sw-core): SessionManager 裝置排序、動態 session 與管理操作" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 17：PAL RpcListener＋sw-daemon framing

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-rpc-framing.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust RPC server：UDS 權限維持 Python 基線 0660，SERIALWRAP_SOCKET_GROUP 有值時 chgrp（SC-08 撤回，OD-14）、Windows 只綁 loopback TCP、每連線依序處理（允許 pipelining）、Python 位元組相容的 framing 早退回應與超長行斷線。」）
**BLOCKED-OWNER：** 無。

**Files:**
- Create: `crates/sw-pal/src/rpc_transport/listener_posix.rs`、`crates/sw-pal/src/rpc_transport/listener_windows.rs`
- Modify: `crates/sw-pal/src/rpc_transport.rs`、`crates/sw-pal/src/conformance/rpc_suite.rs`、`crates/sw-daemon/src/framing.rs`
- Test: `crates/sw-pal/tests/rpc_listener.rs`、`crates/sw-daemon/tests/framing.rs`

**Interfaces:**
- Consumes：arch §4.6 `RpcListenerFactory`／`RpcListener`／`ListenOptions`，以及 Task 0c `RpcServerStream`（`accept` 回傳此型別；P3 client 端 `RpcStream{send_all, recv}` 不用於 server）；Task 1 `MemStream`；`sw_proto::pyjson::{loads, dumps, RPC_WIRE, RPC_EARLY}`；`contracts/golden/rpc/line_limit.json`（P0/P1，RPC-H-02）。
- Produces：

```rust
// sw-daemon/src/framing.rs
pub trait RpcHandler: Send + Sync { fn call(&self, method: &str, params: &PyDict) -> Result<PyDict, PyErr>; fn request_stop(&self); }
pub const MAX_LINE_BYTES: usize;                               // 由 golden 決定，預期 65536（內容不含 '\n'）
pub fn serve_connection(stream: Box<dyn RpcServerStream>, handler: &dyn RpcHandler, stop: &ConnStop);
pub struct ConnStop { /* AtomicBool stopping + 本連線是否正在 handler 內 */ }
pub struct RpcServer { /* acceptor thread、連線上限 128、每連線 (JoinHandle, ConnStop, shutdown handle) */ }
impl RpcServer {
    pub fn start(listener: Box<dyn RpcListener>, handler: Arc<dyn RpcHandler>) -> RpcServer;
    /// ① 停 accept 並 close listener；② 對「不在 handler 內」的連線（閒置、半行、等待寫回應）呼叫 shutdown_read，並以 set_read_timeout(100 ms) 確保讀迴圈醒來；
    /// ③ 等待正在 handler 內的連線把該筆回應寫完（寫回應的 write 逾時 2 s 後放棄，不阻塞 stop）；④ join 全部連線 thread。回傳 handler 是否全部收斂。
    pub fn stop(self, budget: Duration) -> bool;
}
```

- [ ] **Step 1：寫失敗測試**

`crates/sw-daemon/tests/framing.rs`：

```rust
#![cfg(feature = "test-fakes")]
use std::{io::{Read, Write}, sync::{Arc, Mutex}};
use sw_daemon::framing::*;
use sw_pal::fakes::mem_stream::MemStream;
use sw_proto::pyjson::{PyDict, PyValue};

struct H(Mutex<Vec<String>>);
impl RpcHandler for H {
    fn call(&self, m: &str, p: &PyDict) -> Result<PyDict, sw_proto::pycompat::PyErr> {
        self.0.lock().unwrap().push(m.to_string());
        match m { "boom" => Err(sw_proto::pycompat::PyErr::value_error("kaboom")),
                  "echo" => { let mut d = PyDict::new(); d.insert("ok", PyValue::Bool(true)); d.insert("params", PyValue::Dict(p.clone())); d.insert("id", PyValue::from("x")); Ok(d) }
                  _ => { let mut d = PyDict::new(); d.insert("ok", PyValue::Bool(true)); Ok(d) } }
    }
    fn request_stop(&self) { self.0.lock().unwrap().push("<stop>".into()); }
}
fn run(input: &[u8]) -> (Vec<u8>, Vec<String>) {
    let (mut a, b) = MemStream::pair(); let h = Arc::new(H(Mutex::new(vec![])));
    let h2 = h.clone(); let j = std::thread::spawn(move || serve_connection(Box::new(b), &*h2, &ConnStop::new()));
    a.write_all(input).unwrap(); a.close_write();
    let mut out = Vec::new(); a.read_to_end(&mut out).unwrap(); j.join().unwrap();
    let calls = h.0.lock().unwrap().clone(); (out, calls)
}

#[test]
fn framing_byte_table() {
    let cases: &[(&[u8], &str)] = &[
        (b"not json\n", "{\"ok\": false, \"error_code\": \"INVALID_JSON\"}\n"),
        (b"[1,2]\n", "{\"ok\": false, \"error_code\": \"INVALID_REQUEST\"}\n"),
        (b"{\"id\":7}\n", "{\"id\":7,\"ok\":false,\"error_code\":\"INVALID_METHOD\"}\n"),
        (b"{\"id\":1,\"method\":\"boom\"}\n", "{\"id\":1,\"ok\":false,\"error_code\":\"EXCEPTION\",\"message\":\"kaboom\"}\n"),
        (b"{\"id\":2,\"method\":\"echo\",\"params\":[1]}\n", "{\"id\":\"x\",\"ok\":true,\"params\":{}}\n"),
        (b"{\"id\":3,\"method\":\"echo\",\"params\":{\"x\":NaN}}\n", "{\"id\":\"x\",\"ok\":true,\"params\":{\"x\":NaN}}\n"),
        (b"\n\r\n  \t\n", ""),
        (b"\xff\xfe{\"id\":1}\n", "{\"ok\": false, \"error_code\": \"INVALID_JSON\"}\n"),
        (b"{\"id\":4,\"method\":\"daemon.stop\"}\n", "{\"id\":4,\"ok\":true,\"stopping\":true}\n"),
        (b"{\"id\":5,\"method\":\"echo\",\"params\":{\"a\":1,\"a\":2}}\n", "{\"id\":\"x\",\"ok\":true,\"params\":{\"a\":2}}\n"),
    ];
    for (input, expect) in cases { assert_eq!(String::from_utf8(run(input).0).unwrap(), *expect, "input={:?}", String::from_utf8_lossy(input)); }
}

#[test]
fn pipelined_requests_answer_in_order() {
    let (out, calls) = run(b"{\"id\":1,\"method\":\"a\"}\n{\"id\":2,\"method\":\"b\"}\n");
    assert_eq!(String::from_utf8(out).unwrap(), "{\"id\":1,\"ok\":true}\n{\"id\":2,\"ok\":true}\n");
    assert_eq!(calls, vec!["a", "b"]);
}

#[test]
fn over_limit_line_closes_without_reply() {
    let golden: serde_free::Golden = serde_free::load("contracts/golden/rpc/line_limit.json");   // {"max_ok_bytes":N,"first_close_bytes":N+1}
    let ok = format!("{{\"id\":1,\"method\":\"a\",\"params\":{{\"p\":\"{}\"}}}}", "x".repeat(golden.max_ok_bytes - 39));
    assert_eq!(ok.len(), golden.max_ok_bytes);
    assert!(!run(format!("{ok}\n").as_bytes()).0.is_empty());
    let bad = format!("{{\"id\":1,\"method\":\"a\",\"params\":{{\"p\":\"{}\"}}}}", "x".repeat(golden.first_close_bytes - 39));
    let (out, calls) = run(format!("{bad}\n{{\"id\":2,\"method\":\"a\"}}\n").as_bytes());
    assert!(out.is_empty() && calls.is_empty());
}
```

（`serde_free::load` 為 `crates/sw-daemon/tests/support/mod.rs` 中以 `sw_proto::pyjson::loads` 讀檔的 20 行 helper，本 Step 一併建立；sw-daemon 的測試 support 一律放 `tests/support/mod.rs` 單一目錄模組，Task 18／19 只追加，避免 `support.rs` 與 `support/mod.rs` 並存造成 E0761。）

| 測試名（framing／listener） | 精確斷言 |
|---|---|
| `daemon_stop_calls_request_stop_and_continues_reading` | `daemon.stop` 後同連線下一行仍被處理；`request_stop` 被呼叫一次 |
| `client_disconnect_does_not_cancel_handler` | handler 阻塞 200 ms，期間 client 關閉 → handler 仍完成（呼叫紀錄存在），寫回失敗不 panic |
| `pipelined_stop_after_long_call`（Review Focus 5） | 同連線送 `slow`（handler sleep 500 ms）、`daemon.stop` → 回應順序 `slow` 先、`stop` 後；`request_stop` 只在 `slow` 回應寫出後才被呼叫 |
| `idle_connection_does_not_block_stop`（Review Focus 5） | 一條已連線但從未送資料的 client、一條送了半行（無 `\n`）的 client → `RpcServer::stop(5s)` 於 500 ms 內返回 `true` |
| `half_line_and_unread_response_do_not_block_stop` | client 送 100 個請求但從不讀回應（socket buffer 塞滿）→ `stop(5s)` 於 3 s 內返回（寫逾時放棄）；handler 已完成的呼叫不被取消 |
| `stop_waits_for_in_handler_connection` | handler 進行中（sleep 500 ms）時 `stop` → 該回應仍寫出且 `stop` 在其後返回 |
| `handler_panic_is_exception` | handler `panic!("x")` → `{"id":…,"ok":false,"error_code":"EXCEPTION","message":"x"}`，連線續用 |
| `deep_json_closes_connection` | 深度超過 `JSON_MAX_DEPTH` 的請求 → 無回應並關閉（arch §6.2 step 3） |
| `uds_mode_0660_baseline`（取代 `uds_mode_0600_and_dir_0700`；SC-08 撤回，OD-14 #3） | `ListenOptions{mode: 0o660, group: None, ensure_dir_mode: None}` bind → socket `st_mode & 0o777 == 0o660`；目錄權限不被 listener 修改（建立前後 `st_mode` 相同，不斷言 0700）；`group: Some("<既有群組>")` → `0o660` 且 gid 相符；`group: Some("no-such-group")` → chgrp 失敗被忽略、bind 成功、仍 `0o660` |
| `uds_existing_file_unlinked` | 路徑已有一般檔 → bind 成功 |
| `uds_close_removes_socket` | `close()` 後檔案不存在 |
| `tcp_non_loopback_rejected_verbatim` | `Tcp{host:"0.0.0.0"}` → `Err(e)`，`e.py_message == "RPC server 僅允許 loopback 位址（127.0.0.1/localhost/::1），拒絕 '0.0.0.0' 避免遠端暴露（RPC 無認證，非 loopback 會讓遠端呼叫所有命令）"` |
| `tcp_loopback_accepts` | `127.0.0.1:0` bind 後可連線並往返一行 |

UDS bind 順序逐字照 Python 基線 `JsonRpcUnixServer.start`／`_secure_socket`（SC-08 撤回，OD-14 #3）：既有 socket 檔 unlink → bind（不切換 umask）→ `chmod(path, 0o660)`（失敗忽略）→ `group` 有值時 chgrp（`OSError`／查無群組皆忽略）；不建立或 chmod 父目錄（`ensure_dir_mode: None`）。
| `connection_cap_128_pauses_accept` | 第 129 條連線在前 128 條關閉前不被 accept（以 `accept(timeout)` 回 `None` 斷言），關一條後被接受 |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-daemon --features test-fakes --test framing && cargo test -p sw-pal --features test-fakes --test rpc_listener`
Expected：FAIL，`not implemented: slice21 T17 serve_connection`。

- [ ] **Step 3：實作**

`serve_connection` 照 arch §6.2 九步：逐 byte 累積到 `\n`，內容長度超過 `MAX_LINE_BYTES` 立即關閉（不回應、不處理同連線後續）；`trim_py_bytes`（`b" \t\n\r\x0b\x0c"`）；空行略過；`decode_utf8_replace` → `loads`；失敗回 `RPC_EARLY` 的 `INVALID_JSON`；非 dict → `INVALID_REQUEST`；`id` 缺省 null；`method` 非 str → `{"id":…,"ok":false,"error_code":"INVALID_METHOD"}`；`params` 非 dict → `{}`；`daemon.stop` → 先寫 `{"id":…,"ok":true,"stopping":true}` 並 flush，再 `request_stop()`；其餘 `std::panic::catch_unwind(|| handler.call(..))`，`Err`／panic → `EXCEPTION`（message 為 `PyErr` 訊息或 panic payload 字串）；成功 → `resp={"id":id}` 依序併入 result（result 的 `id` 覆寫值、位置不變）；`RPC_WIRE` 輸出。listener：POSIX `bind` 前 `create_dir_all`（新建時 `0o700`）、unlink 既有檔、`bind`、`chmod 0o600`（或 group 時 `0o660`＋`chown(-1, gid)`，錯誤忽略）；TCP 只允許 `LOOPBACK_TCP_HOSTS`，`SO_REUSEADDR`。`RpcServer` 每連線一 thread，計數 ≥128 時 acceptor 暫停 accept；`stop` 依 Produces 四步（讀取迴圈每次醒來檢查 `ConnStop.stopping`，不在 handler 內即結束；handler 內的請求完成並寫回後才結束）。`catch_unwind` 只包 handler 呼叫，panic hook 不得終止行程（Task 19）。

- [ ] **Step 4：執行並確認 PASS**

Run：同 Step 2，另加 `scripts/rust/win-test.sh --package sw-pal --package sw-daemon`
Expected：PASS。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-pal crates/sw-daemon changelog.d/rust-s21-rpc-framing.md
git commit -m "feat(sw-daemon): RPC listener 與 Python 相容 framing" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 18：sw-daemon 分派器（service.rs／health.rs）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-s21-dispatcher.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust RPC 分派：39 個 method 依 Python 參數轉型與回應順序實作、所有 service 回應補 version、health.status 完整欄位（含 OD-11 allowlist 顯示），Slice 2.1 未承接的 15 個 method 回 METHOD_NOT_FOUND 且無副作用。」）
**BLOCKED-OWNER：** 無。

**Files:**
- Modify: `crates/sw-daemon/src/service.rs`、`crates/sw-daemon/src/health.rs`
- Test: `crates/sw-daemon/tests/dispatch.rs`

**Interfaces:**
- Consumes：Task 16 `SessionManager`（含 `route_interactive`）；Task 7 `WalWriter`；Task 8 `CommandRegistry`；P4 `ProcScan::{serialwrapd_instances() -> Result<Vec<DaemonProc>, ScanFailure>, serialwrapd_tty_holders(&[PathBuf]) -> (Vec<u32>, HolderScanStatus)}`（02 §7.1 的 `foreign_holders` 為 path→pids 對照時，對 `attached_real_paths()` 逐一呼叫組出；掃描失敗形狀見 Step 3）；`sw_proto::rpc_types::Params`、`sw_proto::pycompat::coerce_rpc_bool`（Task 0c）；`sw_core::mcu_patterns::McuPatternRegistry`（本 Task 實作 `default()` 與 `to_rpc_rows()`，06 §9.9）。
- Produces：

```rust
pub struct Service { /* manager, wal, registry, procs, version, started_at, profile_count, allowlist, caps */ }
pub struct ServiceDeps { pub manager: Arc<SessionManager>, pub wal: Arc<WalWriter>, pub registry: Arc<CommandRegistry>, pub procs: Arc<dyn ProcScan>,
    pub version: String, pub started_at: String, pub explicit_target_count: usize, pub test_device_allowlist: Option<TestAllowlist> /* Task 19；enabled 時才有值 */, pub pid: u32 }
impl Service {
    pub fn new(deps: ServiceDeps) -> Service;
    pub fn rpc(&self, method: &str, params: &PyDict) -> Result<PyDict, PyErr>;  // dispatch + setdefault("version")
}
pub struct ServiceHandler { svc: Arc<Service>, stop: crossbeam_channel::Sender<()> }
impl ServiceHandler { pub fn new(svc: Arc<Service>, stop: crossbeam_channel::Sender<()>) -> ServiceHandler; }
impl framing::RpcHandler for ServiceHandler {}                                  // call → svc.rpc；request_stop → stop.try_send(())
pub fn health(svc: &Service) -> PyDict;                                         // 02 §7.1 key 順序
```

- [ ] **Step 1：寫失敗測試**

```rust
#![cfg(feature = "test-fakes")]
mod support;
use support::ServiceRig;
use sw_proto::pyjson::{dumps, PyValue, RPC_WIRE};

#[test]
fn deferred_methods_return_method_not_found_without_side_effects() {
    let r = ServiceRig::ready_shell();
    let scope = std::fs::read_to_string("../../tests/acceptance/slice21_scope.yaml").unwrap();
    for m in support::deferred_methods(&scope) {
        let before = r.side_effect_fingerprint();
        let resp = r.call(&m, "{\"selector\":\"COM0\",\"local_path\":\"/tmp/x\",\"remote_path\":\"/tmp/y\"}");
        assert_eq!(dumps(&PyValue::Dict(resp), &RPC_WIRE),
                   format!("{{\"ok\":false,\"error_code\":\"METHOD_NOT_FOUND\",\"method\":\"{m}\",\"version\":\"{}\"}}\n", r.version));
        assert_eq!(r.side_effect_fingerprint(), before, "{m} must not touch WAL/state/UART");
    }
}

#[test]
fn health_status_key_order_and_limits() {
    let r = ServiceRig::ready_shell();
    let h = r.call("health.status", "{}");
    let keys: Vec<&str> = h.keys().map(|k| k.as_str()).collect();
    assert_eq!(keys, ["ok","pid","version","running","started_at","sessions","devices","commands","wal_path","mirror_path","wal","limits",
                      "multi_open","foreign_holders","multi_open_detail"]);       // warnings 只在非空時出現（另測）
    assert_eq!(dumps(h.get("limits").unwrap(), &RPC_WIRE).trim_end(),
        "{\"max_submit_cmd_bytes\":16384,\"warn_submit_cmd_bytes\":4096,\"reject_error_code\":\"CMD_TOO_LONG\",\"newline_error_code\":\"CMD_CONTAINS_NEWLINE\",\"warning_code\":\"CMD_LENGTH_WARNING\",\"newline_forbidden\":true}");
}
```

`ServiceRig`（`crates/sw-daemon/tests/support/mod.rs`）：`ready_shell() -> ServiceRig`（FakeSerial 單一 shell target 已 READY）、`with_allowlist(coms: &[&str]) -> ServiceRig`、`call(&self, method: &str, params_json: &str) -> PyDict`、`call_err(&self, method, params_json) -> PyErr`、`side_effect_fingerprint(&self) -> (u64 wal_seq, Vec<u8> state_json, usize uart_writes)`、`version: String`；`deferred_methods(yaml: &str) -> Vec<String>`。

參數轉型與分派表（每列一個 `#[test]`，`params` 為 JSON 文字）：

| method／params | 精確斷言 |
|---|---|
| `health.ping` `{}` | `{"ok":true,"pong":true,"version":v}` |
| `command.submit` `{"selector":"COM0","cmd":"ls","priority":0}` | 紀錄 `priority == 10`（`int(0 or 10)`） |
| `command.submit` `{"selector":"COM0","cmd":"ls","timeout_s":"abc"}` | `Err(PyErr)`（framing 轉 `EXCEPTION`），且在 selector 檢查之前 |
| `command.submit` `{"cmd":"ls"}` | `{"ok":false,"error_code":"INVALID_ARGS","version":v}` |
| `command.submit` `{"com":"COM0","command":"ls","expected_duration_s":0}` | 紀錄 `expected_duration_s == 0.0`（非 None） |
| `command.submit` `{"selector":"NOSUCH","cmd":"x"}` | `{"ok":false,"error_code":"SESSION_NOT_FOUND","selector":"NOSUCH","version":v}` |
| `command.submit` `{"selector":"COM0","cmd":"x","completion_regex":""}` | `INVALID_COMPLETION_REGEX`，於 selector 解析之前 |
| `session.get_state` `{}` | 空 selector 不檢查 → `SESSION_NOT_FOUND`＋`selector:""` |
| `session.activity` `{}` | `INVALID_ARGS` |
| `session.activity` `{"session_id":"t:COM0"}` | 別名鏈 `selector → session_id → com → alias` 生效 |
| `device.release` `{"selector":"COM0","reason":{"k":1}}` | `reason` 原樣進 `released_reason`（任意 JSON） |
| `device.attach` `{"selector":"COM0","force":"false"}` | `force` 以 truthiness 解為 true（RPC-H-06 KEEP） |
| `session.self_test` `{"selector":"COM0","strict_human_lock":"yes"}` | `coerce_rpc_bool` 為 true |
| `log.tail_raw` `{"selector":"COM0","from_seq":""}` | `{"ok":false,"error_code":"INVALID_ARGS","version":v}` |
| `log.tail_raw` `{"com":"COM0"}`（無 from_seq） | latest 模式；回應鍵 `ok,records,returned,from_seq,last_seq,current_seq,truncated,wal_path,wal_file_exists,version`，`from_seq:null` |
| `log.tail_text` `{"com":"COM0","limit":0}` | limit 0 → 200 |
| `wal.range` `{"from_seq":"x"}` | `Err(PyErr)`（不是 INVALID_ARGS，WAL-D13） |
| `wal.range` `{"from_seq":0,"limit":2}` | 鍵 `ok,records,wal_path,wal_file_exists,available_from_seq,rotated_out,version`，無 `truncated` |
| `wal.current_seq` `{}` | `{"ok":true,"seq":N,"version":v}` |
| `wal.reset` `{}` | `{"ok":true,"previous_seq":N,"rotated_suffix":"YYYYmmdd-HHMMSS","version":v}` |
| `log.tail_raw` 在 WAL 檔被刪且 seq>0 | `WAL_MISSING` 形狀照 04 §1.12（含 `hint` 中文全文） |
| `result.tail` `{"com":"COM0"}`（無 cmd_id） | `{"ok":true,"records":[…],"version":v}`，無 WAL_MISSING 檢查 |
| `alias.set` `{"session_id":"t:COM0","alias":""}` | 無 INVALID_ARGS，`{"ok":true,"session_id":"t:COM0","alias":"","version":v}` |
| `alias.assign` `{"by_id":"b","alias":"a"}` | `{"ok":true,"alias":"a","device_by_id":"b","version":v}` |
| `mcu.patterns` `{}` | `{"ok":true,"patterns":[{"family":"ti-cc26xx","probe":"55 55","expect":"00 cc","baud":115200}],"version":v}` |
| `mcu.status` `{}` | `{"ok":true,"candidates":[…],"flashing":false,"flashing_com":null,"last_detect":null,"version":v}` |
| `session.list` `{}` | `sessions` 依 session_id 排序 |
| `device.list` `{}` | `devices` 依 by_id 排序，元素 `{"by_id","real_path"}` |
| `no.such` `{}` | `{"ok":false,"error_code":"METHOD_NOT_FOUND","method":"no.such","version":v}` |
| `health.status` 純模板設定、有裝置 | 含 `"warnings":["no_profiles_loaded"]`（RPC-H-17：只看 explicit target） |
| `health.status` 無裝置 | `warnings` 依序含 `no_profiles_loaded`（若適用）、`no_devices_found`（`service.py` L570-573） |
| `health.status` allowlist 啟用 | 在 `multi_open_detail` 之後出現 `"test_device_allowlist":{"enabled":true,"coms":["COM9"],"source":"env"}`（P1 T13 凍結形狀，`source ∈ {env, config}`；SC-19）；未啟用時無此鍵 |
| `session.interactive_status` `{"interactive_id":"<id>"}`（無 selector） | 經 `route_interactive` 回 lease 狀態；`{"interactive_id":"nope"}` → `INTERACTIVE_NOT_FOUND`；`{}` → `INVALID_ARGS`（`service.py:850-869` 逐字） |
| `command.cancel` 已排隊命令 | 回 `{"ok":true,"cmd_id":…,"status":"canceled"}`，並 best-effort `route(sid, CancelQueued)`，之後該 session `queue.physical_len()` 減 1 |
| `health.status` multi_open | `ProcScan` fake 回兩個 daemon → `multi_open:true`、`multi_open_detail.daemons` 依 pid 排序；`foreign_holders` 含自身 pid（RPC-D-10） |

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-daemon --features test-fakes --test dispatch`
Expected：FAIL，`not implemented: slice21 T18 Service::new`。

- [ ] **Step 3：實作**

`dispatch` 為單一平面 `match method`（CLAUDE.md「RPC 路由是平面 if/elif」），分支順序與 `service.py:705-1174` 相同；deferred 15 個 method 不寫分支、落入 `_ =>` 的 `METHOD_NOT_FOUND`。每個分支的轉型逐行照 02 §5.2／§7（`Params::str_or`、`int_or`、`float_or`、`truthy`、`raw`、`selector_chain`）；`session.interactive_send／status／close` 只取 `interactive_id`（空 → `INVALID_ARGS`）並經 `manager.route_interactive`，不解析 selector；其餘 session 類 method 先 `manager.resolve(selector)`，查無回 `{"ok":false,"error_code":"SESSION_NOT_FOUND","selector":<原值>}`，再 `manager.route(sid, req, 60 s)`（`session.recover`、`session.attach`、`session.self_test` 用 600 s）。`rpc()`＝`dispatch` 後 `setdefault("version", version)`。`health()` 照 02 §7.1：`commands = registry.len()`、`multi_open` 由 `procs.serialwrapd_instances()`／`serialwrapd_tty_holders(manager.attached_real_paths())`，掃描失敗 → `{multi_open:false, foreign_holders:{}, multi_open_detail:{daemons:[], holders_status:"unknown"}}`。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-daemon --features test-fakes --test dispatch`
Expected：PASS。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-daemon crates/sw-core/src/mcu_patterns.rs changelog.d/rust-s21-dispatcher.md
git commit -m "feat(sw-daemon): RPC 平面分派、參數轉型與 health.status" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 19：sw-daemon daemon_main（啟動順序、有序關機、exit code）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-daemon-main.md`（front matter `type: feat`、`scope: rust-s21`；本文：「Rust serialwrapd：Python 相同的參數、路徑 env 與 exit code（0/1/2/3）；啟動時先掃描、排定 COM、bootstrap attach 再啟 watcher（DF-14）；RPC 綁定後寫 config.yaml 的 socket_path（SC-09 寫入路徑）；關機先停 server、設 manager stopping gate、停 watcher、drain 全部 session（含在途 attach）、WAL sync 再釋放鎖（SC-11），未收斂時以 exit 1 與診斷回報；OD-11 allowlist 僅允許隔離實例。」）
**docker 先行（OD-15）：** systemd-user／systemd-system 監管下的 Rust `serialwrapd` 啟停先以 P2 Task 17 `scripts/docker/run-systemd.sh` 在 systemd container 驗證（journald 收到 daemon stderr、`systemctl show` 狀態），報告標 `L1.5-docker`；`windows-task` 啟動 Rust daemon 以 P2 Task 18 的 throwaway 排程工作（`serialwrap-throwaway-<run_id>`、隔離目錄、allowlist `COM20`）在擁有者 host 執行。

**BLOCKED-OWNER：** (1) systemd-user／systemd-system 監管下以 Rust `serialwrapd` 啟停的實機驗證（L3 P0；docker 結果不取代）；(2) Windows `windows-task` 正式（非 throwaway）監管下啟動 Rust daemon 的 Spike 2 完整證據。

**Files:**
- Modify: `crates/sw-daemon/src/daemon_main.rs`、`crates/sw-daemon/src/main.rs`、`crates/sw-daemon/src/launcher.rs`（`implementation == "rust"` 時改走 `daemon_main::run`）、`docs/rust-refactor/plans/rust-architecture.md` §6.1（啟動順序改為 P1 DF-14 的 `scan → rank → update → bootstrap_attach → watcher.start`；panic 政策）
- Test: `crates/sw-daemon/tests/daemon_process.rs`、`crates/sw-daemon/tests/restart_after_commit.rs`（support 一律在 `crates/sw-daemon/tests/support/mod.rs`）

**Interfaces:**
- Consumes：Tasks 2～18；P3 `RuntimePaths::resolve(env, native, home)`（`String` 欄位）、`resolve_config_path(env, paths, system_config_dir, flavor, home, exists)`、`parse_endpoint`、`load_profiles`、`RuntimeConfig::from_text`；P4 `RuntimeConfig::{set_socket, to_yaml}`、`FsOps::write_file`、`launcher.rs`；Task 0c `resolve_daemon_dirs`、`RuntimeConfig::{windows_exclude_coms, windows_test_device_allowlist}`。
- Produces：

```rust
pub struct DaemonArgs { pub profile_dir: PathBuf, pub socket: String, pub lock: PathBuf }
pub fn parse_args(argv: &[String], paths: &RuntimePaths) -> Result<DaemonArgs, (i32, String /* stderr */)>;
pub fn run(argv: Vec<String>, platform: sw_pal::Platform) -> i32;
#[cfg(feature = "test-fakes")] #[derive(Debug, Clone, PartialEq)]
pub enum TraceEvent { InitialScan, Rank, UpdateDevices, BootstrapAttach, WatcherStart, Bound, EndpointWritten, ServerStopped, WatcherStopped,
                      SessionsDrained { flushed: Vec<(String /*cmd_id*/, String /*error_code*/)>, stalled: Vec<String /*sid*/> }, WalShutdown { ok: bool }, LockReleased }
#[cfg(feature = "test-fakes")] pub struct DaemonHooks { pub before_state_commit: Option<Box<dyn Fn() + Send + Sync>>,
                                                       pub after_state_commit: Option<Box<dyn Fn() + Send + Sync>>,   // 兩者經 StateStore::spawn_with_hooks 傳入（Task 15）
                                                       pub trace: Option<Box<dyn Fn(TraceEvent) + Send + Sync>> }
#[cfg(feature = "test-fakes")] pub fn run_with_hooks(argv: Vec<String>, platform: sw_pal::Platform, hooks: DaemonHooks) -> i32;
#[derive(Clone, Debug, PartialEq)] pub struct TestAllowlist { pub enabled: bool, pub coms: Vec<String>, pub source: &'static str /* env|config|none */, pub rejected_reason: Option<String> }
pub fn resolve_test_allowlist(env: &dyn SysEnv, cfg: &RuntimeConfig, defaults: &RuntimePaths /* 未設 env 時的預設路徑 */) -> TestAllowlist;   // P1 T13 規格 1～3 逐字
fn config_read_path(env: &EnvSnapshot, paths: &RuntimePaths) -> Option<String>;   // SC-09：直接委派 P3 `resolve_config_path`（SERIALWRAP_CONFIG 原樣勝出，即使不存在 → CONFIG_DIR → 系統檔 → CONFIG_DIR）；P3 沒有 `config_candidates`
fn config_write_path(env: &EnvSnapshot, paths: &RuntimePaths) -> String;         // SC-09：SERIALWRAP_CONFIG（strip 非空、expanduser）> CONFIG_DIR/config.yaml；永不寫系統檔
fn to_endpoint_spec(e: &sw_proto::endpoint::Endpoint) -> EndpointSpec;           // sw-proto → sw-pal 的唯一轉換點
fn resolve_paths(p: &sw_pal::Platform) -> Result<RuntimePaths, String>;   // GAP-X1：SERIALWRAP_TCP_PORT 非整數 → Err("serialwrap: invalid SERIALWRAP_TCP_PORT: <v>\n")
fn check_backend_env(env: &dyn SysEnv, caps: &Capabilities) -> Result<(), String>; // SERIALWRAP_{RPC,LOCK,DEVICE}_BACKEND：非法值或非本平台後端 → Err
fn ensure_runtime_dirs(paths: &RuntimePaths, fs: &dyn FsOps) -> PalResult<()>;     // CONFIG/STATE/RUN/WAL/EVENTS/EVENTS_RUNTIME
fn inject_windows_passthrough(templates: &mut Vec<ProfileTemplate>);               // 無 passthrough 時追加 windows-default-passthrough（platform passthrough，同受 SC-35 安靜升 READY）
fn listen_options(env: &dyn SysEnv) -> ListenOptions;                              // 基線（SC-08 撤回）：mode 0o660、ensure_dir_mode None；SERIALWRAP_SOCKET_GROUP strip 後非空 → group
```

- [ ] **Step 1：寫失敗測試**

`crates/sw-daemon/tests/daemon_process.rs`（以 `env!("CARGO_BIN_EXE_serialwrapd")` 實際起行程；隔離 env 9 個目錄＋pop 5 個）：

```rust
use std::{process::Command, time::Duration};
mod support;
use support::{isolated_env, rpc_once, wait_socket};

#[test]
fn second_daemon_same_lock_exits_2_with_stderr() {
    let env = isolated_env();
    let mut a = Command::new(env!("CARGO_BIN_EXE_serialwrapd")).envs(&env.vars).spawn().unwrap();
    wait_socket(&env.socket, Duration::from_secs(10));
    let out = Command::new(env!("CARGO_BIN_EXE_serialwrapd")).envs(&env.vars).output().unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert_eq!(String::from_utf8_lossy(&out.stderr), "serialwrapd: DAEMON_ALREADY_RUNNING\n");
    rpc_once(&env.socket, "{\"id\":1,\"method\":\"daemon.stop\"}");
    assert_eq!(a.wait().unwrap().code(), Some(0));
}

#[test]
fn unreadable_state_json_exits_3_and_releases_lock() {
    let env = isolated_env();
    std::fs::create_dir_all(env.state_dir.join("state.json")).unwrap();       // 讀取失敗
    let out = Command::new(env!("CARGO_BIN_EXE_serialwrapd")).envs(&env.vars).output().unwrap();
    assert_eq!(out.status.code(), Some(3));
    assert!(String::from_utf8_lossy(&out.stderr).starts_with("serialwrapd: 無法讀取既有 state.json（"));
}
```

| 測試名 | 精確斷言 |
|---|---|
| `no_profiles_message_and_continue` | 空 profile dir → stderr 含 `serialwrapd: no profiles loaded\n`，daemon 仍可 `health.ping` |
| `unknown_arg_exit_2_usage` | `--bogus` → exit 2，stderr `usage: serialwrapd [-h] [--profile-dir PROFILE_DIR] [--socket SOCKET] [--lock LOCK]\nserialwrapd: error: unrecognized arguments: --bogus\n` |
| `no_args_uses_env_paths` | 只設 env、無參數啟動 → socket 位於 `$SERIALWRAP_RUN_DIR/serialwrapd.sock`、lock 位於 `$SERIALWRAP_RUN_DIR/serialwrapd.lock`（regression ThrowawayDaemon 契約） |
| `endpoint_written_after_bind` | 啟動後 `$SERIALWRAP_CONFIG_DIR/config.yaml` 為 `socket_path: <socket>\n`（加上既有 key，PyYAML `safe_dump` 排序）；config 目錄唯讀 → stderr `serialwrapd: 寫入 config.yaml endpoint 失敗（非致命）: <msg>` 且 daemon 存活 |
| `serialwrap_config_env_read_and_write_target` | 設 `SERIALWRAP_CONFIG=<tmp>/alt.yaml`（含 `windows: {exclude_coms: [COM3]}`）→ daemon 讀此檔（exclude 生效）、endpoint 寫入此檔、`CONFIG_DIR/config.yaml` 未被建立；未設時讀 `CONFIG_DIR` → 系統檔 fallback（唯讀，永不寫入系統檔；P1 T15） |
| `startup_order_scan_rank_watch` | in-process `run_with_hooks`（FakeDeviceSource 預置 3 個 by-id）：trace 序列前段恰為 `[InitialScan, Rank, UpdateDevices, BootstrapAttach, WatcherStart, Bound, EndpointWritten]`（DF-14；與 P1 `test_service_start_scans_before_watcher_thread` 的 `scan_once → prepare_dynamic_rank → update_devices → bootstrap_attach → watcher.start` 相同）；COM 依 by-id 排序 |
| `sigterm_graceful_exit_0_and_flushes_queue` | in-process：READY、前景命令 `BlockUntilCancel`、另排隊 2 筆 → 送 stop → trace 後段恰為 `[ServerStopped, ShutdownGate, WatcherStopped, SessionsDrained{flushed: [(c2,"FLUSHED_BY_SHUTDOWN"),(c3,"FLUSHED_BY_SHUTDOWN")], stalled: []}, WalShutdown{ok:true}, LockReleased]`，`run_with_hooks` 回 0 |
| `real_sigterm_to_started_daemon_exits_0_after_flush` | 實際行程（watcher、actor、RPC、StateStore thread 都已啟動、session READY、有排隊命令）收 `SIGTERM` → exit 0；之後讀 WAL 最後一筆紀錄完整、`FLUSHED_BY_SHUTDOWN` 已寫入 command 紀錄（以停機前最後一次 `command.get` 輪詢驗證）、lock 可被新 daemon 取得 |
| `shutdown_with_stalled_drain_exits_1_with_diagnostic` | in-process：一個 session writer `BlockForever` → stop → `SessionsDrained{stalled:["t:COM0"]}`，stderr 含 `serialwrapd: shutdown incomplete: drain stalled: t:COM0`，`run_with_hooks` 回 1（不宣告 graceful success）；WAL 仍 sync＋close、lock 仍釋放（行程結束時 kernel 關閉殘留 fd） |
| `shutdown_wal_sync_failure_exits_1` | WAL `sync` 失敗 → stderr `serialwrapd: shutdown incomplete: WAL sync failed: <py_message>`、回 1 |
| `shutdown_releases_tty_before_exit` | PTY master 由測試全程持有；停機後立即以 `open(O_RDWR)` 開同一 PTY slave 成功（handle 已關、TIOCEXCL 由 `CloseToken` 的 TIOCNXCL 解除——行程結束本身不會清除 master 仍開啟的 PTY 旗標，故此斷言能抓到漏做 TIOCNXCL，R0-F3） |
| `wal_dir_readonly_at_start_blocks_login_probe`（Review Focus 3，非 root；root 下 skip 為證據不足） | `SERIALWRAP_WAL_DIR` 指向唯讀目錄 → daemon 啟動、`health.status.wal.healthy==false`、`unhealthy_reason=="dir_unwritable"`；shell profile 的登入 probe 不送（FakeTarget 收到 0 bytes）、session 停 `ATTACHED`＋`last_error:"PROMPT_UNAVAILABLE"`；`command.submit` 回 `SESSION_NOT_READY` 且仍 0 bytes |
| `wal_fault_after_ready_splits_agent_and_auto_tx`（Review Focus 3） | 先 READY，再把 WAL 目錄改唯讀並刪除 `raw.wal.ndjson`（重建失敗）→ `health.status.wal.healthy==false`；`command.submit echo x` → FakeTarget 收到 `echo x\n`、`command.get` 終態 `done`；之後 `session self-test`（`SelfTestProbe`，自動 TX）的 probe bytes 未送出 |
| `daemon_stop_during_force_recover_with_client_gone`（Review Focus 5） | 真 daemon：同連線 pipelining `session.recover --force`（DUT 不回 prompt）與 `daemon.stop`，client 送出後立即斷線 → recover handler 跑完（WAL 有其救援 TX）、WAL `close` 發生在 handler 結束之後（以 trace 順序斷言）、行程 exit 0 於 15 s 內 |
| `allowlist_rejected_reasons_exact` | 參數化：只設 allowlist、`SERIALWRAP_STATE_DIR` 未設 → stderr 恰為 `serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: not an isolated instance: SERIALWRAP_STATE_DIR unset\n`；`SERIALWRAP_RUN_DIR` 等於預設路徑 → `…: not an isolated instance: SERIALWRAP_RUN_DIR equals default\n`；config `windows.test_device_allowlist: "COM7"`（非 list）→ `…: invalid windows.test_device_allowlist\n`；三者皆 exit 2 且 lock 已釋放（P1 T13 規格 2～4） |
| `allowlist_env_overrides_config` | 隔離實例、env `/x/by-id/a`、config `windows.test_device_allowlist: [/x/by-id/b]` → 只看到 a；`health.status.test_device_allowlist == {"enabled":true,"coms":["/x/by-id/a"],"source":"env"}` |
| `uncaught_panic_exit_1` | `run_with_hooks` 在 `run()` 主流程注入 panic → 行程 exit 1、stderr 有 panic 訊息 |
| `handler_panic_does_not_kill_daemon` | 真 daemon：測試專用 method（test-fakes build）在 handler 內 panic → 回 `{"ok":false,"error_code":"EXCEPTION","message":…}`，之後 `health.ping` 仍成功（panic hook 只記錄、不 exit） |
| `backend_env_invalid_exit_1` | `SERIALWRAP_RPC_BACKEND=" posix"` → exit 1（02 §2.1：不 strip，ValueError） |

`crates/sw-daemon/tests/restart_after_commit.rs`（Review Focus 1）：

```rust
#[test]
fn kill_between_commit_and_reply_keeps_released_after_restart() {
    if std::env::var("S21_CHILD").as_deref() == Ok("1") { support::child_daemon_abort_after_commit(); return; }
    let env = support::isolated_env_with_pty_target();
    let mut child = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "kill_between_commit_and_reply_keeps_released_after_restart", "--nocapture"])
        .env("S21_CHILD", "1").envs(&env.vars).spawn().unwrap();
    support::wait_ready(&env, "COM0");
    let r = support::rpc_once(&env.socket, "{\"id\":1,\"method\":\"device.release\",\"params\":{\"selector\":\"COM0\"}}");
    assert!(r.is_empty(), "reply must not be sent: process aborts right after commit");
    assert!(!child.wait().unwrap().success());
    let mut d2 = std::process::Command::new(env!("CARGO_BIN_EXE_serialwrapd")).envs(&env.vars).spawn().unwrap();
    support::wait_socket(&env.socket, std::time::Duration::from_secs(10));
    let s = support::get_state(&env.socket, "COM0");
    assert_eq!(s["state"], "RELEASED");
    assert!(support::pty_can_open_exclusively(&env.pty_slave), "daemon must not reopen the released tty");
    support::rpc_once(&env.socket, "{\"id\":2,\"method\":\"daemon.stop\"}"); d2.wait().unwrap();
}
```

另一案例 `kill_before_commit_restores_previous_state`：子程序以 `before_state_commit: Some(abort)` 執行 → release 請求無回應、子程序異常結束 → 重啟後 `state.json` 為 release 前內容（該 sid 不在 `released`）、session 依裝置存在重新 attach 為 `ATTACHING`／`READY`（不是 `RELEASED`），且 daemon 不宣稱該次 release 完成（§6.2「提交完成前則依最後有效的持久化狀態恢復」）。

（`support::child_daemon_abort_{after,before}_commit` 以 `run_with_hooks(vec!["serialwrapd".into()], sw_pal::native(), DaemonHooks{ before_state_commit / after_state_commit: Some(Box::new(|| std::process::abort())), trace: None })` 執行；`run_with_hooks` 把兩個 hook 交給 `StateStore::spawn_with_hooks`（Task 15），hook 只在 `test-fakes` feature 編譯，產品不讀任何測試 env。）

- [ ] **Step 2：執行並確認 FAIL**

Run：`cargo test -p sw-daemon --features test-fakes --test daemon_process --test restart_after_commit`
Expected：FAIL（daemon 以 `not implemented: slice21 T19 run` panic，exit 101）。

- [ ] **Step 3：實作**

```rust
pub fn run(argv: Vec<String>, p: sw_pal::Platform) -> i32 { run_inner(argv, p, &Hooks::default()) }
// Hooks：私有、恆編譯、產品全為 None；test-fakes 的 run_with_hooks 由 DaemonHooks 轉成 Hooks（含 trace）
fn run_inner(argv: Vec<String>, p: sw_pal::Platform, hooks: &Hooks) -> i32 {
    // main.rs 已在建立任何 thread 之前呼叫 p.signals.install()（見下方 main.rs 說明）
    let env = EnvSnapshot::from_pairs(p.env.vars());
    let paths = match resolve_paths(&p) { Ok(x) => x, Err(msg) => { eprint!("{msg}"); return 1; } };
    let dirs = sw_proto::runtime_paths::resolve_daemon_dirs(&env);
    if let Err(msg) = check_backend_env(&*p.env, &p.caps) { eprintln!("serialwrapd: {msg}"); return 1; }
    let args = match parse_args(&argv, &paths) { Ok(a) => a, Err((code, err)) => { eprint!("{err}"); return code; } };
    if let Err(e) = ensure_runtime_dirs(&paths, &*p.fs) { eprintln!("{}", e.py_message); return 1; }
    let mut load = match load_profiles(&args.profile_dir.to_string_lossy(), &*p.fs) { Ok(l) => l, Err(e) => { eprintln!("{e}"); return 1; } };
    if load.profiles.is_empty() && load.templates.is_empty() { eprint!("serialwrapd: no profiles loaded\n"); }
    if p.caps.native_device == DeviceBackendKind::Win { inject_windows_passthrough(&mut load.templates); }
    let ep = match sw_proto::endpoint::parse_endpoint(&args.socket) { Ok(e) => to_endpoint_spec(&e), Err(e) => { eprintln!("serialwrapd: {}", e.0); return 1; } };
    let lock = match p.instance_lock.acquire(&PathBuf::from(&args.lock), &ep) {
        Ok(g) => g,
        Err(InstanceLockError::AlreadyRunning) => { eprint!("serialwrapd: DAEMON_ALREADY_RUNNING\n"); return 2; }
        Err(InstanceLockError::SocketUnavailable) => { eprint!("serialwrapd: SOCKET_UNAVAILABLE\n"); return 2; }
        Err(InstanceLockError::Io(e)) => { eprintln!("{}", e.py_message); return 1; }
    };
    let cfg = config_read_path(&env, &paths)                                      // SC-09 讀取順序；錯誤一律視為空 config（_safe_runtime_config）
        .and_then(|path| p.fs.read_bytes(Path::new(&path)).ok())
        .and_then(|b| RuntimeConfig::from_text(Some(&String::from_utf8_lossy(&b))).ok())
        .unwrap_or_else(|| RuntimeConfig::from_text(None).unwrap());
    let allow = resolve_test_allowlist(&*p.env, &cfg, &RuntimePaths::resolve(&env.without(&["SERIALWRAP_STATE_DIR", "SERIALWRAP_RUN_DIR", "SERIALWRAP_CONFIG_DIR"]), p.native_traits(), &HomeFromSysEnv(&*p.env)).unwrap());   // 預設路徑＝移除三個隔離變數後的解析結果（HOME／XDG 照舊）
    if let Some(reason) = &allow.rejected_reason { eprint!("serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: {reason}\n"); lock.release(); return 2; }
    let loaded = match load_state(&Path::new(&paths.state_dir).join("state.json"), &*p.fs) {
        Ok((s, _backup)) => s,
        Err(e) => { eprint!("serialwrapd: {}\n", e.message); lock.release(); return 3; }
    };
    let wal = Arc::new(WalWriter::open(WalConfig { dir: PathBuf::from(&paths.wal_dir), rotate_bytes: DEFAULT_WAL_ROTATE_BYTES }, p.wal_fs.clone(), p.clock.clone()));
    let registry = CommandRegistry::new();
    let explicit_targets = load.profiles.len();
    let warn: Arc<dyn Fn(String) + Send + Sync> = Arc::new(|m: String| eprintln!("{m}"));
    let manager = SessionManager::new(ManagerDeps {                                // new() 內建立 WalHealthFanout 並 wal.set_health_sink
        serial: p.serial.clone(), uart_excl: p.uart_excl.clone(), console: p.console.clone(), caps: p.caps.clone(),
        wal: wal.clone(), registry: registry.clone(), state: spawn_state_store(&paths, &p, hooks), loaded,
        load, clock: p.clock.clone(), env: p.env.clone(), fs: p.fs.clone(), warn });
    let mut src = (p.devices)(DeviceSelection { by_id_dir: dirs.by_id_dir.clone().into(), by_path_dir: dirs.by_path_dir.clone().into(),
                                                exclude_coms: cfg.windows_exclude_coms(), test_allowlist: allow.enabled.then(|| allow.coms.clone()) });
    let initial = DeviceWatcher::initial_scan(&mut *src).unwrap_or_default();                  // DF-14：先掃描
    manager.prepare_dynamic_rank(&initial.iter().map(|(k, _)| k.clone()).collect::<Vec<_>>());  // 再排 COM
    manager.update_devices(&initial);
    manager.bootstrap_attach();                                                                 // P1 DF-14：bootstrap 在 watcher.start 之前
    let watcher = DeviceWatcher::start(src, Duration::from_secs(1), manager.watcher_sender(), p.clock.clone());
    let svc = Arc::new(Service::new(ServiceDeps { manager: manager.clone(), wal: wal.clone(), registry: registry.clone(),
        procs: p.procs.clone(), version: sw_proto::version::version().to_string(),
        started_at: sw_proto::pycompat::iso_utc_now(p.clock.wall_unix_us()), explicit_target_count: explicit_targets,
        test_device_allowlist: allow.enabled.then(|| allow.clone()), pid: p.env.pid() }));
    let listener = match p.rpc_listen.bind(&ep, &listen_options(&*p.env)) { Ok(l) => l, Err(e) => { eprintln!("{}", e.py_message); return 1; } };
    let (stop_tx, stop_rx) = crossbeam_channel::bounded(1);
    let server = RpcServer::start(listener, Arc::new(ServiceHandler::new(svc.clone(), stop_tx)));
    let mut cfg_w = cfg.clone(); cfg_w.set_socket(&args.socket);                                // P4；寫入目標依 SC-09（非原子，KEEP）
    if let Err(e) = p.fs.write_file(Path::new(&config_write_path(&env, &paths)), cfg_w.to_yaml().as_bytes()) {
        eprint!("serialwrapd: 寫入 config.yaml endpoint 失敗（非致命）: {}\n", e.py_message);
    }
    p.signals.wait_stop(stop_rx);
    let mut incomplete: Vec<String> = Vec::new();
    if !server.stop(Duration::from_secs(10)) { incomplete.push("rpc handlers did not converge".into()); }   // SC-11 ① 停 server（等進行中的 handler 回應）
    manager.begin_shutdown();                                                                   // ①b stopping gate（R1-F2）：之後熱插／bootstrap／recover 不再 open，channel 殘留 Delta 被丟棄
    watcher.stop();                                                                             // ② 停 watcher（P1：drain 前，避免 drain 中觸發 attach）
    let drained = manager.shutdown(Duration::from_secs(5));                                     // ③ drain 全部 session、FLUSHED_BY_SHUTDOWN
    let stalled: Vec<String> = drained.iter().filter(|(_, ok)| !ok).map(|(s, _)| s.clone()).collect();
    if !stalled.is_empty() { incomplete.push(format!("drain stalled: {}", stalled.join(","))); }
    if let Err(e) = wal.shutdown() { incomplete.push(format!("WAL sync failed: {}", e.py_message)); }   // ④ WAL flush＋sync＋close
    lock.release();                                                                             // ⑤
    if incomplete.is_empty() { 0 } else { eprint!("serialwrapd: shutdown incomplete: {}\n", incomplete.join("; ")); 1 }
}
```

- `main.rs`：第一個動作 `let p = sw_pal::native(); if let Err(e) = p.signals.install() { eprintln!("{}", e.py_message); exit(1) }`（在建立任何 thread 之前，Task 4）；`std::panic::set_hook` **只**把 panic 訊息與位置寫到 stderr（不呼叫 `exit`，讓 framing／actor／reader 的 `catch_unwind` 能產生 `EXCEPTION` 或 WARNING）；整個 `daemon_main::run` 以 `std::panic::catch_unwind` 包住，未被攔截的 panic → `exit(1)`（arch §6.1「未攔截的 panic：exit 1」；停機未收斂的 exit 1 由 `run` 本身回傳，見下方停機規則）。先依 P4 launcher 判斷 manifest：`implementation == "python"` → launcher 既有路徑；`"rust"` 或無 manifest（開發 build）→ `daemon_main::run(std::env::args().collect(), p)`。各工作 thread 的 panic 隔離：RX chunk（Task 10）、actor 事件（Task 11）、watcher tick（Task 5，WARNING `device watcher tick 例外，略過本輪`）、RPC handler（Task 17）。
- `resolve_test_allowlist`（SC-19，P1 T13 規格 1～3 逐字）：env `SERIALWRAP_TEST_DEVICE_ALLOWLIST`（逗號分隔、strip、去空）優先（`source:"env"`），否則 `config.yaml windows.test_device_allowlist`（`source:"config"`；`windows_test_device_allowlist()` 回 `Err(())` → `rejected_reason="invalid windows.test_device_allowlist"`）；皆無 → `enabled:false, source:"none"`；啟用時依 STATE、RUN、CONFIG 順序檢查隔離：未設（strip 後空）→ `"not an isolated instance: <VAR> unset"`，`normcase(abspath(expanduser(v)))` 等於預設 → `"not an isolated instance: <VAR> equals default"`，回報第一個違規。
- `spawn_state_store(&paths, &p, hooks)`：`hooks` 兩個 commit hook 皆 None 時呼叫 `StateStore::spawn`；`test-fakes` 下 `run_with_hooks` 傳入的 `before_state_commit`／`after_state_commit` 經 `StateStore::spawn_with_hooks` 交給 store thread。trace 事件在 `run_inner` 對應位置以 `hooks.trace(...)` 送出（產品為 no-op）。
- logging：`Effect::Warn` 與 PAL WARNING 一律以 `%(message)s` 格式寫 stderr（GAP-G-01）。
- `check_backend_env`：值依 02 §2.1 規則（不 strip、`or "auto"`）解析；非 `posix|unix|termios|win|windows|win32|auto` → `Err("unsupported backend: '<v>'")`；合法但不是本 build 的原生後端（Rust 後端在編譯期決定）→ `Err("backend '<v>' is not available in this build")`；兩者 exit 1（stderr 文字非契約，G5）。
- 停機未收斂（drain stalled、handler 未收斂、WAL sync 失敗）回 exit 1 並寫診斷，不宣告 graceful success；Python 基線在此情境的 exit code 未凍結，PR 描述登記為 SC-11 細化（不屬 A/B 比較範圍，A/B 案例不製造停機未收斂）。

- [ ] **Step 4：執行並確認 PASS**

Run：`cargo test -p sw-daemon --features test-fakes --test daemon_process --test restart_after_commit && cargo build --release -p sw-daemon --target x86_64-unknown-linux-gnu`
Expected：PASS；release binary 產出於 `target/x86_64-unknown-linux-gnu/release/serialwrapd`。

- [ ] **Step 5：Commit**

```bash
git add crates/sw-daemon changelog.d/rust-s21-daemon-main.md
git commit -m "feat(sw-daemon): serialwrapd 啟動順序、有序關機與 exit code" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 20：黑箱契約與既有套件改指 Rust（F1／F2／F3／F4／F12＋安全骨架）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-blackbox.md`（front matter `type: feat`、`scope: rust-s21`；本文：「新增 Slice 2.1 黑箱契約案例（F1／F2／F3／F4／F12 L1 承接、零 TX、獨占、release／attach、帳密未解析、WAL 自癒），同一批案例可指向 Python 或 Rust daemon；修正 Rust daemon 與基線不一致處。」）
**BLOCKED-OWNER：** (1) L2：以 Rust daemon 執行 TestPilot `serialwrap_regression` F1、F2、F3、F4、F12 的 18 個 case（其中 17 個屬 `cases.lock.json` 的 `slice_2.1` 分母；`f4-interactive-line-cmd-defined` 屬 2.2、作提前證據；含 F12 已依 OD-13／TST-D-7 修訂的 oracle），需擁有者 live bench；(2) L3：`realhw` P0 適用核心集合＋WAL／裝置衝突／Recovery 相關 P1，雙平台，需 bench；(3) Windows 端以 Rust daemon 跑同一批黑箱：OD-15 起改為擁有者 host 可執行（`SW_TEST_COM_PAIR=COM20:COM21`，見 Task 22），不再列 BLOCKED-OWNER。交 bench 前，(1)(2) 的情境先以 `scripts/docker/run-l15.sh` 兩容器拓樸跑過並附 `L1.5-docker` 報告（OD-15；不計為 L2／L3）。

**Files:**
- Create: `tests/contract_blackbox/slice21/test_f1_cmd_contract.py`、`test_f2_backpressure.py`、`test_f3_observability.py`、`test_f4_session_semantics.py`、`test_f12_diagnostics.py`、`test_safety_core.py`、`test_slice_scope.py`
- Modify: `tests/contract_blackbox/slice21/conftest.py`（加 `cred_daemon`、`banner_target` fixture）
- Modify（測試資產變更，§10.4，Step 0）：`tests/test_multiagent_e2e.py`、`tests/test_human_agent_coexist.py`（CLI／daemon 改由 `tests/ab_env.AB_ENV` 選擇，未設時維持原本 `[PYTHON, ROOT/serialwrapd.py]` 與 `ROOT/serialwrap`）
- Create: `tests/test_ab_env_existing_suites.py`、`contracts/ab_normalization.yaml`
- Modify（只在發現差異時）：`crates/sw-*/src/**`（修正）、對應 Rust L1 測試（每個修正補一個重現測試）
- Test: 上列 pytest 檔；既有 `tests/test_multiagent_e2e.py`、`tests/test_human_agent_coexist.py`（t2、t3、t4）、`func-test/`（`sm-01-attach-to-ready`、`ha-05-concurrent-submit`、`sm-04-device-unplug`）

**Interfaces:**
- Consumes：Task 15 harness／fixtures（含 `artifact_identity`）；Task 15a `FakeTarget`（`emit`、`written`、`slave_path`）；`bb_harness.cli_prefix()`（A/B 兩輪固定為 P4 Rust CLI release：`target/x86_64-unknown-linux-gnu/release/serialwrap`，§15.9.1）；`tests/ab_env.AB_ENV`；Task 19 release binary；`func-test/runner.py`（`--case` 單值）。
- Produces：`tests/contract_blackbox/slice21/` 完整案例集；`pytest -m cap_posix_pty` 標記（P2 已在 conftest 註冊 `cap_<resource>` marker，TST-D-4）。

- [ ] **Step 0：既有套件改用 A/B 選擇器（測試資產變更，§10.4，獨立審閱）**

`tests/conftest.py` 會 pop `SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE`，且兩支既有套件寫死 `[PYTHON, ROOT/"serialwrapd.py"]`，直接設 env 的「Rust 輪」會靜默跑 Python 而 PASS。修正：兩檔改以 `from ab_env import AB_ENV` 取 `SERIALWRAP_DAEMON_EXE`／`SERIALWRAP_CLI_EXE`（未設時原值不變），並在 daemon 啟動後斷言 `os.readlink(f"/proc/{pid}/exe")` 的 realpath 等於所選 artifact（Python 輪等於 `sys.executable` 的 realpath），不符即 `pytest.fail("artifact mismatch")`。`tests/test_ab_env_existing_suites.py`：`test_default_path_unchanged`（未設 env 時兩檔解析出的 argv 與修改前逐字相同）、`test_env_selects_artifact`（設 env 時 argv[0] 為該 artifact）、`test_mismatch_detected`（以 stub 使 `/proc/<pid>/exe` 不符 → fail）。PR 描述列出本步驟與審閱者。

- [ ] **Step 1：寫失敗測試**

`test_f12_diagnostics.py`（OD-13 修訂 oracle：讀回可關聯 marker 的新紀錄並驗證 CRC，不以 mtime 判定）：

```python
import base64, binascii, re, secrets, subprocess, zlib
import pytest

pytestmark = pytest.mark.cap_posix_pty

def test_f12_version_reported_consistent(shell_daemon):
    cli_v = subprocess.run([shell_daemon.cli, "--version"], capture_output=True, text=True, env=shell_daemon.env).stdout
    status = shell_daemon.sw("daemon", "status")
    assert re.search(r"\d+\.\d+\.\d+", cli_v).group(0) == re.search(r"\d+\.\d+\.\d+", status["version"]).group(0)

def test_f12_wal_path_live_marker_crc(shell_daemon):
    st = shell_daemon.sw("daemon", "status")
    start = shell_daemon.sw("wal", "current-seq")["seq"]
    marker = f"MARKER_{secrets.token_hex(3)}"
    sub = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", f"echo {marker}", "--source", "agent:f12")
    shell_daemon.wait_command_done(sub["cmd_id"], timeout=15)               # 決定性同步：等命令終態，TX 必已寫入 WAL
    page = shell_daemon.sw("log", "tail-raw", "--selector", "COM0", "--from-seq", str(start), "--limit", "200")
    hits = [r for r in page["records"] if r["dir"] == "TX" and marker.encode() in base64.b64decode(r["payload_b64"])]
    assert hits, "TX record carrying the marker must appear after start seq"
    r = hits[0]
    raw = base64.b64decode(r["payload_b64"])
    assert r["cmd_id"] == sub["cmd_id"] and r["len"] == len(raw) and r["crc32"] == f"{zlib.crc32(raw) & 0xffffffff:08x}"
    assert r["seq"] > start and st["wal_path"].endswith("raw.wal.ndjson") and st["wal"]["healthy"] is True
```

`test_safety_core.py`（Slice 2.1 安全出口，L1 部分）：

```python
import errno, os, time
import pytest

pytestmark = pytest.mark.cap_posix_pty

def test_zero_tx_while_autoboot_quiet(shell_daemon):
    t = shell_daemon.targets["COM0"]
    t.emit(b"\r\nU-Boot 2024.01\r\nHit any key to stop autoboot:  3 \r\n")
    time.sleep(0.5)
    before = len(t.written())
    r = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo should-not-send")
    assert r["_rc"] == 2 and r["error_code"] == "AUTOBOOT_QUIET" and 179.0 <= r["retry_after_s"] <= 180.0
    time.sleep(1.0)
    assert t.written()[before:] == b""

def test_external_open_blocked_then_release_attach_handoff(shell_daemon):
    if os.geteuid() == 0:
        pytest.skip("EVIDENCE_MISSING: TIOCEXCL bypassed for root (§6.2)")
    slave = shell_daemon.targets["COM0"].slave_path
    with pytest.raises(OSError) as e:
        os.open(slave, os.O_RDWR | os.O_NOCTTY)
    assert e.value.errno == errno.EBUSY                                     # BC-04 TIOCEXCL
    rel = shell_daemon.sw("device", "release", "--selector", "COM0", "--source", "agent:t", "--reason", "flash")
    assert rel["ok"] is True and rel["session"]["state"] == "RELEASED"
    fd = os.open(slave, os.O_RDWR | os.O_NOCTTY)                             # 外部工具接手；FakeTarget 持有 master，daemon 關閉前漏 TIOCNXCL 時此處 EBUSY（R0-F3）
    held = shell_daemon.sw("device", "attach", "--selector", "COM0")
    assert held["error_code"] == "DEVICE_STILL_HELD" and os.getpid() in held["pids"] and held["holder_unknown"] is False
    before = len(shell_daemon.targets["COM0"].written())
    forced = shell_daemon.sw("device", "attach", "--selector", "COM0", "--force")   # §6.2：force 不得略過已識別外部 holder
    assert forced["_rc"] == 2 and forced["error_code"] == "DEVICE_STILL_HELD"
    assert forced["pids"] == [os.getpid()] and forced["holder_unknown"] is False
    shell_daemon.wait_state("COM0", "RELEASED", timeout=1.0)              # 被拒後 released 保護不得被清除
    time.sleep(1.0)
    assert shell_daemon.targets["COM0"].written()[before:] == b"", "被拒的 force attach 不得對 UART 送出任何 byte"
    os.close(fd)
    ok = shell_daemon.sw("device", "attach", "--selector", "COM0")
    assert ok["ok"] is True
    shell_daemon.wait_state("COM0", "READY")
```

其餘案例（每列一個 test function；`shell_daemon` 為 READY 的 COM0，FakeTarget 對 `echo X` 回 `X\r\nroot@t:/# `、對 `sleep N` 在 N 秒後回 prompt、對 `\x03` 回 `^C\r\nroot@t:/# `）：

| 檔案::測試名 | 承接 case | 精確斷言 |
|---|---|---|
| `test_f1_cmd_contract.py::test_limits_queryable` | f1-limits-queryable | `daemon status.limits.max_submit_cmd_bytes == 16384`、`warn_submit_cmd_bytes == 4096` |
| `::test_too_long_rejected` | f1-too-long-rejected | 16385 bytes → `_rc==2`、`error_code=="CMD_TOO_LONG"`、`cmd_length==16385`、`limit==16384`；FakeTarget 未收到任何新 byte |
| `::test_newline_rejected` | f1-newline-rejected | `--cmd "a\nb"` → `CMD_CONTAINS_NEWLINE` |
| `::test_near_limit_no_logout` | f1-near-limit-no-logout | 4000 bytes 的 `echo` → `command.status=="done"`，session 仍 `READY` |
| `::test_cmdid_survives_timeout` | f1-cmdid-survives-timeout | `sleep 20 --cmd-timeout 2` → 期間 `cmd status` 從未回 `CMD_NOT_FOUND`；終態 `done` 且 `recovery_action=="CTRL_C"` |
| `test_f2_backpressure.py::test_queue_full_backpressure` | f2-queue-full-backpressure | 前景 `sleep 30` 期間連送 300 筆 → 至少一筆 `SESSION_QUEUE_FULL`（`pending==256`、`limit==256`）；全數 cancel 後 `echo ok` 為 `done` |
| `::test_history_bounded` | f2-history-bounded-rss | 600 筆 `echo i` 全部 `done`；`daemon status.commands <= 512`；daemon VmRSS 增量 < 32 MiB（`/proc/<pid>/status`） |
| `::test_recovery_flushes_queue` | f2-recovery-flushes-queue | 前景 `sleep 30`＋排隊 5 筆 → `session recover` → 5 筆 `FLUSHED_BY_RECOVERY`；10 s 內 `echo ok` 為 `done` |
| `::test_rx_window_crossing_prompt` | f2-rx-window-crossing-prompt | FakeTarget 對 `yes | head -c 200000` 回 200000 字元後 prompt → 之後 3 輪 `echo x` 皆 `done` 且 stdout `x` |
| `test_f3_observability.py::test_fail_error_code` | f3-fail-error-code | `session attach --selector NOSUCH` → `_rc==2`、`error_code=="SESSION_NOT_FOUND"`、`_stderr` 非空 |
| `::test_device_error_names_selector` | f3-device-error-names-selector | 上述 `_stderr` 含 `NOSUCH` |
| `::test_cmd_fail_observable` | f3-cmd-fail-observable | `cmd submit --selector NOSUCH` → `SESSION_NOT_FOUND`、`selector=="NOSUCH"` |
| `::test_log_tail_latest` | f3-log-tail-latest | `echo M_<hex>` 後 `log tail-text --selector COM0` 的 `lines` 有一行含 marker |
| `test_f4_session_semantics.py::test_activity_classification` | f4-activity-classification | 命令前後 `session activity` 的 `activity_classification` ∈ `{active, idle-healthy, quiet-suspicious, newly-attached, offline}` |
| `::test_background_result_tail_consistent` | f4-background-result-tail-consistent | bg 命令輸出 `M1..M50`（FakeTarget 分 10 個 chunk 送）→ 以 `next_chunk` 分頁讀完，每個 marker 恰出現 1 次 |
| `::test_interactive_line_cmd_defined` | f4-interactive-line-cmd-defined（lock 屬 2.2，提前證據，不計入 2.1 分母） | lease 期間 submit → 有 `error_code`（合法 (a)）或 `cmd_id` 且終態 `done/error` 非 `PROMPT_TIMEOUT`；close 後 READY |
| `test_safety_core.py::test_credentials_unresolved_no_empty_login` | f10-unresolved-creds-terminal（lock 屬 2.3，L1 前移的提前證據，不計入 2.1 分母） | `cred_daemon`（FakeTarget 只回 `login: `）→ `state=="ATTACHED"`、`last_error=="CREDENTIALS_UNRESOLVED"`、`FakeTarget.written()==b""` |
| `::test_released_survives_two_restarts_dynamic` | DF-04 | 純模板設定下 release → 停／啟 daemon 兩次 → 仍 `RELEASED`、外部 `os.open` 成功（daemon 未持有） |
| `::test_socket_mode_0660` | KEEP（SC-08 撤回，OD-14 #3） | 未設 `SERIALWRAP_SOCKET_GROUP`：`stat(endpoint).st_mode & 0o777 == 0o660`（Python 與 Rust 同值） |
| `::test_attach_not_ready_last_error_field` | SC-34 | `cred_daemon` 進 `ATTACHED`＋`CREDENTIALS_UNRESOLVED` 後 `serialwrap session attach --selector COM0` → stdout JSON `ok is False`、`error_code == "SESSION_NOT_READY"`、`last_error == "CREDENTIALS_UNRESOLVED"`、`"last_error_detail" in resp`；`session recover` 同情境 → `ok is True`、`recovered is False`、`error_code == "SESSION_NOT_READY"`、`last_error == "CREDENTIALS_UNRESOLVED"`；兩者 `t.written() == b""` |
| `::test_passthrough_quiet_ready_and_cmd` | SC-35 | 以 others-template 等價 profile（`platform: passthrough`、`prompt_regex: '.*'`、`ready_probe: ''`）起 daemon，FakeTarget 不主動輸出、以真實時間等待（常數 30 s 無 env 覆寫，本案約 40 s）：attach 後 `t.written() == b""`；29 s 時 `cmd submit` → `error_code == "SESSION_NOT_READY"` 且 `passthrough_ready_in_s` 為 0～1 的數；31 s 後 `get_state.state == "READY"`；`cmd submit --completion-regex '(?m)^__RC__=0\r*$'`（FakeTarget 對該命令回 `__RC__=0`）→ `partial is False`；無 completion regex 的命令逾時 → `error_code == "PROMPT_TIMEOUT"`、`recovery_action == "NONE"`、`t.written()` 不含 `b"\x03"`／`b"\x04"`、state 仍 `READY` |
| `::test_wal_unlink_recreates_with_meta` | BC-01 | 刪 `raw.wal.ndjson` 後送命令 → 新檔第一筆 `dir=="META"`、`meta.event=="WAL_RECREATED_AFTER_EXTERNAL_UNLINK"`、`loss_flag is True`、`potentially_lost_seq_range[1] < 新檔第二筆 seq` |
| `::test_recovering_submit_session_recovering` | SC-01 | FakeTarget 關閉（I/O error）瞬間 submit → `SESSION_RECOVERING` 或（已回 ATTACHING 時）`SESSION_NOT_READY`，絕不 `ok:true` |
| `::test_reject_paths_zero_tx`（參數化） | §11.3 不變量 1 | 每列先記 `before = len(t.written())` 與 WAL `current_seq`，觸發拒絕後 1 s：`t.written()[before:] == b""` 且 WAL 無新 TX 行。列：`CMD_TOO_LONG`、`CMD_CONTAINS_NEWLINE`、`SESSION_QUEUE_FULL`、`SESSION_NOT_READY`（DETACHED／ATTACHING／RELEASED 三態）、`SESSION_RECOVERING`、`SESSION_NOT_READY`（passthrough ATTACHED 未滿 30 s，SC-35）、`PROFILE_NOT_COMMAND_CAPABLE`（非 passthrough 的空 ready_probe profile）、`READY_UNCONFIRMED`、`AUTOBOOT_QUIET`、`SESSION_INTERACTIVE_BUSY`（lease 期間的執行期拒絕）、`INVALID_COMPLETION_REGEX`、延後 method 的 `METHOD_NOT_FOUND`（`file.push`） |
| `::test_stale_context_not_replayed` | §11.3 不變量（不回放失效上下文） | 開 agent lease 後依序做 release→attach、`session recover`、FakeTarget 送 U-Boot banner（reboot）三種情境；每種之後以舊 `interactive_id` `interactive-send "echo STALE"` → `INTERACTIVE_NOT_FOUND`，且 `t.written()` 不含 `STALE` |
| `::test_two_daemons_same_tty` | §6.2 兩個 daemon 競爭開埠 | 第二個 daemon 用不同 RUN／STATE／socket／lock、`SERIALWRAP_BY_ID_DIR` 指向同一個 FakeTarget slave → 第二個 daemon 的 session 不進 READY，`last_error` 以 `ATTACH_FAILED:DeviceHeldError` 開頭；`t.written()` 只含第一個 daemon 的 TX；root 下 skip（證據不足） |
| `test_slice_scope.py::test_deferred_methods`（僅 `SERIALWRAP_AB_LABEL` 以 `rust-s21` 開頭時執行） | 範圍清單 | 15 個 method 經 `rpc()` 皆回 `METHOD_NOT_FOUND`＋`method`＋`version`，且 WAL `current_seq` 不變 |

既有套件（不改原檔，只以 env 指向 artifact；`RUST_CLI=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrap`、`RUST_D=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrapd`，Python 輪把兩者換成 `$PWD/serialwrap` 與 `$PWD/tests/contract_blackbox/bin/py-serialwrapd`）：

| 命令 | 預期 |
|---|---|
| `SERIALWRAP_CLI_EXE=$RUST_CLI SERIALWRAP_DAEMON_EXE=$RUST_D python3 -m pytest -q tests/test_multiagent_e2e.py` | `1 passed` |
| `SERIALWRAP_CLI_EXE=$RUST_CLI SERIALWRAP_DAEMON_EXE=$RUST_D python3 -m pytest -q tests/test_human_agent_coexist.py -k "t2_wal_reset_seq_resets_to_zero or t3_wal_current_seq_matches_live or t4_bind_idempotent_ready_session"` | 3 passed（t1、t5～t8 依賴 human console，屬 2.2，列於範圍清單） |
| `for c in sm-01-attach-to-ready ha-05-concurrent-submit sm-04-device-unplug; do SERIALWRAP_CLI_EXE=$RUST_CLI SERIALWRAP_DAEMON_EXE=$RUST_D python3 func-test/runner.py --case $c --report-dir target/s21-func/rust/$c \|\| exit 1; done` | 三次執行各自 PASS（`--case` 為單值，每次一個；gate 逐一核對三個 case ID 的報告）；`sm-04` 使用 P2 修好的 DETACHED oracle |

- [ ] **Step 2：執行並確認 FAIL**

Run（Python 基線）：`python3 -m pytest -q tests/contract_blackbox/slice21 -m cap_posix_pty`
Expected：PASS（`test_slice_scope.py` skipped）。
Run（Rust）：`SERIALWRAP_DAEMON_EXE=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrapd SERIALWRAP_AB_LABEL=rust-s21 python3 -m pytest -q tests/contract_blackbox/slice21 -m cap_posix_pty`
Expected：在 TDD 階段（Tasks 1～19 尚未實作）FAIL：daemon 啟動即 exit 101（`not implemented: slice21 T19 run`）。

- [ ] **Step 3：實作（差異修正迴圈）**

1. 對每個 Rust 失敗案例，以同一案例跑 Python 基線取得 stdout／WAL／state.json，先各自驗證原始 bytes 完整性（WAL 每行 CRC／len、JSON 可解析），再依 §15.6.3 以本 Task 新增的 `contracts/ab_normalization.yaml`（只列具名欄位映射、`approved_revision`、`reviewed_by`，不提供 regex；須獨立審閱，P2 bridge 比較器的 `APPROVED_RULES` 不受影響；具名映射：`wall_ts`、`*_at`、`mono_ts_ns`、`cmd_id`、`interactive_id`、nonce、`vtty`／PTY 路徑、`pid`）正規化後比對；只有靜態格式 golden（`contracts/golden/rpc/*.json`、WAL 序列化 golden）做直接位元組比對。`jq -S` 只用於定位。
2. 定位到擁有該行為的 crate／Task，先在該 Task 的 Rust 測試檔新增重現案例（紅燈），再修正，直到 L1 與黑箱同時綠燈。
3. 預期熱點：`SessionPublic` 欄位順序與 float 格式、`AUTOBOOT_QUIET` hint 字串、`DEVICE_STILL_HELD` 的 `holder_unknown` 位置（以 P1 golden `contracts/golden/rpc/device_attach_still_held.json` 為準）、`log.tail_raw` metadata、stderr `serialwrap: … failed:` 由 CLI 產生（不屬 daemon）。
4. 不得放寬案例斷言；若判定是 Python 基線本身的缺陷，停下並依 arch §0 第 2 點登記 DF，不在本 Task 自行改契約。

- [ ] **Step 4：執行並確認 PASS**

Run：Step 2 兩條命令＋既有套件三條命令（Rust 與 Python 各一輪，依 §15.9.1 先停 A、確認行程與 PTY 釋放後再跑 B）
Expected：兩個 artifact 全部 PASS；`test_slice_scope.py` 只在 Rust 輪執行並 PASS。

- [ ] **Step 5：Commit**

```bash
git add tests/contract_blackbox/slice21 tests/test_multiagent_e2e.py tests/test_human_agent_coexist.py tests/test_ab_env_existing_suites.py contracts/ab_normalization.yaml crates changelog.d/rust-s21-blackbox.md
git commit -m "test(contract): Slice 2.1 黑箱案例並修正 Rust daemon 差異" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 21a：P2 runner 補動作（測試資產變更，§10.4）

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** 無（`func-test/` 不屬 `code_paths`）。
**BLOCKED-OWNER：** 無。獨立 PR（或本 Phase PR 中獨立 commit 並列於 PR 描述），由非實作者審閱並附 runner 自測結果；不改 `tools/uart_stdio_bridge/`。

**Files:**
- Modify: `func-test/runner.py`、`func-test/lib/scenario_actions.py`（P2 Task 11 的動作表所在檔）
- Test: `func-test/tests/test_s21_actions.py`

**Interfaces:**
- Consumes：P2 runner 動作表與 `DaemonHarness`。
- Produces（新增動作，與 P2 既有 12 個並列）：
  - `external_open`：`target: session_slave|by_id_link`、`hold: flock_tiocexcl|plain`、`save_as`；在協調器行程外以子行程開埠並持有（`flock_tiocexcl` 模擬遵守鎖的外部工具；`plain` 模擬不取鎖的工具），結果寫 `control.ndjson`（含子行程 pid）。
  - `external_close`：`from`（`save_as` 名）；`hold=flock_tiocexcl` 時子行程先 `ioctl(TIOCNXCL)` 再 close（收到 SIGTERM 的 handler 內執行；SIGKILL 不得用於此動作），再結束子行程並確認 fd 已關——否則測試替身自己留下的 TIOCEXCL 會在 bridge 持有 PTY master 時讓後續 `device attach` 永遠 EBUSY（R0-F3）。
  - `external_open` 回報 `{"opened":bool,"errno":int|null}`；release 後的 `external_open` 得 `opened:false, errno:16`（EBUSY）時情境判 FAIL（daemon 關閉前未解除獨占），不得轉為 `DEVICE_STILL_HELD` 或 SKIP。
  - `measure_rpc_latency`：`method`、`samples`、`save_as`；以直接 NDJSON socket 往返計時（不經 CLI），樣本寫入 `comparison.json.latency_samples_ms[<save_as>]`。
  - `measure_event_latency`：`event ∈ {release_request, command_cancel, lease_expiry, stop_request}`、`samples`、`save_as`；以 `contracts/resource_limits.yaml event_latency` 的 `accept_point`／`complete_point` 定義計時（daemon 端點以 `daemon status` 的 test-fakes 計時欄位或 WAL META 時戳取得，量測方法隨 `method_revision` 凍結）。
  - `wait_cli_until`：`argv`、`json_path`、`equals`、`timeout_s`；輪詢 CLI 直到條件成立（取代固定 sleep 的決定性同步）。

- [ ] **Step 1：寫失敗測試**：`test_s21_actions.py`：動作表含上述 5 個名稱；`external_open hold=flock_tiocexcl` 後同一路徑 `open_exclusive` 等價探測（`fcntl.flock(LOCK_EX|LOCK_NB)`）失敗；`external_close` 後成功；`test_external_close_clears_tiocexcl_with_master_open`：測試持有 PTY master，`external_open hold=flock_tiocexcl` → 一般 `os.open(slave)` 得 `EBUSY`（非 root）→ `external_close` → 一般 `os.open(slave)` 成功（非 EBUSY）；`measure_rpc_latency samples=10` 對 Python 基線 daemon 產生 10 個正數樣本；`wait_cli_until` 逾時 → `StepError`；P2 `test_runner_actions_exist` 與 `test_fault_actions_not_in_uart_stream` 仍綠。
- [ ] **Step 2：執行並確認 FAIL**：`python3 -m pytest -q func-test/tests/test_s21_actions.py` → `KeyError: 'external_open'`。
- [ ] **Step 3：實作**：照 Interfaces；控制只經協調器，不寫入 UART byte stream（§15.3）。
- [ ] **Step 4：執行並確認 PASS**：`python3 -m pytest -q func-test/tests`。
- [ ] **Step 5：Commit**：`test(func-test): 新增外部持有、延遲量測與條件等待動作`（含 Co-authored-by trailer）。

---

## Task 21：L1.5 bridge 回歸（P2 凍結 2.1 情境＋s21 情境）與事件延遲量測

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s21-l15.md`（front matter `type: feat`、`scope: tests`；本文：「Slice 2.1 L1.5：以 Rust daemon 重跑 P2 凍結的 8 個 2.1 情境，新增 Quiet 零 TX、部分寫出中止不重送、release／attach 交接、外部占用、帳密未解析、WAL 外部刪除、reboot 最小防護與事件延遲情境。」）
**擁有者 host 可執行（OD-15）：** Windows L1.5：同一批情境在原生 Windows daemon（OD-11 隔離實例＋`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20`）＋com0com DUT-side pair `COM20`／`COM21` 上的 A/B 證據（OD-08；含 `win-com-*` 三個凍結案例，BO-S21-13），orchestrator 經 WSL interop 代跑。

**BLOCKED-OWNER：** 實機 A/B（§15.9.2）。

**§5.3 開工前凍結檢查（P5 Task 0 雙審修正，gate 開頭執行）：** `contracts/resource_limits.yaml` 的 `event_latency` 必須 `freeze_status: frozen`，且 `reviewed_by`（≠ `owner`）、`reviewed_at`、`review_conclusion` 皆已附入；`limits` 內無 `owner_decision.status: pending_owner_decision` 列（NC-P5-01）。任一不成立即拒絕開跑，回報 INSUFFICIENT_EVIDENCE，不得以 skip 或 PASS 代替。

**Files:**
- Create: `func-test/scenarios/{s21-wire-equals-wal,s21-quiet-zero-tx,s21-partial-tx-abort,s21-release-attach-handoff,s21-exclusive-conflict,s21-exclusive-conflict-plain-open,s21-credentials-unresolved,s21-wal-unlink-under-traffic,s21-reboot-minimal-guard,s21-event-latency}.yaml`（P2 scenario YAML 格式，`meta.scenario.revision`、`invariants`、`behavior_changes` 必填）
- Create: `tests/acceptance/l15/test_slice21_l15.py`
- Test: 同上

**Interfaces:**
- Consumes：P2 `func-test/runner.py --mode bridge --scenarios --case <id> --selftest-report <path> --report-dir <dir> [--dut-mode docker]`（`--case` 單值）與 P2 動作集合；Task 21a 新動作；P2 `func-test/scenarios/l15-*.yaml`；Task 0b `slice_2.1.json`；Task 19／20 artifact；bridge 資格報告（P2 Task 8，`is_qualified` 為 False 時結果一律 `INSUFFICIENT_EVIDENCE`）。
- Produces：`target/l15/slice21/<run_id>/{python,rust}/<case_id>/`（runner 產生的 `manifest.json`、`wire.ndjson`、`control.ndjson`、`correlation.ndjson`、`comparison.json`、`wal/`、`cli.ndjson`、`daemon.log`）；`contracts/resource_limits.yaml event_latency.measurement_runs`（三輪量測結果與 host load 紀錄）。

**執行清單**（Python 基線先跑 A、確認 daemon 行程／PTY／lock 釋放後以新 DUT 實例跑 Rust B，§15.9.1）：
- P2 凍結、`daemon_slice=2.1` 的 8 個：`l15-delayed-reply`、`l15-half-prompt`、`l15-slow-reader-backpressure`、`l15-transport-disconnect`、`l15-multi-source-contention-cli`、`l15-stale-event-delay`、`l15-wal-interference`、`l15-reject-zero-tx`（原檔不改；`l15-slow-reader-backpressure` 與 `l15-multi-source-contention-cli` 以 `assert_wire_contiguous` 驗證 §11.3 禁止交錯）。
- 本 Task 新增的 10 個 `s21-*`（下表）。
- Windows 凍結案例 `win-com-open-exclusive`、`win-com-txrx-bridge`、`win-com-cancel-close`（需 `windows_com`）列 BO-S21-13；`win-wal-av-*` 三個（需 `endpoint_av`）列 BO-S21-14；`win-wal-f12-progress`（L2 實體）列 BO-S21-16。

- [ ] **Step 1：寫失敗測試**

`tests/acceptance/l15/test_slice21_l15.py`：

```python
import json, os, pathlib, subprocess, sys
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
P2_21 = ["l15-delayed-reply", "l15-half-prompt", "l15-slow-reader-backpressure", "l15-transport-disconnect",
         "l15-multi-source-contention-cli", "l15-stale-event-delay", "l15-wal-interference", "l15-reject-zero-tx"]
S21 = sorted(p.stem for p in (ROOT / "func-test/scenarios").glob("s21-*.yaml"))
GATE = os.environ.get("SERIALWRAP_GATE") == "1"
RECORD_ONLY: set[str] = set()   # s21-exclusive-conflict-plain-open 已依規格 §6.2 定案為拒絕（§0.2），改為正式判定

def test_case_list_matches_freeze():
    lock = json.loads((ROOT / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))
    cases = __import__("yaml").safe_load((ROOT / "tests/acceptance/cases.yaml").read_text(encoding="utf-8"))
    l15 = {c["case_id"] for c in cases["cases"] if c.get("suite") == "l15-scenario" and c["case_id"] in lock["sets"]["slice_2.1"]}
    assert l15 == set(P2_21)
    assert len(S21) == 10

@pytest.mark.cap_posix_pty
@pytest.mark.parametrize("case", P2_21 + S21)
@pytest.mark.parametrize("label,daemon_env", [("python", "SERIALWRAP_L15_PY_DAEMON"), ("rust", "SERIALWRAP_L15_RUST_DAEMON")])
def test_case(case, label, daemon_env, tmp_path):
    exe, report = os.environ.get(daemon_env), os.environ.get("SERIALWRAP_L15_SELFTEST_REPORT")
    if not exe or not report:
        if GATE:
            pytest.fail(f"{daemon_env}/SERIALWRAP_L15_SELFTEST_REPORT not set under SERIALWRAP_GATE=1")   # gate 下缺證據即失敗，不得 skip
        pytest.skip("EVIDENCE_MISSING: artifact or bridge selftest report not provided")
    env = dict(os.environ, SERIALWRAP_DAEMON_EXE=exe, SERIALWRAP_AB_LABEL=f"{label}-s21")
    out = tmp_path / label / case
    r = subprocess.run([sys.executable, "func-test/runner.py", "--mode", "bridge", "--scenarios", "--case", case,
                        "--selftest-report", report, "--report-dir", str(out)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    verdict = json.loads(next(out.rglob("comparison.json")).read_text(encoding="utf-8"))
    if case in RECORD_ONLY:
        assert verdict["verdict"] in {"PASS", "FAIL"}, verdict        # 只要求證據完整；結果寫入報告供裁決
        return
    assert r.returncode == 0 and verdict["verdict"] == "PASS", verdict
```

s21 情境（每檔一列；斷言由 runner 依 §15.6.3 判定，不使用固定 sleep 作為證據）：

| 情境檔 | 步驟（P2／Task 21a 動作） | 斷言 |
|---|---|---|
| `s21-wire-equals-wal` | READY 後 20 次 `cli cmd submit "echo L15_<i>"`，每次 `wait_cli_until` 終態；`op_end` | `assert_compare`：每埠、每 endpoint_generation 的 `wal_tx_bytes == wire.serial_read`、`wire.serial_write == wal_rx_bytes`（不比 chunk 邊界、seq、時間） |
| `s21-quiet-zero-tx` | `op_begin quiet` → `dut_control emit` U-Boot banner → `wait_wire_marker "Hit any key"`（serial_write）→ `cli cmd submit "echo X"`（預期 `AUTOBOOT_QUIET`）→ `op_end quiet` | `assert_zero_tx quiet`；CLI 回應含 `retry_after_s` |
| `s21-partial-tx-abort` | `bridge_control pause_pump`（停止讀 PTY master，kernel buffer 填滿後 writer 阻塞）→ `cli cmd submit` 16000 bytes 的 `echo`（`save_as big`）→ `wait_cli_until cmd status big command.status == running` → `cli device release` → `bridge_control resume_pump` → `wait_cli_until device release 已回` → `op_end` | `assert_command_resolved big`：終態且 payload 在 wire 出現 ≤1 次（不重送）；BC-03 映射與實際觀測一致：wire `serial_read` 中該命令 bytes > 0 ⇒ `COMMAND_ABORTED_IN_FLIGHT`，= 0 ⇒ `FLUSHED_BY_RECOVERY`；兩種情形都要求 wire bytes 總量 == WAL 該 cmd_id 的 TX `len` 總和 < 16001。部分寫出的決定性路徑由 L1（Task 12／12b）覆蓋 |
| `s21-release-attach-handoff` | `cli device release` → `external_open target=session_slave hold=flock_tiocexcl save_as ext` → `cli device attach`（預期 `DEVICE_STILL_HELD`）→ `external_close ext` → `cli device attach` → `wait_state READY` | release 回 ok 時 handle 已關且獨占已解除（`external_open` 立即回 `opened:true`；`errno:16` EBUSY 判 FAIL，R0-F3）；`external_close` 後的 `cli device attach` 成功（測試替身也已 TIOCNXCL）；`DEVICE_STILL_HELD.holder_unknown==false`、`pids` 含 ext pid；交接期間 `assert_zero_tx` |
| `s21-exclusive-conflict` | daemon 啟動前 `external_open hold=flock_tiocexcl` → 啟 daemon → `wait_cli_until session list` 該 session `state ∈ {DETACHED, ATTACHED}` → `cli device attach` | session 不進 READY；`last_error` 以 `ATTACH_FAILED:DeviceHeldError` 開頭；attach 回 `DEVICE_STILL_HELD`；`wire.serial_read` 為 0 byte |
| `s21-exclusive-conflict-plain-open`（規格 §6.2；BC-04、SC-05） | daemon 啟動前 `external_open target=session_slave hold=plain save_as ext`（普通 `open`，不 flock、不 TIOCEXCL，模擬 minicom）→ 啟 daemon → `wait_cli_until session list` 該 session `state == DETACHED` → `op_begin attach-plain` → `cli device attach`（預期 `DEVICE_STILL_HELD`）→ `cli device attach --force`（預期 `DEVICE_STILL_HELD`）→ `op_end attach-plain` → `external_close ext` → `cli device attach` → `wait_state READY` | session 在 `external_close` 前從未進 `ATTACHING`／`READY`；`last_error == "ATTACH_FAILED:DeviceHeldError"`；兩次 attach（含 `--force`）回應皆為 `{"ok":false,"error_code":"DEVICE_STILL_HELD","pids":[<ext pid>],"holder_unknown":false,"selector":"COM0"}`；`external_close` 前 `wire.serial_read` 為 0 byte（`assert_zero_tx attach-plain` 且全段無 probe／登入 TX）；外部關閉後 attach ok 並 READY。Python（P1 T10 鎖後重掃）與 Rust 皆須 PASS |
| `s21-credentials-unresolved` | DUT plan 只回 `login: `；profile 宣告 `user_env`／`pass_env` 但未提供 → `wait_state ATTACHED` | `last_error=="CREDENTIALS_UNRESOLVED"`；整段 `wire.serial_read` 為 0 byte |
| `s21-wal-unlink-under-traffic` | 背景每 100 ms 一筆命令（`parallel`）→ `wal_control unlink_current` → 再 10 筆 | `assert_wal_meta contains_value WAL_RECREATED_AFTER_EXTERNAL_UNLINK, loss_flag true, has_keys [potentially_lost_seq_range, range_known]`；之後每筆命令 `assert_wal_marker TX`（CRC／len 正確，F12 修訂 oracle） |
| `s21-reboot-minimal-guard`（§11.6 最小 reboot 防護） | READY → `cli cmd submit "reboot"` → `dut_control logical_reboot banner_delay_ms:500` → `op_begin quiet` → 期間 `cli cmd submit "echo DURING"`（預期 `AUTOBOOT_QUIET`／`READY_UNCONFIRMED`）→ `op_end quiet` → DUT 回 prompt → `wait_state READY`（重新登入）→ `cli cmd submit "echo AFTER"` | `assert_zero_tx quiet`；`echo DURING` 在 wire 出現 0 次；`reboot` 命令終態 `recovery_action:"EXPECT_REBOOT"` 或 `done`；`echo AFTER` done；wire `dut_generation` 2 |
| `s21-event-latency` | 依 `resource_limits.yaml event_latency` 對五個事件各量 `samples_per_run` 次：idle 一輪、DUT plan `flood`（20000 B/s、chunk 256、30 s）期間一輪；整個情境重複 `repeats=3` | 每輪每事件樣本最大值 ≤ 對應 `max_ms_idle`／`max_ms_flood`（`gate_rule: every_run_max_le_max_ms`）；p99 只寫入 `comparison.json` 作診斷；host `loadavg 1min > CPU 數` 的輪次作廢重跑並記錄 |

- [ ] **Step 2：執行並確認 FAIL**

Run：`SERIALWRAP_L15_PY_DAEMON=$PWD/tests/contract_blackbox/bin/py-serialwrapd SERIALWRAP_L15_RUST_DAEMON=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrapd SERIALWRAP_L15_SELFTEST_REPORT=$HOME/b-log/rust-refactor/p2/selftest-linux/selftest-report.json python3 -m pytest -q tests/acceptance/l15/test_slice21_l15.py`
Expected：`python` 參數全部 PASS（含 `s21-exclusive-conflict-plain-open`；若 Python 基線在此 FAIL，表示 P1 T10 鎖後重掃未落地，回報 P1 owner）；`rust` 參數若在 Task 19 前執行則 FAIL（daemon exit 101），Task 19 後才進入差異修正。

- [ ] **Step 3：實作**

- 失敗時依 Task 20 Step 3 的差異修正迴圈處理；延遲超標時以 `perf record -g` 或 actor 內 debug 計時（僅 test-fakes build）定位，修正對象限 Task 10／11（mailbox 優先序、reader 背壓）。
- 不得放寬 `event_latency` 值；若量測顯示凍結值不可達，停止並提請擁有者與 `reviewed_by` 審閱者重新凍結（新 `method_revision`），不得在本 Task 內改值。
- 三輪結果寫入 `contracts/resource_limits.yaml event_latency.measurement_runs`（`run_id`、`artifact_sha256`、`loadavg`、`cpu_count`、各事件 `max_ms`／`p99_ms`），並跑 `tests/test_slice21_scope.py` 確認凍結值未變。

- [ ] **Step 4：執行並確認 PASS**

Run：Step 2 命令，另以 `SERIALWRAP_GATE=1` 再跑一次
Expected：37 項全部 PASS（`test_case` 36 個參數組合＝(8＋10) 情境 × 2 label，加 `test_case_list_matches_freeze`），`s21-exclusive-conflict-plain-open` 兩個 label 皆 PASS；`SERIALWRAP_GATE=1` 下無 skip；`target/l15/slice21/<run_id>/` 兩輪證據齊全。

- [ ] **Step 5：Commit**

```bash
git add func-test/scenarios/s21-*.yaml tests/acceptance/l15 contracts/resource_limits.yaml crates changelog.d/rust-s21-l15.md
git commit -m "test(l15): Slice 2.1 bridge 回歸、安全情境與事件延遲量測" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 22：Windows 原生 L1 證據與 Slice 2.1 出口 gate 包

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-s21-gate.md`（front matter `type: feat`、`scope: rust-s21`；本文：「新增 Slice 2.1 出口 gate 腳本與報告 schema：彙整 Linux／Windows 原生 L1、黑箱 A/B、L1.5 與 policy 結果，並把需擁有者執行的證據明列為 BLOCKED-OWNER、不得標 PASS。」）
**BLOCKED-OWNER：** 本 Task 彙整全 Phase 的 BLOCKED-OWNER（見 §結尾總表），自身新增：(1) `scripts/rust/slice21_gate.sh --with-com-pair` 的完整執行——OD-15 起在擁有者 host 以 `SW_TEST_COM_PAIR=COM20:COM21` 由 orchestrator 代跑（可執行 gate，非 BLOCKED-OWNER；`com.status` 須為實際 PASS／FAIL，`EVIDENCE_MISSING` 只在 host 不可用時允許）；(2) Spike 3（第三方 EDR 開啟時的 WAL 寫入／rotate／unlink／外部 reader）以 Rust `WalFs` 重跑並與 Stage 0 Python 結果並列。

**§5.3 開工前凍結檢查（P5 Task 0 雙審修正，gate 開頭執行）：** `contracts/resource_limits.yaml` 的 `event_latency` 必須 `freeze_status: frozen`，且 `reviewed_by`（≠ `owner`）、`reviewed_at`、`review_conclusion` 皆已附入；`limits` 內無 `owner_decision.status: pending_owner_decision` 列（NC-P5-01）。任一不成立即拒絕開跑，回報 INSUFFICIENT_EVIDENCE，不得以 skip 或 PASS 代替。

**Files:**
- Create: `scripts/rust/slice21_gate.sh`、`scripts/rust/slice21_gate_judge.py`、`tests/acceptance/slice21_gate_report.schema.json`、`tests/acceptance/slice21_gate_expect.yaml`、`tests/acceptance/slice21_bo.yaml`（BO 清單＋`case_ids`，與本計畫結尾總表同序）、`tests/test_slice21_gate_report.py`、`tests/test_slice21_gate_judge.py`、`tests/contract_blackbox/slice21/test_windows_nocom.py`、`docs/rust-refactor/slice21-owner-runbook.md`、`crates/sw-core/examples/wal_s1w.rs`（規格 §1.2 WAL 指標的 Rust 量測器：以 P1 T19 `scripts/stage0/wal_microbench.py` 凍結的 S1-W 資料混合與取樣口徑驅動 Task 7 `WalWriter`＋Task 6 原生 `WalFs`，輸出單筆寫入 p50／p95／p99 與每次 rotate 停頓；只量測、不改 WAL 行為）
- Modify: `tests/acceptance/l1_required.lock.json`（只以 P2 `regen_lock.py --l1-add-only` 登記 `s21_core`（linux／windows）與 `s21_gate:<section>`；R5-F3）
- Test: `tests/test_slice21_gate_report.py`、`tests/test_slice21_gate_judge.py`、`tests/contract_blackbox/slice21/test_windows_nocom.py`

**§1.2 WAL 效能 section（`wal_s1w_perf`，跨計畫審查補入：原計畫無任何 Task 量測 Rust WAL 的 §1.2 指標）：** `cargo run --release -p sw-core --example wal_s1w -- --out target/slice21-gate/<run_id>/wal_s1w.json`；判定門檻逐字取規格 §1.2（Linux 單筆 p50 ≤0.005 ms、p95 ≤0.01 ms、p99 ≤0.02 ms、rotate 最大停頓 ≤3 ms；Windows ≤0.03／0.08／0.20 ms、≤15 ms），量測環境欄位依 §1.1.1；超標記 FAIL 並保留樣本，不得改列觀察值。Windows 原生與 EDR 開啟下的正式量測列 BLOCKED-OWNER（`EVIDENCE_MISSING`，不得 PASS）。

**Interfaces:**
- Consumes：Tasks 0～21；P3 `scripts/rust/win-test.sh`、`scripts/rust/win-blackbox.ps1`、`scripts/rust/mkbundle.sh`。
- Produces：`scripts/rust/slice21_gate.sh [--with-com-pair] [--run-id ID]` → `target/slice21-gate/<run_id>/report.json`，schema：

```json
{"schema_version": 1, "run_id": "str", "started_at": "ISO8601", "git_revision": "40hex", "slice": "2.1",
 "identity": {"cli_path": "str", "cli_sha256": "64hex", "daemon_implementation": "rust|python", "daemon_version": "str",
              "daemon_artifact_id": "str", "daemon_sha256": "64hex", "contracts_revision": "str", "cases_lock_sha256": "64hex",
              "freeze_file_sha256": "64hex", "tool_revision": {"uart_stdio_bridge_tree_sha256": "64hex", "runner_sha256": "64hex"},
              "platform": {"os": "str", "kernel": "str", "arch": "str"}, "device": "str|null"},
 "sections": [{"name": "linux_l1|linux_cap_posix_pty|windows_native_l1|windows_cap_windows_com|blackbox_python|blackbox_rust|existing_suites_rust|l15_ab|event_latency|wal_s1w_perf|cfg_boundary|pytest_full|policy_check|case_uptake",
               "status": "PASS|FAIL|EVIDENCE_MISSING", "command": "str", "exit": 0, "passed": 0, "failed": 0, "skipped": 0, "deselected": 0,
               "ignored": 0, "required_set": "s21_core|s21_gate:<name>|null", "l1_results": "relpath|null",
               "l1_verdict": {"verdict": "PASS|FAIL|INSUFFICIENT|INVALID", "reasons": ["str"], "missing": ["nodeid"], "skipped_required": [{}], "duplicates": ["nodeid"], "not_run": ["nodeid"], "required_sha256": "64hex"},
               "allowed_missing_bo": "BO-S21-NN|null", "log": "relpath"}],
 "case_uptake": {"denominator": 0, "valid_pass": 0, "rate": 0.0, "window_start": "YYYY-MM-DD", "pass_case_ids": ["str"]},
 "pending_decisions": [{"id": "str", "scenario": "str", "evidence": "relpath"}],
 "blocked_owner": [{"id": "BO-S21-NN", "item": "str", "case_ids": ["str"], "status": "BLOCKED-OWNER", "procedure": "docs/rust-refactor/slice21-owner-runbook.md#bo-s21-nn", "evidence": "relpath|null"}],
 "verdict": "READY_FOR_OWNER_EVIDENCE|INCOMPLETE|FAIL"}
```

判定規則（`scripts/rust/slice21_gate_judge.py`，gate 腳本最後呼叫；純函式可單測）：
- **測試類 section**（`linux_l1`、`linux_cap_posix_pty`、`windows_native_l1`、`windows_cap_windows_com`、`blackbox_python`、`blackbox_rust`、`existing_suites_rust`、`pytest_full`）的 `PASS` 必要條件（R5-F3，取代原「`passed >= expected_min_passed`」計數門檻——計數門檻擋不住「以其他測試湊數、必要案例沒跑」與重複計數）：`required_set` 非 null（`linux_l1`／`windows_native_l1`＝`s21_core`，即 P9 `S21-L1-CORE` 的集合；其餘＝`s21_gate:<name>`；對應寫在 `tests/acceptance/slice21_gate_expect.yaml`，nodeid 清單本身只在 P2 `l1_required.lock.json`）、`l1_results` 指向該 section 的 `l1-results.v1`（pytest 段帶 `SW_RESOURCE_REPORT`＋`--junitxml`；cargo 段為 P3 `cargo_l1_results.py`；Windows 段為 `win-test` 報告的 `l1_results`），且 P2 `l1_judge.judge(l1_results, required_for(lock, required_set, platform), platform=…, junit=…)` 為 `PASS`（每個凍結 nodeid 恰執行一次且 `passed`、無重複、無必要案例 skip／ignored／deselected／未執行、runner `exit_code == 0`、`finished`）。`l1_verdict` 為 `FAIL` 或任一列 `failed`／`error` → `FAIL`；`INSUFFICIENT`／`INVALID`／`l1_results` 缺檔／set 不存在 → `EVIDENCE_MISSING`。計數欄（`passed`…`ignored`）只作摘要，仍須與 `l1_results` 一致，不一致 → `EVIDENCE_MISSING`。非測試類 section（`l15_ab`、`event_latency`、`wal_s1w_perf`、`cfg_boundary`、`policy_check`、`case_uptake`）`required_set` 為 null，沿用各自規則（`exit == 0` 且其報告判定 PASS）。各 `s21_core`／`s21_gate:*` 集合以 P2 `regen_lock.py --l1-add-only` 凍結，時點依 P2 Task 13（R5 審查後續）：P5 自 Task 1 起每個 Task 在自己的紅燈 commit 登記新增 nodeid（lock diff 隨該 commit），本 Task 只登記 gate 自身測試；PR 的 `regen_lock.py --check --base` 確保 Phase 內已登記者不消失。
- 只有 `allowed_missing_bo` 非 null 的 section（`windows_cap_windows_com`→BO-S21-01）可在 `EVIDENCE_MISSING` 下不阻擋。
- `verdict`：任何 section `FAIL` → `FAIL`；否則任何非豁免 section 為 `EVIDENCE_MISSING`、`identity` 欄位缺漏、或 `--dry-run` → `INCOMPLETE`；全部非豁免 section `PASS` → `READY_FOR_OWNER_EVIDENCE`。本環境永不輸出 `PASS` 作為 Slice 出口。

- [ ] **Step 1：寫失敗測試**

`tests/test_slice21_gate_report.py`：

```python
import json, pathlib, subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]

def _schema():
    return json.loads((ROOT / "tests/acceptance/slice21_gate_report.schema.json").read_text(encoding="utf-8"))

def test_blocked_owner_items_can_never_be_pass():
    s = _schema()
    assert s["properties"]["blocked_owner"]["items"]["properties"]["status"]["const"] == "BLOCKED-OWNER"
    assert set(s["properties"]["sections"]["items"]["properties"]["status"]["enum"]) == {"PASS", "FAIL", "EVIDENCE_MISSING"}
    assert set(s["properties"]["verdict"]["enum"]) == {"READY_FOR_OWNER_EVIDENCE", "INCOMPLETE", "FAIL"}
    sec = s["properties"]["sections"]["items"]["required"]
    assert {"skipped", "deselected", "required_set", "l1_results", "l1_verdict", "ignored"} <= set(sec)
    assert "expected_min_passed" not in s["properties"]["sections"]["items"]["properties"]   # R5-F3：計數門檻已移除
    assert {"cli_sha256", "daemon_sha256", "daemon_implementation", "contracts_revision", "cases_lock_sha256", "tool_revision", "platform"} <= set(s["properties"]["identity"]["required"])

def test_gate_dry_run_lists_all_blocked_owner_items(tmp_path):
    r = subprocess.run(["bash", "scripts/rust/slice21_gate.sh", "--dry-run", "--run-id", "t", "--out", str(tmp_path)],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    rep = json.loads((tmp_path / "t" / "report.json").read_text(encoding="utf-8"))
    ids = [b["id"] for b in rep["blocked_owner"]]
    assert ids == [f"BO-S21-{i:02d}" for i in range(1, 17)]
    assert all(b["status"] == "BLOCKED-OWNER" for b in rep["blocked_owner"])
    assert rep["verdict"] == "INCOMPLETE"                            # dry-run 永不 READY
    com = [s for s in rep["sections"] if s["name"] == "windows_cap_windows_com"][0]
    assert com["status"] == "EVIDENCE_MISSING"                      # 未提供 SW_TEST_COM_PAIR

def test_runbook_has_anchor_per_item():
    text = (ROOT / "docs/rust-refactor/slice21-owner-runbook.md").read_text(encoding="utf-8")
    for i in range(1, 17):
        assert f"### BO-S21-{i:02d}" in text

def test_bo_case_ids_cover_frozen_windows_cases():
    rep_ids = {c for b in __import__("yaml").safe_load((ROOT / "tests/acceptance/slice21_bo.yaml").read_text(encoding="utf-8"))["items"] for c in b["case_ids"]}
    assert {"win-com-open-exclusive", "win-com-txrx-bridge", "win-com-cancel-close", "win-wal-av-rotate", "win-wal-av-unlink",
            "win-wal-av-reader-handle", "win-wal-f12-progress"} <= rep_ids
```

`tests/test_slice21_gate_judge.py`（假報告的負向測試；以 `slice21_gate_judge.judge(report, expect)` 純函式）：

| 測試名 | 精確斷言 |
|---|---|
| `test_all_skipped_is_not_pass` | section `exit 0, passed 0, skipped 36` → `EVIDENCE_MISSING`；verdict `INCOMPLETE` |
| `test_zero_tests_is_not_pass` | `cargo test -- --ignored cap_posix_pty__` 輸出 0 tests、exit 0 → `EVIDENCE_MISSING` |
| `test_count_threshold_cannot_substitute_required_nodeids`（R5-F3，failing-first；取代原 `test_below_expected_min_is_missing`） | `linux_l1` 的 `required_set="s21_core"` 凍結 `[cargo:sw-core::wal::a, …::b, …::c]`；`l1_results` 有 50 列 `passed` 但都不是這三個（另跑了其他測試）、`exit 0`、`passed 50` → `EVIDENCE_MISSING`，`l1_verdict.missing` 三筆；verdict `INCOMPLETE`（舊規則 `passed >= expected_min_passed` 會給 PASS） |
| `test_required_nodeid_ignored_is_missing`（R5-F3） | `linux_cap_posix_pty` 凍結 `cap_posix_pty__x`，其列 `outcome "skipped"`（libtest `ignored`）→ `EVIDENCE_MISSING`、`l1_verdict.skipped_required[0].nodeid` 為之 |
| `test_duplicate_nodeid_is_missing`（R5-F3） | 同一必要 nodeid 兩列皆 `passed`（兩支 exe 重複執行）→ `EVIDENCE_MISSING`、`l1_verdict.duplicates` 含之；一列 `failed` 一列 `passed` → `FAIL` |
| `test_runner_exit_or_unfinished_is_missing`（R5-F3） | 全部必要 nodeid `passed` 但 `l1_results.exit_code == 101` 而 section `exit 0`（計數與報告不一致）→ `EVIDENCE_MISSING`；`finished: false` → `EVIDENCE_MISSING` |
| `test_missing_l1_results_is_missing`（R5-F3） | 測試類 section `l1_results: null` 或檔不存在、或 `required_set` 不在 lock → `EVIDENCE_MISSING`；非測試類 section（`policy_check`）`required_set: null` 不受影響 |
| `test_full_required_set_pass`（R5-F3） | 每個測試類 section 的凍結 nodeid 各恰一列 `passed`、`exit_code 0`、`finished true`、計數一致 → section `PASS`；搭配其餘 section PASS 與 `windows_cap_windows_com` 缺 → verdict `READY_FOR_OWNER_EVIDENCE` |
| `test_deselected_counts_as_missing` | `deselected 1`（且該 nodeid 屬 `required_set`）→ `EVIDENCE_MISSING`（`l1_verdict.deselected_required` 非空） |
| `test_only_allowed_bo_section_may_be_missing` | 只有 `windows_cap_windows_com` 缺 → `READY_FOR_OWNER_EVIDENCE`；`blackbox_rust` 缺 → `INCOMPLETE` |
| `test_failed_section_is_fail` | 任一 `failed 1` → verdict `FAIL` |
| `test_identity_required` | 缺 `daemon_sha256` 或 `cases_lock_sha256` → `INCOMPLETE` |
| `test_identity_hash_matches_artifact` | `daemon_sha256` 與實際 `target/.../serialwrapd` 的 sha256 不符 → `FAIL` |
| `test_case_uptake_from_valid_pass_only` | `case_uptake.valid_pass` 只計 gate 判定為 PASS 的 `slice_2.1` 案例；`rate == valid_pass / denominator`、`denominator == slice_2.1.json.denominator` |

`tests/contract_blackbox/slice21/test_windows_nocom.py`（Windows Python 3.12 經 `scripts/rust/win-blackbox.ps1 -Suite slice21-nocom` 執行；POSIX 上 skip）：

| 測試名 | 精確斷言 |
|---|---|
| `test_tcp_endpoint_loopback_and_ping` | `SERIALWRAP_ENDPOINT` 未設、`SERIALWRAP_TCP_PORT=<空閒埠>` → daemon 綁 `127.0.0.1:<port>`；`health.ping` 回 `pong:true` |
| `test_second_daemon_exit_2` | 同 endpoint 再啟動 → exit 2、stderr `serialwrapd: DAEMON_ALREADY_RUNNING\n`（TCP 探測先於 LockFileEx） |
| `test_wal_share_delete_while_running` | daemon 執行中 `cmd /c del <wal_dir>\raw.wal.ndjson` 成功；下一筆紀錄觸發 META 重建 |
| `test_config_yaml_socket_path_written` | `config.yaml` 含 `socket_path: tcp://127.0.0.1:<port>` |
| `test_deferred_methods_windows` | `session.console_attach` → `METHOD_NOT_FOUND` |
| `test_session_public_console_endpoint_null` | 以 Windows device backend＋空 SERIALCOMM allowlist 情境無 session；以 `SERIALWRAP_TEST_DEVICE_ALLOWLIST` 指定不存在的 `COM250` 並設隔離目錄 → `health.status.test_device_allowlist == {"enabled": true, "coms": ["COM250"], "source": "env"}` |
| `test_stop_exit_0` | `daemon.stop` → `{"ok":true,"stopping":true}`、行程 exit 0、lock 檔仍存在 |

- [ ] **Step 2：執行並確認 FAIL**

Run：`python3 -m pytest -q tests/test_slice21_gate_report.py`
Expected：FAIL，`FileNotFoundError: .../tests/acceptance/slice21_gate_report.schema.json`。

- [ ] **Step 3：實作**

`scripts/rust/slice21_gate.sh`（bash，`set -euo pipefail`；`--dry-run` 只產生報告骨架與 BLOCKED-OWNER 清單、各 section `EVIDENCE_MISSING`）依序執行並記錄每段 exit／計數；測試類 section 一律另產 `l1-results.v1` 寫入 `l1_results`（cargo 段以 P3 `cargo_l1_results.py --platform linux --out … -- <該段 cargo 參數>` 執行、取代直接 `cargo test`；pytest 段帶 `SW_RESOURCE_REPORT=<section>/resource-report.json` 與 `--junitxml=<section>/junit.xml`；Windows 段取 `win-test` 報告的 `l1_results`），計數由該檔彙總（R5-F3）：

1. `python3 scripts/rust/check_cfg_boundary.py`
2. `cargo test --workspace --features test-fakes`
3. `cargo test -p sw-pal --features test-fakes -- --ignored cap_posix_pty__`
4. `scripts/rust/win-test.sh --package sw-pal --package sw-core --package sw-daemon`
5. `--with-com-pair` 時 `scripts/rust/win-test.sh --caps windows_com --package sw-pal`；否則該 section 寫 `EVIDENCE_MISSING`
6. `cargo build --release -p sw-daemon -p sw-cli --target x86_64-unknown-linux-gnu` 與 `--target x86_64-pc-windows-gnu`
7. `python3 -m pytest -q tests/contract_blackbox/slice21 -m cap_posix_pty --junitxml=…`（Python）與 Rust 版（Task 20 Step 2 命令），兩輪皆帶 `SW_RESOURCE_REPORT`，逐案結果與計數取自 `l1-results.v1`（junit 只作一致性核對）
8. Task 20 既有套件：兩條 pytest 命令＋三次單 case 的 `func-test/runner.py --case <id>`（逐一核對三個 case 報告）
9. Task 21 L1.5 命令（`SERIALWRAP_GATE=1`），並把 `event_latency` 三輪結果寫成獨立 section
9b. 計算 `case_uptake`（Task 0b `slice_breaker.acceptance_rate`）；`s21-exclusive-conflict-plain-open` 已定案（§0.2），計入 `l15_ab` section 判定，**不**列入 `pending_decisions`
9c. 收集 `identity`（CLI／daemon 路徑與 sha256、`daemon status` 的 implementation／version、`contracts/` revision、`cases.lock.json` 與凍結檔 sha256、bridge `tree_sha256`、runner sha256、`uname`、device）
10. `pwsh.exe -File scripts/rust/win-blackbox.ps1 -Suite slice21-nocom -Daemon <win serialwrapd.exe>`
11. `python3 -m pytest -q tests/`
12. `python3 -m policy_check --repo .`

`verdict` 一律由 `slice21_gate_judge.py` 依上方判定規則計算（gate 腳本本身不判定）。`docs/rust-refactor/slice21-owner-runbook.md` 每個 `### BO-S21-NN` 小節寫：目的、前置（bench／COM pair／管理員）、逐字命令、預期證據檔、回填方式（把證據路徑填回 `report.json` 對應項的 `evidence` 欄後由審查者核對）。

BLOCKED-OWNER 清單（`BO-S21-01`～`16`，與本計畫結尾總表同序；`tests/acceptance/slice21_bo.yaml` 為機器可讀版本，含各項 `case_ids`）。

- [ ] **Step 4：執行並確認 PASS**

Run：`python3 -m pytest -q tests/test_slice21_gate_report.py tests/test_slice21_gate_judge.py && SERIALWRAP_GATE=1 bash scripts/rust/slice21_gate.sh --run-id $(date +%y%m%d-%H%M%S)`
Expected：pytest PASS；完整 gate 產出 `verdict == "READY_FOR_OWNER_EVIDENCE"`，`windows_cap_windows_com` 為 `EVIDENCE_MISSING`（BO-S21-01 豁免），其餘 section 皆 `PASS` 且 `skipped == 0`；若任一 section 為 `EVIDENCE_MISSING`，verdict 為 `INCOMPLETE`，不得以此報告宣稱本環境 gate 完成。

- [ ] **Step 5：Commit**

```bash
git add scripts/rust/slice21_gate.sh scripts/rust/slice21_gate_judge.py tests/acceptance/slice21_gate_report.schema.json tests/acceptance/slice21_gate_expect.yaml tests/acceptance/slice21_bo.yaml tests/test_slice21_gate_report.py tests/test_slice21_gate_judge.py tests/contract_blackbox/slice21/test_windows_nocom.py docs/rust-refactor/slice21-owner-runbook.md changelog.d/rust-s21-gate.md
git commit -m "chore(rust): Slice 2.1 出口 gate 腳本、報告 schema 與擁有者證據手冊" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 23：Slice 2.1 完成校準報告（§12.2、OD-02、OD-13）

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** 無（純文件）。
**BLOCKED-OWNER：** 校準中「bench 等待時間」「實機回歸重跑次數」兩欄需擁有者填入實際值；未填前報告標「待擁有者補值」，不得據以重算後續切片工期。

**Files:**
- Create: `docs/rust-refactor/slice21-calibration.md`、`tests/test_slice21_calibration.py`

**Interfaces:**
- Consumes：Task 0b `slice_2.1.json` 與 snapshots、Task 22 `report.json`、git log（各 Task 起訖 commit 時間）。
- Produces：`slice21-calibration.md`，固定章節：`## 實測工作量`（每個 Task 的紅燈 commit→綠燈 commit 時距與修正輪數）、`## Bench 等待`（BLOCKED-OWNER 各項等待天數）、`## 安全里程碑`（§11.6 Slice 2.1 安全出口各項的證據狀態）、`## 平台缺陷`（Windows／POSIX 各自發現的 PAL 缺陷與修正）、`## 回歸重跑`（L1.5／L2／L3 重跑次數與原因）、`## 承接率`（`case_uptake` 週序列與斷路器狀態）、`## OD-02 24 週上限輸入`（以實測速率外推 Slice 2.2～2.4 的週數，標明為規劃估計、非承諾；超過 24 週時寫「需重提 OD-02」）。

- [ ] **Step 1：寫失敗測試**：`test_slice21_calibration.py` 斷言上述 7 個章節標題存在、`## 承接率` 的分母等於 `slice_2.1.json.denominator`、`## OD-02 24 週上限輸入` 含「規劃估計」字樣。
- [ ] **Step 2：執行並確認 FAIL**：`python3 -m pytest -q tests/test_slice21_calibration.py` → `FileNotFoundError`。
- [ ] **Step 3：實作**：以 git log 與 gate 報告產生數據；擁有者欄位留 `待擁有者補值`。
- [ ] **Step 4：執行並確認 PASS**：同 Step 2 → PASS。
- [ ] **Step 5：Commit**：`docs(rust-refactor): 新增 Slice 2.1 完成校準報告`（含 Co-authored-by trailer）。

---

## PR 與出口

- PR：`wt/rust-refactor/slice21-daemon-core` → `feature/rust-refactor`；標題 `feat(rust): Slice 2.1 Rust daemon core`；body 依 `.github/pull_request_template.md` 填 Policy Checklist，並附 `report.json` 的 section 摘要、`identity`、`case_uptake`、`pending_decisions` 與 BLOCKED-OWNER 清單；不引用 issue 時上 `policy-exempt:issue-link`；Rust daemon 尚非部署 artifact，README／docs 不改對外操作說明，上 `policy-exempt:docs-sync`（附理由）。PR 描述另列：§10.4 測試資產變更清單（Task 15a、Task 20 Step 0、Task 21a）與各自的獨立審閱者；DF 候選（Task 19 停機未收斂 exit 1）；§0.2「普通外部 holder（未取 flock）的拒絕」已依規格 §6.2 定案，不再列 DF 候選。
- **合併阻擋（OD-08）**：本 PR 動到 PAL（serial、lock、device source、WalFs、RPC listener），依 OD-08「動到 PAL 的 PR 必須取得原生 Windows COM handle 證據；記憶體 fake 不得充數」，PR 保持 draft／不得合併，直到 BO-S21-01 證據回填並經審查，或擁有者在 PR 上留下明確書面豁免（比照 04 計畫 Task 9）。OD-15 起 BO-S21-01 可在擁有者 host（`COM20`／`COM21`）取得，預期以實跑證據解除阻擋，不以豁免代替。
- 回歸 case 政策（#155）：本 Phase 非 bug 修復；實機案例由 BO-S21-07／08 以既有 TestPilot case 承接，PR 描述註明「未新增 regression case：F1／F2／F3／F4／F12 既有 18 個 case（17 個屬 `slice_2.1` 分母、`f4-interactive-line-cmd-defined` 為提前證據）由擁有者以 Rust daemon 執行」。
- Slice 2.1 出口（§11.6）需同時具備：本環境 gate `READY_FOR_OWNER_EVIDENCE`（不得為 `INCOMPLETE`）＋BO-S21-01～16 證據回填並經審查＋`pending_decisions` 經擁有者裁決＋Task 23 校準報告。未回填前不得宣告 Slice 2.1 出口通過。

## BLOCKED-OWNER 總表

| ID | 項目 | case_ids（cases.lock） | 來源 Task |
|---|---|---|---|
| BO-S21-01 | 原生 Windows COM handle gate（OD-08）：`SW_TEST_COM_PAIR` 下 `cap_windows_com__*` 全部（獨占 ACCESS_DENIED、CancelIoEx 收斂、部分寫出、conformance）；PR 合併前置。**OD-15 起轉為擁有者 host 可執行 gate**（`COM20:COM21`，orchestrator 經 WSL interop 代跑），不再等待擁有者提供 pair | — | 3、22 |
| BO-S21-02 | Windows 外部 TeraTerm／PuTTY 先開 COM 時 `DEVICE_STILL_HELD{pids:[],holder_unknown:true}` 實機證據 | — | 3 |
| BO-S21-03 | Windows USB 拔除時 overlapped 在途 I/O 完成碼實測與 drain 3000 ms 收斂 | — | 3、12 |
| BO-S21-04 | 實體 USB-UART（CH340／FTDI）上的 TIOCEXCL＋flock 與 RTS/CTS 卡住時可取消寫入（`cap_physical_uart__*`） | — | 2 |
| BO-S21-05 | 外部 minicom 先開埠時 daemon 得 `Held` 的 Linux 實機證據 | — | 2 |
| BO-S21-06 | Windows `CTRL_BREAK`／`CTRL_CLOSE` 停止 daemon（互動 console），以及 `windows-task` 監管下的啟停（Spike 2）；OD-15：先以 P2 Task 18 throwaway 排程工作在擁有者 host 跑（可執行），正式工作的 Spike 2 證據仍待擁有者 | — | 4、19 |
| BO-S21-07 | L2（交 bench 前先以 `scripts/docker/run-l15.sh` 跑 `L1.5-docker`，不計 L2）：Rust daemon 執行 `serialwrap_regression` F1／F2／F3／F4／F12 的 18 個 case（F12 用 OD-13 修訂 oracle） | `f1-*`（5）、`f2-*`（4）、`f3-*`（4）、`f4-activity-classification`、`f4-background-result-tail-consistent`、`f12-*`（2）；提前證據 `f4-interactive-line-cmd-defined` | 20 |
| BO-S21-08 | L3（先以 docker 兩容器拓樸跑 `L1.5-docker`，不計 L3）：`realhw` P0 適用核心集合＋WAL／裝置衝突／Recovery 相關 P1，Linux 與 Windows 各一輪 | `p0-doctor`、`p0-cmd-async`、`p0-clear-reattach`、`p0-selftest`、`p0-wal-live`、`p0-multiopen`、`p1-cmd-serial`、`p1-rst-daemon`、`p1-rst-recover`、`p1-hp-cycle`、`p1-hp-reorder` | 20 |
| BO-S21-09 | 雙平台實機「外部燒錄工具接手→`device attach` 收回」交接證據 | — | 12 |
| BO-S21-10 | 實機 shell／prplOS 板的登入→READY 與 reboot quiet 最小防護（L3 P0 reboot 相關核心） | — | 13、21 |
| BO-S21-11 | 同款 CH340（by-id 相同）以 by-path 區分的 COM 指派實機證據 | — | 16 |
| BO-S21-12 | systemd-user／systemd-system 監管下 Rust `serialwrapd` 啟停（L3 P0）；OD-15：先在 systemd container（`scripts/docker/run-systemd.sh`）驗證並附 `L1.5-docker` 報告，實機 L3 仍待擁有者 | — | 19 |
| BO-S21-13 | Windows L1.5（原生 daemon＋com0com DUT-side pair 或實體 USB-UART；OD-15 起 com0com 部分在擁有者 host 以 `COM20`／`COM21` 可執行）與實機 A/B（§15.9.2，仍 BLOCKED-OWNER），含 Windows 端以 Rust daemon 跑 `tests/contract_blackbox/slice21` 全套 | `win-com-open-exclusive`、`win-com-txrx-bridge`、`win-com-cancel-close` | 20、21 |
| BO-S21-14 | Spike 3：第三方 EDR 開啟下 Rust `WalFs` 的寫入／rotate／unlink／外部 reader 結果，與 Stage 0 Python 結果並列 | `win-wal-av-rotate`、`win-wal-av-unlink`、`win-wal-av-reader-handle` | 6、22 |
| BO-S21-15 | 藍牙 SPP 虛擬 COM 實際存在時的 SERIALCOMM 排除證據（需配對藍牙裝置的 Windows 機台） | — | 5 |
| BO-S21-16 | Windows L2：EDR 開啟下以實體 UART 驗證 F12 WAL 進展 oracle | `win-wal-f12-progress` | 20、22 |
