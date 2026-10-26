# P6 Slice 2.2：Human Console、Telnet、Lease、Suspend／Resume 與 DeferredBuffer 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Rust daemon（`serialwrapd`）承接 Python 基線的 human console 全部行為：POSIX PTY console（daemon 同持 master＋slave 的 keepalive、跨 `conn_epoch` 保留同一 `/dev/pts/N`、哨兵 primary）、TCP＋Telnet console（逐位元組相同的 12 bytes greeting、NVT 摺疊、IAC 逸出、拒絕入向 Binary、跨 chunk parser）、第一 console raw ownership 與其餘 line-buffer（本地回顯／backspace 編輯）、suspend／resume 與 DeferredBuffer（BC-02）、interactive lease 的 open／send／status／close／expire（含 soft preempt 與 `ATTACHED` 救援 `boot_interrupt`／`login_required`）、每 console 1 MiB 出向 ring（drop-oldest，SC-15）、`session.console_list`，以及 OD-01 選配的 com0com Serial console。完成後 `tests/contract_blackbox/` 的 console／lease 案例與既有 `tests/test_human_agent_coexist.py` 改指 Rust artifact 時全部通過。

**Architecture:** 本 Phase **擴充** P5（`06-slice21-daemon-core.md`，下稱 06）已交付的 sans-IO `SessionCore`、`Mailbox`、`TxWriter`＋`Fence`、`lease.rs`、`op_runner.rs`、1 s `ControlEvent::Tick` 與 POSIX 哨兵 PTY，不重做、不改名；每一項差量列於「P6 對 P5 的增補清單」。狀態決策全部放在 `SessionCore`（`sw-core`）：ownership、line editor、DeferredBuffer、human TX lane、lease 表都是 `SessionCore` 的欄位，輸入只靠 `MailboxItem`，輸出只靠 `Effects`。I/O 由每 session 一條 `ConsoleHub` thread 負責（PTY master／TCP socket／com0com handle 的多工、Telnet filter、每 console 出向 ring），平台差異只在 `sw-pal` 的 `ConsoleTransportFactory`＋新增的 `ConsoleMux`。Human 位元組一律經 P5 的單一 `TxWriter` 與 `Fence`：每 session 同時最多一筆 human TX 未結算，由 per-session human TX pump thread 送出，actor thread 永不阻塞送出；OS 寫入前重驗 `input_epoch`、`owner_gen` 與 human admission。BC-02 的失效以 `input_epoch` 世代實作。它就是 P1 Python `UARTBridge._owner_gen`（02 T09，只在 BC-02 失效點遞增）在 Rust 的名稱；`ConsoleInputBytes`、deferred entry 與 human `TxPermit` 都帶擷取當下的值。

**Tech Stack:** Rust 1.96.1（edition 2021；`x86_64-unknown-linux-gnu`、`x86_64-pc-windows-gnu`）、`crossbeam-channel =0.5.15`、`indexmap =2.11.4`、`libc =0.2.186`（僅 sw-pal POSIX）、`windows-sys =0.61.2`（僅 sw-pal Windows，Winsock＋COMM）、Python 3.12 pytest（黑箱與 golden 產生）、tmux（既有 coexist 測試）。

**Spec:** `docs/rust-refactor/serialwrap-rust-spec-final.md` §5.1.2、§5.1.3、§5.2.1、§5.2.2、§5.2.6、§5.3（Console／Interactive 列）、§5.4、§7.1～§7.5、§11.3、§11.6（Slice 2.2 列）、§12.2 Stage 2 表、§15.8.4；`docs/rust-refactor/owner-decisions-2026-10-15.md` OD-01、OD-04（BC-02）、OD-08、OD-12、OD-15；`docs/rust-refactor/plans/rust-architecture.md` §1.3、§4.7、§5.1～§5.4、§5.10、§5.11、§9；`docs/rust-refactor/plans/spec-reconciliation.md` BC-02、SC-13、SC-15、SC-16、DF-02、UIO-12.5～12.8、UIO-12.10-a／c、SES-16.1、SES-16.4-07、GAP-G-02；盤點 `inventory/06-uartio-console.md` §2、§6～§8、§12；`inventory/05-session.md` §1.2、§1.5、§3.1、§5.1、§10、§15、§16.1；`inventory/02-rpc.md` §6.1、§6.2、§7.6、§9；`inventory/10-gaps.md` G-01、G-02、G-10。

---

## Global Constraints

（自 `00-master-plan.md` 摘錄本 Phase 適用者，逐字有效）

- 平台相依 production code 只在 `sw-pal`；其他 crate 禁止 `#[cfg(unix)]`／`#[cfg(windows)]`／`target_os`／`cfg!`／`cfg_attr` 平台分支與 build script 間接分支（§3.1）；允許 `#[cfg(test)]` 與 `#[cfg(feature = "test-fakes")]`。本 Phase 的 PTY／Winsock／COMM 程式碼只能出現在 `crates/sw-pal/src/console_*`。
- RPC／CLI `error_code` 與 session `last_error` 為兩個獨立命名空間（ADR-07、§9.3）。本 Phase 只輸出既有 RPC 碼：`SESSION_NOT_READY`、`SESSION_RECOVERING`、`SESSION_BUSY`、`SESSION_INTERACTIVE_BUSY`、`INTERACTIVE_NOT_FOUND`、`INTERACTIVE_EXPIRED`、`CONSOLE_NOT_FOUND`、`FLASHING_BUSY`、`INVALID_ARGS`、`EXCEPTION`，不得新增碼。`RECOVERING` 與 `FLASHING` 下的 `InteractiveOpen` 依 SC-01／SC-02（規格 §5.3）分別回 `SESSION_RECOVERING`、`FLASHING_BUSY`。
- 錯誤碼計數由 `contracts/error_codes.yaml` 資料產生，不得硬編碼 78（§9.2、OD-06）。
- `--retries` 只對 11 個唯讀方法重試；`session.console_list` 在白名單內，`session.interactive_status` **絕不重試**（它會 touch lease）（§4.2）。
- CLI JSON：`json.dumps(..., ensure_ascii=False, separators=(",", ":"))` 等價輸出；`state.json`／WAL 以 `sort_keys=True`。RPC 回應 key 依 Python 插入序（`pydict!`），`version` 以 `setdefault` 補在尾端；`EXCEPTION` 回應無 `version`。
- 背壓上限：`DEFERRED_INPUT_MAX_BYTES=262144`（BC-02：超過時刪除該 client 整段）、每 Console 出向 Ring Display `1 MiB`（§5.4）、Control `64 KiB`（D-05）、human TX backlog `262144 bytes`（D-15）、每 session Telnet console 32 個（D-18）。Actor mailbox／RX／TX／WAL／hub 佇列一律有界；沒有無界 channel、沒有無界 buffer。所有上限登記於 `contracts/resource_limits.yaml`（Task 0）。
- **Actor thread 永不阻塞送出**：actor 對 human TX pump、`ConsoleHub` 一律 `try_send`；hub 對 mailbox 一律 `try_send`，滿載時改走 D-17 的有界暫存並停止讀取。可以阻塞的只有 pump thread、OpRunner 與 reader thread（arch §5.2、§5.10）。
- Drain timeout 3,000 ms → `RECOVERING`＋`drain_stalled:true`＋`drain_timeout_epoch`（§5.2.5）；`device.release` 逾時回 `DRAIN_STALLED`（OD-14 #4，P5 已實作；drain 中其他操作仍回 `SESSION_RECOVERING`）。本 Phase 在 drain Phase 1 內追加 console 上下文失效，不改其時限與回應碼。
- Passthrough（SC-35，OD-14 #2，P5 已實作安靜升 READY）：passthrough 進入 READY 後，human console 的 raw ownership、agent 命令的 suspend／resume、DeferredBuffer 與 lease 規則與一般 READY session 完全相同；passthrough ATTACHED 期間 human 鍵入產生的 RX 回顯會重新起算 30 s 安靜視窗（只看 RX，不看 human TX），本 Phase 不另加規則。
- Windows 序列埠：`CreateFileW(OPEN_EXISTING, dwShareMode=0)`（§6.2）——選配 com0com console-side 埠同樣以 share mode 0 開啟。
- OD-15：擁有者 host 已通過 Spike 1 基本資格（DUT-side pair `CNCA0=COM20`／`CNCB0=COM21`、HVCI 執行中）；需原生 Windows COM 的 Windows L1.5 改由 orchestrator 經 WSL interop 在該 host 以 `SW_TEST_COM_PAIR=COM20:COM21`、OD-11 隔離實例＋`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20` 執行（可執行 gate）。Console-side pair、控制線隔離、peer-close 仍是 Spike 1 剩餘項，結果未知。L2／L3 交 bench 前先跑 P2 Task 17 docker 兩容器拓樸（`L1.5-docker`，不計為 L2／L3）。
- Windows RPC 與 Telnet console 的信任範圍＝整機本機使用者（OD-12）；Telnet listener 只綁 `127.0.0.1`；規格與文件不得宣稱「同使用者隔離」。
- Rust-only 契約（SC-28、SC-29、SC-31）的黑箱案例一律以 P3 `tests/contract_blackbox/conftest.py` 的 `rust_only` marker 標記；Python 端報告為 skipped，不計 PASS。
- repo 政策：不得 commit 到 `main`；`.project-policy.yml` 的 `code_paths` 含 `tests/**`，因此只要動到 `tests/` 就要附 `changelog.d/<issue>-<slug>.md`（無 issue 時用本計畫指定的 `rust-s22-*` 名稱；`type` 只能是 `policy_check/changelog.py` `TYPE_TO_SECTION` 的鍵：`feat`、`fix`、`refactor`、`perf`、`change`、`docs`、`remove`、`deprecate`、`security`）。commit 用 Conventional Commits 繁中 subject，含 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer；完成前跑 `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .`。
- 本 Phase 的工作分支：`wt/rust-refactor/p6-slice22`（base `feature/rust-refactor`，符合 R-12 `wt/<slug>/<subtask>`，與 06／08 相同拓樸）；需要平行子工作樹時用 `wt/rust-refactor/p6-<task-slug>`，PR base 同為 `feature/rust-refactor`。紅燈 commit 只存在於工作分支，全部綠燈前不開 PR。

## Review Focus

1. **BC-02 的 `input_epoch` 失效邊界**：banner／quiet 重新 arm／reboot 命令／drain Phase 1／release／FlashSyncMatched 之後，mailbox 內帶舊 `input_epoch` 的 `ConsoleInputBytes`、deferred entry、已排入 TxWriter 但尚未進 OS 的 human job（`FenceReject::InputStale`），都不可到達 UART。沒有失效的 suspend／resume 與 soft preempt 不得遺失或重排任何按鍵（Task 4、Task 6、Task 11 `test_bc02_*`）。
2. **Human TX 的 OS 寫入前重驗與同源順序**：路由判定為 raw 之後、OS 實際提交之前（06 Task 9 `FenceSubmitGate` 臨界區，授權與提交同一把 Fence 鎖，R0-F1；不是「出列時驗一次後放鎖再 write」），若 `owner_gen` 改變、human admission 關閉或 `input_epoch` 改變，必須 `NotSubmitted`。被拒 bytes 依 D-15 結算：仍有效就依序轉入 deferred，失效就丟棄並診斷。WAL 只記實際確認的 bytes；部分寫出或結果不明的 human TX 不得重送（Task 6 `fence_human_raw.rs`、`human_tx_lane` 系列）。HumanReplay 另受 D-22 回放保留約束：回放結算前前景不釋放（下一命令不出列、`interactive_open`／`send` 回 `SESSION_BUSY`），繞過佇列取得 ownership 的操作先撤銷 `replay_auth`，尚未進 OS 的回放 `ReplayRevoked`、零 TX、放回 deferred 前端；舊按鍵不得落入下一個命令或 lease（`human_replay_paused_before_submit_then_ownership_change_is_zero_tx`、`queued_command_waits_for_replay_settle`、`ownership_change_mid_replay_redefers_unsent`，R2-F4）。
3. **Telnet parser 與出向 ring 的位元組完整性**：每 client 一個跨 chunk 狀態機；greeting 必須在該 client 加入 fan-out 之前送出；協商回覆與 greeting 為 `OutItem::Control`，永不被 drop-oldest 丟棄，但 Control 總量受 64 KiB 上限保護（超過就關閉該 console）；drop-oldest 不得切開 `IAC IAC`；Telnet 控制 bytes 永不成為 DUT TX（Task 2、Task 5、Task 9）。
4. **Lease 到期 tick（SC-13）與 touch 的競態**：重用 06 的 `ControlEvent::Tick`／`TimerEvent::LeaseExpiry`。tick 以處理當下的 `last_activity` 重算 deadline。tombstone 依 P1 語意可重複讀：tick 到期後 send／status／close 都回 `INTERACTIVE_EXPIRED`，FIFO 16 擠出後才是 `INTERACTIVE_NOT_FOUND`。stash 還原與回放只在 stash 未過期且 peer 仍在時進行，否則丟棄該 client 的 deferred（D-16、SC-31，Task 7）。
5. **Ownership 簿記的 #78／#136 分流與 DF-02**：`console_detach` 放棄整個 suspend 簿記，I/O 斷線只讓出 `suspended_owner`；`/proc` 掃描不得在 actor thread 內執行；只有 daemon 內部 human console 路徑（`SubmitOrigin::HumanConsole`）可被升格為 interactive lease，RPC 帶入的 `human:` source 一律當 agent（Task 6、Task 7、Task 10）。
6. **活性**：UART 寫入卡住（`FakeWriteStep::BlockForever`）時，持續按鍵與 RX 洪流不得讓 actor 或 hub 阻塞；`get_state` 仍在 50 ms 內回應，release 能進 `RECOVERING`。同一 client「最後一行輸入後立即斷線」時，該行仍被提交，因為 `ConsoleDisconnected` 與輸入走同一條 data lane（Task 6 `actor_liveness.rs`、Task 9）。

---

## 本 Phase 的設計決議（細化，Task 0 寫回架構文件與契約）

| ID | 決議 | 依據 |
|---|---|---|
| D-01 | **「穩定 symlink」的範圍**：console PTY **不建 symlink**，`vtty` 就是 `/dev/pts/N`；穩定性來自 `ConsoleHub` 每 session 一條、跨 `conn_epoch` 保留 console（等價 Python `PreservedConsoles`）。唯一的 symlink 是 `/dev/ttyMCU`，屬 P8。本 Phase scope 文字中的 "stable symlinks" 依此解讀，不新增任何 console symlink | UIO-12.7（KEEP＋DOC）、arch §4.7 |
| D-02 | **`input_epoch: u64`（`Generations` 新欄位）即 P1 Python `UARTBridge._owner_gen`（02 T09）的 Rust 名稱**，只在 BC-02 失效點遞增：BootBannerDetected、quiet 重新 arm、reboot 命令偵測、drain Phase 1（recovery／io_error／release／clear／bind／shutdown）、FlashSyncMatched。唯一對外入口是 `SessionCore::invalidate_human_input(reason, fx)`，P7 quiet arm 與 P8 FlashSyncMatched 呼叫它；`DeferredBuffer` 是 `SessionCore` 的私有欄位，不提供其他 Phase 直接操作。遞增、`FenceState.input_epoch` 更新與 `CoreDeps.input_epoch_pub`（hub 讀取用的 `Arc<AtomicU64>`）寫入在**同一次 `Fence::with`** 內完成，這是線性化點：之後才進 OS 的 human job 一律 `InputStale`。`ConsoleInputBytes`、deferred entry、human `TxPermit.input_epoch` 帶擷取當下的值，不等於現值即丟棄。Rust `owner_gen` 依 arch §5.3 只在 owner 變更時遞增（lease 開關、raw 授予／撤銷、suspend depth 0↔1、soft preempt、self-heal grant；**admission 切換不遞增**），只供 HumanRaw permit 與 lease timer，**不**用於 deferred 回放判定，否則正常 suspend 0→1／resume 1→0 的 `owner_gen++` 會讓 #53 回放與在途按鍵全部失效。06 T12 `begin_stop` 的 `self.gens.owner_gen += 1  // BC-02` 改為 `invalidate_human_input(Recovery／Release)`（ownership 清空另依 §5.3 遞增 `owner_gen`）。此為對 arch §5.2／§5.3／§5.5／§5.11「`owner_gen` 隔離 DeferredBuffer 回放與 console 輸入」的細化 | §5.2.6、BC-02、arch §5.3、02 T09 |
| D-03 | `FenceState` 新增 `human_admission_blocked: bool`、`input_epoch: u64`、`replay_auth: Option<u64>`（D-22）；`TxPermit` 新增 `input_epoch: Option<u64>`（只有 HumanRaw／HumanReplay 為 `Some`，06 既有 permit 建構點一律 `None`）與 `replay_hold: Option<u64>`（只有 HumanReplay 為 `Some(hold_id)`，其餘一律 `None`）；`FenceReject` 新增 `HumanAdmissionClosed`、`InputStale`、`ReplayRevoked`。`Fence::admit` 在 06 既有檢查之後：`HumanRaw` 依序比對 `input_epoch`（不符→`InputStale`）、`owner_gen`（不符→既有 `OwnerMismatch`）、`human_admission_blocked`（→`HumanAdmissionClosed`）；`HumanReplay` 依序比對 `input_epoch`（不符→`InputStale`）、**專屬回放授權** `permit.replay_hold.is_some() && permit.replay_hold == st.replay_auth`（不符→`ReplayRevoked`），不比對 `owner_gen` 與 `human_admission_blocked`（Python `resume_interactive` 的 flush 不帶 raw token，且發生在 `_finish_foreground_operation` 開放 admission **之前**，`session_manager.py:3699-3703`）。回放可以忽略 owner_gen／admission 的前提是 D-22 的回放保留仍有效：任何新操作取得 ownership 時，會在同一次 `Fence::with` 內撤銷 `replay_auth`，之後才進 OS 的回放一律 `ReplayRevoked`、零 TX，並依 D-15(3) 重新 defer。06 T9「`owner_gen` 只對 HumanRaw／HumanReplay」因此改為只對 HumanRaw，HumanReplay 改用 `replay_auth`。被拒 bytes 依 D-15 結算 | 06 §5.1、§6.6；arch §5.4；§11.3「不得將不同來源的 bytes 以契約禁止的方式交錯」「Lease 切換不得回放失效上下文的輸入」 |
| D-04 | PAL 新增 `ConsoleMux`（arch §4.7 未列）：一條 `ConsoleHub` thread 要同時等待多個 PTY master、socket、listener 與 waker。POSIX 用 `poll(2)`＋eventfd（非 Linux 用 self-pipe）；Windows 用 `WSAPoll`＋loopback socket pair 作 waker；com0com handle 由 PAL 內部 reader thread（有界 channel 64）轉成 mux 事件。mux 需要底層 fd／SOCKET，因此 arch §4.7 的 `ConsoleIo`／`ConsoleListener` 追加 `fn mux_handle(&self) -> MuxHandle`；`MuxHandle` 是 PAL 私有建構的不透明型別（POSIX fd、Windows SOCKET、com0com channel id、fake id），只有同一個 PAL 的 mux 能解讀 | arch §1.4、§5.1 ConsoleHub |
| D-05 | 出向 ring（SC-15，Rust-only）：每 console 一個 `OutRing`，元素為 `OutItem::Control`（greeting、協商回覆；不被 drop-oldest 丟）或 `OutItem::Display { bytes, iac_escaped }`（RX fan-out、本地回顯；可丟）。Display 合計超過 1,048,576 bytes 時，丟最舊的整個 Display 元素（已部分寫出的 front 元素不丟）；單一元素本身超過上限時只保留尾端 1,048,576 bytes，起點落在 `FF FF` 對中間時再後移 1 byte。Control 合計上限 `CONSOLE_CONTROL_MAX_BYTES=65536`：Telnet 對每個未支援的 `WILL x` 都回 `DONT x` 而不去重（`telnet_console.py:113-118`），只送不讀的 client 會讓 Control 無限累積，所以超過上限時 hub 關閉該 console（依一般斷線流程送 `ConsoleDisconnected`）並寫 WARNING，不靜默丟棄（SC-31(c)）。PTY console 在 peer 掃描判定「無外部 holder 且已過 2.0 s grace」時清空其 Display，避免新開 minicom 時灌入最多 1 MiB 舊資料；Python 只會留 kernel buffer 內容 | §5.4、SC-15、06 §7.6 |
| D-06 | SC-16 依 P1 T12 規則 4～5（Python 基線，A/B 一致）：line editor 緩衝上限 `CONSOLE_LINE_BUFFER_MAX=16385` bytes。緩衝已滿 16385 bytes 時再收到非終止、非 backspace 的 byte，就清空緩衝、進入 discarding 並寫一次 WARNING；discarding 期間後續 byte（含 backspace 與可列印字元）一律丟棄**且不回顯**；下一個 CR／LF 結束 discarding、不送 arbiter。discarding 結束時終止字元是否回顯 `\r`，以 P1 實作後 Python `_consume_console_input` 產生的 golden 為準（Task 3 golden 含 ≥16386 bytes 的案例）。恰 16385 bytes 的行照送 arbiter，由 arbiter 回 `CMD_TOO_LONG`。arbiter 拒絕 `CMD_TOO_LONG`、`SESSION_QUEUE_FULL`、`SESSION_NOT_READY`（P1 T12 規則 5 的集合）時寫 WARNING | SC-16、G-02、02 T12 |
| D-07 | BC-02 溢位語意逐字照 P1 T09 規則 1：某 client 的 deferred entry 附加後長度 > 262144，就刪除該 entry（連同本次 bytes）並寫 WARNING；之後同一 client 的 bytes 開新 entry 照常保留（沒有 poison 狀態）。回放只送 `input_epoch` 等於現值、且 `client_id`＋`client_lifetime` 仍存活的 entry（P1 T09 規則 4）；stale prune、detach、drop、reap 一併刪除該 client 的 entry（規則 5） | BC-02、arch §5.11、02 T09 |
| D-08 | SC-13 依 P1 T12 規則 1～2 與 06 T14 既有 tick：重用 06 的 `ControlEvent::Tick`（1.0 s）與 `TimerEvent::LeaseExpiry`，**不**新增事件。tick 只處理非 human lease 的 timeout 到期（human lease 只看 peer）；到期時 status 設為 `"expired"`，走 `_close_interactive_locked` 等價流程（stash 還原與回放受 D-16 限制），lease 進 06 的 `Tombstones`（每 session FIFO 16）。tombstone 可重複讀：`interactive_send`／`interactive_status`／`interactive_close` 遇 tombstone 一律回 `{"ok":false,"error_code":"INTERACTIVE_EXPIRED","interactive_id":…}`，且**不移除**；被 FIFO 擠出後才是 `INTERACTIVE_NOT_FOUND`。存取時判定（lazy）的到期與 tick 相同進 tombstone（Python Stage 0 T12 起 `keep_tombstone=True`，`session_manager.py:5865-5875,7183-7197`，02 T12）：第一次回 `INTERACTIVE_EXPIRED` 並關閉、移出 active、`tombstones.push(id,"expired")`，之後持續回 `INTERACTIVE_EXPIRED`，被 FIFO 擠出後才 `INTERACTIVE_NOT_FOUND`（06 `send_expired_closes` 已同步改正）（parity-amendments-2026-10-26 #54）。06 `tick_expires_lease_sc13` 期望 tick 後 `status` 回 `NOT_FOUND`，與 P1 不符，由本 Phase Task 7 改正 | SC-13、SES-16.4-07、05 §10.2～§10.5、02 T12 |
| D-09 | `/proc` 掃描（`held_slave_paths()`）不得在 actor thread 執行，來源有三：(a) RPC handler thread 處理 `session.console_attach`／`console_detach`／`console_list`／`interactive_open`，以及 `session.list`／`session.get_state`／`session.activity`／`health.status` 的 session 摘要時，先取一次快照，並以 `ControlEvent::ConsoleScan{scan, source: ScanSource::Rpc}` 從**同一個 sender thread、緊接在該 request 之前**送進 control lane（同 sender FIFO），core 先 prune 再處理 request，prune 時點與 Python 相同，06 的 `SessionRequest` 變體因此不必加欄位；(b) `ConsoleHub` 每 `HUMAN_PEER_GRACE_S=3.0` s 掃一次並送 `ConsoleScan{source: Hub}`，供 reaper 與 self-heal 使用；(c) 命令執行路徑的 lease refresh 用最近一次快照（≤3.0 s）。peer 判定時點不屬位元組契約 | UIO-12.10-c（IMPL）、06 §7.5、05 §10.5 |
| D-10 | DF-02：06 `SubmitReq` 新增 `pub origin: SubmitOrigin`（`enum SubmitOrigin { Rpc, HumanConsole { client_id: String } }`，06 既有建構點一律 `Rpc`）。只有 `SubmitOrigin::HumanConsole`（line editor 產生）會跳過 agent gate、在 lease 判定時視為 human、逾時升格 `PROMOTE_HUMAN_INTERACTIVE`、reboot 命令走 human 分支開 lease。`SubmitOrigin::Rpc` 即使 `source` 以 `human:` 開頭也一律以 agent 對待（OD-14 #5 確認原預設） | DF-02、RPC-H-05 |
| D-11 | SC-28（新登記，Rust-only）：env `SERIALWRAP_CONSOLE_BACKEND` ∈ {`auto`,`pty`,`tcp`}（不分大小寫、strip），只由 `sw_pal::native()` 讀取。`auto`／空／未知值：有 PTY 能力就用 `Pty`，否則 `TcpTelnet`；`tcp`：一律 `TcpTelnet`；`pty` 但平台無 PTY：退回 `TcpTelnet` 並寫 WARNING `SERIALWRAP_CONSOLE_BACKEND=pty 在此平台不支援，改用 tcp`。用途是讓 Linux L1.5 能測 Telnet backend；名稱加入 `tests/conftest.py` 的 pop 清單與 `bb_harness.isolated_env` 的刪除清單。Python 沒有此 env，所以 Linux 上的 Telnet 黑箱只能對 Rust 執行（信心較低）；Windows 上兩版 daemon 原生都是 Telnet，同一批案例做 A/B（Task 12） | Python `_select_backend` 慣例（06 §10.4） |
| D-12 | SC-29（新登記，Rust-only，OD-01 選配）：`config.yaml` 新鍵 `windows.serial_console`，型別為 mapping：`<DUT 的 by_id（Windows COM 名，如 "COM5")>: {console_port: "COM40", peer_port: "COM41"}`（埠名僅為範例；擁有者 host 的 COM20／COM21 已作 DUT-side pair，Console-side pair 須另建不衝突的埠，OD-15）。只在 `caps.com0com_console=true`（Windows）時生效；兩個埠名自動併入 `DeviceSelection.exclude_coms`，永不被列舉成受管 UART（§7.1、§7.4）。session 的 bridge 起來後，`ConsoleHub` 以 `open_com0com_console(console_port)` 開啟（share mode 0），註冊為 `ConsoleTransportKind::Com0com`（`label="serial:<console_port>"`、`vtty=<console_port>`、ownership 規則同 TCP accept、`has_external_peer` 恆 true、不被 reaper 回收）。虛擬端的 DTR／RTS／baud 變更不轉送到 DUT UART（不呼叫任何 `set_baud`／modem 控制）。開啟失敗只寫 WARNING，session 照常運作；非 Windows 平台出現此鍵時寫一次 WARNING 後忽略 | §7.3、OD-01、OD-08 |
| D-13 | **SC-31(a)（新登記，Rust-only）**：沒有 active `conn_epoch`（`DETACHED`／`RECOVERING`／`RELEASED`／drain 中，即 `fence.active_epoch == None`）時收到的 console 輸入一律丟棄，並寫 D-19 的限速 WARNING。Python（含 P1）會把這段輸入留在 kernel PTY buffer，等新 bridge 讀入後以新的 `_owner_gen` 處理，違反 §11.3「Recovery 不得回放失效上下文的輸入」。OD-04 核准的 BC-02 範圍未涵蓋這個情形，所以另登記 SC-31，不併入 BC-02 | §11.3、§5.3「失效則丟棄並可診斷」 |
| D-14 | 規格 §5.1.3 事件與 Rust 事件的對照，Task 0 寫入 recon 新列 `CON-EVT-01`（KEEP＋DOC）：`ConsoleIdleTimeout` 在 Python 基線不存在，保留為規格事件、Rust 不產生，不新增 console 閒置斷線政策；`ConsolePeerGraceExpired` 是 `ConsoleScan` 處理中 human lease 的 peer-loss grace（3.0 s）判定；`InteractiveExpired` 對應 06 的 `ControlEvent::Tick`／`TimerEvent::LeaseExpiry` | §5.1.3、05 §10.5 |
| D-15 | **Human TX lane**（每 session 一條，`human_console/human_tx.rs`）：(1) 同時最多一筆 human TX（HumanRaw 或 HumanReplay）未結算；後續的依序排在 `HumanLane.backlog`，上限 `HUMAN_TX_BACKLOG_MAX_BYTES=262144`，溢位時清空該 client 的 backlog 並寫 WARNING。(2) 某 client 有未結算 job 或 backlog 非空時，它的新輸入不論路由為 Raw 或 Defer，一律排到 backlog 尾端，結算後再依序重新路由，以保持同源順序（"ab" 不會變 "ba"）。(3) 結算規則：`Completed(len)` 送下一筆；`Completed(n<len)` 或 `Unknown` 不重送剩餘部分（BC-03 no-auto-resend），寫 WARNING；`NotSubmitted` 時，chunk 的 `input_epoch` 已不等於現值就丟棄並寫 WARNING，否則：HumanRaw 附加到該 client 的 deferred entry（Python `send_bytes` 第三個 defer 點，06 §5.1 第 1 步）；HumanReplay（含 `ReplayRevoked`）以 `DeferredBuffer::restore_front` 放回該 client deferred entry 的**前端**，同一 hold 在 backlog 中尚未送出的回放項目也依原順序放回各自 client 的前端（D-22(4)），等下一個回放點再送，不在本次結算重送。(4) 重新路由時若路由為 Raw，但該 client 的 deferred 非空，就把輸入附加到 deferred，並在 context 允許時立即以 HumanReplay 回放該 client（在 D-22 的回放保留下進行；已有 hold 時併入同一 hold），這樣「finish 先於 rejection completion」時不會卡在 deferred。context 允許的條件包含 `foreground.is_none()`：前景操作進行中不立即回放，留在 deferred 等該操作的 `fg_finish`。(5) actor 經容量 1 的 channel `try_send` 給 per-session pump thread（`serialwrap-human-tx-<COM>`）；pump 對 06 `TxWriterHandle.jobs` 阻塞送出、等 `TxReport`，再以 control lane 回 `HumanTxCompleted`。pump 可以阻塞，actor 不會。drain Phase 1 cancel 後，TxWriter 結束，pump 收到 `Disconnected` 就回 `NotSubmitted`。(6) TX 帳本結算（06 T9／T12 結算協定，R1-F8）：core 在 `HumanTxCompleted` 依 (3) 結算該 job 後，對其 `op_id` 呼叫 06 `settle_tx`（恰一次；`report.epoch` 不等於 `conn_epoch` 時不產生），否則每個 human chunk 都會在同一 epoch 的 TX 帳本留下項目 | arch §5.4、§5.7、§5.10、§11.3 #2 #5 |
| D-16 | **SC-31(b)（Rust-only）**：lease close 或 expire 時，若 stash 的 human lease 沒有還原（stash 已過期或 peer 不在），`PostClose.replay_allowed=false`：先刪除 stash client 的 deferred entry（WARNING reason `lease_dropped`），再 `resume()`，不回放該 client 的輸入；其他 client 的回放不受影響。Python `_PostCloseAction.execute` 會無條件 `resume_interactive()` 並 flush，違反規格 §5.3「InteractiveClose／Expired：只有上下文仍有效才還原 Human／回放」與 §11.3 | §5.3、§11.3 |
| D-17 | **ConsoleHub 不阻塞**：(1) hub 對 mailbox 一律 `try_send`。滿載時事件進 hub 的有界 `pending`（`CONSOLE_HUB_PENDING_EVENTS=64`，保持 FIFO），並暫停讀取所有 console io（背壓留在 OS socket／PTY buffer）；pending 非空時 `mux.wait` 逾時縮為 10 ms 以便重試。(2) 每輪迴圈處理 `cmd` 與 `rx` channel 各最多 `CONSOLE_HUB_LOOP_QUOTA=64` 筆，避免持續 RX 使排空迴圈不結束。(3) `ConsoleDisconnected` 走 **data lane**（與同 client 的 `ConsoleInputBytes` 同一條 FIFO），不能超車尚未處理的輸入；`ConsoleConnected`、`ConsoleScan` 走 control lane。(4) actor 對 hub `cmd` channel `try_send`：`Out(Display)` 滿載就丟棄並計數；`Close`／`CloseAll`／`Shutdown` 在 channel 滿時改寫入 `HubHandle.critical`（`Mutex<HubCritical>`：關閉集合上限為 console 數，加上 `close_all`、`shutdown` 旗標）並 `wake()`，hub 每輪先處理 critical。(5) 輸入的 `input_epoch` 在每輪 `mux.wait` **之前**取一次快照（P1 T09 規則 3：`_loop` 每輪頂端取 gen），該輪讀到的所有輸入都帶這個值；失效若發生在快照之後，該輪輸入一律 stale | arch §5.1、§5.2、§5.3 有界延遲 |
| D-18 | 新增有界資源（Task 0 登記於 `contracts/resource_limits.yaml`，含 overflow 策略）：`console_hub_rx_queue` 256 chunks（reject，計數＋WARNING）、`console_hub_ctrl_queue` 64（reject：Display 丟棄；critical 走旁路）、`console_hub_pending_events` 64（block_sender：暫停讀取）、`console_out_ring_display_bytes` 1048576（drop_oldest）、`console_control_max_bytes` 65536（close_connection）、`console_line_buffer_bytes` 16385（reject）、`human_tx_backlog_bytes` 262144（clear_all）、`telnet_consoles_per_session` 32（pause_accept：達上限時 listener 不加入 mux 讀取，超過 backlog 8 由 OS 拒絕；console 數降下即恢復，SC-31(d)）、`com0com_reader_channel` 64（block_sender）。PTY console 數由顯式 RPC 決定，無 peer 者 2.0 s 後被 reap，不另設上限 | arch §0.5、§5.4、§5.10 |
| D-19 | 「失效則丟棄並可診斷」（§5.3 Console 輸入列）：stale `input_epoch`、未知 client 或舊 lifetime、無 active epoch（SC-31(a)）、flash 期間的輸入丟棄，各寫一條 WARNING，限速為每 `(client_id, reason, input_epoch)` 至多一筆 | §5.3 |
| D-20 | SessionPublic 的 `vtty` 重現 Python `session.vtty_path` 的**快取**語意，不即時計算 primary：只在 Python 的更新點改值，包括 attach 完成（`session_manager.py:2706`、`:3130`、`:3306`）、`console_attach` 時若為 None 才設為新 console 的 vtty（`:3957`、`:3961`）、`detach_console`（`:3994`）、`_set_retained_consoles`（`:1750-1753`），以及清為 None 的位置（`:3096`、`:3116`、`:3126`、`:3169`）。`console_count` 與 `console_endpoint` 即時取自 `HumanConsoleState`。`PublicCtx` 在 `session_core.rs` 由 `HumanConsoleState` 產生，不放在 `sw-proto` | 02 §6.1、`to_public_dict` |
| D-21 | line editor 的半行緩衝（`ConsoleRecord.line`）在 BC-02 失效時**不清除**，與 Python／P1 相同：它是第二個 console 尚未送出的編輯行，不屬 deferred 回放。Task 0 在 recon 登記 `CON-LINE-01`（KEEP＋DOC） | §11.3、02 T09 |
| D-22 | **回放保留（Replay hold，R2-F4）**：Python 的 `resume_interactive()` flush 與 `_finish_foreground_operation` 的 admission flush 都在 `_foreground_operation` token 仍持有時同步寫完，token 在回放完成後才清（`session_manager.py:3699-3703`、`:1104-1120`），所以下一個命令、`interactive_open`／`send`、self-test 或 file transfer 不可能在舊按鍵寫出前取得 UART。Rust 的回放是非同步 lane，必須顯式保留同一段區間：(1) 每個回放點（`fg_finish` 最外層 resume 與 admission 開放、`human_suspend_end`、lease close／expire 的 `PostClose` resume、D-15(4) 的立即回放）在 `take_replay` 有非空結果時建立 `ReplayHold { hold_id, remaining }`（`HumanConsoleState.replay_hold`），並在**同一次** `Fence::with` 內設 `st.replay_auth = Some(hold_id)`；該批每筆 HumanReplay 的 permit 帶 `replay_hold: Some(hold_id)`。已有 hold 時新增的回放併入同一 hold（`remaining += n`）。`take_replay` 結果為空時不建立 hold。(2) 未撤銷的 hold 存在期間 `SessionCore::foreground_busy()` 為 true（`foreground.is_some() \|\| human.replay_hold.as_ref().is_some_and(\|h\| !h.revoked)`），所有 06／07／09 的「前景忙」判定一律改用它：佇列不出列（命令維持 accepted，不回錯，對應 Python 每 session 單一 arbiter worker 要等 `execute_command` 連同 flush 返回才取下一筆）；READY `interactive_open`、`interactive_send` 回 `SESSION_BUSY`；`console_attach` 回 `"interactive_owner":false`；SessionPublic `foreground_busy` 為 true；例外（parity-amendments-2026-10-26 #56，對齊 Python `_interactive_post_close`）：hold 來源為 lease **tick 到期**的 `PostClose` 還原（`ReplayHold.origin == LeaseTickClose`）時，還原結算前 `interactive_open`、interactive 模式 `command.submit`、human 救援與 `console_attach` 一律回 `{"ok":false,"error_code":"SESSION_INTERACTIVE_BUSY"}`（`console_attach` 另帶 `"selector":<COM>`，`session_manager.py:6236,6514,6692,6853,6949,7045`），與鎖內 `tests/test_stage0_lease_flash_console.py::test_tick_blocks_new_lease_until_owner_restore_finishes` 相同；P8 push／pull 回 `SESSION_BUSY`、FlashSyncProbeStart 依 SC-14 拒絕。(3) 該 hold 最後一筆回放結算（`Completed`、partial、`Unknown`、`NotSubmitted` 皆算）時，在同一次 `Fence::with` 內清 `replay_auth = None`、`replay_hold = None`，然後依 06「前景空閒則出列」取下一筆命令。(4) **撤銷（OS 提交點的後備）**：任何不經佇列的新操作取得 ownership 時——`fg_begin`、`human_suspend_begin`、`StartOp` 任一前景 op、`interactive_open`（含 ATTACHED 救援分支：Python 該分支不檢查前景，`session_manager.py:4027` 只在 READY 判 `SESSION_BUSY`）、`set_interactive_owner` 改為非 human owner、`begin_stop`——先在同一次 `Fence::with` 內設 `replay_auth = None` 並把 hold 標為 revoked，再做自己的 ownership／admission 變更。revoked hold 在 backlog 中尚未送出的回放立即 `restore_front` 回 deferred；在途那一筆若尚未通過 `FenceSubmitGate`，OS 提交點回 `ReplayRevoked`、零 TX，結算時 `restore_front`（D-15(3)）；若已通過，它在 TxWriter 單寫入者順序上早於新操作的任何 TX，照實記錄。被撤銷的 bytes 在新操作的回放點（例如其 `fg_finish`）以新 hold 重新回放；若該點依 D-16 不允許回放，依 D-16 丟棄。revoked hold 保留到其在途那一筆結算為止（期間不算前景忙，`foreground_busy()` 只計未撤銷的 hold）；若新回放點在此之前到達，先記 `replay_after_revoked=true`、不 `take_replay`，待在途那筆結算並 `restore_front` 後才執行該次回放並建立新 hold，避免較新的 bytes 先於被撤銷的舊 bytes 寫出。hold 本身沒有逾時：UART 寫入卡住時 hold 一直存在，與 Python token 被背壓中的 `send_bytes` 卡住相同；drain Phase 1 由 `begin_stop` 撤銷並 `invalidate_human_input` 結束。(5) human 之間的 owner 變更（self-heal grant、detach、drop、pending human promote）不撤銷：回放 bytes 仍經單筆在途的 human lane，在新 owner 的 HumanRaw 之前寫出，順序與 Python 同步 flush 相同 | §5.2.6、§5.3 Console 輸入列、§11.3；`session_manager.py:1048-1120`、`:3699-3703`、`:4027`、`:4232`；`uart_io.py:1378-1400` |

**WARNING log 模板**（daemon stderr、`%(message)s`、只輸出 WARNING 以上，arch §6.1；以 06 `Effect::Warn(String)` 輸出；日誌文字不是位元組契約，黑箱只斷言 `client_id` 與 reason／code 子字串；P1 Python 的模板文字不同，A/B 不比對日誌全文）：

| 情境 | 模板 |
|---|---|
| line 被 arbiter 拒絕（D-06） | `console line 被拒（com=%s client_id=%s error_code=%s bytes=%d）` |
| line 超過 16385 bytes（D-06） | `console line 超過 16385 bytes，整行丟棄（com=%s client_id=%s）` |
| deferred 溢位（D-07） | `deferred input 超過 262144 bytes，整段清空（com=%s client_id=%s dropped=%d）` |
| deferred 失效（D-02） | `deferred input 因上下文失效清空（com=%s reason=%s dropped=%d）`，reason ∈ `boot_banner`／`quiet_rearm`／`reboot_command`／`recovery`／`release`／`flash` |
| lease 上下文失效（D-16） | `deferred input 因 lease 上下文失效清空（com=%s client_id=%s reason=lease_dropped dropped=%d）` |
| 輸入丟棄（D-19） | `console 輸入因上下文失效丟棄（com=%s client_id=%s reason=%s bytes=%d）`，reason ∈ `stale_epoch`／`unknown_client`／`no_active_epoch`／`flash` |
| human TX 未完整確認（D-15） | `human TX 未完整確認，不重送（com=%s client_id=%s outcome=%s written=%d len=%d）` |
| human TX backlog 溢位（D-15） | `human TX backlog 超過 262144 bytes，整段清空（com=%s client_id=%s dropped=%d）` |
| ring 溢位（D-05，每次溢位事件一筆） | `console 出向佇列溢位，丟棄最舊顯示資料（com=%s client_id=%s dropped=%d）` |
| Control 溢位（D-05） | `console 協商回覆累積超過 65536 bytes，關閉該 console（com=%s client_id=%s）` |
| hub RX 佇列滿（D-18，每次由未滿轉滿一筆） | `console fan-out 佇列已滿，丟棄顯示資料（com=%s dropped=%d）` |
| hub mailbox 滿（D-17，每次由未滿轉滿一筆） | `console 輸入暫停讀取：session mailbox 已滿（com=%s pending=%d）` |
| Telnet console 達上限（D-18） | `console 數已達 32，暫停接受新的 Telnet 連線（com=%s）` |
| com0com 開啟失敗（D-12） | `serial console 開啟失敗（com=%s port=%s）：%s` |
| 非 Windows 出現 `windows.serial_console`（D-12） | `windows.serial_console 在此平台不支援，已忽略` |
| console RX handler 例外（沿用 G-01） | `console RX handler 例外（com=%s），略過此塊、console 續行` |

---

## 跨 Phase 介面（本 Phase 消費、由 P2／P3／P5 交付）

以下簽章逐字取自 06（P5）、04（P3）、03（P2）的 Produces 段落。Task 1 Step 2 以 `scripts/rust/check_p6_prereqs.py` 逐符號確認存在；任一缺漏屬上游 Phase 缺陷，停止並回報（不是本 Phase 自行補做）。本 Phase 對這些型別的修改**只限**下一節「P6 對 P5 的增補清單」所列項目。

```rust
// sw-proto（P3，04 T3～T5）
pub enum PyValue { Null, Bool(bool), Int(PyInt), Float(f64), Str(String), List(Vec<PyValue>), Dict(PyDict) }
pub struct PyDict(pub indexmap::IndexMap<String, PyValue>);
impl PyDict { pub fn new() -> Self; pub fn insert(&mut self, k: &str, v: PyValue); pub fn get(&self, k: &str) -> Option<&PyValue>;
              pub fn setdefault(&mut self, k: &str, v: PyValue); pub fn update(&mut self, other: &PyDict); pub fn get_truthy(&self, k: &str) -> bool; }
macro_rules! pydict { … }
pub mod pyjson { pub fn loads(s: &str) -> Result<PyValue, JsonError>; pub fn dumps(v: &PyValue, o: &DumpOpts) -> String; pub const RPC_WIRE: DumpOpts; }
pub struct Params<'a>(&'a PyDict);  // str_or / int_or / float_or / truthy / raw / selector_chain
pub mod pycompat { pub fn decode_utf8_replace(b: &[u8]) -> String; pub fn clean_text(s: &str) -> String;
                   pub fn a2b_base64_lenient(b: &[u8]) -> Result<Vec<u8>, B64Error>;   // clean_text／a2b_base64_lenient：06 T0c
                   pub fn splitlines(s: &str) -> Vec<&str>;
                   pub fn shlex_split(s: &str) -> Result<Vec<String>, ShlexError>; }   // shlex_split 由 06 T0c 交付（本 Phase Task 3 只加回歸 golden，不重新定義）
pub mod pyfmt { pub fn py_str(v: &PyValue) -> String; }   // py_str 在 pyfmt（04 T4），不在 pycompat
pub mod pyre { pub fn compile(pat: &str, flags: PyFlags) -> Result<PyRegex, PyReError>; }
pub mod constants { /* arch §3.1：HUMAN_ACTIVE_WINDOW_S、HUMAN_PEER_GRACE_S、STALE_CONSOLE_GRACE_S、MAX_RECOVERY_LEASE_S、
                       BOOTLOADER_RX_TAIL_BYTES、ATTACHED_CONSOLE_LEASE_TIMEOUT_S、DEFERRED_INPUT_MAX_BYTES、CONSOLE_TCP_BACKLOG、
                       SERIAL_READ_CHUNK（telnet::* 不在上游，由本 Phase Task 1 定義） */ }
// golden：scripts/rust/goldens/_tsv.py（四欄 TSV）、tests/test_rust_goldens_fresh.py（GENERATORS 追加列）

// sw-pal（P5，06 T1～T2；arch §4.7）
pub trait ConsoleTransportFactory: Send + Sync { fn kind(&self) -> ConsoleKind; fn create_pty(&self) -> PalResult<PtyPair>;
    fn bind_tcp_console(&self) -> PalResult<Box<dyn ConsoleListener>>; fn open_com0com_console(&self, port: &str) -> PalResult<Box<dyn ConsoleIo>>;
    fn held_slave_paths(&self) -> Option<HashSet<String>>; }
pub struct PtyPair { pub master: Box<dyn ConsoleIo>, pub slave_keepalive: KeepAlive, pub slave_path: String, pub slave_ref: PtySlaveRef }
// 06 T2 已建 crates/sw-pal/src/console_pty.rs＋console_pty/posix.rs（openpty、raw termios、keepalive）
pub enum FakeWriteStep { …, BlockUntilCancel { accept_before_cancel: usize }, BlockForever }   // FakeDevice::{script_write, release_block, take_tx}

// sw-core（P5，06 T8～T14）
pub type Mono = u64;                                                   // ns；測試直接寫整數，不是 Mono(n)
#[derive(Clone, Debug, Default)] pub struct Generations { pub conn_epoch: Option<u64>, pub fsm_gen: u64, pub owner_gen: u64, pub bridge_generation: u64 }
pub struct SubmitReq { pub session_id: String, pub command: String, pub source: String, pub mode: String, pub timeout_s: f64,
                       pub priority: i128, pub expected_duration_s: Option<f64>, pub completion_regex: Option<String> }
pub enum TxClass { Flash, FlashProbe, LeaseOwner, HumanRaw, HumanReplay, LoginProbe, ReadyReconfirm, Command, FileTransfer, SystemRecoverKey, SelfTestProbe, InteractiveOpenCmd }
pub struct TxPermit { pub op_id: OpId, pub epoch: u64, pub owner_gen: u64, pub class: TxClass, pub source: String, pub cmd_id: Option<String>, pub log: bool }
pub struct FenceState { pub active_epoch: Option<u64>, pub owner_gen: u64, pub frozen: bool, pub quiet_active: bool, pub reconfirm_pending: bool,
                        pub flash_mode: FlashMode, pub wal_unhealthy: bool, pub cancelled: HashSet<OpId>, pub in_flight: Option<InFlight> }
pub enum FenceReject { StaleEpoch, Frozen, Quiet, ReconfirmPending, WalUnhealthy, Flash, OwnerMismatch, Cancelled }
impl Fence { pub fn with<R>(&self, f: impl FnOnce(&mut FenceState) -> R) -> R; pub fn admit(st: &FenceState, p: &TxPermit) -> Result<(), FenceReject>; }
pub struct TxJob { pub permit: TxPermit, pub payload: Vec<u8>, pub reply: crossbeam_channel::Sender<TxReport> }
pub struct TxReport { pub op_id: OpId, pub epoch: u64, pub result: TxResult, pub wal_seq: Option<u64>, pub reject: Option<FenceReject> }
pub struct TxWriterHandle { pub jobs: crossbeam_channel::Sender<TxJob> }   // bounded(64)；只有可阻塞的 thread 能對它 send
pub enum DataEvent { UartRxChunk { epoch: u64, rx_seq: u64, wal_seq: u64, bytes: Vec<u8> } }
pub enum ControlEvent { Request { req: SessionRequest, reply: Reply }, DeviceAttached{..}, DeviceDetached{..}, BridgeOpened{..}, UartIoError{..},
    WorkerStopped(..), WorkerDrainTimeout{..}, OpFinished { op_id, epoch, outcome: OpOutcome, after_rx_seq }, LoginFinished{..},
    Timer(TimerEvent), StateCommitted{..}, Tick, Shutdown { reply: Reply } }
pub enum TimerEvent { BootQuietExpired{..}, LeaseExpiry { lease_id: String, deadline: Mono }, DrainTimeout{..} }
pub enum MailboxItem { Control(ControlEvent), Data(DataEvent) }
pub enum SessionRequest { GetState, Activity, Submit(SubmitReq), Release{..}, DeviceAttach{..}, SessionAttach, Recover{..}, Clear, Bind{..},
    InteractiveOpen { owner: String, timeout_s: f64, command: String, allow_attached: bool },
    InteractiveSend { interactive_id: String, data: String, encoding: String },
    InteractiveStatus { interactive_id: String, screen_chars: i128 }, InteractiveClose { interactive_id: String },
    SelfTest{..}, LogStart, LogStop, LogStatus, ResultTail{..} }
pub enum Effect { Reply(Reply, Result<PyDict, PyErr>), OpenBridge{..}, BeginDrain{..}, StartLogin{..}, StartOp { grant: OpGrant, spec: OpSpec },
    Schedule { at: Mono, ev: TimerEvent }, WalMeta{..}, Persist{..}, Notify(ManagerNote), Warn(String) }
pub struct Effects(pub Vec<Effect>);
pub struct CoreDeps { pub fence: Arc<Fence>, pub registry: Arc<CommandRegistry>, pub wal: Arc<WalWriter>, pub rx: Arc<RxShared>,
                      pub epochs: Arc<EpochAllocator>, pub wall_iso: Arc<dyn Fn() -> String + Send + Sync>, pub rng: Arc<dyn Fn(&mut [u8]) + Send + Sync> }
pub struct PublicCtx { pub console_count: i128, pub vtty: Option<String>, pub console_endpoint: Option<Option<String>> }
impl SessionCore { pub fn new(..) -> SessionCore; pub fn on_event(&mut self, ev: MailboxItem, now: Mono) -> Effects;
                   pub fn public(&self, now: Mono, ctx: &PublicCtx) -> PyDict; pub fn state(&self) -> SessionState; pub fn generations(&self) -> &Generations;
                   fn begin_stop(&mut self, kind: StopKind, waiter: Option<Reply>, now: Mono) -> Effects; }      // Phase 1（06 T12）
pub trait EffectExecutor: Send { fn execute(&mut self, e: Effect, tx: &crossbeam_channel::Sender<ControlEvent>); }
// uart_bridge.rs（06 T10）
impl RxShared { pub fn tail(&self, max_chars: usize) -> String; /* 0 → 全窗 */ }
pub struct ReaderCtx { pub com: String, pub epoch: u64, pub wal: Arc<WalWriter>, pub rx: Arc<RxShared>, pub sentinel: Option<Box<dyn ConsoleIo>>,
                       pub data: crossbeam_channel::Sender<DataEvent>, pub control: crossbeam_channel::Sender<ControlEvent>, pub clock: Arc<dyn Clock> }
// quiet_gate.rs（06 T13）
pub fn detect_boot_banner(text: &str) -> bool; pub fn is_reboot_command(cmd: &str) -> bool;
impl QuietState { pub fn on_rx_text(&mut self, chunk: &str, profile: &SessionProfile, now: Mono) -> QuietTransition; }
// op_runner.rs／lease.rs（06 T14）
pub enum OpSpec { Command(QueuedCommand), Recover{..}, SelfTestProbe { timeout_s: f64, probe: String, nonce: String },
                  InteractiveOpenCmd { command: String, owner: String }, InteractiveSend { payload: Vec<u8>, owner: String }, RebootRecovery{..}, DetectTemplate{..} }
pub fn encode_interactive_payload(data: &str, encoding: &str) -> Result<Vec<u8>, PyErr>;
pub struct InteractiveLease { pub interactive_id: String, pub owner: String, pub created_at: String, pub timeout_s: f64, pub last_activity: Mono,
                              pub status: &'static str, pub recovery_mode: bool }
impl InteractiveLease { pub fn touch(&mut self, now: Mono); pub fn expired(&self, now: Mono) -> bool; }
pub struct Tombstones { /* VecDeque<(String id, &'static str status)> cap 16 */ }
// 測試 harness（06）
// crates/sw-core/tests/support/mod.rs：CoreHarness { new, to_ready, to_attached, rpc, rpc_deferred, feed, rx, advance, public, force_state }、
//   ActorRig { new, attach, ready, request, request_async, get_state, wait_state, stderr_lines, dev: FakeDevice, clock }、agent_submit、SHELL_PROFILE
// crates/sw-daemon/tests/support/mod.rs：ServiceRig { ready_shell, with_allowlist, call, call_err, side_effect_fingerprint, version }、deferred_methods
```

```python
# Python 黑箱（P3 04 T2、P2 03、P5 06 T15／T20）——stdlib only，不 import sw_core
# tests/contract_blackbox/bb_harness.py：is_rust()、isolated_env(tmp, **overrides)、run_cli(...)、cli_prefix()、daemon_prefix()
# tests/contract_blackbox/conftest.py：markers rust_only、posix_only、windows_only、cap_posix_pty
# tests/contract_blackbox/bb_faketarget.py（P3 04 T2 建立、06 T15a 擴充；P2 沒有 contract_blackbox/faketarget.py）：
#   FakeTarget(workdir, *, prompt, user, password, login=True, boot_banner=b"", noise_interval_s=None)；.start(by_id_dir, name)、.slave_path、.stop()、.written()、.emit(data)、.pause_responding(s)
# tests/contract_blackbox/slice21/harness.py、conftest.py（06 T15／T20）：slice21_daemon_factory、shell_daemon（已 READY）、banner_target、Slice21Daemon.sw(...)
# tools/uart_stdio_bridge（P2）：POSIX PTY／Windows pySerial 端點、DUT 模擬器 local-process 模式、scenario run
# tests/acceptance/cases.yaml、cases.schema.json、cases.lock.json、regen_lock.py --check|--add-only（P2 03 T12）
# tools/contracts/catalog.py：load_catalogs(repo_root)（P0 01 T1）
```

### P6 對 P5 的增補清單

本 Phase 對 06 已交付型別與檔案的修改只限下表。表外的 06 介面不得更名或改變語意；06 既有測試除下表列明者外，一律應維持綠燈。

| # | 06 現況 | P6 差量 | 檔案 | Task |
|---|---|---|---|---|
| 1 | `Generations` 無 `input_epoch` | 加 `pub input_epoch: u64`（初值 0，D-02） | `generations.rs` | 1（欄位）、6（語意） |
| 2 | `CoreDeps` | 加 `pub input_epoch_pub: Arc<AtomicU64>`（hub 讀取；只在 `Fence::with` 內寫入） | `session_core.rs` | 1、6 |
| 3 | `TxPermit` | 加 `pub input_epoch: Option<u64>`、`pub replay_hold: Option<u64>`（D-03、D-22）；06 所有建構點兩者皆補 `None` | `tx_writer.rs`＋各建構點 | 1、6 |
| 4 | `FenceState`、`FenceReject` | `FenceState` 加 `human_admission_blocked`、`input_epoch`、`replay_auth: Option<u64>`（D-22）；`FenceReject` 加 `HumanAdmissionClosed`、`InputStale`、`ReplayRevoked` | `tx_writer.rs` | 1、6 |
| 5 | `Fence::admit` 對 HumanRaw／HumanReplay 比對 `owner_gen` | 依 D-03：HumanReplay 改比對 `input_epoch` 與專屬回放授權 `replay_hold == replay_auth`（不比對 `owner_gen`／admission；授權由 D-22 在新操作取得 ownership 時撤銷） | `tx_writer.rs` | 6 |
| 6 | `SubmitReq` 無 origin、無 `SubmitOrigin` | 加 `pub origin: SubmitOrigin` 與 enum；06 建構點補 `SubmitOrigin::Rpc`（`agent_submit` helper 同） | `arbiter.rs`、`tests/support/mod.rs` | 1、6 |
| 7 | `DataEvent` 只有 `UartRxChunk` | 加 `ConsoleInputBytes { client_id, client_lifetime, input_epoch, bytes }`、`ConsoleDisconnected { client_id, client_lifetime }`（不帶 `rx_seq`；`recv_next` 照常） | `mailbox.rs` | 1、6、9 |
| 8 | `ControlEvent` | 加 `ConsoleConnected { client_id, client_lifetime, label, vtty, kind }`、`ConsoleScan { scan: ConsoleScanSnapshot, source: ScanSource }`、`HumanTxCompleted { tag: HumanTxTag, report: TxReport }`、`ConsoleAttachReq { label: Option<String>, pty: Option<PtyPair>, reply: Reply }`（actor 層 envelope：`PtyPair` 不進 `SessionCore`） | `mailbox.rs`、`actor.rs` | 1、6、9、10 |
| 9 | `SessionRequest` | 加 `ConsoleAttach { label: Option<String>, pty: Option<PtyInfo> }`、`ConsoleDetach { client_id }`、`ConsoleList`；既有 `Interactive*` 不變 | `session_core.rs` | 1、7、10 |
| 10 | `Effect` | 加 `ConsoleOut { client_id, item: OutItem }`、`ConsoleClose { client_id }`、`ConsoleCloseAll`、`ConsoleAdopt { token, client_id, client_lifetime, label }`、`ConsoleDiscard { token }`、`HumanTx { tag: HumanTxTag, permit: TxPermit, payload: Vec<u8> }`、`LeaseIndex(LeaseIndexOp)`；日誌一律用既有 `Warn` | `session_core.rs`、`actor.rs` | 1、6、7 |
| 11 | `begin_stop` 內 `owner_gen += 1  // BC-02` | 改呼叫 `invalidate_human_input(Recovery／Release)`；加 `ownership.reset_for_stop()`、retained human owner、Release 時 `ConsoleCloseAll`；`closed_consoles` 改取 `HumanConsoleState.consoles.len()` | `session_core.rs`（或 `drain.rs`） | 6 |
| 12 | `SelfTestProbe` 不 suspend human | 有 human lease 時在 `StartOp(SelfTestProbe)` 前 `human_suspend_begin`，`OpFinished(SelfTest)` 後 `human_suspend_end`（只 suspend／resume，不動 admission，Python `session_manager.py:4505-4580`） | `session_core.rs` | 6 |
| 13 | quiet arm／reboot 命中點 | 呼叫 `invalidate_human_input(BootBanner／QuietRearm／RebootCommand)`（P7 再細化 quiet 路徑） | `session_core.rs`、`quiet_gate.rs` | 6 |
| 14 | `lease.rs`：`InteractiveLease`、`Tombstones` | `InteractiveLease` 加 `suspended_human: bool`、`peer_lost_at: Option<Mono>`；新增 `LeaseState`、`PostClose`、`lease_context`；`status` 維持 `&'static str` | `lease.rs` | 7 |
| 15 | lease tick（`Tick`／`LeaseExpiry`） | 到期流程加 stash 還原、回放（D-16）與 tombstone 可重複讀（D-08）；**不**新增 `LeaseTick` 事件 | `session_core.rs` | 7 |
| 16 | `crates/sw-core/tests/lease_selftest.rs::tick_expires_lease_sc13` 期望 `INTERACTIVE_NOT_FOUND` | 改為 `INTERACTIVE_EXPIRED`（P1 SC-13） | 該測試檔 | 7 |
| 17 | `encode_interactive_payload`（`op_runner.rs`） | 直接重用，不另寫 | — | 7 |
| 18 | `open_attached_*` 救援判定（06 T14） | 抽出到新檔 `rescue.rs`（行為不變，供 P7 重用），並加上 stash human lease | `rescue.rs`、`session_core.rs` | 7 |
| 19 | `RxShared::tail(usize)` | 加 `tail_py(n: i128)`（Python `text[-n:]`：n>0 尾 n 字元、n==0 全窗、n<0 去掉開頭 \|n\| 字元） | `uart_bridge.rs` | 7 |
| 20 | `PublicCtx` 由 2.1 executor 固定填值 | 改由 `HumanConsoleState` 產生（D-20） | `session_core.rs` | 10 |
| 21 | `ReaderCtx.sentinel`（reader 自己 fan-out／丟棄哨兵輸入） | P6 一律傳 `None`；RX 改 `try_send` 到 hub；`tests/rx_reader.rs` 的 `sentinel_*` 兩案改寫為「`sentinel: None` 時 reader 對 hub 的 `try_send` 順序在 WAL 之後」 | `uart_bridge.rs`、`tests/rx_reader.rs` | 9 |
| 22 | `console_pty/posix.rs`（06 T2 已建） | Modify：加 `MuxHandle`、`mux_handle()`；不重建檔案 | `crates/sw-pal/src/console_pty/posix.rs` | 8 |
| 23 | `service.rs` 已有 `session.interactive_*` 四分支 | 新增 `session.console_*` 三分支；四個 interactive 分支改經 `LeaseIndex` 路由；selector 查無時依 Python 回 `SESSION_NOT_READY{selector}` | `crates/sw-daemon/src/service.rs`、`session_manager.rs` | 10 |
| 24 | 2.1 範圍測試把 console 方法當 deferred | `crates/sw-daemon/tests/*::deferred_methods_return_method_not_found_without_side_effects` 與 `tests/contract_blackbox/slice21/test_slice_scope.py`（Linux／Windows 兩案）改為只檢查 `slice` 晚於 `sw_core::SLICE` 的列；`sw_core::SLICE` 改 `"2.2"`；`support::deferred_methods` 加 `delivered: &str` 參數；`slice21_scope.yaml` 本身是 2.1 歷史紀錄，**不修改** | 上列檔案、`crates/sw-core/src/lib.rs` | 10 |
| 25 | `contracts/resource_limits.yaml` | 追加 D-18 各列 | 該檔 | 0 |
| 26 | 「前景忙」判定直接讀 `self.foreground.is_some()`（出列、`open_busy_foreground`、`interactive_send` 的 `SESSION_BUSY`、SessionPublic `foreground_busy`） | 一律改呼叫 `SessionCore::foreground_busy()`（`foreground.is_some()` 或存在未撤銷的 `human.replay_hold`，D-22(2)）；hold 最後一筆結算時觸發一次出列。06 既有測試期望不變（無 hold 時兩者相等） | `session_core.rs` | 6 |

### 下游通知（P7／P8 必須採用的名稱）

- P7（08）：P6 合併後 SC 集合為 `({SC-01..SC-29} ∪ {SC-31, SC-34, SC-35, SC-36}) − {SC-08}`（SC-30 保留給 P7；SC-34／SC-35 由 P1-OC 登記、SC-36 由 P4 Task 19 登記（OD-15 Windows 安裝的選用 com0com 元件，Rust-only）、SC-08 依 OD-14 #3 撤回）。P7 Task 0 的 `test_bc_real_sc_df_ranges` 因此應改為 `({f"SC-{i:02d}" for i in range(1, 32)} | {"SC-34", "SC-35", "SC-36"}) - {"SC-08"}`，不是 08 原文的 `range(1, 31)`；Task 0 會同步修改 08 該行。SC-13 tick 已由 06 實作，P7 的 reprobe tick 共用 06 的 `ControlEvent::Tick`。
- P8（09）：DeferredBuffer 失效一律呼叫 `SessionCore::invalidate_human_input(InvalidateReason::Flash, fx)`；不存在 `clear_all_and_bump_owner_gen()`、`suspend_interactive`／`resume_interactive`。前景操作用 `fg_begin`／`fg_finish`；只需 suspend human raw 的操作（file transfer，比照 self-test）用 `human_suspend_begin`／`human_suspend_end`。前景忙判定（push／pull 的 `SESSION_BUSY`、SC-14 probe 拒絕）一律用 `SessionCore::foreground_busy()`，回放保留（D-22）期間為 true；`human_suspend_end` 的回放同樣建立 hold。Task 0 會同步修改 09 的 L56、L1108。

---

## 執行順序

1. **前置階段**：Task 0、Task 1 **完整**執行 Step 1～5（契約登記、必要案例集合凍結、介面骨架、harness 簽章、`cargo_nonzero.sh`、前置符號檢查），全部 commit 後才進入 TDD 階段。Task 1 Step 2 的 `check_p6_prereqs.py` 必須 PASS，否則停止並回報上游缺陷。
2. **TDD 階段**：依 Task 2 → Task 13 的順序，只執行每個 Task 的「寫失敗測試」與「執行並確認 FAIL」。golden 產生器、測試 helper 與 harness 擴充屬測試步驟，在此階段交付並先自測：竄改一筆 golden 時，對應的 Rust golden 測試必須 FAIL（Task 2、Task 3 的工具自測）。每個 Expected 都要指定真正缺少的行為，也就是 `todo!("P6 Task N")` panic 或具體斷言失敗；不得以缺檔、缺工具或編譯失敗充當紅燈。
   - Rust：一律帶 `--features test-fakes`（06 測試檔以 `#![cfg(feature = "test-fakes")]` 開頭，沒帶 feature 會整檔被編掉、0 個測試也算通過），並經 `scripts/rust/cargo_nonzero.sh` 執行，它在實際執行的測試數為 0 時 exit 1。`cargo_nonzero.sh` 只是開發迴圈防呆；切片出口不以「執行數 >0」判定，而以 Task 13 `s22_gate_check.py` 對凍結 nodeid 清單逐案判定（R5-F3）。
   - Python 黑箱：雙邊案例先對 Python 基線（P1 已交付 BC-02／SC-13／SC-16）執行，**必須 PASS**，證明 oracle 成立；再對 Rust artifact 執行，必須 FAIL。Rust-only 案例（SC-28／SC-29／SC-31）以 `rust_only` marker 標記，Python 端為 skipped。
3. **實作階段**：依 Task 2 → Task 13 的順序執行「實作」「確認 PASS」「commit」。Task 6／7 依賴 Task 2～5 的型別；Task 9 依賴 Task 8；Task 10 依賴 Task 6／7／9；Task 11、12、13 為整合收斂。
4. 每個 Task 實作完成後，依 master plan 做雙審（審查 A：Claude Sonnet effort max；審查 B：Codex `gpt-6.1-sol` medium），審查者不得是該 Task 的實作者。
5. **Windows 執行步驟**（`scripts/rust/win-test.sh`、`win-runner.sh`、`win-blackbox.ps1`）需要寫入 `/mnt/c` 並呼叫 `cmd.exe`／`powershell.exe`。Codex 的 `-s workspace-write` 沙箱可能擋下這些操作，所以一律由 orchestrator 執行，並把 `target/win-test-report/<run_id>.json` 路徑交給實作者；Codex 實作者只改程式碼。

| Task | 標題 | 測試作者 | 實作者 |
|---|---|---|---|
| 0 | 契約登記、案例凍結與架構細化 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 1 | 常數、模組骨架與 harness 簽章 | agy-gemini-3.8-flash-high | haiku-xhigh |
| 2 | Telnet 協定層 | haiku-xhigh | codex-gpt-6-luna-max |
| 3 | Line editor、shlex 與 human line mode | haiku-xhigh | agy-gemini-3.8-flash-high |
| 4 | DeferredBuffer（BC-02） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 5 | 每 console 出向 ring（SC-15） | haiku-xhigh | agy-gemini-3.8-flash-high |
| 6 | Ownership 狀態機、human TX lane 與 Fence 整合 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 7 | Interactive lease 差量（tombstone、soft preempt、ATTACHED 救援、stash） | haiku-xhigh | codex-gpt-6-luna-max |
| 8 | PAL console backends（PTY／TCP／ConsoleMux／com0com） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 9 | ConsoleHub runtime 與 RX fan-out | haiku-xhigh | codex-gpt-6-luna-max |
| 10 | RPC 表面、LeaseIndex 路由與 SessionPublic 欄位 | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 11 | 黑箱契約與既有測試改指 Rust（Linux L1.5） | agy-gemini-3.8-flash-high | codex-gpt-6-luna-max |
| 12 | Windows 原生 console 與選配 com0com | haiku-xhigh | codex-gpt-6-luna-max |
| 13 | 案例證據對照、文件、全套 gate | agy-gemini-3.8-flash-high | haiku-xhigh |

Task 10 改由 Codex 實作：它涵蓋 `session_manager.rs` 的 LeaseIndex、RPC thread 的 PTY 預建與跨 thread 交接（`ConsoleAttachReq` envelope、`ConsoleAdopt`／`ConsoleDiscard` 的 fd 生命週期），屬 master plan §2 的高風險並行項。

---

## Task 0：契約登記、案例凍結與架構細化

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-s22-contracts.md`（`type: change`、`scope: contracts`：「登記 Slice 2.2 Rust-only 契約 SC-28、SC-29、SC-31，凍結 Slice 2.2 必要案例集合並細化 console 架構」）。本 Task 會改 `tests/**`（屬 `code_paths`），R-09 需要 fragment。
**BLOCKED-OWNER evidence:** 無。

**Files:**
- Modify: `contracts/behavior_changes.yaml`（`changes` 新增 SC-28、SC-29、SC-31，依 SC→編號升冪插入，遞增檔案 `revision`；既有列一律 add-only，不改 P1 已標 `IMPLEMENTED` 的 BC-02、SC-13、SC-15、SC-16 文字）
- Modify: `tests/test_contracts_catalog.py`（`test_bc_real_sc_df_ranges` 的 SC 期望改為 `({f"SC-{i:02d}" for i in range(1, 30)} | {"SC-31", "SC-34", "SC-35", "SC-36"}) - {"SC-08"}`，並加註「SC-30 保留給 P7（08 §0.5）；SC-34／SC-35 由 P1-OC（02b）登記；SC-36 由 P4 Task 19 登記（OD-15）；SC-08 依 OD-14 #3 撤回、編號不重用」；`test_bc_real_rust_only_python_null` 的 rust_only 名單加 SC-28、SC-29、SC-31）
- Modify: `contracts/resource_limits.yaml`（追加 D-18 九列；06 `test_resource_limits_contract_values` 只比對它列出的鍵，不受影響）
- Modify: `tests/acceptance/cases.yaml`、`tests/acceptance/cases.lock.json`（只能經 `regen_lock.py --add-only` 追加：三個新 SC 案例，以及 P0 `behavior_changes.yaml` 中 `phases` 含 P6、但 P2 清單尚未收錄的 case_ids，例如 BC-02 四案、`sc13-lease-expiry-tick`、`sc15-console-ring-drop-oldest`、`sc16-console-line-reject-log`）
- Create: `tests/acceptance/slice22_evidence_map.yaml`（鍵集合＝凍結的 `slice_2.2` 集合；值先填 `{status: PENDING}`，Task 13 補 evidence）
- Modify: `docs/rust-refactor/plans/rust-architecture.md`：§1.3 sw-core 檔案列加 `human_console/{mod.rs,ownership.rs,line_editor.rs,out_ring.rs,hub.rs,line_mode.rs,human_tx.rs}`、`rescue.rs`；§4.7 加 `ConsoleMux`、`MuxHandle`、`ConsoleIo::mux_handle`；§5.2 L682 的 `ConsoleInputBytes{client_lifetime, owner_gen_at_capture, bytes}` 改為 `ConsoleInputBytes{client_id, client_lifetime, input_epoch, bytes}`，並加 `ConsoleDisconnected` 列；§5.3 世代表加 `input_epoch` 列，`owner_gen` 列的隔離對象改為「HumanRaw permit、lease timer」；§5.4 `FenceState`／`TxPermit`／`FenceReject` 加 D-03 欄位（含 `replay_auth`、`replay_hold`、`ReplayRevoked`）與規則表的 HumanRaw／HumanReplay 列；§5.5 第 5 步「DeferredBuffer 整段清空，`owner_gen++`」改為「`invalidate_human_input`（`input_epoch++`）」；§5.10 加 D-18 各列；§5.11 DeferredBuffer `Entry` 的 `owner_gen` 改為 `input_epoch`，加 D-07 溢位語意、D-15 human TX lane 與 D-22 回放保留（§5.11 已預先寫入 D-22 不變式條目，Task 0 只核對）
- Modify: `docs/rust-refactor/plans/spec-reconciliation.md`：§1.1 SC 表加 SC-28、SC-29、SC-31；SES-16.1、UIO-12.5 的「加上下文標記（owner_gen）」改為「加上下文標記（Python `_owner_gen`＝Rust `input_epoch`，07 D-02）」；UIO-12.7 補「P6 scope 的 stable symlink 依 07 D-01」；新增 KEEP＋DOC 列 `CON-EVT-01`（D-14）、`CON-LINE-01`（D-21）、`CON-REPLAY-01`（D-22：resume／admission flush 在 `_foreground_operation` token 釋放前完成，下一操作看不到舊按鍵；Rust 以回放保留＋`replay_auth` 撤銷重現，`session_manager.py:3699-3703`、`:1104-1120`）；更新 §5 統計
- 核對（不修改）：`docs/rust-refactor/plans/08-slice23-login-events.md` 的 SC 範圍（`({…range(1, 32)} | {"SC-34", "SC-35", "SC-36"}) - {"SC-08"}`，SC-31 屬 P6）與 `09-slice24-transfer-flash.md` 的 P6 介面名（`invalidate_human_input(InvalidateReason::Flash, fx)`、`fg_begin`／`fg_finish`、`human_suspend_begin`／`end`、`DeferredEntry{client_lifetime, input_epoch, bytes}`；P8 SC 改號為 SC-32／SC-33）——跨計畫審查已預先寫入兩檔，本 Task 只以 `tests/test_contracts_slice22.py` 斷言確認
- Create: `tests/acceptance/breaker/slice_2.2.json`（凍結快照）、`tests/acceptance/breaker/slice_2.2.snapshots.ndjson`（空檔）：以 06 Task 0b `scripts/rust/slice_breaker.py`（不重建）凍結 `slice_2.2` 必要案例分母與觀測窗口起訖（規格 §12.2「分母與起訖時點於每個切片開始時凍結」，OD-02／OD-13）；Task 13 出口以 `slice_breaker.py --freeze tests/acceptance/breaker/slice_2.2.json --snapshots …` 判定，觸發即停止擴大部署並退守 Stage 1
- Modify: `tests/conftest.py`（pop 清單加 `SERIALWRAP_CONSOLE_BACKEND`）、`tests/contract_blackbox/bb_harness.py`（`isolated_env` 刪除清單加同名 env）
- Create: `tests/test_contracts_slice22.py`

**Interfaces:**
- Consumes：P0 `behavior_changes.yaml` 結構（01 T1 規則、T5 共通欄位）：頂層 `schema_version`、`revision`、`changes`、`pending_owner_confirmations`；每筆 `id`、`kind`、`title`、`status`、`approval{owner, date, basis, decision_ref}`、`owner_confirmation`、`applies_to{python, rust}`、`approved_at_revision`、`ab_participation`、`spec_refs`、`reconciliation_refs`、`scope`（`both`｜`rust_only`）、`phases`、`case_ids`、`compatibility_impact`、`approval_conditions`、`related_decisions`。`tools/contracts/catalog.py::load_catalogs`。P2 `cases.schema.json` 欄位（`case_id`、`contract`、`invariants`、`layer`、`platforms`、`capabilities`、`daemon_slice`、`prerequisites`、`necessity`、`issues`、`behavior_changes`、`expected`、`evidence`）與 `regen_lock.py`。
- Produces：`behavior_changes.yaml` 三筆（下表）；凍結後的 `cases.lock.json` `slice_2.2` 集合（之後 Task 只能 add-only）；`slice22_evidence_map.yaml` 骨架。

| id | title | case_ids | reconciliation_refs | related_decisions | spec_refs |
|---|---|---|---|---|---|
| SC-28 | Console backend 選擇 env `SERIALWRAP_CONSOLE_BACKEND`（auto／pty／tcp） | `[sc28-console-backend-env]` | `[SC-28]` | `[]` | `["§7.2", "§11.5"]` |
| SC-29 | OD-01 選配 com0com Serial console（`windows.serial_console`） | `[sc29-com0com-serial-console]` | `[SC-29]` | `[OD-01, OD-08]` | `["§7.3", "§7.4"]` |
| SC-31 | Console 失效上下文與資源上限的 Rust-only 細化（無 active epoch 丟棄輸入、lease 上下文失效不回放、Control 溢位關閉、Telnet console 上限暫停 accept） | `[sc31-console-stale-context-rust]` | `[SC-31, CON-EVT-01]` | `[OD-04]` | `["§5.3", "§11.3", "§5.4"]` |

三筆共通：`kind: SC`、`status: APPROVED`、`approval: {owner: hamanpaul, date: "<Task 0 執行日，加引號>", basis: reconciliation, decision_ref: "spec-reconciliation.md §1.1 <id>"}`、`owner_confirmation: not_required`、`applies_to: {python: null, rust: {stage: slice-2.2}}`、`scope: rust_only`、`phases: [P6]`、`approved_at_revision: <新的檔案 revision>`、`ab_participation: absolute_only`（P0 禁止 `contract_revision` 鍵）；`compatibility_impact` 與 `approval_conditions` 分別逐字寫入 D-11、D-12、D-13＋D-16＋D-05 Control 上限＋D-18 accept 上限的決議文字。

- [ ] **Step 1：寫失敗測試**

```python
# tests/test_contracts_slice22.py
"""P6 Slice 2.2 契約登記檢查：SC-28／SC-29／SC-31 依 P0 schema、案例凍結、架構與調和表細化、conftest 隔離。"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import yaml

from tools.contracts.catalog import load_catalogs

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEW_SC = {"SC-28": "sc28-console-backend-env", "SC-29": "sc29-com0com-serial-console", "SC-31": "sc31-console-stale-context-rust"}


def _changes() -> dict[str, dict]:
    doc = yaml.safe_load((ROOT / "contracts" / "behavior_changes.yaml").read_text(encoding="utf-8"))
    return {row["id"]: row for row in doc["changes"]}


def test_catalog_still_loads() -> None:
    load_catalogs(ROOT)                                     # P0 loader：任一結構錯誤即 CatalogError


def test_new_sc_rows_follow_p0_schema() -> None:
    rows = _changes()
    for sc, case_id in NEW_SC.items():
        r = rows[sc]
        assert r["kind"] == "SC" and r["scope"] == "rust_only" and r["phases"] == ["P6"], sc
        assert r["applies_to"]["python"] is None and r["applies_to"]["rust"] == {"stage": "slice-2.2"}, sc
        assert r["case_ids"] == [case_id], sc
        assert r["compatibility_impact"].strip() and r["approval_conditions"].strip(), sc
    assert "SERIALWRAP_CONSOLE_BACKEND" in rows["SC-28"]["title"]
    assert "windows.serial_console" in rows["SC-29"]["title"] and "OD-01" in rows["SC-29"]["related_decisions"]


def test_sc30_reserved_for_p7() -> None:
    ids = {i for i in _changes() if i.startswith("SC-")}
    # SC-34／SC-35 由 P1-OC 登記；SC-36 由 P4 Task 19 登記（OD-15）；SC-08 依 OD-14 #3 撤回（編號不重用）
    assert ids == ({f"SC-{i:02d}" for i in range(1, 30)} | {"SC-31", "SC-34", "SC-35", "SC-36"}) - {"SC-08"}


def test_p1_rows_not_rewritten() -> None:
    rows = _changes()
    for i in ("BC-02", "SC-13", "SC-15", "SC-16"):
        assert rows[i]["status"] in {"APPROVED", "IMPLEMENTED", "VERIFIED"}, i


def test_reconciliation_and_architecture_refined() -> None:
    recon = (ROOT / "docs/rust-refactor/plans/spec-reconciliation.md").read_text(encoding="utf-8")
    for token in ("| SC-28 |", "| SC-29 |", "| SC-31 |", "| CON-EVT-01 |", "| CON-LINE-01 |"):
        assert token in recon, token
    ses = next(l for l in recon.splitlines() if l.startswith("| SES-16.1 |"))
    assert "input_epoch" in ses
    arch = (ROOT / "docs/rust-refactor/plans/rust-architecture.md").read_text(encoding="utf-8")
    for token in ("input_epoch", "ConsoleMux", "MuxHandle", "human_admission_blocked", "InputStale", "HumanAdmissionClosed"):
        assert token in arch, token
    assert "owner_gen_at_capture" not in arch
    assert "回放時只送 `owner_gen`" not in arch
    p8 = (ROOT / "docs/rust-refactor/plans/09-slice24-transfer-flash.md").read_text(encoding="utf-8")
    assert "clear_all_and_bump_owner_gen" not in p8 and "invalidate_human_input" in p8


def test_resource_limits_slice22_rows() -> None:
    rows = {r["name"]: r for r in yaml.safe_load((ROOT / "contracts/resource_limits.yaml").read_text(encoding="utf-8"))["limits"]}
    expect = {"console_hub_rx_queue": (256, "reject"), "console_hub_ctrl_queue": (64, "reject"),
              "console_hub_pending_events": (64, "block_sender"), "console_out_ring_display_bytes": (1048576, "drop_oldest"),
              "console_control_max_bytes": (65536, "close_connection"), "console_line_buffer_bytes": (16385, "reject"),
              "human_tx_backlog_bytes": (262144, "clear_all"), "telnet_consoles_per_session": (32, "pause_accept"),
              "com0com_reader_channel": (64, "block_sender")}
    assert {k: (rows[k]["value"], rows[k]["overflow"]) for k in expect} == expect


def test_conftest_and_harness_isolate_console_backend_env() -> None:
    assert '"SERIALWRAP_CONSOLE_BACKEND"' in (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    assert '"SERIALWRAP_CONSOLE_BACKEND"' in (ROOT / "tests/contract_blackbox/bb_harness.py").read_text(encoding="utf-8")


def test_slice22_case_set_frozen_and_mapped() -> None:
    subprocess.run([sys.executable, str(ROOT / "tests/acceptance/regen_lock.py"), "--check"], check=True)
    lock = set(json.loads((ROOT / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))["sets"]["slice_2.2"])
    assert set(NEW_SC.values()) <= lock
    assert {"bc02-deferred-overflow-clear", "bc02-mailbox-stale-input-not-replayed", "sc13-lease-expiry-tick",
            "sc15-console-ring-drop-oldest", "sc16-console-line-reject-log", "f4-interactive-line-cmd-defined",
            "p0-blog-clean", "p1-wal-reset", "p1-wal-fullrun", "cowork-01-agent-bad-cmd", "ha-01-human-then-agent",
            "hk-02-agent-preempt-human", "win-telnet-raw-owner", "l15-multi-source-contention-console"} <= lock
    emap = yaml.safe_load((ROOT / "tests/acceptance/slice22_evidence_map.yaml").read_text(encoding="utf-8"))["cases"]
    assert set(emap) == lock, "evidence map 的分母必須等於凍結的 slice_2.2 集合"
```

- [ ] **Step 2：執行並確認 FAIL**

Run: `python3 -m pytest -q tests/test_contracts_slice22.py`
Expected: FAIL（`KeyError: 'SC-28'`、recon／arch token 缺漏、`FileNotFoundError: slice22_evidence_map.yaml`）；`test_catalog_still_loads` 與 `test_p1_rows_not_rewritten` PASS。

- [ ] **Step 3：實作**

1. `behavior_changes.yaml`：依上表與共通欄位新增三筆；遞增 `revision`；`python3 -c "from tools.contracts.catalog import load_catalogs; load_catalogs(__import__('pathlib').Path('.'))"` 必須無錯。同一 commit 修改 `tests/test_contracts_catalog.py` 的兩個斷言（見 Files）。
2. `cases.yaml`：先以 `load_catalogs` 列出 `phases` 含 `P6` 的全部 `case_ids`，凡 `cases.yaml` 尚未收錄者以 `daemon_slice: "2.2"`、`necessity: required`、`behavior_changes: [<該項 id>]` 追加（雙邊項 `platforms: [linux, windows]`）。再追加三個新 SC 案例（`case_id` 與 `behavior_changes.yaml` 相同；`layer: L1.5`；`platforms: [linux]`（SC-29 為 `[windows]`）；`capabilities`：SC-28 `[cap_posix_pty]`、SC-29 `[windows_com]`、SC-31 `[cap_posix_pty]`；`daemon_slice: "2.2"`；`prerequisites` 依 P2 features 表：SC-28 `[console_telnet]`、SC-29 `[console_telnet]`、SC-31 `[deferred_buffer, lease]`；`necessity: required`；`issues: []`；`behavior_changes: [SC-xx]`；`expected` 寫 D 決議的判定句；`evidence` 寫 Task 11／12 的報告檔名）。執行 `python3 tests/acceptance/regen_lock.py --add-only`，再 `--check`。**此刻的 `slice_2.2` 集合就是本 Phase 的出口分母**；之後只能 add-only，不得刪除、延後或改為非必要（§11.3）。PR 說明記錄 `cases.lock.json` 的 git blob sha1。
3. `slice22_evidence_map.yaml`：`{schema_version: 1, frozen_lock_sha1: <上一步 sha1>, cases: {<每個 slice_2.2 case_id>: {status: PENDING}}}`。
4. `resource_limits.yaml`：依 D-18 追加九列（`unit` 為 `chunks`／`events`／`bytes`／`consoles`；`source` 寫 `07 D-18`）。
5. `rust-architecture.md`、`spec-reconciliation.md`、08、09：依 Files 清單逐節修改。§5.11 的 `Entry` 改寫為：
   ```rust
   struct DeferredEntry { client_lifetime: u64, input_epoch: u64, bytes: Vec<u8> }   // 以 client_id 為 IndexMap key
   ```
   並把「回放時只送 owner_gen 等於目前上下文」改為「回放時只送 `input_epoch` 等於目前值、且 client 仍存活的項目；`owner_gen` 只供 HumanRaw permit 與 lease timer」。recon §1.1 新增三列：
   `| SC-28 | env SERIALWRAP_CONSOLE_BACKEND（auto／pty／tcp）選擇 console backend，只由 sw_pal::native() 讀取 | §7.2、§11.5 | Rust-only |`
   `| SC-29 | OD-01 選配 com0com Serial console：config.yaml windows.serial_console，兩端埠自動排除於裝置列舉 | §7.3、§7.4、OD-01 | Rust-only |`
   `| SC-31 | Console 失效上下文與資源上限：無 active epoch 時丟棄輸入；lease close／expire 未還原 stash 時不回放該 client；Control 超過 64 KiB 關閉 console；Telnet console 達 32 暫停 accept | §5.3、§11.3、§5.4 | Rust-only |`
6. `tests/conftest.py` 的高優先 pop tuple 與 `bb_harness.isolated_env` 的刪除清單加入 `"SERIALWRAP_CONSOLE_BACKEND"`。

- [ ] **Step 4：執行並確認 PASS**

Run: `python3 -m pytest -q tests/test_contracts_slice22.py tests/test_contracts_catalog.py tests/test_slice21_scope.py tests/test_conftest_isolation.py && python3 tests/acceptance/regen_lock.py --check && python3 -m policy_check --repo .`
Expected: PASS；policy_check 無 FAIL（R-09 有 fragment）。`git diff contracts/behavior_changes.yaml` 只有新增列與 `revision` 變更（審查 A／B 的必查項）。

- [ ] **Step 5：commit**

```bash
git add contracts/behavior_changes.yaml contracts/resource_limits.yaml tests/test_contracts_catalog.py tests/acceptance/ docs/rust-refactor/plans/ tests/conftest.py tests/contract_blackbox/bb_harness.py tests/test_contracts_slice22.py changelog.d/rust-s22-contracts.md
git commit -m "chore(contracts): 登記 Slice 2.2 Rust-only 契約並凍結案例集合" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

#### parity-amendments-2026-10-26 追加（Python 1:1 承接）

> 來源：`docs/rust-refactor/plans/parity-amendments-2026-10-26.md`（稽核 A／B 與 P6 審查）。依 OD-14 #1（外部可觀察行為與 Python 1:1，只可新增、不可修改），下列為本 Task 的**必要**測試列與完成條件：紅燈先行，新增 nodeid 於本 Task 紅燈 commit 以 P2 `regen_lock.py --l1-add-only` 登記到所列集合；斷言值以 Python 基線實測為準（不得自訂）。

**新增 Step（Python 基線 parity 測試入鎖）**：建立 `s22_gate:py_parity`（linux／windows，規則同 06 Task 0b #76），add-only 登記：

| # | 稽核來源 | 測試／要求 | 精確斷言或完成條件 |
|---|---|---|---|
| #105 | B:E-109 | `tests/test_holder_probe_namespace.py::TestUartBridgeHeldPathsNamespace` 5 筆 | `passed`（posix_only） |
| #106 | B:E-26 | `tests/test_stage0_bridge_drain.py::{test_drain_timeout_closes_preserved_consoles_that_cannot_be_returned,test_unsafe_stop_receipt_closes_preserved_consoles,test_concurrent_stop_delivers_preserved_consoles_once}` | `passed`；Rust 側為刻意架構差異（console 歸 ConsoleHub，arch §5.1、D-01），斷言改為「drain 失敗不關閉 hub 內 console、交還恰一次」的絕對契約 |
| #107 | B:E-87 | `tests/test_agent_defer_tx.py::test_console_input_supports_backspace_and_local_echo` | `passed`；Task 11 L1.5 func-test 另加 0x08／0x7F 輸入案例 |
| #108 | B:D-61、D-101 | `tests/test_refine_ownership.py` 中 Windows TCP `attach_console` 不開 lease 與 `test_existing_human_owner_window_is_deferred_before_suspend` 等未鎖 nodeid（逐名列入 PR） | `passed` |
| #183 | P6 審查 F4 | `tests/test_slice21_case_freeze.py:57` | **已實作追加修正**（p6）：revision 解析為 `(YYYY,MM,DD,N)` 整數 tuple 比較；凍結檔新增 `case_fingerprints`（每個必要案例完整 YAML 的 sha256）並逐案驗證，取代已失效的整檔位元組指紋（補強，非削弱） |
| #184 | P6 審查 F5 | `tests/test_acceptance_cases.py:44-54` | **已實作追加修正**（p6）：Stage 1A 群組改以 `case_id.startswith("bb-")` 或 `source_ref` 前綴判定，並斷言所有 `bb-*` 的 `implemented_by == "04-stage1a-rust-cli.md"` |
| #185 | P6 審查 F3 | T0 commit `ae33e28` 單獨紅燈 | 合併前把 `e5e2c56` 的 marker 修補與 `5516137` 的 MIRROR 九列 fixup 進 T0 commit；不重寫歷史時，PR 描述須明載「T0 單獨為紅燈、終態綠燈」 |

---

## Task 1：常數、模組骨架與 harness 簽章

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-s22-console-skeleton.md`（`type: feat`、`scope: rust-console`：「新增 Rust daemon human console 模組骨架、Slice 2.2 常數與測試 harness 簽章」）。
**BLOCKED-OWNER evidence:** 無。

**Files:**
- Create: `scripts/rust/check_p6_prereqs.py`（逐符號檢查「跨 Phase 介面」）、`scripts/rust/cargo_nonzero.sh`（包裝 `cargo test`，實際執行的測試數為 0 時 exit 1）
- Modify: `crates/sw-proto/src/constants.rs`
- Create: `crates/sw-core/src/human_console/{mod.rs,ownership.rs,line_editor.rs,out_ring.rs,hub.rs,line_mode.rs,human_tx.rs}`
- Create: `crates/sw-core/src/telnet_console.rs`、`crates/sw-core/src/deferred.rs`、`crates/sw-core/src/rescue.rs`
- Modify: `crates/sw-core/src/lib.rs`（`pub mod` 宣告）、`generations.rs`（`input_epoch`）、`tx_writer.rs`（`TxPermit.input_epoch`、`FenceState`、`FenceReject` 新欄位與變體；06 建構點補 `None`）、`arbiter.rs`（`SubmitOrigin`、`SubmitReq.origin`）、`mailbox.rs`、`session_core.rs`（新增事件、request、effect 變體與 `CoreDeps.input_epoch_pub`，處理分支先 `todo!`）、`lease.rs`（新欄位）
- Create: `crates/sw-pal/src/console_mux.rs`、`console_mux/{posix.rs,windows.rs}`、`console_tcp.rs`、`console_tcp/{posix.rs,windows.rs}`、`console_com0com.rs`、`console_com0com/{posix.rs,windows.rs}`、`fakes/console.rs`、`conformance/console_suite.rs`、`testutil.rs`（`#[cfg(feature = "test-fakes")]`）
- Modify: `crates/sw-core/tests/support/mod.rs`（P6 harness 簽章，本體 `todo!`）；Create: `crates/sw-core/tests/support/hub.rs`；Modify: `crates/sw-daemon/tests/support/mod.rs`
- Create: `crates/sw-proto/tests/constants_slice22.rs`

**Interfaces:**
- Consumes：「跨 Phase 介面」全部項目。
- Produces（`crates/sw-proto/src/constants.rs`，值即契約；每個常數定義前先 `grep -n "pub const <NAME>" crates/sw-proto/src/`，已由 P3／P5 定義者**不得重複定義**，只在測試中斷言其值）：

```rust
pub const CONSOLE_LINE_BUFFER_MAX: usize = 16385;              // SC-16
pub const CONSOLE_OUT_RING_BYTES: usize = 1_048_576;          // SC-15
pub const CONSOLE_CONTROL_MAX_BYTES: usize = 65_536;          // D-05
pub const CONSOLE_HUB_RX_QUEUE: usize = 256;                   // D-18：reader→hub 顯示佇列（chunk 數）
pub const CONSOLE_HUB_CTRL_QUEUE: usize = 64;                  // D-18：actor→hub 控制佇列
pub const CONSOLE_HUB_PENDING_EVENTS: usize = 64;              // D-17：hub→mailbox 暫存
pub const CONSOLE_HUB_LOOP_QUOTA: usize = 64;                  // D-17：每輪每 channel 最多處理筆數
pub const TELNET_CONSOLES_PER_SESSION: usize = 32;             // D-18
pub const HUMAN_TX_BACKLOG_MAX_BYTES: usize = 262_144;         // D-15
pub const INTERACTIVE_TOMBSTONE_MAX: usize = 16;               // arch §5.10（06 已定義時只斷言）
pub const DEFAULT_INTERACTIVE_TIMEOUT_S: f64 = 60.0;           // service.py:843
pub const DEFAULT_SCREEN_CHARS: i128 = 2048;                   // service.py:860
pub const HUMAN_CONSOLE_CMD_TIMEOUT_S: f64 = 30.0;             // service.py:313-322
pub const HUMAN_CONSOLE_CMD_PRIORITY: i128 = 100;              // 與 06 SubmitReq.priority: i128 同型別
pub const HUMAN_INTERACTIVE_COMMANDS: [&str; 18] = ["alsamixer","btop","htop","less","menuconfig","more","most","nano",
    "nmtui","screen","tig","tmux","top","vi","view","vim","vimdiff","watch"];
pub const SUDO_VALUE_OPTS: [&str; 8] = ["-u","-g","-h","-p","-C","-T","-r","-t"];
pub const INTERACTIVE_KEY_MAP: [(&str, &[u8]); 9] = [("enter", b"\n"), ("tab", b"\t"), ("escape", b"\x1b"),
    ("ctrl-c", b"\x03"), ("ctrl-d", b"\x04"), ("up", b"\x1b[A"), ("down", b"\x1b[B"), ("right", b"\x1b[C"), ("left", b"\x1b[D")];   // 06 op_runner 已有對應表時只斷言一致
pub const CONSOLE_PRIMARY_LABEL: &str = "primary";
pub const CONSOLE_CLIENT_ID_HEX: usize = 12;
pub mod telnet {                                               // 上游未定義；本 Phase 唯一定義處
    pub const IAC: u8 = 0xFF; pub const SE: u8 = 0xF0; pub const SB: u8 = 0xFA; pub const WILL: u8 = 0xFB;
    pub const WONT: u8 = 0xFC; pub const DO: u8 = 0xFD; pub const DONT: u8 = 0xFE;
    pub const OPT_BINARY: u8 = 0x00; pub const OPT_ECHO: u8 = 0x01; pub const OPT_SGA: u8 = 0x03;
    pub const TELNET_GREETING: [u8; 12] = [0xFF,0xFB,0x01, 0xFF,0xFB,0x03, 0xFF,0xFD,0x03, 0xFF,0xFB,0x00];
}
```

- Produces（測試 harness 簽章；本體在各 Task 的 Step 1 由測試作者補齊，Task 6／7／9／10 的 Files 都包含對應 support 檔）：

```rust
// crates/sw-core/tests/support/mod.rs（06 既有，P6 追加）
impl CoreHarness {
    pub fn attach_pty_console(&mut self, cid: &str, peer: bool) -> PyDict;   // SessionRequest::ConsoleAttach（harness 配 PtyInfo token；cid 由固定 rng 決定時以回傳值為準）＋ConsoleScan
    pub fn connect_stream_console(&mut self, cid: &str, kind: ConsoleTransportKind) -> u64;   // ControlEvent::ConsoleConnected；回 lifetime
    pub fn input(&mut self, cid: &str, bytes: &[u8]) -> Vec<Effect>;       // DataEvent::ConsoleInputBytes（目前 input_epoch 與 lifetime）
    pub fn input_with(&mut self, cid: &str, lifetime: u64, epoch: u64, bytes: &[u8]) -> Vec<Effect>;
    pub fn disconnect(&mut self, cid: &str) -> Vec<Effect>;                // DataEvent::ConsoleDisconnected
    pub fn scan(&mut self, held: Option<&[&str]>) -> Vec<Effect>;          // ControlEvent::ConsoleScan{source: Hub}
    pub fn set_auto_complete_human_tx(&mut self, on: bool);                // true（預設）：每筆 Effect::HumanTx 立即回 Completed(len)
    pub fn complete_human_tx(&mut self, outcome: TxOutcome, reject: Option<FenceReject>) -> Vec<Effect>;   // 對尚未結算的那筆
    pub fn human_tx(&self) -> Vec<(TxClass, String, Vec<u8>)>;             // 已發出的 HumanTx：(class, source, payload)
    pub fn last_human_permit(&self) -> Option<TxPermit>;                   // 最近一筆 HumanTx 的 permit（斷言 replay_hold／input_epoch）
    pub fn foreground_busy(&self) -> bool;                                 // SessionCore::foreground_busy()（D-22(2)）
    pub fn replay_hold(&self) -> Option<ReplayHold>;                       // HumanConsoleState.replay_hold
    pub fn fence_replay_auth(&self) -> Option<u64>;                        // FenceState.replay_auth
    pub fn deferred_len(&self, cid: &str) -> usize;                        // 私有 DeferredBuffer 的 len_for（test-fakes 讀取器）
    pub fn tx_order(&self) -> Vec<(TxClass, Vec<u8>)>;                     // 依 effect 發出順序：HumanTx 的 (class, payload) 與 StartOp 會送 UART 的 (class, 預定 bytes)
    pub fn tx_effect_count(&self) -> usize;                                // HumanTx＋StartOp 中會送 UART 的 spec
    pub fn warns(&self) -> Vec<String>;                                    // Effect::Warn
    pub fn lease_index_ops(&self) -> Vec<LeaseIndexOp>;
    pub fn effect_kinds(&self) -> Vec<&'static str>;                       // 依序的 Effect 變體名稱（驗證 LeaseIndex 在 Reply 前）
    pub fn wire(&self, d: &PyDict) -> String;                              // pyjson::dumps(RPC_WIRE)，不含尾端換行
    pub fn ids(&self) -> Vec<String>;                                      // 固定 seed rng 依序產生的 32-hex
    pub fn advance_s(&mut self, s: f64) -> Vec<Effect>;                    // = advance(Duration::from_secs_f64(s))（投遞 Tick 與到期 timer）
}
pub fn open_req(owner: &str, timeout_s: f64, command: &str, allow_attached: bool) -> SessionRequest;
pub fn status_req(id: &str, screen_chars: i128) -> SessionRequest;
pub fn send_req(id: &str, data: &str, encoding: &str) -> SessionRequest;
pub fn close_req(id: &str) -> SessionRequest;
pub struct TxRig { pub fence: Arc<Fence>, pub dev: FakeDevice, /* spawn_tx_writer＋FakeWalFs */ }
impl TxRig { pub fn new() -> TxRig;
    pub fn permit(&self, class: TxClass, owner_gen: u64, input_epoch: Option<u64>, source: &str) -> TxPermit;   // replay_hold 一律 None
    pub fn replay_permit(&self, owner_gen: u64, input_epoch: u64, hold: u64, source: &str) -> TxPermit;      // class HumanReplay、input_epoch Some、replay_hold Some(hold)（D-22）
    pub fn submit(&self, p: TxPermit, payload: &[u8]) -> crossbeam_channel::Receiver<TxReport>;
    pub fn wal_tx(&self) -> Vec<PyDict>; }
// crates/sw-core/tests/support/hub.rs（P6 新增）
pub struct HubHarness { pub hub: HubHandle, pub fake: FakeConsoleFactory, pub clock: Arc<FakeClock> /* control／data receiver */ }
impl HubHarness { pub fn tcp(com: &str) -> HubHarness; pub fn pty(com: &str) -> HubHarness;
    pub fn adopt_pty_with(&self, io: FakeIo) -> FakeConsoleRef; pub fn reader_push(&self, b: &[u8]) -> bool;   // 回 try_send 是否成功
    pub fn settle(&self);                                    // 等 hub 處理完目前所有 channel 內容（上限 2 s）
    pub fn control_events(&self) -> Vec<ControlEvent>; pub fn data_events(&self) -> Vec<DataEvent>;
    pub fn status(&self) -> &HubStatus; pub fn ring_display_bytes(&self, cid: &str) -> usize; pub fn block_mailbox(&self, on: bool); }
// crates/sw-pal/src/fakes/console.rs（test-fakes）
pub struct FakeConsoleFactory; pub struct FakeIo; pub struct FakeConsoleRef;
impl FakeIo { pub fn stalled() -> FakeIo; pub fn unbounded() -> FakeIo; pub fn accept_n(n: usize) -> FakeIo; pub fn failing_read() -> FakeIo; }
impl FakeConsoleRef { pub fn client_id(&self) -> String; pub fn written(&self) -> Vec<u8>; pub fn written_len(&self) -> usize; pub fn closed(&self) -> bool; pub fn push_input(&self, b: &[u8]); }
impl FakeConsoleFactory { pub fn connect(&self) -> FakeConsoleRef; pub fn fail_next_com0com(&self); pub fn set_held(&self, held: Option<HashSet<String>>); }
// crates/sw-pal/src/testutil.rs（test-fakes；只在 sw-pal 內含 cfg 分支）
pub fn tcgetattr_path(p: &str) -> PalResult<TermiosView>; pub fn master_is_nonblocking(p: &PtyPair) -> bool;
pub fn no_symlink_points_to(target: &str, dirs: &[&Path]) -> bool; pub fn is_dev_pts_path(p: &str) -> bool;   // ^/dev/pts/\d+$ 手寫判斷，不引入 regex-lite
pub fn wait_accept(l: &mut dyn ConsoleListener, timeout: Duration) -> (Box<dyn ConsoleIo>, String);
pub fn nodelay(io: &dyn ConsoleIo) -> bool; pub fn reuseaddr(l: &dyn ConsoleListener) -> bool;
// crates/sw-daemon/tests/support/mod.rs（06 既有，P6 追加）
impl ServiceRig { pub fn with_console_backend(kind: &str) -> ServiceRig; pub fn uart_writes(&self) -> Vec<Vec<u8>>;
                 pub fn console_ring_stats(&self, client_id: &str) -> (usize /* display_bytes */, u64 /* dropped_total */); }
pub fn deferred_methods(yaml: &str, delivered: &str) -> Vec<String>;      // 只回 slice 晚於 delivered 的列（增補清單 #24）
```

骨架中 Task 2～10 的公開函式先以 `todo!("P6 Task N")` 實作，型別與簽章照各 Task 的 Produces 段落逐字建立；06 既有函式**不得**改成 `todo!`。新增的 enum 變體在 06 既有 `match` 中以 `todo!("P6 Task N")` 分支補齊，使 workspace 可編譯。

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-proto/tests/constants_slice22.rs
use sw_proto::constants::*;

#[test]
fn slice22_constants_match_python_baseline() {
    assert_eq!(CONSOLE_LINE_BUFFER_MAX, 16385);
    assert_eq!(CONSOLE_OUT_RING_BYTES, 1 << 20);
    assert_eq!(CONSOLE_CONTROL_MAX_BYTES, 65536);
    assert_eq!(TELNET_CONSOLES_PER_SESSION, 32);
    assert_eq!(HUMAN_TX_BACKLOG_MAX_BYTES, DEFERRED_INPUT_MAX_BYTES);
    assert_eq!(DEFERRED_INPUT_MAX_BYTES, 262144);
    assert_eq!(INTERACTIVE_TOMBSTONE_MAX, 16);
    assert_eq!(DEFAULT_INTERACTIVE_TIMEOUT_S, 60.0);
    assert_eq!(DEFAULT_SCREEN_CHARS, 2048);
    assert_eq!(HUMAN_CONSOLE_CMD_TIMEOUT_S, 30.0);
    assert_eq!(HUMAN_CONSOLE_CMD_PRIORITY, 100i128);
    assert_eq!(HUMAN_PEER_GRACE_S, 3.0);
    assert_eq!(STALE_CONSOLE_GRACE_S, 2.0);
    assert_eq!(HUMAN_ACTIVE_WINDOW_S, 60.0);
    assert_eq!(MAX_RECOVERY_LEASE_S, 120.0);
    assert_eq!(BOOTLOADER_RX_TAIL_BYTES, 512);
    assert_eq!(ATTACHED_CONSOLE_LEASE_TIMEOUT_S, 86400.0);
    let mut sorted = HUMAN_INTERACTIVE_COMMANDS.to_vec(); sorted.sort();
    assert_eq!(sorted, HUMAN_INTERACTIVE_COMMANDS.to_vec(), "清單維持字母序以便對照 service.py:49-68");
    assert_eq!(telnet::TELNET_GREETING, [0xFF,0xFB,0x01,0xFF,0xFB,0x03,0xFF,0xFD,0x03,0xFF,0xFB,0x00]);
}
```

`scripts/rust/check_p6_prereqs.py`：對「跨 Phase 介面」區塊的每個符號，以 `(檔案, 正規式)` 表逐項檢查並列出缺漏。至少包含：`crates/sw-core/src/generations.rs` 的 `pub struct Generations`；`mailbox.rs` 的 `pub enum ControlEvent`、`\bTick\b`、`LeaseExpiry`；`session_core.rs` 的 `pub enum SessionRequest`、`InteractiveOpen \{`、`fn begin_stop`、`pub struct PublicCtx`；`tx_writer.rs` 的 `pub enum FenceReject`、`pub struct TxWriterHandle`；`arbiter.rs` 的 `pub struct SubmitReq`；`quiet_gate.rs` 的 `pub fn detect_boot_banner`、`pub fn is_reboot_command`；`op_runner.rs` 的 `pub fn encode_interactive_payload`、`SelfTestProbe`；`lease.rs` 的 `pub struct Tombstones`；`uart_bridge.rs` 的 `pub fn tail\(`、`sentinel:`；`crates/sw-pal/src/console_pty/posix.rs` 存在；`crates/sw-core/tests/support/mod.rs` 的 `pub struct CoreHarness`、`ActorRig`；`crates/sw-daemon/tests/support/mod.rs` 的 `pub struct ServiceRig`；`tests/contract_blackbox/bb_harness.py` 的 `def is_rust`、`def isolated_env`；`tests/contract_blackbox/faketarget.py` 的 `def emit`、`def written`；`tests/contract_blackbox/slice21/conftest.py` 的 `def shell_daemon`；`tests/acceptance/regen_lock.py`；`scripts/rust/goldens/_tsv.py`。缺任何一項就 exit 1。

- [ ] **Step 2：執行並確認 FAIL**

Run: `python3 scripts/rust/check_p6_prereqs.py && cargo test -p sw-proto --test constants_slice22`
Expected: `check_p6_prereqs.py` exit 0（否則停止並回報上游缺陷，不在 P6 補做）；`cargo test` 斷言失敗或 `cannot find value CONSOLE_CONTROL_MAX_BYTES`（本 Task 屬前置階段，常數測試允許以編譯失敗作紅燈）。

- [ ] **Step 3：實作**

加入上列常數；建立骨架檔與 `pub mod`；依「P6 對 P5 的增補清單」#1～#10、#14 加入欄位與變體（06 既有建構點補 `input_epoch: None`、`origin: SubmitOrigin::Rpc`）；harness 簽章以 `todo!` 本體加入 support 檔；`cargo_nonzero.sh` 以 `cargo test "$@" 2>&1 | tee` 後加總 `test result:` 行的 passed＋failed，總和為 0 時 exit 1。

- [ ] **Step 4：執行並確認 PASS**

Run: `scripts/rust/cargo_nonzero.sh -p sw-proto --test constants_slice22 && cargo check --workspace --all-targets --features test-fakes && cargo check --workspace --target x86_64-pc-windows-gnu && scripts/rust/cargo_nonzero.sh --workspace --features test-fakes && python3 scripts/rust/check_cfg_boundary.py`
Expected: PASS；兩個 target 皆可編譯；06 既有測試全部仍綠燈（骨架只加變體、不改既有行為）；cfg 邊界檢查 exit 0。

- [ ] **Step 5：commit**

```bash
git add crates/ scripts/rust/check_p6_prereqs.py scripts/rust/cargo_nonzero.sh changelog.d/rust-s22-console-skeleton.md
git commit -m "feat(rust-console): 新增 Slice 2.2 console 常數、模組骨架與 harness 簽章" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

#### parity-amendments-2026-10-26 追加（Python 1:1 承接）

> 來源：`docs/rust-refactor/plans/parity-amendments-2026-10-26.md`（稽核 A／B 與 P6 審查）。依 OD-14 #1（外部可觀察行為與 Python 1:1，只可新增、不可修改），下列為本 Task 的**必要**測試列與完成條件：紅燈先行，新增 nodeid 於本 Task 紅燈 commit 以 P2 `regen_lock.py --l1-add-only` 登記到所列集合；斷言值以 Python 基線實測為準（不得自訂）。

| # | 稽核來源 | 測試／要求 | 精確斷言或完成條件 |
|---|---|---|---|
| #179 | P6 審查 F1 | Step 4 gate 改寫 | 「06 既有測試全部仍綠燈」改為「與 P5 基線逐 test binary 結果相同」（以 `cargo test --workspace --features test-fakes --no-fail-fast` 於 p5 與 p6 各跑並比對，腳本化為 `scripts/rust/compare_test_baseline.py`）；計畫進度欄明列「上游 06 T8～T14 未交付，本 Phase Task 6～13 為 BLOCKED-UPSTREAM」 |
| #180 | P6 審查 F2 | `scripts/rust/check_p6_prereqs.py` | `--max-task` 輸出末行固定印「仍有 N 項後續缺漏」；新增 `--strict`（全表模式、非零 exit）；`s22_gate_check.py` 只以全表模式呼叫並斷言 exit 0；登記 T10／T11 為 BLOCKED-UPSTREAM |
| #186 | P6 審查偏離 (h) | `crates/sw-daemon/tests/support/mod.rs` | 補 `pub fn deferred_methods(yaml: &str, delivered: &[&str]) -> Vec<String>`（`todo!` 本體亦可），不依賴 `ServiceRig`（Low，T10 前任一 commit） |

---

## Task 2：Telnet 協定層

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s22-telnet-filter.md`（`type: feat`、`scope: rust-console`：「Rust daemon 新增與 Python 位元組相同的 Telnet greeting、入向 NVT filter 與出向 IAC 逸出」）。
**BLOCKED-OWNER evidence:** Tera Term／PuTTY／Windows `telnet.exe` 實際連線的互動驗證（需互動桌面，`cap_interactive_desktop`）。

**Files:**
- Modify: `crates/sw-core/src/telnet_console.rs`
- Create: `crates/sw-core/tests/telnet_filter.rs`
- Create: `scripts/rust/goldens/gen_telnet_filter.py`（stdlib＋`sw_core`；沿用 P3 `scripts/rust/goldens/_tsv.py` 四欄 TSV，固定 seed，只接受 `--out`）
- Create: `contracts/golden/telnet_filter.tsv`
- Create: `crates/sw-core/tests/common/golden.rs`（TSV 解析與 hex 工具，Task 3 共用；以 `include_str!` 嵌入 golden，Windows runner 不需 staging 檔案）
- Modify: `tests/test_rust_goldens_fresh.py`（P3 的 `GENERATORS` 追加 `("gen_telnet_filter.py", "telnet_filter.tsv")`）

**Interfaces:**
- Consumes：`sw_proto::constants::telnet::*`；P3 golden TSV 慣例（04 T3：用 hex 而非 JSON，避免 golden 讀取依賴受測的 `pyjson`）。本 Phase 的 bytes 欄位不經 UTF-8 解碼：`input` 欄為以 `,` 分隔的 chunk hex，`expected` 欄為以 `,` 分隔的 `<uart hex>/<reply hex>`（每個 chunk 一組）。
- Produces：

```rust
// crates/sw-core/src/telnet_console.rs
pub use sw_proto::constants::telnet::TELNET_GREETING;
#[derive(Debug, Default, PartialEq, Eq, Clone)]
pub struct TelnetFeed { pub uart: Vec<u8>, pub reply: Vec<u8> }
#[derive(Debug, Clone)]
pub struct TelnetFilter { /* st: St, local_on: [bool; 256] */ }
impl TelnetFilter {
    pub fn new() -> Self;                       // local_on = {ECHO, SGA, BINARY}
    pub fn feed(&mut self, data: &[u8]) -> TelnetFeed;
}
impl Default for TelnetFilter { fn default() -> Self { Self::new() } }
pub fn escape_iac(payload: &[u8]) -> std::borrow::Cow<'_, [u8]>;   // 無 0xFF → Cow::Borrowed（同一物件）
```

- [ ] **Step 1：寫失敗測試**

`crates/sw-core/tests/telnet_filter.rs`（逐案對照 `tests/test_telnet_filter.py`，34 個 Python item 全數承接）：

```rust
mod common;
use common::golden::{rows, unhex, XorShift};
use sw_core::telnet_console::{escape_iac, TelnetFeed, TelnetFilter, TELNET_GREETING};
const IAC: u8 = 0xFF; const WILL: u8 = 0xFB; const WONT: u8 = 0xFC; const DO: u8 = 0xFD;
const DONT: u8 = 0xFE; const SB: u8 = 0xFA; const SE: u8 = 0xF0;
const BIN: u8 = 0x00; const ECHO: u8 = 0x01; const SGA: u8 = 0x03; const TTYPE: u8 = 0x18; const NAWS: u8 = 0x1F;

fn f(uart: &[u8], reply: &[u8]) -> TelnetFeed { TelnetFeed { uart: uart.to_vec(), reply: reply.to_vec() } }

#[test] fn greeting_bytes() {
    assert_eq!(TELNET_GREETING, [IAC,WILL,ECHO, IAC,WILL,SGA, IAC,DO,SGA, IAC,WILL,BIN]);
}
#[test] fn cr_emitted_immediately_at_chunk_end() {
    let mut t = TelnetFilter::new();
    assert_eq!(t.feed(b"\r"), f(b"\r", b""));
    assert_eq!(t.feed(b"x"), f(b"x", b""));
}
#[test] fn dont_echo_gets_wont_once_only() {
    let mut t = TelnetFilter::new();
    assert_eq!(t.feed(&[IAC, DONT, ECHO]), f(b"", &[IAC, WONT, ECHO]));
    assert_eq!(t.feed(&[IAC, DONT, ECHO]), f(b"", b""));
    // 被 DONT 關閉後無法重開：DO ECHO 回 WONT（06 §8.4 注意事項）
    assert_eq!(t.feed(&[IAC, DO, ECHO]), f(b"", &[IAC, WONT, ECHO]));
}
#[test] fn no_ff_returns_same_object() {
    let p = b"hello world";
    assert!(matches!(escape_iac(p), std::borrow::Cow::Borrowed(x) if std::ptr::eq(x, &p[..])));
}
#[test] fn golden_matches_python_byte_for_byte() {
    let mut n = 0;
    for r in rows(include_str!("../../../contracts/golden/telnet_filter.tsv")) {   // op == "telnet_feed"
        let mut t = TelnetFilter::new();
        let chunks: Vec<&str> = if r.input.is_empty() { vec![] } else { r.input.split(',').collect() };
        let outs: Vec<&str> = if r.expected.is_empty() { vec![] } else { r.expected.split(',').collect() };
        assert_eq!(chunks.len(), outs.len(), "case {}", r.id);
        for (c, o) in chunks.iter().zip(&outs) {
            let (uart, reply) = o.split_once('/').unwrap();
            let got = t.feed(&unhex(c));
            assert_eq!((got.uart, got.reply), (unhex(uart), unhex(reply)), "case {}", r.id);
        }
        n += 1;
    }
    assert!(n >= 2000, "golden 案例數 {n}");
}
const PICK: [u8; 17] = [IAC, WILL, WONT, DO, DONT, SB, SE, 0x00, 0x0A, 0x0D, ECHO, SGA, TTYPE, NAWS, 0xF1, 0xF6, b'a'];   // 涵蓋所有狀態轉移
#[test] fn chunking_is_invisible() {
    // 同一串 bytes，任意切割後的 uart／reply 串接必須等於一次餵入的結果（SW_TEST_SEED 決定序列，預設 0x5EED）
    let seed: u64 = std::env::var("SW_TEST_SEED").ok().and_then(|s| s.parse().ok()).unwrap_or(0x5EED);
    let mut rng = XorShift(seed);
    for _ in 0..5000 {
        let data: Vec<u8> = (0..rng.below(64)).map(|_| PICK[rng.below(PICK.len() as u64) as usize]).collect();
        let whole = TelnetFilter::new().feed(&data);
        let mut t = TelnetFilter::new(); let mut acc = TelnetFeed::default(); let mut i = 0;
        while i < data.len() { let n = 1 + rng.below((data.len() - i) as u64) as usize;
            let part = t.feed(&data[i..i + n]); acc.uart.extend(part.uart); acc.reply.extend(part.reply); i += n; }
        assert_eq!(acc, whole, "data={data:02x?}");
    }
}
```

`crates/sw-core/tests/common/golden.rs`：`pub struct Row { pub id: String, pub op: String, pub input: String, pub expected: String }`（`input`／`expected` 保留 TSV 原字串，不做 hex 解碼）；`pub fn rows(tsv: &str) -> Vec<Row>`；`pub fn unhex(s: &str) -> Vec<u8>`；`pub fn unhex_utf8(s: &str) -> String`；`pub struct XorShift(pub u64)` 與 `fn below(&mut self, n: u64) -> u64`（xorshift64*，n==0 回 0）。工具自測 `golden_tamper_is_detected`（寫在 `telnet_filter.rs`）：把第一列的 expected 改掉一個 hex 字元後重跑比對邏輯，必須出現不相等。

其餘 Python 案例以下表逐一寫成 `#[test]`（`t = TelnetFilter::new()`；`→` 右側為 `t.feed(...)` 的 `(uart, reply)`）：

| Rust 測試名 | 輸入（依序 feed） | 期望 |
|---|---|---|
| `plain_ascii` | `b"ls -la"` | `(b"ls -la", b"")` |
| `high_bytes_passthrough` | `[0x80,0xFE,0x01]` | `([0x80,0xFE,0x01], b"")` |
| `iac_iac_unescapes_to_single_ff` | `b"a"+[IAC,IAC]+b"b"` | `(b"a\xffb", b"")` |
| `iac_iac_split_across_feeds` | `[IAC]`；`[IAC]` | `(b"",b"")`；`([0xFF],b"")` |
| `cr_nul_folds_to_cr` | `b"PING\r\x00"` | `(b"PING\r", b"")` |
| `cr_lf_folds_to_cr` | `b"PING\r\n"` | `(b"PING\r", b"")` |
| `cr_then_lf_across_boundary_swallows_lf` | `b"\r"`；`b"\n"` | `(b"\r",b"")`；`(b"",b"")` |
| `cr_then_nul_across_boundary_swallows_nul` | `b"\r"`；`[0x00]` | `(b"\r",b"")`；`(b"",b"")` |
| `consecutive_crs_all_emitted` | `b"\r\r"` | `(b"\r\r", b"")` |
| `bare_lf_passthrough` | `b"a\nb"` | `(b"a\nb", b"")` |
| `cr_then_iac_negotiation` | `b"\r"+[IAC,WILL,ECHO]` | `(b"\r", [IAC,DONT,ECHO])` |
| `cr_then_iac_iac_is_cr_ff`（06 §8.4 注意） | `b"\r"+[IAC,IAC]` | `(b"\r\xff", b"")` |
| `do_acks_of_greeting_get_no_reply_{echo,sga,binary}` | `[IAC,DO,opt]` | `(b"", b"")` |
| `do_unsupported_option_gets_wont` | `[IAC,DO,TTYPE]` | `(b"", [IAC,WONT,TTYPE])` |
| `will_sga_ack_gets_no_reply` | `[IAC,WILL,SGA]` | `(b"", b"")` |
| `will_binary_refused_to_keep_nvt_inbound` | `[IAC,WILL,BIN]` | `(b"", [IAC,DONT,BIN])` |
| `unsolicited_will_gets_dont_{naws,ttype,echo}` | `[IAC,WILL,opt]` | `(b"", [IAC,DONT,opt])` |
| `wont_gets_no_reply` | `[IAC,WONT,NAWS]` | `(b"", b"")` |
| `putty_opening_burst` | `[IAC,WILL,NAWS,IAC,WILL,TTYPE,IAC,DO,ECHO]` | `(b"", [IAC,DONT,NAWS,IAC,DONT,TTYPE])` |
| `negotiation_split_across_feeds` | `[IAC]`；`[WILL]`；`[NAWS]` | `(b"",b"")`；`(b"",b"")`；`(b"",[IAC,DONT,NAWS])` |
| `sb_swallowed_entirely` | `[IAC,SB,NAWS,0,0x50,0,0x18,IAC,SE]+b"after"` | `(b"after", b"")` |
| `sb_spanning_three_reads` | `[IAC,SB,NAWS]`；`[0,0x50,0,0x18]`；`[IAC,SE]+b"after"` | `(b"",b"")`；`(b"",b"")`；`(b"after",b"")` |
| `sb_with_escaped_iac_inside` | `[IAC,SB,TTYPE,IAC,IAC,0x01,IAC,SE]+b"x"` | `(b"x", b"")` |
| `nop_and_ayt_swallowed` | `[IAC,0xF1,IAC,0xF6]+b"ok"` | `(b"ok", b"")` |
| `ip_is_not_ctrl_c` | `[IAC,0xF4]` | `(b"", b"")`（IP 不轉 0x03） |
| `interleaved_data_negotiation_cr` | `b"a"+[IAC,DO,SGA]+b"b\r\x00c"` | `(b"ab\rc", b"")` |
| `escapes_ff` | `escape_iac([0xFF,0x01,0xFF])` | `[0xFF,0xFF,0x01,0xFF,0xFF]` |
| `dense_ff_buffer` | `escape_iac([0xFF;4])` | `[0xFF;8]` |
| `per_client_state_isolated` | 兩個 filter：A feed `[IAC]`、B feed `b"z"`、A feed `[IAC]` | B=`(b"z",b"")`；A 第二次=`([0xFF],b"")` |

`scripts/rust/goldens/gen_telnet_filter.py --out contracts/golden/telnet_filter.tsv`：內建 seed `20261015`、2000 案；以 `random.Random(seed)` 從上述 PICK 集合產生長度 0～96 的序列、隨機切成 1～8 個 chunk，以 `sw_core.telnet_console.TelnetFilter` 逐 chunk 餵入，每案輸出一列 `<case_id>\ttelnet_feed\t<chunk hex,…>\t<uart hex>/<reply hex>,…`（經 `_tsv.py` 寫出）。

`tests/test_rust_goldens_fresh.py::test_golden_is_fresh[gen_telnet_filter.py-telnet_filter.tsv]`（P3 既有的參數化測試）：於 tmp 目錄重新產生並與 checked-in 檔位元組比對（Python 行為若改變，golden 必須同步更新）。

- [ ] **Step 2：執行並確認 FAIL**

Run: `python3 scripts/rust/goldens/gen_telnet_filter.py --out contracts/golden/telnet_filter.tsv && python3 -m pytest -q tests/test_rust_goldens_fresh.py -k telnet && scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test telnet_filter`
Expected: pytest PASS（golden 與 Python 一致）；`golden_tamper_is_detected` PASS（工具自測）；其餘 cargo 測試 FAIL（`not yet implemented: P6 Task 2`）。

- [ ] **Step 3：實作**

逐字移植 `sw_core/telnet_console.py:63-139`（06 §8.4 狀態表）：

```rust
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum St { Data, DataCr, Iac, OptWill, OptWont, OptDo, OptDont, Sb, SbIac }

impl TelnetFilter {
    pub fn feed(&mut self, data: &[u8]) -> TelnetFeed {
        let mut out = TelnetFeed::default();
        for &b in data {
            match self.st {
                St::Data | St::DataCr => {
                    if self.st == St::DataCr { self.st = St::Data; if b == 0x00 || b == 0x0A { continue; } }
                    if b == IAC { self.st = St::Iac; }
                    else if b == 0x0D { out.uart.push(0x0D); self.st = St::DataCr; }   // CR 立即 emit
                    else { out.uart.push(b); }
                }
                St::Iac => self.st = match b {
                    IAC => { out.uart.push(0xFF); St::Data }
                    WILL => St::OptWill, WONT => St::OptWont, DO => St::OptDo, DONT => St::OptDont, SB => St::Sb,
                    _ => St::Data,                              // SE/NOP/DM/BRK/IP/AO/AYT/EC/EL/GA 一律吞
                },
                St::OptWill => { if b != OPT_SGA { out.reply.extend_from_slice(&[IAC, DONT, b]); } self.st = St::Data; }
                St::OptDo   => { if !self.local_on[b as usize] { out.reply.extend_from_slice(&[IAC, WONT, b]); } self.st = St::Data; }
                St::OptDont => { if self.local_on[b as usize] { self.local_on[b as usize] = false; out.reply.extend_from_slice(&[IAC, WONT, b]); } self.st = St::Data; }
                St::OptWont => self.st = St::Data,
                St::Sb      => { if b == IAC { self.st = St::SbIac; } }
                St::SbIac   => self.st = if b == SE { St::Data } else { St::Sb },
            }
        }
        out
    }
}
pub fn escape_iac(p: &[u8]) -> Cow<'_, [u8]> {
    if !p.contains(&0xFF) { return Cow::Borrowed(p); }
    let mut v = Vec::with_capacity(p.len() + p.iter().filter(|&&b| b == 0xFF).count());
    for &b in p { v.push(b); if b == 0xFF { v.push(0xFF); } }
    Cow::Owned(v)
}
```

出向不做 LF→CRLF 轉換（宣告 WILL BINARY，06 §8.4）。

- [ ] **Step 4：執行並確認 PASS**

Run: `scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test telnet_filter`；orchestrator 另執行 `scripts/rust/win-test.sh --package sw-core --features test-fakes --test telnet_filter`（golden 以 `include_str!` 嵌入，Windows 端不需 staging）
Expected: 全數 PASS（含 `golden_matches_python_byte_for_byte` ≥2000 案例、`chunking_is_invisible` 5000 次）；Windows 報告該 exe `passed` 等於 Linux 端的測試數。

- [ ] **Step 5：commit**

```bash
git add crates/sw-core/src/telnet_console.rs crates/sw-core/tests/telnet_filter.rs crates/sw-core/tests/common/ scripts/rust/goldens/gen_telnet_filter.py contracts/golden/telnet_filter.tsv tests/test_rust_goldens_fresh.py changelog.d/rust-s22-telnet-filter.md
git commit -m "feat(rust-console): 移植 Telnet greeting、NVT filter 與 IAC 逸出" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

#### parity-amendments-2026-10-26 追加（Python 1:1 承接）

> 來源：`docs/rust-refactor/plans/parity-amendments-2026-10-26.md`（稽核 A／B 與 P6 審查）。依 OD-14 #1（外部可觀察行為與 Python 1:1，只可新增、不可修改），下列為本 Task 的**必要**測試列與完成條件：紅燈先行，新增 nodeid 於本 Task 紅燈 commit 以 P2 `regen_lock.py --l1-add-only` 登記到所列集合；斷言值以 Python 基線實測為準（不得自訂）。

| # | 稽核來源 | 測試／要求 | 精確斷言或完成條件 |
|---|---|---|---|
| #181 | P6 審查 F8 | `crates/sw-core/tests/common/golden.rs`；`.gitattributes` | **已實作追加修正**（p6）：`XorShift` 建構時 `seed \| 1`（或 splitmix64 初始化），`SW_TEST_SEED=0` 不再空轉；`.gitattributes` 加 `contracts/golden/** text eol=lf` |
| #182 | P6 審查 F9 | `scripts/rust/goldens/_tsv.py` | **已實作追加修正**（p6）：`write_raw_tsv` 收進 `_tsv.py`（含唯一性檢查），`read_tsv` 支援 raw 與 hex(UTF-8) 兩種模式並於 docstring 註明 |

---

## Task 3：Line editor、shlex 與 human line mode

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-s22-line-editor.md`（`type: feat`、`scope: rust-console`：「Rust daemon 第二 console 起的 line-buffer 編輯、本地回顯、16385 bytes 行上限（SC-16）與 POSIX shlex 分詞」）。
**BLOCKED-OWNER evidence:** 無。

**Files:**
- Modify: `crates/sw-core/src/human_console/line_editor.rs`、`crates/sw-core/src/human_console/line_mode.rs`
- 沿用（不修改）：`crates/sw-proto/src/pycompat.rs` 的 `shlex_split`（06 Task 0c 已交付，含 `ShlexError`）。本 Task 只新增 ≥500 列的 `shlex_split.tsv` 回歸 golden；golden 與 06 實作不符時屬 06 缺陷，於本 Task 修正 06 版本（單一實作，不得另寫一份）。若 06 的 `is_reboot_command` 自帶等價分詞，改為共用 `shlex_split`
- Create: `crates/sw-core/tests/line_editor.rs`、`crates/sw-core/tests/human_line_mode.rs`、`crates/sw-proto/tests/shlex_golden.rs`
- Create: `scripts/rust/goldens/gen_line_editor.py`、`scripts/rust/goldens/gen_human_line_mode.py`、`scripts/rust/goldens/gen_shlex.py`
- Create: `contracts/golden/line_editor.tsv`、`contracts/golden/human_console_mode.tsv`、`contracts/golden/shlex_split.tsv`
- Modify: `tests/test_rust_goldens_fresh.py`（`GENERATORS` 追加三列）

**Interfaces:**
- Consumes：`sw_proto::pycompat::decode_utf8_replace`；Task 2 `crates/sw-core/tests/common/golden.rs`；`sw_proto::constants::{CONSOLE_LINE_BUFFER_MAX, HUMAN_INTERACTIVE_COMMANDS, SUDO_VALUE_OPTS}`；`sw_proto::pyre`（`^[A-Za-z_][A-Za-z0-9_]*=.*$`）。
- Produces：

```rust
// crates/sw-core/src/human_console/line_editor.rs
#[derive(Debug, Default, Clone)]
pub struct LineEditor { buf: Vec<u8>, discarding: bool }
#[derive(Debug, Default, PartialEq, Eq)]
pub struct LineOutput { pub lines: Vec<String>, pub echo: Vec<u8>, pub overflowed_lines: u32 }
impl LineEditor {
    pub fn new() -> Self;
    pub fn consume(&mut self, data: &[u8]) -> LineOutput;   // _consume_console_input（uart_io.py:721-750）＋SC-16
    pub fn buffered_len(&self) -> usize;
}
pub fn pop_last_utf8_char(buf: &mut Vec<u8>) -> bool;      // _pop_last_console_char（uart_io.py:712-719）

// crates/sw-core/src/human_console/line_mode.rs
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HumanLineMode { Line, Interactive }
impl HumanLineMode { pub fn as_str(self) -> &'static str; }   // "line" | "interactive"
pub fn human_console_mode(command: &str) -> HumanLineMode;     // _human_console_mode（service.py:89-142）

// crates/sw-proto/src/pycompat.rs（本 Phase 新增）
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ShlexError { NoClosingQuotation, NoEscapedCharacter }   // Python ValueError 訊息 "No closing quotation"／"No escaped character"
// 沿用 06 T0c：pub fn shlex_split(s: &str) -> Result<Vec<String>, ShlexError>; // shlex.split(s, posix=True)：comments=False、whitespace_split=True
```

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/line_editor.rs
mod common;
use common::golden::{rows, unhex, unhex_utf8};
use sw_core::human_console::line_editor::{LineEditor, LineOutput};

fn out(lines: &[&str], echo: &[u8], overflowed: u32) -> LineOutput {
    LineOutput { lines: lines.iter().map(|s| s.to_string()).collect(), echo: echo.to_vec(), overflowed_lines: overflowed }
}

#[test] fn backspace_deletes_one_utf8_char_and_echoes() {
    let mut e = LineEditor::new();
    assert_eq!(e.consume("ab中".as_bytes()), out(&[], "ab中".as_bytes(), 0));
    assert_eq!(e.consume(b"\x7f"), out(&[], b"\x08 \x08", 0));
    assert_eq!(e.consume(b"\r"), out(&["ab"], b"\r", 0));
}
#[test] fn crlf_within_one_chunk_is_one_line() {
    assert_eq!(LineEditor::new().consume(b"ls\r\n"), out(&["ls"], b"ls\r", 0));
}
#[test] fn cr_and_lf_in_separate_chunks_emit_extra_empty_line() {   // UIO-12.10-a KEEP
    let mut e = LineEditor::new();
    assert_eq!(e.consume(b"ls\r"), out(&["ls"], b"ls\r", 0));
    assert_eq!(e.consume(b"\n"), out(&[""], b"\r", 0));
}
#[test] fn exactly_16385_bytes_is_submitted_for_arbiter_to_reject() {
    let mut e = LineEditor::new();
    let mut data = vec![b'a'; 16385]; data.push(b'\r');
    let o = e.consume(&data);
    assert_eq!(o.lines.len(), 1); assert_eq!(o.lines[0].len(), 16385); assert_eq!(o.overflowed_lines, 0);
}
#[test] fn byte_16386_discards_whole_line_until_terminator() {
    let mut e = LineEditor::new();
    let mut data = vec![b'a'; 16386]; data.extend_from_slice(b"\x7f\x7fbb\r\n");
    let o = e.consume(&data);
    assert!(o.lines.is_empty()); assert_eq!(o.overflowed_lines, 1);
    assert_eq!(e.buffered_len(), 0);
    assert_eq!(e.consume(b"ok\r"), out(&["ok"], b"ok\r", 0));
}
#[test] fn golden_matches_python() {
    // contracts/golden/line_editor.tsv：op "line_consume"；input＝以 "," 分隔的 chunk hex；
    // expected＝每 chunk 一組 "<行數>:<line1 hex>+<line2 hex>…/<echo hex>"，以 "," 分隔（行為 UTF-8 hex，可含 U+FFFD）
    let mut n = 0;
    for r in rows(include_str!("../../../contracts/golden/line_editor.tsv")) {
        let mut e = LineEditor::new();
        let chunks: Vec<&str> = if r.input.is_empty() { vec![] } else { r.input.split(',').collect() };
        let outs: Vec<&str> = if r.expected.is_empty() { vec![] } else { r.expected.split(',').collect() };
        assert_eq!(chunks.len(), outs.len(), "case {}", r.id);
        for (c, o) in chunks.iter().zip(&outs) {
            let (lines_part, echo) = o.split_once('/').unwrap();
            let (count, joined) = lines_part.split_once(':').unwrap();
            let want: Vec<String> = if count == "0" { vec![] } else { joined.split('+').map(unhex_utf8).collect() };
            let got = e.consume(&unhex(c));
            assert_eq!(got.lines, want, "case {}", r.id);
            assert_eq!(got.echo, unhex(echo), "case {}", r.id);
        }
        n += 1;
    }
    assert!(n >= 1000, "golden 案例數 {n}");
}
```

line editor 其餘案例（同一 `LineEditor` 依序 `consume`）：

| 測試名 | 輸入 | 期望 `LineOutput` |
|---|---|---|
| `lfcr_folds` | `b"x\n\r"` | lines `["x"]`、echo `b"x\r"` |
| `cr_cr_gives_empty_second_line` | `b"x\r\r"` | lines `["x",""]`、echo `b"x\r\r"` |
| `backspace_on_empty_no_echo` | `b"\x08"` | lines `[]`、echo `b""` |
| `backspace_resets_terminator_fold` | `b"\r\x7f\n"` | lines `["",""]`、echo `b"\r\r"` |
| `control_bytes_buffered_not_echoed` | `b"a\x03\x1bb\r"` | lines `["a\x03\x1bb"]`、echo `b"ab\r"` |
| `tab_echoed` | `b"\t\r"` | lines `["\t"]`、echo `b"\t\r"` |
| `invalid_utf8_line_decoded_with_replace` | `[0xFF,0x0D]` | lines `["\u{FFFD}"]`、echo `[0xFF,0x0D]` |
| `pop_continuation_only_buffer` | `[0x80,0x80,0x7F,0x0D]` | lines `[""]`（Python 迴圈 `idx>0` 停在 0 → 整段刪除）、echo `[0x80,0x80,0x08,0x20,0x08,0x0D]` |
| `discarding_does_not_echo` | 16386×`b'a'`+`b"z\x7f\r"` | `overflowed_lines=1`；echo 等於前 16385 個 `a`（第 16386 個起丟棄且不回顯，P1 T12 規則 4）＋終止字元的回顯（是否為 `\r` 以 golden 為準） |

human line mode（對照 `tests/test_service_human_console.py` 與 service.py:89-142）：

| 測試名 | 輸入 | 期望 |
|---|---|---|
| `vim_is_interactive` | `"vim /etc/config"` | `Interactive` |
| `ls_is_line` | `"ls -la"` | `Line` |
| `sudo_vim_is_interactive` | `"sudo vim x"` | `Interactive` |
| `sudo_value_opt_skipped` | `"sudo -u root top"` | `Interactive` |
| `env_assignments_skipped` | `"FOO=1 BAR=2 less x"` | `Interactive` |
| `env_cmd_skipped` | `"env -i A=1 htop"` | `Interactive` |
| `exec_prefix_skipped` | `"exec watch date"` | `Interactive` |
| `double_dash_skipped` | `"-- top"` | `Interactive` |
| `basename_used` | `"/usr/bin/vi f"` | `Interactive` |
| `unclosed_quote_falls_back_to_split` | `"vim 'unterminated"` | `Interactive` |
| `empty_is_line` | `"   "` | `Line` |
| `golden_matches_python` | `contracts/golden/human_console_mode.tsv`：op `line_mode`、input 為命令 UTF-8 hex、expected 為 `line`／`interactive` 的 hex（≥300 列，含引號、跳脫、sudo 尾端缺值、引號未閉合） | 全部相等 |

`crates/sw-proto/tests/shlex_golden.rs`：`contracts/golden/shlex_split.tsv`（op `shlex_split`；input 為字串 UTF-8 hex；expected 為 `OK:` 加上以 `\x1f` 串接的 tokens，或 `ERR:No closing quotation`／`ERR:No escaped character` 的 hex；≥500 列，含單雙引號、反斜線跳脫、引號內反斜線、尾端反斜線、空字串、全空白、非 ASCII），逐列比對 `shlex_split` 結果；另以手寫案例斷言 `shlex_split("a 'b c' \"d\\\"e\"")==["a","b c","d\"e"]`、`shlex_split("x 'y")==Err(NoClosingQuotation)`、`shlex_split("x \\")==Err(NoEscapedCharacter)`。

- [ ] **Step 2：執行並確認 FAIL**

Run:
```bash
python3 scripts/rust/goldens/gen_line_editor.py --out contracts/golden/line_editor.tsv
python3 scripts/rust/goldens/gen_human_line_mode.py --out contracts/golden/human_console_mode.tsv
python3 scripts/rust/goldens/gen_shlex.py --out contracts/golden/shlex_split.tsv
python3 -m pytest -q tests/test_rust_goldens_fresh.py
scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test line_editor --test human_line_mode
scripts/rust/cargo_nonzero.sh -p sw-proto --test shlex_golden
```
Expected: pytest PASS；cargo FAIL（`not yet implemented: P6 Task 3`）。`gen_line_editor.py` 以 `object.__new__(UARTBridge)` 取得未初始化實例，對 `ConsoleClient(client_id="g", label="g", master_fd=-1, slave_fd=-1, slave_path="", attached_at=0.0)` 呼叫 P1 實作後的 `_consume_console_input`（內建 seed `20261015`、1000 個隨機案例），並**固定加入**溢位案例：恰 16385 bytes＋`\r`、16386 bytes＋`\r`、16386 bytes＋backspace＋可列印字元＋`\r\n`、溢位行跨 chunk 結束、溢位後下一行正常，讓 D-06 與 Python 的差異無處可藏。`gen_shlex.py` 以 `shlex.split(s, posix=True)` 產生，`ValueError` 記為 `ERR:<str(exc)>`。

- [ ] **Step 3：實作**

```rust
impl LineEditor {
    pub fn consume(&mut self, data: &[u8]) -> LineOutput {
        let mut o = LineOutput::default();
        let mut last_term: Option<u8> = None;               // 每次呼叫重設（UIO-12.10-a）
        for &b in data {
            match b {
                0x08 | 0x7F => {
                    last_term = None;
                    if !self.discarding && pop_last_utf8_char(&mut self.buf) { o.echo.extend_from_slice(b"\x08 \x08"); }
                }
                0x0A | 0x0D => {
                    if matches!(last_term, Some(t) if t != b) && self.buf.is_empty() && !self.discarding { last_term = None; continue; }
                    if self.discarding { self.discarding = false; o.overflowed_lines += 1; }
                    else { o.lines.push(decode_utf8_replace(&self.buf)); }
                    self.buf.clear(); o.echo.push(b'\r'); last_term = Some(b);
                }
                _ => {
                    last_term = None;
                    if !self.discarding {
                        if self.buf.len() >= CONSOLE_LINE_BUFFER_MAX { self.buf.clear(); self.discarding = true; }
                        else { self.buf.push(b); }
                    }
                    // P1 T12 規則 4：discarding 期間（含觸發溢位的那個 byte）不回顯；若 golden 顯示 Python 對觸發 byte 或終止字元另有回顯，以 golden 為準
                    if !self.discarding && (b == 0x09 || (0x20..=0x7E).contains(&b) || b >= 0x80) { o.echo.push(b); }
                }
            }
        }
        o
    }
}
pub fn pop_last_utf8_char(buf: &mut Vec<u8>) -> bool {
    if buf.is_empty() { return false; }
    let mut idx = buf.len() - 1;
    while idx > 0 && (buf[idx] & 0xC0) == 0x80 { idx -= 1; }
    buf.truncate(idx); true
}
```

`shlex_split`：逐字移植 CPython 3.12 `shlex.shlex`（`posix=True`、`whitespace_split=True`、`commenters=""`）的狀態機：`whitespace`＝`" \t\r\n"`、`quotes`＝`'"`、`escape`＝`\\`、`escapedquotes`＝`"`；EOF 時仍在引號內 → `NoClosingQuotation`，仍在跳脫 → `NoEscapedCharacter`。`human_console_mode`：`shlex_split(command)` 失敗時改用 `command.trim().split_whitespace()`；迴圈規則逐字照 service.py:89-142（跳過空 token、`--`、env 賦值；`sudo` 後吃選項，`SUDO_VALUE_OPTS` 吃兩格、其他 `-` 開頭吃一格、`--` 吃一格後停；`env` 後吃 `-` 開頭與賦值，`--` 吃一格後停；`command`／`builtin`／`exec` 吃一格；第一個實際命令的 basename（以 `/` 切最後一段，等於 POSIX `os.path.basename`）在 `HUMAN_INTERACTIVE_COMMANDS` → `Interactive`，否則 `Line`；token 用盡回 `Line`）。

- [ ] **Step 4：執行並確認 PASS**

Run: `scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test line_editor --test human_line_mode && scripts/rust/cargo_nonzero.sh -p sw-proto --test shlex_golden`
Expected: PASS。

- [ ] **Step 5：commit**

```bash
git add crates/sw-core/src/human_console/line_editor.rs crates/sw-core/src/human_console/line_mode.rs crates/sw-proto/src/pycompat.rs crates/sw-core/tests/line_editor.rs crates/sw-core/tests/human_line_mode.rs crates/sw-proto/tests/shlex_golden.rs scripts/rust/goldens/ contracts/golden/line_editor.tsv contracts/golden/human_console_mode.tsv contracts/golden/shlex_split.tsv tests/test_rust_goldens_fresh.py changelog.d/rust-s22-line-editor.md
git commit -m "feat(rust-console): 新增 line-buffer 編輯、本地回顯與 human line mode 判定" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

#### parity-amendments-2026-10-26 追加（Python 1:1 承接）

> 來源：`docs/rust-refactor/plans/parity-amendments-2026-10-26.md`（稽核 A／B 與 P6 審查）。依 OD-14 #1（外部可觀察行為與 Python 1:1，只可新增、不可修改），下列為本 Task 的**必要**測試列與完成條件：紅燈先行，新增 nodeid 於本 Task 紅燈 commit 以 P2 `regen_lock.py --l1-add-only` 登記到所列集合；斷言值以 Python 基線實測為準（不得自訂）。

| # | 稽核來源 | 測試／要求 | 精確斷言或完成條件 |
|---|---|---|---|
| #177 | P6 審查 F6（SC-16 WARNING 時機） | `overflow_warning_at_trigger_byte`、`overflow_without_terminator_still_warns`、`overflow_across_chunks_warns_once` | **已實作追加修正**（p6 `crates/sw-core/src/human_console/line_editor.rs:46-52`）：新增 `LineEditor::take_overflow_started(&mut self) -> bool`（discarding 由 false→true 時設旗標），hub／core 在第 16386 個 byte 到達當下寫 WARNING（與 `uart_io.py:1706-1711` 同時機）；`LineOutput` 三欄不變；golden 產生器加「未終止」「跨 chunk 終止」兩情境 |

---

## Task 4：DeferredBuffer（BC-02）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s22-deferred-bc02.md`（`type: feat`、`scope: rust-console`：「Rust daemon DeferredBuffer 依 BC-02 溢位時刪除整段、跨上下文以 input_epoch 失效」）。
**BLOCKED-OWNER evidence:** 實板 reboot 期間 human 鍵入不得回放（L2 F5／F9 與 realhw `p1-con-defer` 於 bench 執行；程序見 `docs/rust-refactor/procedures/cross-reboot-console.md`，Task 13）。

**Files:**
- Modify: `crates/sw-core/src/deferred.rs`
- Create: `crates/sw-core/tests/deferred_bc02.rs`

**Interfaces:**
- Consumes：`sw_proto::constants::DEFERRED_INPUT_MAX_BYTES`；`indexmap::IndexMap`。
- Produces（型別為 `pub`，供 L1 測試直接驗證；但它只作為 `SessionCore` 的私有欄位存在，其他 Phase 只能經 `invalidate_human_input` 影響它，D-02）：

```rust
// crates/sw-core/src/deferred.rs
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeferredEntry { pub client_lifetime: u64, pub input_epoch: u64, pub bytes: Vec<u8> }
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeferOutcome { Appended { len: usize }, StaleEpoch, Overflowed { dropped: usize } }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InvalidateReason { BootBanner, QuietRearm, RebootCommand, Recovery, Release, Flash }
impl InvalidateReason { pub fn as_str(self) -> &'static str; }  // boot_banner|quiet_rearm|reboot_command|recovery|release|flash
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Replay { pub client_id: String, pub client_lifetime: u64, pub source: String /* "human:<client_id>" */, pub bytes: Vec<u8> }
#[derive(Debug)]
pub struct DeferredBuffer { entries: IndexMap<String, DeferredEntry>, cap: usize }
impl DeferredBuffer {
    pub fn new(cap: usize) -> Self;
    pub fn append(&mut self, client_id: &str, client_lifetime: u64, input_epoch: u64, current_epoch: u64, bytes: &[u8]) -> DeferOutcome;
    pub fn invalidate_all(&mut self) -> usize;                    // 回傳丟棄 bytes 總數
    pub fn remove_client(&mut self, client_id: &str) -> usize;     // detach／drop／reap／lease_dropped
    pub fn take_replay(&mut self, current_epoch: u64, is_live: &dyn Fn(&str, u64) -> bool) -> Vec<Replay>;   // 依首次插入序；清空全部 entry
    pub fn take_replay_for(&mut self, client_id: &str, current_epoch: u64, is_live: &dyn Fn(&str, u64) -> bool) -> Option<Replay>;   // D-15 第 4 點
    // D-15(3)／D-22(4)：被撤銷或 NotSubmitted 的回放放回前端。items 依原送出順序、各帶擷取時的 input_epoch；
    // epoch 不等於 current_epoch 者丟棄；其餘依序排在所有既有 entry 之前，同 client 已有 entry 時 bytes 接在該 entry 前面並把 entry 移到該位置；
    // 合併後長度 > cap 依 D-07 刪除整個 entry。回傳丟棄的 bytes 總數。client 存活與 lifetime 由呼叫端先行判定。
    pub fn restore_front(&mut self, items: Vec<(Replay, u64)>, current_epoch: u64) -> usize;
    pub fn total_bytes(&self) -> usize;
    pub fn len_for(&self, client_id: &str) -> usize;
}
```

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/deferred_bc02.rs
use sw_core::deferred::*;
const CAP: usize = 262144;
fn live(_: &str, _: u64) -> bool { true }

#[test] fn overflow_deletes_whole_entry_then_new_bytes_kept() {   // BC-02，P1 T09 規則 1（對照改寫後的 test_deferred_buffer_capped）
    let mut d = DeferredBuffer::new(CAP);
    assert_eq!(d.append("c1", 1, 0, 0, &vec![b'a'; CAP]), DeferOutcome::Appended { len: CAP });
    assert_eq!(d.append("c1", 1, 0, 0, b"b"), DeferOutcome::Overflowed { dropped: CAP + 1 });
    assert_eq!(d.len_for("c1"), 0);
    assert_eq!(d.append("c1", 1, 0, 0, &[b'B'; 10]), DeferOutcome::Appended { len: 10 }, "溢位後的 bytes 開新 entry 並保留");
    assert_eq!(d.take_replay(0, &live), vec![Replay { client_id: "c1".into(), client_lifetime: 1, source: "human:c1".into(), bytes: vec![b'B'; 10] }]);
}
#[test] fn stale_epoch_bytes_never_enter_buffer() {
    let mut d = DeferredBuffer::new(CAP);
    assert_eq!(d.append("c1", 1, 3, 4, b"old"), DeferOutcome::StaleEpoch);
    assert_eq!(d.total_bytes(), 0);
}
#[test] fn invalidate_then_replay_sends_nothing_old() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("c1", 1, 0, 0, b"ZZ");
    assert_eq!(d.invalidate_all(), 2);
    assert!(d.take_replay(1, &live).is_empty());
}
#[test] fn replay_preserves_first_insert_order_and_source() {
    let mut d = DeferredBuffer::new(CAP);
    d.append("bb", 1, 0, 0, b"1"); d.append("aa", 2, 0, 0, b"2"); d.append("bb", 1, 0, 0, b"3");
    assert_eq!(d.take_replay(0, &live), vec![
        Replay { client_id: "bb".into(), client_lifetime: 1, source: "human:bb".into(), bytes: b"13".to_vec() },
        Replay { client_id: "aa".into(), client_lifetime: 2, source: "human:aa".into(), bytes: b"2".to_vec() },
    ]);
    assert_eq!(d.total_bytes(), 0);
}
```

其餘案例：

| 測試名 | 操作 | 期望 |
|---|---|---|
| `exact_cap_kept` | append `CAP` bytes | `Appended{len:CAP}`，`len_for==CAP` |
| `dead_client_not_replayed` | append c1；`take_replay(0, &\|c,_\| c!="c1")` | 回傳空、entry 已清 |
| `lifetime_mismatch_not_replayed` | append c1 lifetime 1；`is_live` 只認 lifetime 2 | 回傳空 |
| `entry_epoch_mismatch_not_replayed` | append c1 epoch 0；`take_replay(1, live)` | 回傳空 |
| `new_lifetime_replaces_old_entry` | append c1 lifetime 1 `b"x"`；append c1 lifetime 2 `b"y"` | replay 只含 `b"y"`、lifetime 2 |
| `new_epoch_replaces_old_entry` | append c1 epoch 0（current 0）；append c1 epoch 1（current 1） | replay(1) 只含後者（P1 T09 規則 1 的 owner_gen 取代） |
| `remove_client_keeps_others_order` | append a,b,c；`remove_client("b")` | replay 順序 a,c |
| `overflow_isolated_per_client` | c1 溢位、c2 append `b"x"` | replay 只含 c2 |
| `take_replay_for_only_that_client` | append a,b；`take_replay_for("b", 0, live)` | 回 b 的 Replay；a 仍在 |
| `empty_append_noop` | append `b""` | `Appended{len:0}`，replay 不含該 client（Python 只回放非空 buffer） |
| `restore_front_precedes_newer_bytes_and_clients`（D-22(4)） | append c3 `b"z"`、c1 `b"new"`（epoch 0）；`restore_front(vec![(c1 b"x", 0), (c2 b"y", 0)], 0)` | 回 0；`take_replay(0, live)` 恰為 `[c1 b"xnew", c2 b"y", c3 b"z"]`（被撤銷的舊按鍵排在同 client 新輸入之前，跨 client 保持原回放順序） |
| `restore_front_drops_stale_epoch` | `restore_front(vec![(c1 b"x", 0)], 1)` | 回 1；`total_bytes()==0` |
| `restore_front_overflow_deletes_entry` | append c1 `CAP` bytes；`restore_front(vec![(c1 b"x", 0)], 0)` | 回 `CAP + 1`；`len_for("c1")==0`（D-07） |

- [ ] **Step 2：執行並確認 FAIL**

Run: `scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test deferred_bc02`
Expected: FAIL（`not yet implemented: P6 Task 4`）。

- [ ] **Step 3：實作**

```rust
pub fn append(&mut self, cid: &str, life: u64, epoch: u64, cur: u64, bytes: &[u8]) -> DeferOutcome {
    if epoch != cur { return DeferOutcome::StaleEpoch; }
    let e = self.entries.entry(cid.to_string())
        .or_insert_with(|| DeferredEntry { client_lifetime: life, input_epoch: epoch, bytes: Vec::new() });
    if e.client_lifetime != life || e.input_epoch != epoch {        // 同 id 新生命週期或新上下文：舊內容作廢
        *e = DeferredEntry { client_lifetime: life, input_epoch: epoch, bytes: Vec::new() };
    }
    if e.bytes.len() + bytes.len() > self.cap {
        let dropped = e.bytes.len() + bytes.len();
        self.entries.shift_remove(cid);                            // P1 T09 規則 1：刪除整個 entry
        return DeferOutcome::Overflowed { dropped };
    }
    e.bytes.extend_from_slice(bytes);
    DeferOutcome::Appended { len: e.bytes.len() }
}
pub fn take_replay(&mut self, cur: u64, is_live: &dyn Fn(&str, u64) -> bool) -> Vec<Replay> {
    let drained: Vec<_> = self.entries.drain(..).collect();
    drained.into_iter()
        .filter(|(cid, e)| !e.bytes.is_empty() && e.input_epoch == cur && is_live(cid, e.client_lifetime))
        .map(|(cid, e)| Replay { source: format!("human:{cid}"), client_lifetime: e.client_lifetime, client_id: cid, bytes: e.bytes })
        .collect()
}
```

`remove_client` 與 `take_replay_for` 用 `IndexMap::shift_remove`，保持其餘順序。

- [ ] **Step 4：執行並確認 PASS**

Run: `scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test deferred_bc02`
Expected: PASS（14 個測試）。

- [ ] **Step 5：commit**

```bash
git add crates/sw-core/src/deferred.rs crates/sw-core/tests/deferred_bc02.rs changelog.d/rust-s22-deferred-bc02.md
git commit -m "feat(rust-console): 依 BC-02 實作 DeferredBuffer 整段刪除與 input_epoch 失效" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 5：每 console 出向 ring（SC-15）

**Test author model:** haiku-xhigh　**Implementer model:** agy-gemini-3.8-flash-high
**Changelog fragment:** `changelog.d/rust-s22-console-ring.md`（`type: feat`、`scope: rust-console`：「每個 console 1 MiB 出向 ring，慢速 console drop-oldest 不阻塞 UART／WAL」）。
**BLOCKED-OWNER evidence:** 無。

**Files:**
- Modify: `crates/sw-core/src/human_console/out_ring.rs`
- Create: `crates/sw-core/tests/out_ring.rs`

**Interfaces:**
- Consumes：`sw_proto::constants::CONSOLE_OUT_RING_BYTES`。
- Produces：

```rust
// crates/sw-core/src/human_console/out_ring.rs
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OutItem { Control(Vec<u8>), Display { bytes: Vec<u8>, iac_escaped: bool } }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PushOutcome { Queued, DroppedOldest { bytes: usize } }
#[derive(Debug)]
pub struct OutRing { /* q: VecDeque<OutItem>, front_off: usize, display_bytes: usize, cap: usize, dropped_total: u64 */ }
impl OutRing {
    pub fn new(cap: usize) -> Self;
    pub fn push(&mut self, item: OutItem) -> PushOutcome;
    pub fn front(&self) -> Option<&[u8]>;          // front 元素尚未寫出的部分
    pub fn consume(&mut self, n: usize);            // try_write 回報寫出 n bytes
    pub fn clear_display(&mut self) -> usize;       // D-05：無 peer 的 PTY；不清 Control、不清部分寫出的 front
    pub fn display_bytes(&self) -> usize;
    pub fn is_empty(&self) -> bool;
    pub fn dropped_total(&self) -> u64;
    pub fn back(&self) -> Option<&OutItem>;          // 最新一個元素（測試與診斷用）
    pub fn control_bytes(&self) -> usize;            // 尚未寫出的 Control bytes 總數（D-05 上限由 hub 判斷）
}
```

- [ ] **Step 1：寫失敗測試**

```rust
// crates/sw-core/tests/out_ring.rs
use sw_core::human_console::out_ring::*;
fn disp(b: &[u8]) -> OutItem { OutItem::Display { bytes: b.to_vec(), iac_escaped: false } }

#[test] fn drop_oldest_keeps_newest_within_cap() {
    let mut r = OutRing::new(10);
    r.push(disp(b"aaaa")); r.push(disp(b"bbbb"));
    assert_eq!(r.push(disp(b"cccc")), PushOutcome::DroppedOldest { bytes: 4 });
    assert_eq!(r.front(), Some(&b"bbbb"[..])); assert_eq!(r.display_bytes(), 8);
}
#[test] fn control_items_are_never_dropped() {
    let mut r = OutRing::new(4);
    r.push(OutItem::Control(vec![0xFF, 0xFB, 0x01]));
    r.push(disp(b"xxxx")); r.push(disp(b"yyyy"));
    assert_eq!(r.front(), Some(&[0xFF, 0xFB, 0x01][..]));
    r.consume(3); assert_eq!(r.front(), Some(&b"yyyy"[..]));
}
#[test] fn oversized_escaped_item_never_starts_inside_iac_pair() {
    let mut r = OutRing::new(4);
    // escaped 串：61 FF FF FF FF 62 → 取尾 4 bytes 起點落在 FF 對中間 → 後移 1
    r.push(OutItem::Display { bytes: vec![0x61, 0xFF, 0xFF, 0xFF, 0xFF, 0x62], iac_escaped: true });
    assert_eq!(r.front(), Some(&[0xFF, 0xFF, 0x62][..]));
}
#[test] fn partially_written_front_is_protected() {
    let mut r = OutRing::new(8);
    r.push(disp(b"abcdefgh")); r.consume(3);
    r.push(disp(b"ZZZZ"));
    assert_eq!(r.front(), Some(&b"defgh"[..]), "已部分寫出的元素不得被丟，避免輸出斷尾");
}
```

| 測試名 | 操作 | 期望 |
|---|---|---|
| `fifo_order_mixed` | push Control A、Display B、Control C | 依序 front A→B→C |
| `oversized_raw_item_keeps_tail` | cap 4，push `disp(b"abcdef")` | front `b"cdef"` |
| `clear_display_keeps_control` | push Display、Control、Display；`clear_display()` | 只剩 Control；回傳清除 bytes 數 |
| `dropped_total_accumulates` | cap 2，push 5 個 1-byte Display | `dropped_total()==3` |
| `one_mib_flood_bounded` | cap `CONSOLE_OUT_RING_BYTES`，push 3 MiB（每筆 8192，第 n 筆內容為 n 的位元組重複） | `display_bytes() <= 1 MiB`、`back()` 為最後一筆的 8192 bytes、`dropped_total() > 0` |
| `control_bytes_tracks_unwritten` | push Control 3 bytes、Control 5 bytes；`consume(4)` | `control_bytes()==4` |

- [ ] **Step 2：執行並確認 FAIL**

Run: `scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test out_ring`
Expected: FAIL（`not yet implemented: P6 Task 5`）。

- [ ] **Step 3：實作**

`push(Display)`：若 `bytes.len() > cap` 先截成尾端 `cap` bytes；`iac_escaped` 時計算起點前連續 `0xFF` 的個數 r（在原 bytes 中，自起點往前數），r 為奇數則起點再後移 1。推入後 `while display_bytes > cap`：從 index 0（若 `front_off > 0` 則從 index 1）往後找第一個 Display 元素移除並累加 dropped；回傳 `DroppedOldest{bytes}`（若有丟）。`consume(n)`：`front_off += n`，到達長度即 pop 並 `front_off = 0`；Display 的 `display_bytes` 依實際寫出量遞減。

- [ ] **Step 4：執行並確認 PASS**

Run: `scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test out_ring`
Expected: PASS。

- [ ] **Step 5：commit**

```bash
git add crates/sw-core/src/human_console/out_ring.rs crates/sw-core/tests/out_ring.rs changelog.d/rust-s22-console-ring.md
git commit -m "feat(rust-console): 新增每 console 1 MiB 出向 ring（drop-oldest）" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

#### parity-amendments-2026-10-26 追加（Python 1:1 承接）

> 來源：`docs/rust-refactor/plans/parity-amendments-2026-10-26.md`（稽核 A／B 與 P6 審查）。依 OD-14 #1（外部可觀察行為與 Python 1:1，只可新增、不可修改），下列為本 Task 的**必要**測試列與完成條件：紅燈先行，新增 nodeid 於本 Task 紅燈 commit 以 P2 `regen_lock.py --l1-add-only` 登記到所列集合；斷言值以 Python 基線實測為準（不得自訂）。

| # | 稽核來源 | 測試／要求 | 精確斷言或完成條件 |
|---|---|---|---|
| #178 | P6 審查 F7 | `partially_written_front_drops_newest_reported` | **已實作追加修正**（p6 `out_ring.rs:92-100`）：保護部分寫出的 front 時，若被丟的是剛推入的新項，回 `PushOutcome::DroppedNewest{bytes}`（新增變體）並在 WARNING 文字註明；斷言 `back()` 仍為舊項、回報為 `DroppedNewest{bytes:4}`。SC-15 語意（可丟、不阻塞）不變 |

---

## Task 6：Ownership 狀態機、human TX lane 與 Fence 整合

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s22-console-ownership.md`（`type: feat`、`scope: rust-console`：「第一 console raw ownership、suspend／resume 巢狀計數、admission gate、單筆在途的 human TX lane 與 OS 寫入前 input_epoch／ownership 重驗」）。
**BLOCKED-OWNER evidence:** 實板 human＋agent 共存（L2 `f5-raw-ownership-survives-agent-rounds`、`f5-deferred-bytes-flushed`；realhw `p0-console-raw`、`p1-con-fanout`、`p1-con-defer`）。

**Files:**
- Modify: `crates/sw-core/src/human_console/{ownership.rs,mod.rs,human_tx.rs}`
- Modify: `crates/sw-core/src/session_core.rs`（console 事件處理、`fg_begin`／`fg_finish`、`human_suspend_begin`／`end`、`invalidate_human_input`、`begin_stop` 差量（增補 #11）、self-test suspend（#12）、quiet arm／reboot 命中點（#13））
- Modify: `crates/sw-core/src/tx_writer.rs`（D-03 的 `Fence::admit` 規則）
- Modify: `crates/sw-core/src/mailbox.rs`（增補 #7、#8 的事件處理；`ConsoleDisconnected` 屬 data lane）
- Modify: `crates/sw-core/src/actor.rs`（executor 執行 `HumanTx`：`try_send` 給 pump；spawn／stop pump thread）
- Modify: `crates/sw-core/src/arbiter.rs`（`SubmitOrigin` 判定點）
- Modify: `crates/sw-core/tests/support/mod.rs`（Task 1 簽章的本體）
- Create: `crates/sw-core/tests/ownership.rs`、`crates/sw-core/tests/session_core_console.rs`、`crates/sw-core/tests/fence_human_raw.rs`、`crates/sw-core/tests/human_tx_lane.rs`、`crates/sw-core/tests/actor_liveness.rs`

**Interfaces:**
- Consumes：Task 3 `LineEditor`、`human_console_mode`；Task 4 `DeferredBuffer`；Task 5 `OutItem`；06 的 `SessionCore`、`CoreDeps`、`Effects`、`TxPermit`、`TxClass`、`Fence`、`TxJob`、`TxReport`、`TxWriterHandle`、`CommandRegistry`／`SubmitReq`、`begin_stop`、`OpSpec::SelfTestProbe`、`QuietTransition`、`is_reboot_command`、`EffectExecutor`。
- Produces：

```rust
// crates/sw-core/src/human_console/ownership.rs
#[derive(Debug, Default, Clone, PartialEq)]
pub struct OwnershipState {
    pub interactive_owner: Option<String>, pub admission_blocked: bool, pub pending_owner: Option<String>,
    pub agent_active: bool, pub suspended_owner: Option<String>, pub suspend_depth: u32,
    pub last_human_input_at: Option<Mono>, pub flash_mode: bool,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InputRoute { DropFlash, Raw { owner: String }, Defer, Line }
impl OwnershipState {
    pub fn set_interactive_owner(&mut self, owner: Option<&str>) -> bool;           // uart_io.py:1345-1358；回 true＝owner 實際改變
    pub fn try_grant_if_idle(&mut self, owner: &str) -> bool;                       // :1464-1481
    pub fn suspend(&mut self) -> bool;                                               // :1360-1376；回 true＝depth 0→1
    pub fn resume(&mut self, has_client: &dyn Fn(&str) -> bool) -> bool;            // true＝最外層（depth 1→0），呼叫端回放
    pub fn set_admission(&mut self, allowed: bool, has_client: &dyn Fn(&str) -> bool) -> bool; // true＝此刻回放
    pub fn promote_pending(&mut self, has_client: &dyn Fn(&str) -> bool);           // :1310-1329
    pub fn on_stream_accept(&mut self, client_id: &str);                             // TCP／com0com accept（:393-410）
    pub fn on_detach(&mut self, client_id: &str);                                    // detach_console（#78）
    pub fn on_drop(&mut self, client_id: &str);                                      // _drop_console_client（#136）
    pub fn on_prune(&mut self, client_id: &str);                                     // _prune_stale_consoles_locked：只清 owner
    pub fn reset_for_stop(&mut self);                                                // bridge.stop：清 owner/pending/suspend/depth/agent_active/admission；保留 flash_mode
    pub fn route_input(&mut self, client_id: &str, now: Mono) -> InputRoute;         // _handle_console_rx 步驟 2～5
}

// crates/sw-core/src/human_console/mod.rs
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConsoleTransportKind { Pty, TcpTelnet, Com0com }
#[derive(Debug, Clone)]
pub struct ConsoleRecord { pub client_id: String, pub client_lifetime: u64, pub label: String, pub vtty: String,
    pub kind: ConsoleTransportKind, pub internal: bool, pub attached_at: Mono, pub line: LineEditor }
#[derive(Debug)]
pub struct HumanConsoleState { pub consoles: IndexMap<String, ConsoleRecord>, pub primary_client_id: Option<String>,
    pub ownership: OwnershipState, deferred: DeferredBuffer /* 私有（D-02） */, pub lane: HumanLane,
    pub peer_present: HashMap<String, bool>, pub next_lifetime: u64, pub vtty_cache: Option<String> /* D-20 */,
    pub replay_hold: Option<ReplayHold> /* D-22 */, pub next_hold_id: u64, pub replay_after_revoked: bool /* D-22(4) */ }
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayHold { pub hold_id: u64, pub remaining: usize, pub revoked: bool }   // D-22：remaining＝本 hold 尚未結算的 HumanReplay 數
#[derive(Debug, Clone)] pub enum ScanSource { Rpc, Hub }
#[derive(Debug, Clone)] pub struct ConsoleScanSnapshot { pub held: Option<HashSet<String>>, pub taken_at: Mono }

// crates/sw-core/src/human_console/human_tx.rs（D-15）
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HumanTxTag { pub seq: u64, pub client_id: String, pub client_lifetime: u64, pub input_epoch: u64, pub class: TxClass, pub len: usize }
#[derive(Debug, Default)]
pub struct HumanLane { /* in_flight: Option<(HumanTxTag, Vec<u8>)>, backlog: VecDeque<BacklogItem>, backlog_bytes: usize, next_seq: u64 */ }
impl HumanLane {
    pub fn is_busy_for(&self, client_id: &str) -> bool;        // in_flight 或 backlog 中有該 client
    pub fn in_flight(&self) -> Option<&HumanTxTag>;
}
pub struct HumanTxPump;                                          // actor.rs：thread "serialwrap-human-tx-<COM>"
impl HumanTxPump { pub fn spawn(com: &str, tx: TxWriterHandle, control: crossbeam_channel::Sender<ControlEvent>) -> HumanTxPump;
                   pub fn try_submit(&self, tag: HumanTxTag, permit: TxPermit, payload: Vec<u8>) -> Result<(), PumpFull>;   // bounded(1)
                   pub fn stop(self); }

// crates/sw-core/src/session_core.rs（新增方法；事件、request、effect 變體見增補清單 #7～#10）
impl SessionCore {
    pub fn fg_begin(&mut self, source_is_agent: bool, fx: &mut Effects) -> FgAdmission;   // admission 關；agent 且有 human lease 時 suspend
    pub fn fg_finish(&mut self, adm: FgAdmission, fx: &mut Effects);                       // resume（若 suspend）→ admission 開；依序回放（建立 D-22 hold）
    pub fn human_suspend_begin(&mut self, fx: &mut Effects) -> bool;                       // 只 suspend（self-test、P8 file transfer）；先撤銷 replay_auth（D-22(4)）
    pub fn human_suspend_end(&mut self, suspended: bool, fx: &mut Effects);                // 回放時建立 D-22 hold
    pub fn invalidate_human_input(&mut self, reason: InvalidateReason, fx: &mut Effects); // BC-02 唯一入口（D-02）
    pub fn foreground_busy(&self) -> bool;                                                 // foreground.is_some() || replay_hold 存在且未 revoked（D-22(2)，增補 #26）
}
pub struct FgAdmission { pub suspended_human: bool }
```

- [ ] **Step 1：寫失敗測試**

`crates/sw-core/tests/ownership.rs`（對照 `tests/test_suspend_resume_reentrant.py`、`tests/test_refine_ownership.py`、`tests/test_interactive_raw.py` 的 bridge 層斷言；`Mono` 是 `u64`）：

```rust
use sw_core::human_console::ownership::*;
fn yes(_: &str) -> bool { true }

#[test] fn nested_double_suspend_resume_restores_owner() {
    let mut o = OwnershipState::default();
    o.set_interactive_owner(Some("human:c1"));
    o.suspend(); o.suspend();
    assert!(!o.resume(&yes)); assert_eq!(o.interactive_owner, None);
    assert!(o.resume(&yes)); assert_eq!(o.interactive_owner.as_deref(), Some("human:c1"));
    assert!(!o.agent_active); assert_eq!(o.suspend_depth, 0);
}
#[test] fn extra_unbalanced_resume_is_noop() {
    let mut o = OwnershipState::default();
    assert!(!o.resume(&yes)); assert_eq!(o.suspend_depth, 0);
}
#[test] fn human_owner_during_admission_block_goes_pending() {
    let mut o = OwnershipState::default();
    o.set_admission(false, &yes);
    o.set_interactive_owner(Some("human:c1"));
    assert_eq!(o.interactive_owner, None); assert_eq!(o.pending_owner.as_deref(), Some("human:c1"));
    assert!(o.set_admission(true, &yes)); assert_eq!(o.interactive_owner.as_deref(), Some("human:c1"));
}
#[test] fn detach_suspended_owner_abandons_suspend_but_drop_does_not() {
    let mut a = OwnershipState::default(); a.set_interactive_owner(Some("human:c1")); a.suspend();
    let mut b = a.clone();
    a.on_detach("c1"); assert_eq!((a.suspended_owner.clone(), a.agent_active, a.suspend_depth), (None, false, 0));   // #78
    b.on_drop("c1");   assert_eq!((b.suspended_owner.clone(), b.agent_active, b.suspend_depth), (None, true, 1));    // #136
}
#[test] fn route_raw_updates_last_human_input_only_on_raw() {
    let mut o = OwnershipState::default(); o.set_interactive_owner(Some("human:c1"));
    assert_eq!(o.route_input("c1", 5), InputRoute::Raw { owner: "human:c1".into() });
    assert_eq!(o.last_human_input_at, Some(5));
    assert_eq!(o.route_input("c2", 6), InputRoute::Line); assert_eq!(o.last_human_input_at, Some(5));
}
```

| 測試名 | 情境 | 期望 |
|---|---|---|
| `single_suspend_resume_restores_owner` | owner c1；suspend；resume | resume 回 true、owner c1 |
| `deferred_flushed_only_on_outermost_resume` | depth 2 → 第一次 resume | 回 false（不回放） |
| `deep_nesting_balanced` | suspend×5、resume×5 | 只有第 5 次回 true |
| `route_owner_with_admission_blocked_defers` | owner c1、admission 關 | `route_input("c1")==Defer` |
| `route_suspended_human_defers` | owner c1、suspend | `route_input("c1")==Defer`、`route_input("c2")==Line` |
| `route_flash_drops` | `flash_mode=true` | 任何 client `DropFlash` |
| `tcp_accept_first_gets_owner` | 空狀態 `on_stream_accept("t1")` | owner `human:t1` |
| `tcp_accept_during_admission_sets_pending_once` | admission 關；accept t1、t2 | pending `human:t1`（不被 t2 覆寫）、owner None |
| `tcp_accept_during_suspend_becomes_suspended_owner` | suspend（owner None）；accept t1 | `suspended_owner==human:t1`（#134），owner None |
| `tcp_accept_with_existing_owner_is_line` | owner c1；accept t2 | owner 仍 c1、`route_input("t2")==Line` |
| `try_grant_refuses_when_pending_or_flash` | pending 有值 → grant；flash → grant | 皆 false |
| `promote_pending_agent_owner_kept` | admission 關，`set_interactive_owner(Some("agent"))` | owner 直接為 `agent`（非 human 不受 gate） |
| `promote_pending_human_requires_client` | pending `human:gone`；`set_admission(true, &\|c\| c!="gone")` | owner None、pending None |
| `reset_for_stop_keeps_flash` | flash＋owner＋suspend；`reset_for_stop()` | 只剩 `flash_mode=true` |

`crates/sw-core/tests/session_core_console.rs`（06 `CoreHarness`＋FakeClock；預設 `set_auto_complete_human_tx(true)`；每個「拒絕／丟棄」列另斷言 `h.tx_effect_count()` 不變）：

| 測試名 | 事件序列 | 期望 Effects／狀態 |
|---|---|---|
| `raw_input_becomes_human_tx` | READY；TCP c1 connected；`input("c1", b"\x1b[A")` | 一個 `Effect::HumanTx`：`permit.class==HumanRaw`、`source=="human:c1"`、`cmd_id==None`、`log==true`、`permit.input_epoch==Some(現值)`、`permit.owner_gen==fence.owner_gen`、payload `b"\x1b[A"` |
| `stale_epoch_input_dropped_and_diagnosed` | `input_with(c1, life, 舊 epoch, b"x")` 兩次 | 無 HumanTx、無 ConsoleOut；`warns()` 恰一筆含 `client_id=c1`、`reason=stale_epoch`（D-19 限速） |
| `unknown_or_old_lifetime_dropped` | 不存在的 client 或舊 lifetime | 無效果；WARNING `reason=unknown_client` |
| `line_input_echo_then_submit_order` | c2 為非 owner；`input("c2", b"ls\r")` | 先 `ConsoleOut{c2, Display(b"ls\r")}`，後 registry 收到 `SubmitReq{command:"ls", source:"human:c2", origin:HumanConsole{client_id:"c2"}, mode:"line", timeout_s:30.0, priority:100, ..}` |
| `line_rejected_logs_warning_zero_tx` | arbiter 回 `SESSION_NOT_READY`（ATTACHED shell） | WARNING 含 `client_id=c2`、`error_code=SESSION_NOT_READY`；`tx_effect_count()` 不變 |
| `agent_command_suspends_and_resume_replays` | owner c1；`fg_begin(true)`；`input("c1", b"x")`；`fg_finish` | fg 期間無 HumanRaw；finish 後一筆 `HumanReplay`、source `human:c1`、payload `b"x"`、`permit.input_epoch==Some(現值)`、`h.last_human_permit().replay_hold==Some(id)` 且 `h.fence_replay_auth()==Some(id)`；owner 回 c1；此列以 `set_auto_complete_human_tx(false)` 執行：回放結算前 `h.foreground_busy()==true`，`complete_human_tx(Completed(1))` 後為 false 且 `h.fence_replay_auth()==None`（D-22(1)(3)） |
| `gate_closed_input_replayed_on_finish` | owner c1；`fg_begin(false)`（human source 不 suspend）；`input("c1", b"y")`；`fg_finish` | finish 時回放 `b"y"`（admission 開放點） |
| `admission_toggle_does_not_bump_owner_gen` | owner c1；記下 `owner_gen`；`fg_begin(false)`；`fg_finish` | `owner_gen` 不變（D-02）；回放 job 不會被 `OwnerMismatch` |
| `banner_invalidates_deferred_and_bumps_epoch` | owner c1；`fg_begin(true)`；`input("c1", b"Z")`；`invalidate_human_input(BootBanner)`；`fg_finish` | finish 無任何 HumanTx；`input_epoch` 與 `fence.input_epoch` 皆＋1、`input_epoch_pub` 同值；WARNING 含 `reason=boot_banner` |
| `drain_phase1_resets_console_state_keeps_consoles` | owner c1＋suspend；`begin_stop(Recover)` | consoles 保留、ownership 全清（保留 flash_mode）、deferred 清空、`input_epoch+1`、retained human owner 為 `human:c1` |
| `release_phase1_closes_all_consoles` | `begin_stop(Release)` | 一個 `ConsoleCloseAll`；consoles 清空；release 回應 `closed_consoles` 等於進入 Phase 1 時的 console 數 |
| `input_without_active_epoch_dropped_sc31` | RECOVERING（`fence.active_epoch==None`）中 `input("c1", b"R")` | 無 HumanTx、無 deferred；WARNING `reason=no_active_epoch`（SC-31(a)） |
| `owner_change_updates_fence_before_tx` | `set_interactive_owner` 改變後的下一筆 HumanTx | 發出時 `fence.owner_gen==permit.owner_gen==g+1`（core 在 `Fence::with` 內同步更新，無中介 effect） |
| `second_console_while_lease_gets_interactive_busy` | c1 human lease；c2 line `b"ls\r"` 執行期 | 命令紀錄 `SESSION_INTERACTIVE_BUSY`、無 TX（Python execute-time 行為，05 §5.1 第 8 步） |
| `flash_drops_input_diagnosed` | 有 deferred；`flash_mode=true`；輸入 | 新輸入丟棄＋WARNING `reason=flash`；`invalidate_human_input(Flash)` 清空既有 deferred（P8 的 FlashSyncMatched 呼叫點） |
| `self_test_probe_suspends_human_without_admission` | c1 human lease；`SelfTest` RPC → `StartOp(SelfTestProbe)`；probe 期間 `input("c1", b"k")`；`OpFinished(SelfTest)` | probe 期間無 HumanRaw、`fence.human_admission_blocked==false`；finish 後一筆 HumanReplay `b"k"`，位於 `SelfTestProbe` 寫出之後（增補 #12） |
| `last_line_before_disconnect_is_submitted` | c2 line；`input("c2", b"reboot\r")`；`disconnect("c2")`（同一 data lane 依序） | `reboot` 被提交（不因 disconnect 超車而被當 unknown client 丟棄，D-17(3)） |
| `queued_command_waits_for_replay_settle`（R2-F4，D-22(2)(3)；此列設 `set_auto_complete_human_tx(false)`） | owner c1 human lease；agent 命令 A `start_foreground_command("echo a")`；A 執行中 `input("c1", b"x")`；排隊 agent 命令 B `"echo b"`；注入 `OpFinished{A}`（觸發 `fg_finish`，回放 `x` 進 lane，不結算） | 此時 effects 中**沒有** B 的 `StartOp`、B 的 status 仍為 `accepted`、`foreground_busy()==true`；`complete_human_tx(Completed(1))` 回傳的 effects 才出現 B 的 `StartOp`（之前任何一批都沒有）。`h.tx_order()` 恰為 `[(Command, b"echo a\n"), (HumanReplay, b"x"), (Command, b"echo b\n")]`，`x` 不會落在 B 之中或之後 |

`crates/sw-core/tests/human_tx_lane.rs`（`set_auto_complete_human_tx(false)`，以 `complete_human_tx` 手動結算）：

| 測試名 | 情境 | 期望 |
|---|---|---|
| `single_job_in_flight` | owner c1；輸入 `b"a"`、`b"b"` | 只有一筆 HumanTx（`a`）；結算 `Completed(1)` 後才出現第二筆（`b`） |
| `rejected_then_new_input_keeps_order` | `a` 在途；`fg_begin(true)`；輸入 `b`；結算 `a` 為 `NotSubmitted`＋`HumanAdmissionClosed`；`fg_finish` | finish 後一筆 HumanReplay，payload `b"ab"`（不是 `"ba"`，D-15(2)） |
| `late_rejection_after_finish_is_replayed` | `a` 在途；`fg_begin(true)`；`fg_finish`；之後才結算 `a` 為 `NotSubmitted`＋`OwnerMismatch` | 立即出現 HumanReplay `b"a"`（D-15(4)，不會卡在 deferred） |
| `rejected_with_stale_epoch_dropped` | `a` 在途；`invalidate_human_input(QuietRearm)`；結算 `NotSubmitted`＋`InputStale` | 無重送、無 deferred；WARNING `reason=stale_epoch` |
| `human_raw_partial_not_resent` | `abc` 在途；結算 `Completed(1)` 並帶錯誤 | 無重送；WARNING 含 `outcome=partial`、`written=1`、`len=3` |
| `human_raw_unknown_not_resent` | 結算 `Unknown` | 無重送；WARNING 含 `outcome=unknown` |
| `backlog_overflow_clears_client` | `a` 在途、不結算；同 client 再輸入共 262145 bytes | 該 client backlog 清空、WARNING（human TX backlog 模板）；其他 client 不受影響 |
| `replay_items_are_serialized` | deferred c1 `"x"`、c2 `"y"`；回放點 | 依序兩筆 HumanReplay，第二筆在第一筆結算後才出現；兩筆 `permit.replay_hold` 相同（同一 hold，`remaining` 由 2 降到 0 後才釋放） |
| `ownership_change_mid_replay_redefers_unsent`（R2-F4，取代舊 fence 案 `human_replay_ignores_admission_and_owner_gen` 的語意） | c1 human lease（owner c1）；`fg_begin(true)` 期間 deferred 有 c1 `"x"`、c2 `"y"`（c2 entry 的建立方式同 `replay_items_are_serialized`）；`fg_finish` 回放（hold h：`x` 在途、`y` 在 backlog）；**回放未結算時**直接呼叫 `fg_begin(true)`（模擬繞過佇列取得 ownership 的新操作）；之後 `input("c1", b"n")`；結算在途 `x` 為 `TxOutcome::NotSubmitted`＋`Some(FenceReject::ReplayRevoked)`；`fg_finish` | `fg_begin` 返回時 `h.fence_replay_auth()==None`、`h.deferred_len("c2")==1`（`y` 已立即放回）；`fg_begin` 到第二次 `fg_finish` 之間 `h.human_tx()` 無任何新 HumanTx（`y` 不出 lane、`x` 不重送、`n` 進 deferred）；第二次 `fg_finish` 後依序出現 HumanReplay `human:c1 b"xn"`、`human:c2 b"y"`，兩者 `replay_hold==Some(h2)`、`h2!=h`；全程 TX 序列中沒有任何 `x`／`y` 夾在兩次 fg 之間 |
| `revoked_replay_completed_before_revoke_is_kept` | 同上但在途 `x` 以 `Completed(1)` 結算（表示它在撤銷前已通過 `FenceSubmitGate`） | 不重送 `x`、WARNING 無；`y` 仍 `restore_front` 回 deferred，第二次 `fg_finish` 只回放 `b"y"`（D-22(4) 已提交者照實記錄） |
| `replay_hold_released_on_stale_epoch` | hold h 中 `x` 在途；`invalidate_human_input(QuietRearm)`；結算 `NotSubmitted`＋`InputStale` | `x` 丟棄、WARNING `reason=stale_epoch`；`h.replay_hold()==None`、`h.fence_replay_auth()==None`、`h.foreground_busy()==false`（失效不會讓 hold 卡住前景） |
| `new_replay_point_waits_for_revoked_in_flight`（D-22(4)） | c1 human lease；hold h 回放 `x` 在途；`fg_begin(true)`（撤銷）；`input("c1", b"n")`；**在途 `x` 尚未結算時**就 `fg_finish` | `fg_finish` 回傳的 effects 無 HumanTx、`h.foreground_busy()==false`（revoked hold 不算前景忙）；接著結算 `x` 為 `NotSubmitted`＋`ReplayRevoked`，回傳的 effects 恰一筆 HumanReplay `human:c1 b"xn"`（新 hold、`x` 在 `n` 之前），不是 `b"n"` 後再 `b"x"` |

`crates/sw-core/tests/fence_human_raw.rs`（06 Task 1 的 FakeDevice＋`TxRig`）：

```rust
#![cfg(feature = "test-fakes")]
mod support;
use support::TxRig;
use sw_core::tx_writer::*;
use sw_pal::fakes::FakeWriteStep;

#[test]
fn human_raw_queued_behind_blocked_write_rejected_after_admission_close() {     // 失效先於 OS admission
    let r = TxRig::new();
    r.fence.with(|s| { s.owner_gen = 7; s.input_epoch = 3; s.human_admission_blocked = false; });
    r.dev.script_write(vec![FakeWriteStep::BlockForever]);                     // 第一筆 Command 卡在 OS write 內
    let cmd = r.submit(r.permit(TxClass::Command, 7, None, "agent"), b"ls\n");
    let raw = r.submit(r.permit(TxClass::HumanRaw, 7, Some(3), "human:c1"), b"k");   // 排在 TxWriter queue，尚未 admit
    r.fence.with(|s| s.human_admission_blocked = true);
    r.dev.release_block();
    assert!(matches!(cmd.recv().unwrap().result.outcome, TxOutcome::Completed(3)));
    let rep = raw.recv().unwrap();
    assert!(matches!(rep.result.outcome, TxOutcome::NotSubmitted));
    assert_eq!(rep.reject, Some(FenceReject::HumanAdmissionClosed));
    assert_eq!(r.dev.take_tx(), b"ls\n".to_vec(), "UART 不得收到 human byte");
    assert_eq!(r.wal_tx().len(), 1, "未提交的寫入不得產生 TX WAL");
}

#[test]
fn admission_close_after_os_submission_counts_as_written() {                  // 已標 in-flight 的照實記錄（arch §5.4）
    let r = TxRig::new();
    r.fence.with(|s| { s.owner_gen = 7; s.input_epoch = 3; });
    r.dev.script_write(vec![FakeWriteStep::BlockForever]);
    let raw = r.submit(r.permit(TxClass::HumanRaw, 7, Some(3), "human:c1"), b"k");
    r.dev.wait_blocked();                                                      // job 已經 FenceSubmitGate 提交 OS，於 gate 外等完成（不持鎖）
    r.fence.with(|s| s.human_admission_blocked = true);
    r.dev.release_block();
    assert!(matches!(raw.recv().unwrap().result.outcome, TxOutcome::Completed(1)));
    assert_eq!(r.wal_tx().len(), 1);
}

#[test]
fn human_raw_input_epoch_bump_after_authorize_before_os_submit_is_zero_tx() {  // R0-F1：出列檢查通過後、OS 提交前失效
    let r = TxRig::new();
    r.fence.with(|s| { s.owner_gen = 7; s.input_epoch = 3; });
    r.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let raw = r.submit(r.permit(TxClass::HumanRaw, 7, Some(3), "human:c1"), b"k");
    assert!(r.dev.wait_paused(std::time::Duration::from_secs(2)));
    r.fence.with(|s| { s.input_epoch = 4; s.owner_gen = 8; });               // reboot／quiet re-arm／換手
    r.dev.release_pause();
    let rep = raw.recv().unwrap();
    assert!(matches!(rep.result.outcome, TxOutcome::NotSubmitted));
    assert_eq!(rep.reject, Some(FenceReject::InputStale));
    assert_eq!(r.dev.os_submits(), 0);
    assert!(r.dev.take_tx().is_empty(), "失效世代的 human byte 不得到達 UART");
    assert!(r.wal_tx().is_empty());
}

#[test]
fn human_replay_paused_before_submit_then_ownership_change_is_zero_tx() {   // R2-F4：回放進 OS 前，下一個操作取得 ownership
    let r = TxRig::new();
    r.fence.with(|s| { s.owner_gen = 7; s.input_epoch = 3; s.replay_auth = Some(5); });
    r.dev.script_write(vec![FakeWriteStep::PauseBeforeSubmit]);
    let replay = r.submit(r.replay_permit(7, 3, 5, "human:c1"), b"old");
    assert!(r.dev.wait_paused(std::time::Duration::from_secs(2)));            // 已過出列快速檢查，停在 OS 提交前
    // 下一命令 fg_begin 的等價 fence 變更（D-22(4)）：同一次 Fence::with 撤銷回放授權、關 admission、suspend 使 owner_gen++；
    // input_epoch 刻意不變——只比對 input_epoch 擋不住這個競態
    r.fence.with(|s| { s.replay_auth = None; s.human_admission_blocked = true; s.owner_gen = 8; });
    let cmd = r.submit(r.permit(TxClass::Command, 8, None, "agent"), b"ls\n");
    r.dev.release_pause();
    let rep = replay.recv().unwrap();
    assert!(matches!(rep.result.outcome, TxOutcome::NotSubmitted));
    assert_eq!(rep.reject, Some(FenceReject::ReplayRevoked));
    assert!(matches!(cmd.recv().unwrap().result.outcome, TxOutcome::Completed(3)));
    assert_eq!(r.dev.take_tx(), b"ls\n".to_vec(), "舊按鍵不得寫進下一個命令");
    assert_eq!(r.wal_tx().len(), 1, "被撤銷的回放不得產生 TX WAL");
}

#[test]
fn human_replay_with_live_auth_ignores_admission_and_owner_gen() {        // Python parity：resume flush 早於 admission 開放，且不帶 raw token
    let r = TxRig::new();
    r.fence.with(|s| { s.owner_gen = 8; s.input_epoch = 3; s.human_admission_blocked = true; s.replay_auth = Some(5); });
    let rep = r.submit(r.replay_permit(7, 3, 5, "human:c1"), b"x").recv().unwrap();
    assert!(matches!(rep.result.outcome, TxOutcome::Completed(1)));
    assert_eq!(rep.reject, None);
    assert_eq!(r.dev.take_tx(), b"x".to_vec());
    assert_eq!(r.wal_tx().len(), 1);
}

#[test]
fn human_replay_without_matching_auth_rejected() {                         // D-03：hold id 不符、授權已撤銷、permit 未帶 hold 三案
    for (auth, hold) in [(Some(6u64), Some(5u64)), (None, Some(5)), (None, None)] {
        let r = TxRig::new();
        r.fence.with(|s| { s.owner_gen = 7; s.input_epoch = 3; s.replay_auth = auth; });
        let mut p = r.replay_permit(7, 3, 5, "human:c1");
        p.replay_hold = hold;
        let rep = r.submit(p, b"x").recv().unwrap();
        assert!(matches!(rep.result.outcome, TxOutcome::NotSubmitted), "auth={auth:?} hold={hold:?}");
        assert_eq!(rep.reject, Some(FenceReject::ReplayRevoked));
        assert!(r.dev.take_tx().is_empty());
        assert!(r.wal_tx().is_empty());
    }
}
```

另三案：`human_raw_owner_gen_mismatch`（permit 7、fence 8 → `NotSubmitted`＋`OwnerMismatch`）；`human_raw_and_replay_input_stale`（permit `Some(3)`、fence 4；replay 的 `replay_auth` 與 hold 相符 → 兩種 class 都 `InputStale`，`input_epoch` 先於 `replay_auth` 判定）；舊案 `human_replay_ignores_admission_and_owner_gen`（無條件忽略 ownership 的成功案例）**刪除**，由上方 `human_replay_paused_before_submit_then_ownership_change_is_zero_tx`、`human_replay_with_live_auth_ignores_admission_and_owner_gen`、`human_replay_without_matching_auth_rejected` 取代（R2-F4）；`invalidate_linearizes_with_queued_replay`（第一筆 Command 卡住、HumanReplay 排隊、`fence.with(|s| s.input_epoch += 1)`、放行 → replay `InputStale`、UART 只有 Command bytes）。`FakeDevice::wait_blocked` 屬 06 fakes 的追加方法，本 Task 一併實作。

`crates/sw-core/tests/actor_liveness.rs`（06 `ActorRig`，真 actor thread）：

| 測試名 | 情境 | 期望 |
|---|---|---|
| `blocked_uart_does_not_freeze_actor` | READY；PTY c1 human owner；`dev.script_write([BlockForever])`；對 c1 送 100 個按鍵 | 每次 `get_state` 在 50 ms 內回應；同時最多 1 筆 human job 在 TxWriter |
| `release_reaches_recovering_while_uart_blocked` | 同上後 `device.release` | 3.5 s 內 state 為 `RECOVERING`（drain stalled），actor 不 hang |

- [ ] **Step 2：執行並確認 FAIL**

Run: `scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test ownership --test session_core_console --test human_tx_lane --test fence_human_raw --test actor_liveness`
Expected: FAIL（`not yet implemented: P6 Task 6`；`fence_*` 斷言失敗，因為 06 的 `Fence::admit` 尚未檢查 admission、input_epoch 與 `replay_auth`；`human_replay_paused_before_submit_then_ownership_change_is_zero_tx` 在只比對 `input_epoch` 的實作下會寫出 `old`，必須先紅）。

- [ ] **Step 3：實作**

1. `OwnershipState`：逐字移植 06 §6.2～§6.6 與 uart_io.py:1310-1400、1464-1481、503-524（`on_drop`）、1259-1279（`on_detach`）、589-605（`on_prune`）。核心：

```rust
pub fn route_input(&mut self, cid: &str, now: Mono) -> InputRoute {
    if self.flash_mode { return InputRoute::DropFlash; }
    let me = format!("human:{cid}");
    if self.interactive_owner.as_deref() == Some(me.as_str()) {
        if self.admission_blocked { return InputRoute::Defer; }
        self.last_human_input_at = Some(now);
        return InputRoute::Raw { owner: me };
    }
    if self.agent_active && self.suspended_owner.as_deref() == Some(me.as_str()) { return InputRoute::Defer; }
    InputRoute::Line
}
pub fn on_stream_accept(&mut self, cid: &str) {
    let owner = format!("human:{cid}");
    if self.interactive_owner.is_none() && self.suspend_depth == 0 {
        if self.admission_blocked { if self.pending_owner.is_none() { self.pending_owner = Some(owner); } }
        else { self.interactive_owner = Some(owner); }
    } else if self.suspend_depth > 0 && self.suspended_owner.is_none() {
        self.suspended_owner = Some(owner);
    }
}
```

2. `DataEvent::ConsoleInputBytes` 處理順序：`input_epoch != gens.input_epoch` → 丟（D-19 `stale_epoch`）；client 不存在或 lifetime 不符 → 丟（`unknown_client`）；`fence.active_epoch` 為 None → 丟（SC-31(a)，`no_active_epoch`）；`lane.is_busy_for(cid)` → 排進 backlog 尾端（D-15(2)）；否則依 `route_input`：`DropFlash` → 丟（`flash`）；`Raw` → 若該 client deferred 非空，先 append 到 deferred 再走 D-15(4)，否則 `lane` 送出 HumanRaw（`permit = TxPermit{class: HumanRaw, owner_gen: gens.owner_gen, input_epoch: Some(gens.input_epoch), source: owner, cmd_id: None, log: true, ..}`、`Effect::HumanTx`）；`Defer` → `deferred.append(...)`，`Overflowed` 寫 D-07 WARNING；`Line` → `LineEditor::consume`，echo 非空時先發 `ConsoleOut{Display{bytes: echo, iac_escaped:false}}`（hub 對 Telnet client 再做 `escape_iac`），每行 `registry.admit(SubmitReq{session_id, command: line, source: format!("human:{cid}"), origin: SubmitOrigin::HumanConsole{client_id}, mode: human_console_mode(&line).as_str().into(), timeout_s: 30.0, priority: 100, expected_duration_s: None, completion_regex: None}, ..)`，被拒且 `error_code` 屬 D-06 集合時寫 WARNING；`overflowed_lines>0` 寫 D-06 第二條 WARNING。
3. `DataEvent::ConsoleDisconnected` 走 data lane：依序處理（同 client 先前的輸入已處理完），執行 `ownership.on_drop`、`deferred.remove_client`、移除 console，並清掉該 client 的 lane backlog。
4. Ownership 實際變更（owner 改變、suspend 0↔1、promote、grant、detach／drop 改到 owner）時，在同一次 `deps.fence.with` 內 `gens.owner_gen += 1` 並寫 `fence.owner_gen`；admission 變更只寫 `fence.human_admission_blocked`，**不**動 `owner_gen`（D-02）。因為 core 同步更新 fence，同一批 Effects 中後續的 HumanTx 必然帶新值。
5. `fg_begin(source_is_agent)`：先 `revoke_replay(fx)`（D-22(4)：同一次 `Fence::with` 內 `st.replay_auth = None`、hold 標 revoked，backlog 中屬該 hold 的回放 `restore_front`；無 hold 時 no-op），再 `ownership.set_admission(false)`（寫 fence）；若 `source_is_agent` 且目前 lease 為 human → `ownership.suspend()`，回 `FgAdmission{suspended_human:true}`。`fg_finish(adm)`：若 suspended → `ownership.resume()`，最外層時 `deferred.take_replay(gens.input_epoch, live)` 逐筆排進 lane（`TxClass::HumanReplay`、source `human:<cid>`、`cmd_id:None`、`log:true`、`input_epoch: Some(現值)`、`replay_hold: Some(hold_id)`；非空時依 D-22(1) 建立或併入 hold，並在同一次 `Fence::with` 設 `replay_auth`）；接著 `set_admission(true)`，回 true 時再 `take_replay` 一次（併入同一 hold）。順序對照 05 §5.1「finally 先 resume、再 `_finish_foreground_operation`」。`fg_finish` 清掉 `foreground` 後，若有 hold 則**不**出列，出列延到 hold 最後一筆結算（第 9 點）；無 hold 時照 06 立即出列。06 命令執行路徑在既有 begin／finish 點呼叫這兩個 hook（`source_is_agent = !matches!(req.origin, SubmitOrigin::HumanConsole{..})`，D-10）。`human_suspend_begin`／`end` 只做 suspend／resume＋回放（`begin` 先 `revoke_replay`，`end` 的回放同樣建立 hold），不動 admission；在 `StartOp(SelfTestProbe)` 前與 `OpFinished(SelfTest)` 後呼叫（有 human lease 時）。06 所有讀 `self.foreground.is_some()` 判斷前景忙的點改呼叫 `foreground_busy()`（增補 #26）。
6. `invalidate_human_input(reason)`：在**同一次** `deps.fence.with` 內 `gens.input_epoch += 1`、`st.input_epoch = gens.input_epoch`、`deps.input_epoch_pub.store(gens.input_epoch, Release)`；鎖外 `dropped = deferred.invalidate_all()`、丟棄 lane backlog 中舊 epoch 的項目；`dropped>0` 寫 D-02 WARNING。呼叫點：quiet arm 與 rearm（`QuietTransition::Armed` → `BootBanner`／`QuietRearm`）、`is_reboot_command` 命中點（`RebootCommand`）、`begin_stop`（kind→`Recovery` 或 `Release`）；`Flash` 由 P8 的 FlashSyncMatched 呼叫。
7. `begin_stop` 差量（增補 #11）：刪除 `self.gens.owner_gen += 1; // BC-02`，改呼叫 `invalidate_human_input`；`ownership.reset_for_stop()`（owner 變更依第 4 點遞增 `owner_gen`）；把目前 human lease owner 記入 `retained_human_owner`／`retained_human_timeout_s`（05 §3.1 第 1 步）；`kind==Release` 時發 `ConsoleCloseAll` 並清空 `consoles`／retained；其餘保留 consoles。Release 回應的 `closed_consoles` 取進入 Phase 1 時的 `consoles.len()`。
8. `tx_writer.rs` `Fence::admit`：依 D-03 順序加入 HumanRaw／HumanReplay 規則（HumanReplay：`input_epoch` → `replay_hold == replay_auth`；`FenceSubmitGate` 在 OS 提交臨界區內重跑，所以撤銷與提交互斥）。`begin_stop` 在設 `frozen` 的同一次 `Fence::with` 內一併清 `replay_auth`，並清除 hold。
9. Human TX lane（D-15）：`Effect::HumanTx` 由 executor 呼叫 `pump.try_submit`；core 保證同時只有一筆，所以 `Err(PumpFull)` 屬不變式違反，寫 WARNING，並以 `HumanTxCompleted{NotSubmitted}` 回送 core 重新路由（經 executor 已有的 control sender `try_send`；失敗時保留在 executor 的 1 格重試槽，下一次 `Tick` 再送）。pump thread 迴圈：`recv` → `jobs.send(TxJob{permit, payload, reply: bounded(1)})`（可阻塞）→ `reply.recv()` → `control.send(HumanTxCompleted{tag, report})`；`jobs.send` 回 `Disconnected` 時以 `NotSubmitted` 回報。`HumanTxCompleted` 依 D-15(3) 結算後，產生 06 `Effect::SettleTx{epoch, op_id}`（D-15(6)，R1-F8），再取 backlog 下一筆重新路由。結算的是 HumanReplay 時 `replay_hold.remaining -= 1`；降到 0 時依 D-22(3) 在同一次 `Fence::with` 清 `replay_auth`（hold 未被撤銷時）、清 `replay_hold`，然後執行 06 的出列（`foreground_busy()` 轉 false 後的第一個出列點）。測試 `human_tx_settles_ledger_each_chunk`：同一 epoch 由 c1 raw 輸入 1000 個 1-byte chunk → effects 中 `SettleTx` 恰 1000 個且 op_id 與各 `HumanTx` permit 一一對應；`ServiceRig` 讀現行 worker `tx().ledger_gauge().live_ops` 於最後一個 `HumanTxCompleted` 後 2 s 內為 0。
10. 06 `SelfTestProbe`、quiet arm、reboot 命中點依增補 #12、#13 接上。

- [ ] **Step 4：執行並確認 PASS**

Run: `scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test ownership --test session_core_console --test human_tx_lane --test fence_human_raw --test actor_liveness && scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes && python3 scripts/rust/check_cfg_boundary.py`
Expected: PASS；06 既有 sw-core 測試無新失敗（`drain_release.rs` 的 `release_success_response_shape` 仍為 `closed_consoles:1`，POSIX 哨兵算 1）。

- [ ] **Step 5：commit**

```bash
git add crates/sw-core/src/ crates/sw-core/tests/ crates/sw-pal/src/fakes/ changelog.d/rust-s22-console-ownership.md
git commit -m "feat(rust-console): 實作 raw／line ownership、human TX lane 與寫入前 input_epoch 重驗" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 7：Interactive lease 差量（tombstone、soft preempt、ATTACHED 救援、stash）

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s22-interactive-lease.md`（`type: feat`、`scope: rust-console`：「interactive lease 承接 human lease、soft preempt、stash 還原、可重複讀的 tombstone 與 ATTACHED 救援的 human 分支」）。
**BLOCKED-OWNER evidence:** 真實 U-Boot autoboot 倒數中 `interactive-open --allow-attached` 取得 `boot_interrupt`，並以 `interactive-send` 中斷 autoboot（L2 F9 與 realhw `p1-con-softpreempt`、`p1-con-busy`、`p1-con-liveness`、`p1-cmd-modes`；程序見 `docs/rust-refactor/procedures/cross-reboot-console.md`）。

本 Task 是對 06 T14 的**差量**：agent lease 的 open／send／status／close、1 s tick、`LeaseExpiry`、`encode_interactive_payload`、`open_attached_*` 救援判定已由 06 交付。下表標「回歸」的列對 06 實作預期 PASS，若 FAIL 即為 P5 缺陷，於本 Task Step 3 修正並在 PR 說明列出。

**Files:**
- Modify: `crates/sw-core/src/lease.rs`（增補 #14）
- Create: `crates/sw-core/src/rescue.rs`（自 06 T14 抽出救援判定，行為不變；增補 #18）
- Modify: `crates/sw-core/src/session_core.rs`（human lease、soft preempt、stash、refresh、self-heal、retained human、tombstone 可重複讀、`LeaseIndex` effect、`ConsoleAttach`／`ConsoleDetach`／`ConsoleList` request 處理）
- Modify: `crates/sw-core/src/uart_bridge.rs`（`RxShared::tail_py`，增補 #19）
- Modify: `crates/sw-core/src/session_manager.rs`（全域 `LeaseIndex`：`interactive_id → session_id`，executor 執行 `Effect::LeaseIndex`）
- Modify: `crates/sw-core/tests/lease_selftest.rs`（只改 `tick_expires_lease_sc13` 的期望，增補 #16）
- Modify: `crates/sw-core/tests/support/mod.rs`
- Create: `crates/sw-core/tests/lease_p6.rs`、`crates/sw-core/tests/rescue.rs`、`crates/sw-core/tests/session_core_lease.rs`

**Interfaces:**
- Consumes：Task 6 全部；06 `InteractiveLease`、`Tombstones`、`encode_interactive_payload`、`RxShared::tail`、`detect_boot_banner`、`CoreDeps.rng`、`pycompat::{clean_text, splitlines}`、`pyre::compile`；`SessionProfile { hard_timeout_s, login_regex, password_regex, bootloader_prompts, .. }`（06 config）。
- Produces：

```rust
// crates/sw-core/src/lease.rs（06 既有＋P6 追加；status 維持 &'static str）
pub struct InteractiveLease { /* 06 既有欄位 */ pub suspended_human: bool, pub peer_lost_at: Option<Mono> }
impl InteractiveLease { /* 06 既有 touch／expired（now-last > timeout，嚴格大於） */
    pub fn is_human(&self) -> bool; pub fn human_client_id(&self) -> Option<&str>; }
#[derive(Debug, Default)]
pub struct LeaseState { pub active: Option<InteractiveLease>, pub stashed_human: Option<InteractiveLease>,
    pub tombstones: Tombstones /* 06 */, pub retained_human_owner: Option<String>, pub retained_human_timeout_s: Option<f64> }
#[derive(Debug, Default)]
pub struct PostClose { pub needs_resume: bool, pub clear_owner_after_resume: bool,
                       pub replay_allowed: bool, pub dropped_stash_client: Option<String> }   // _PostCloseAction（05 §1.5）＋D-16
pub fn lease_context(lease: Option<&InteractiveLease>, last_human_input_at: Option<Mono>, now: Mono) -> PyDict;
   // {"interactive_owner","human_attached","human_active","recovery_mode"}（02 §6.2）

// crates/sw-core/src/rescue.rs（P7 login FSM 必須重用，不得另寫）
pub fn matches_any_bootloader_prompt(rx_tail: &str, patterns: &[String]) -> Option<String>;   // session_manager.py:125-152
pub fn matches_login_or_password(text: &str, login_regex: &str, password_regex: &str) -> bool; // login_fsm.py:140-162
pub enum RescueClass { BootloaderPrompt, BootInterrupt, LoginRequired, NotBootloader }
pub fn classify_attached_rescue(rx_tail_raw: &str, profile: &SessionProfile) -> RescueClass;  // 05 §10.1 ATTACHED 分支順序

// crates/sw-core/src/session_core.rs（增補 #9、#10）
pub struct PtyInfo { pub token: u64, pub slave_path: String }       // actor 層持有實際 PtyPair，core 只見資料（Task 10）
pub enum LeaseIndexOp { Insert { interactive_id: String }, Remove { interactive_id: String } }
// 效果順序不變式：同一批 Effects 中 LeaseIndex 一律排在 Reply 之前（executor 依序執行，先更新全域索引再回覆），
// 使 client 收到 interactive_id 後立即 interactive_send 不會得到假的 INTERACTIVE_NOT_FOUND

// crates/sw-core/src/uart_bridge.rs
impl RxShared { pub fn tail_py(&self, n: i128) -> String; }   // n>0 → tail(n)；n==0 → 全窗；n<0 → 去掉開頭 |n| 字元（text[-n:]）
```

- [ ] **Step 1：寫失敗測試**

`crates/sw-core/tests/session_core_lease.rs`（06 `CoreHarness`＋FakeClock；`h.wire()` 以 `RPC_WIRE` 比對 key 順序；`h.ids()` 為固定 seed 產出的 32-hex；open／status／close 用 Task 1 的 `open_req`／`status_req`／`close_req`）：

```rust
#![cfg(feature = "test-fakes")]
mod support;
use support::*;
use sw_core::session_core::*;
use sw_core::tx_writer::TxClass;

#[test]
fn soft_preempt_idle_human_then_close_restores_and_replays() {           // #53
    let mut h = CoreHarness::new(SHELL_PROFILE); h.to_ready();
    let c1 = h.attach_pty_console("c1", /*peer*/ true).get("client_id").unwrap().clone();   // 自動開 human lease
    let r = h.rpc(open_req("agent", 60.0, "", false)).unwrap();
    assert_eq!(r.get("soft_preempted"), Some(&true.into()));
    assert_eq!(r.get("recovery_mode"), Some(&false.into()));
    h.input("c1", b"Q");                                                  // 進 deferred
    assert!(h.human_tx().is_empty());
    let id = sw_proto::pycompat::py_str(r.get("interactive_id").unwrap());
    h.rpc(close_req(&id)).unwrap();
    assert_eq!(h.human_tx(), vec![(TxClass::HumanReplay, "human:c1".to_string(), b"Q".to_vec())]);
    let _ = c1;
}

#[test]
fn tick_expiry_restores_human_and_tombstone_is_repeatable() {         // SC-13（P1 T12 規則 1～2）
    let mut h = CoreHarness::new(SHELL_PROFILE); h.to_ready();
    h.attach_pty_console("c1", true);
    let id = sw_proto::pycompat::py_str(h.rpc(open_req("agent", 1.0, "", false)).unwrap().get("interactive_id").unwrap());
    h.input("c1", b"W");
    h.advance_s(2.0);                                                     // 投遞 06 的 ControlEvent::Tick
    assert_eq!(h.human_tx(), vec![(TxClass::HumanReplay, "human:c1".into(), b"W".to_vec())]);
    let expired = format!(r#"{{"ok":false,"error_code":"INTERACTIVE_EXPIRED","interactive_id":"{id}"}}"#);
    for req in [status_req(&id, 2048), status_req(&id, 2048), send_req(&id, "x", "plain"), close_req(&id), close_req(&id)] {
        assert_eq!(h.wire(&h.rpc(req).unwrap()), expired, "tombstone 可重複讀且不移除");
    }
}

#[test]
fn tick_uses_current_deadline_after_touch() {
    let mut h = CoreHarness::new(SHELL_PROFILE); h.to_ready();
    let id = sw_proto::pycompat::py_str(h.rpc(open_req("agent", 2.0, "", false)).unwrap().get("interactive_id").unwrap());
    h.advance_s(1.5); h.rpc(status_req(&id, 2048)).unwrap();             // touch
    h.advance_s(1.0);                                                     // 距 touch 1.0 s，Tick 不得到期
    assert_eq!(h.rpc(status_req(&id, 2048)).unwrap().get("ok"), Some(&true.into()));
}

#[test]
fn lease_index_insert_precedes_reply() {
    let mut h = CoreHarness::new(SHELL_PROFILE); h.to_ready();
    h.rpc(open_req("agent", 60.0, "", false)).unwrap();
    let kinds = h.effect_kinds();
    let li = kinds.iter().position(|k| *k == "LeaseIndex").unwrap();
    let rp = kinds.iter().position(|k| *k == "Reply").unwrap();
    assert!(li < rp);
}
```

其餘 lease 案例（對照 `tests/test_bootloader_recovery.py` lease 系列、`tests/test_login_recovery_lease.py`、`tests/test_interactive_raw.py` peer-grace／self-heal、`tests/test_refine_ownership.py` interactive 系列）。凡期望為 `ok:false` 的列，另斷言 `h.tx_effect_count()` 不變；open 類拒絕列一律帶 `command:"help"` 執行一次，斷言沒有 `StartOp(InteractiveOpenCmd)`（§11.3 #1 零 TX）：

| 測試名 | 情境 | 期望（RPC_WIRE 或狀態） |
|---|---|---|
| `open_twice_busy`（回歸） | 兩次 agent open | 第二次 `{"ok":false,"error_code":"SESSION_INTERACTIVE_BUSY","interactive_session_id":"<id0>"}` |
| `open_active_human_busy` | c1 human lease、c1 raw 輸入過（`last_human_input_at` 在 60 s 內） | `SESSION_INTERACTIVE_BUSY` |
| `open_human_owner_never_preempts` | c1 lease；owner `human:x` open | `SESSION_INTERACTIVE_BUSY` |
| `open_ready_fg_busy`（回歸） | READY 且前景操作中 | `{"ok":false,"error_code":"SESSION_BUSY","selector":"COM0"}` |
| `open_not_ready_without_allow` | ATTACHED、`allow_attached=false` | `{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM0"}` |
| `open_state_matrix` | 以 `force_state` 逐一設定，`allow_attached=true` | RECOVERING → `SESSION_RECOVERING`（形狀照 06 `recovering_interactive_open_session_recovering`，SC-01）；FLASHING → `FLASHING_BUSY`（SC-02）；ATTACHING／DETACHED／RELEASED → `{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM0"}` |
| `attached_unhealthy_not_ready` | ATTACHED＋allow；`running`／`serial_alive`／`vtty_alive` 任一 false | `SESSION_NOT_READY`＋selector（三案） |
| `attached_bootloader_prompt` | rx tail 末行 `=> `、profile `bootloader_prompts=["^=> $"]` | `{"ok":true,"interactive_id","session","recovery_mode":true}`（無 `boot_interrupt`、無 `login_required`） |
| `attached_banner_boot_interrupt`（回歸） | rx tail `Hit any key to stop autoboot:  3` | 同上加 `"boot_interrupt":true` |
| `attached_login_required` | rx tail `\x1b[1m(none) login:\x1b[0m ` | 加 `"login_required":true` |
| `attached_bootloader_priority_over_login` | 末行 `=> `，前面有 `login:` | 無 `login_required` |
| `attached_banner_priority_over_login` | banner＋`login:` | 只有 `boot_interrupt` |
| `attached_not_bootloader`（回歸） | rx tail `random text` | `{"ok":false,"error_code":"SESSION_NOT_READY","selector":…,"error_detail":"NOT_BOOTLOADER"}` |
| `attached_fallback_prompts_not_used` | `bootloader_prompts=[]`、末行 `=> ` | `NOT_BOOTLOADER`（不用 `UBOOT_FALLBACK_PROMPTS`，05 §16.4） |
| `recovery_lease_timeout_clamped`（回歸） | allow＋timeout 500 | lease `timeout_s==120.0` |
| `recovery_small_timeout_passthrough` | allow＋timeout 30 | `30.0` |
| `ready_allow_attached_no_clamp` | READY＋allow＋timeout 500 | `500.0`、`recovery_mode:false` |
| `attached_stashes_human_lease` | ATTACHED＋c1 human lease＋banner | 開 recovery lease（`suspended_human=true`）、stash＝c1 lease、ownership suspended |
| `attached_existing_agent_busy` | ATTACHED＋既有 agent lease | `SESSION_INTERACTIVE_BUSY` |
| `open_command_sent_after_open`（回歸） | open 帶 `command:"help"` | lease 建立後一個 `StartOp(InteractiveOpenCmd{command:"help", owner})`；不受 quiet 擋 |
| `send_plain`／`send_key_ctrl_c`／`send_base64`（回歸） | `"ab"`；key `" CTRL-C "`；`"aGk="` | `{"ok":true,"interactive_id":…,"bytes":n}`；`StartOp(InteractiveSend{payload})` |
| `send_invalid_encoding_is_pyerr`（回歸） | encoding `"hex"` | `Err(PyErr)`，message `INVALID_INTERACTIVE_ENCODING`（framing 轉 `EXCEPTION`） |
| `send_checks_order` | 依序：無 lease → `INTERACTIVE_NOT_FOUND`；過期 → `INTERACTIVE_EXPIRED`（並關閉）；bridge 不存在（DETACHED）→ `SESSION_NOT_READY`；FLASHING → `FLASHING_BUSY`；fg busy → `SESSION_BUSY`；以上皆帶 `interactive_id` | 逐案；每案零 TX |
| `send_invalid_encoding_after_busy_check` | fg busy＋encoding 錯 | 回 `SESSION_BUSY`，不 raise |
| `send_allowed_in_attached` | recovery lease 在 ATTACHED | ok（不檢查 state，只擋 FLASHING） |
| `status_shape_and_touch`（回歸） | status `screen_chars:10` | `{"ok":true,"interactive_id","owner","status":"active","recovery_mode",…,"screen":clean_text(tail_py(10)),"session"}`；`last_activity` 更新 |
| `status_screen_chars_negative` | `screen_chars:-3`，RX 窗 `"abcdef"` | `screen=="def"`（`tail_py(-3)`＝`text[3:]`，02 §11 H-08） |
| `status_not_ready_without_bridge` | lease 存在但 bridge 不存在 | `{"ok":false,"error_code":"SESSION_NOT_READY","interactive_id":…}` |
| `status_expired_closes_restores` | 過期 recovery lease＋stash（peer 在） | `INTERACTIVE_EXPIRED`；stash 還原、HumanReplay 送出 |
| `close_unknown` | close 不存在 id | `{"ok":false,"error_code":"INTERACTIVE_NOT_FOUND","interactive_id":…}` |
| `tombstone_fifo_16` | 17 個 agent lease 依序 tick 到期 | 第 1 個 id 存取 → `INTERACTIVE_NOT_FOUND`、`LeaseIndex(Remove)`；第 17 個 → `INTERACTIVE_EXPIRED` |
| `lazy_expiry_keeps_tombstone_expired`（回歸，06 `send_expired_closes`；原名 `lazy_expiry_then_not_found`）（parity-amendments-2026-10-26 #54） | timeout 1、advance 1.001 s（Tick 前）、send；再 send、status、close；之後再開 16 個 lease 並讓其到期 | 第一次 `INTERACTIVE_EXPIRED`；再 send／status／close 皆 `INTERACTIVE_EXPIRED`、tombstone 不移除；第 17 個 tombstone 擠出後原 id → `INTERACTIVE_NOT_FOUND` |
| `human_lease_not_expired_by_tick` | c1 human lease（timeout 86400）、時間推進 10^6 s | lease 仍 active |
| `stash_dropped_when_peer_gone_sc31` | soft preempt 後 c1 輸入 `b"Q"` 進 deferred；c1 peer 消失；close | `interactive_session_id` 為 null；`human_tx()` 不含 `Q`；WARNING `reason=lease_dropped`；resume 後 owner None（`clear_owner_after_resume`）（D-16） |
| `stash_dropped_when_expired_sc31` | stash lease 已過期；c1 有 deferred；close | 同上 |
| `close_restore_replay_holds_foreground`（R2-F4，D-22(1)(2)；以下三列設 `set_auto_complete_human_tx(false)`） | READY；soft preempt 後 c1 輸入 `b"Q"` 進 deferred；close agent lease（stash 還原、peer 在） | 一筆 HumanReplay `b"Q"`，`h.last_human_permit().replay_hold==Some(id)`、`h.fence_replay_auth()==Some(id)`；結算前排隊的 agent 命令沒有 `StartOp`、`h.foreground_busy()==true`；`complete_human_tx(Completed(1))` 後才出列 |
| `interactive_rpc_busy_during_replay_hold`（R2-F4，D-22(2)） | 同上，回放 `Q` 未結算時依序呼叫 READY `interactive_open{owner:"agent"}`、對另一個既有 recovery lease 的 `interactive_send`（以 `force_state` 製造） | 兩者 `{"ok":false,"error_code":"SESSION_BUSY",…}`（形狀照 `open_ready_fg_busy`／`send_checks_order`），帶 `command:"help"` 的 open 無 `StartOp(InteractiveOpenCmd)`、`tx_effect_count()` 不變；`get_state` 的 `foreground_busy==true`；結算後同一 open 成功 |
| `attached_rescue_open_mid_replay_redefers`（R2-F4，D-22(4)） | ATTACHED；c1 human lease 被 agent recovery lease A stash 期間 `input("c1", b"k")` 進 deferred；close A（stash 還原、peer 在）→ 回放 `k`（hold h，未結算）；rx tail 為 banner，再 `interactive_open{allow_attached:true, owner:"agent"}` 開 B | open 成功（ATTACHED 分支不檢查前景，Python parity，`session_manager.py:4027` 只在 READY 判 busy），且撤銷先於 `set_interactive_owner`：`effect_kinds()` 中撤銷後無 HumanTx；回應後 `h.fence_replay_auth()==None`、`h.replay_hold().unwrap().revoked==true`；以 `complete_human_tx(TxOutcome::NotSubmitted, Some(FenceReject::ReplayRevoked))` 結算後無重送、`h.deferred_len("c1")==1`、`h.replay_hold()==None`；B 的 `interactive_send "ab"` 期間 `human_tx()` 無新項目；close B（stash 還原、peer 在）後才出現一筆新的 HumanReplay `b"k"`，其 `replay_hold` 與 h 不同，且在 `ab` 的 `StartOp(InteractiveSend)` 之後 |
| `peer_flap_within_grace_keeps_lease` | c1 peer 消失 → `scan`（記 `peer_lost_at`）→ 2.0 s 後 peer 回來 → `scan` | lease 保留、`peer_lost_at` 清除 |
| `peer_gone_past_grace_tears_down` | peer 消失 → `scan` → 3.1 s 後 `scan` | `ConsoleClose{c1}`、lease 關閉 |
| `refresh_closes_when_owner_mismatch` | human lease 但 ownership owner 不同 | lease 關閉 |
| `self_heal_regrants_after_owner_loss` | primary 有 peer、無 lease、ownership idle；`scan` | owner `human:<primary>`、新 lease timeout `max(hard_timeout_s, 86400)`；**不**另呼叫 `set_interactive_owner` |
| `self_heal_skips_during_agent_active` | suspend 中 | 不授予 |
| `self_heal_skips_non_ready_attached_states` | RECOVERING | 不授予 |
| `retained_human_restored_after_ready` | `begin_stop(Recover)` 時 c1 為 human lease；新 epoch 進 READY | 以 `retained_human_timeout_s` 重開 `human:c1` lease、owner `human:c1` |
| `retained_human_restored_after_attached` | 同上，但新 epoch 登入失敗停在 ATTACHED | 同樣重開 lease 並取得 raw ownership（`session_manager.py:2728` 在 READY 與 ATTACHED 都還原） |
| `cmd_interactive_mode_opens_lease` | arbiter 執行 mode interactive、command `vim` | lease owner＝source、timeout `max(timeout_s, hard_timeout_s)`；`StartOp(Command)`、cmd_id 有值；回 `{"ok":true,"execution_mode":"interactive","interactive_session_id":…,"stdout":"","status":"interactive"}` |
| `human_timeout_promoted_only_for_internal_origin` | `SubmitOrigin::HumanConsole` 命令逾時 | 回 `"recovery_action":"PROMOTE_HUMAN_INTERACTIVE"`、`"partial":true` 並開 lease；`SubmitOrigin::Rpc`＋source `human:x` 逾時 → 走 agent 的 `_recover_after_failure`（DF-02） |
| `human_reboot_opens_lease` | `HumanConsole` 來源 `reboot`，2 s 內無 prompt | state `ATTACHED`、`last_error=="REBOOTING"`、lease 開啟、`recovery_action:"PROMOTE_HUMAN_INTERACTIVE"`；`invalidate_human_input(RebootCommand)` 已執行 |
| `lease_context_shapes` | 無 lease／human idle／human active／recovery | `{"interactive_owner":null,"human_attached":false,"human_active":false,"recovery_mode":false}` 等四組，`suspended_human` 不出現 |

`crates/sw-core/tests/lease_p6.rs`：`InteractiveLease::is_human`、`human_client_id`（`"human:c1"`→`Some("c1")`、`"agent"`→`None`、`"human:"`→`Some("")`）；`PostClose` 預設值；`lease_context` 四組（同上表）以 `pyjson::dumps(RPC_WIRE)` 比對 key 順序。

`crates/sw-core/tests/rescue.rs`：`matches_any_bootloader_prompt` 取最後一個非空白行、invalid regex 略過、空輸入回 None；`matches_login_or_password` 先 `clean_text` 再比對、cleaned 或 raw 任一命中即 true、invalid regex 略過；`classify_attached_rescue` 的四類各兩例。

`crates/sw-core/tests/lease_selftest.rs::tick_expires_lease_sc13`（06 既有）：期望由「`status` 回 `INTERACTIVE_NOT_FOUND`」改為「`status` 回 `INTERACTIVE_EXPIRED`，且第二次 `status` 仍為 `INTERACTIVE_EXPIRED`」（P1 SC-13；增補 #16）。

- [ ] **Step 2：執行並確認 FAIL**

Run: `scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test lease_p6 --test rescue --test session_core_lease --test lease_selftest`
Expected: P6 新行為 FAIL（`not yet implemented: P6 Task 7`、tombstone 斷言失敗、`tick_expires_lease_sc13` 新期望失敗）；標「回歸」的列 PASS。

- [ ] **Step 3：實作**

1. `interactive_open`：在 06 的實作上加入 human 分支，逐字依 session_manager.py:4009-4189（05 §10.1）。外層 retry 迴圈：refresh 回傳 `PostClose{needs_resume:true}` 時，先在 core 內執行 resume 效果（受 `replay_allowed` 限制），再重新評估一次，最多兩輪（Python `while True` 實際只會因 resume 重來一次）。soft preempt：human lease、`lease_context(..)["human_active"]==false`、`!owner.starts_with("human:")` → `stashed_human = active.take()`、`ownership.suspend()`、開 lease（`suspended_human:true`）、回應加 `"soft_preempted":true`。ATTACHED 分支：`running`（IoWorker 存活）、`serial_alive`（06 worker 回報）、`vtty_alive`（Task 9 hub 狀態）任一 false → `SESSION_NOT_READY`；`rx_tail(BOOTLOADER_RX_TAIL_BYTES)` 交給 `rescue::classify_attached_rescue`；`timeout = timeout_s.min(MAX_RECOVERY_LEASE_S)`。狀態矩陣依 `open_state_matrix`。READY 分支與 `interactive_send` 的前景忙判定用 `foreground_busy()`（回放 hold 期間回 `SESSION_BUSY`，D-22(2)）；ATTACHED 救援分支不判前景（Python parity），改在開 lease 前 `revoke_replay`（D-22(4)）。
2. `_open_interactive_locked` 等價：`interactive_id` 取自 `deps.rng`（06 既有）、`created_at = wall_iso`、先 `revoke_replay`（D-22(4)；owner 為 `human:` 時不撤銷，D-22(5)）再 `ownership.set_interactive_owner(Some(owner))`、`Effect::LeaseIndex(Insert)` 放在 `Reply` 之前。`_close_interactive_locked` 等價（05 §10.4）回傳 `PostClose`，由同一 handler 立即執行：stash 還原條件為 `!stash.expired(now) && peer_present(stash client)`；成立時 `replay_allowed=true`，resume 的回放依 D-22(1) 建立 hold（lease tick／lazy 到期的 close 同）；不成立時 `replay_allowed=false`、`dropped_stash_client=Some(cid)`，先 `deferred.remove_client(cid)` 並寫 D-16 WARNING，再 `ownership.resume()`（不回放該 client），最後依 `clear_owner_after_resume` 執行 `set_interactive_owner(None)`。
3. 到期：沿用 06 的 `ControlEvent::Tick` 與 `TimerEvent::LeaseExpiry`（deadline 以處理當下的 `last_activity` 重算）。tick 到期時 status 設 `"expired"`、執行 close 流程、`tombstones.push(id, "expired")`；超過 16 時 pop 最舊者並發 `LeaseIndex(Remove)`。`send`／`status`／`close` 先查 `active`，再查 tombstones：命中就回 `INTERACTIVE_EXPIRED`，**不移除**。lazy 到期（存取時判定）與 tick 相同：回 `INTERACTIVE_EXPIRED`、執行 close 流程、`tombstones.push(id, "expired")`，`LeaseIndex(Remove)` 延到被 FIFO 擠出時才發（parity-amendments-2026-10-26 #54）。
4. `refresh`（`_refresh_interactive_locked`，05 §10.5）：human lease 以 `human.peer_present`（D-09 最近一次 `ConsoleScan`）判 peer；超過 grace → `ConsoleClose{cid}` 並移除 console（等同 `detach_console`）後 close。
5. Self-heal（`ConsoleScan{source: Hub}` 處理內）：條件逐字照 session_manager.py:1638-1690；成立時 `ownership.try_grant_if_idle(...)` 回 true 才記 lease（不呼叫 `set_interactive_owner`）。同一事件內先 reap（Task 9 的規則），後 self-heal（Python 順序）。
6. Retained human：新 epoch 的 attach 結果為 READY **或** ATTACHED 時，都執行 `_restore_retained_human_console_locked` 等價（`session_manager.py:2728`）。
7. `rescue.rs`：把 06 T14 救援判定原樣搬移，06 的呼叫點改呼叫此模組（`open_attached_*` 回歸測試必須仍 PASS）。`lease_context` 照 Produces 註解；`encode_interactive_payload` 直接呼叫 06 `op_runner.rs` 的版本。
8. `RxShared::tail_py`：`n >= 0` 呼叫 `tail(n as usize)`；`n < 0` 回傳去掉開頭 `|n|` 個字元的全窗文字（`|n|` 超過長度時回空字串）。

- [ ] **Step 4：執行並確認 PASS**

Run: `scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test lease_p6 --test rescue --test session_core_lease --test lease_selftest && scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes`
Expected: PASS。

- [ ] **Step 5：commit**

```bash
git add crates/sw-core/src/lease.rs crates/sw-core/src/rescue.rs crates/sw-core/src/session_core.rs crates/sw-core/src/uart_bridge.rs crates/sw-core/src/session_manager.rs crates/sw-core/tests/ changelog.d/rust-s22-interactive-lease.md
git commit -m "feat(rust-console): interactive lease 承接 human lease、stash 與可重複讀 tombstone" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

#### parity-amendments-2026-10-26 追加（Python 1:1 承接）

> 來源：`docs/rust-refactor/plans/parity-amendments-2026-10-26.md`（稽核 A／B 與 P6 審查）。依 OD-14 #1（外部可觀察行為與 Python 1:1，只可新增、不可修改），下列為本 Task 的**必要**測試列與完成條件：紅燈先行，新增 nodeid 於本 Task 紅燈 commit 以 P2 `regen_lock.py --l1-add-only` 登記到所列集合；斷言值以 Python 基線實測為準（不得自訂）。

| # | 稽核來源 | 測試／要求 | 精確斷言或完成條件 |
|---|---|---|---|
| #35 | B:D-71 | `open_with_command_reply_waits_tx_and_expired_maps` | `interactive_open` 帶 `command`：回覆等該 TX 結算後才送出；TX 提交前 lease 已失效（permit `lease`）→ `{"ok":false,"error_code":"INTERACTIVE_EXPIRED",…}`、零 TX（`session_manager.py:7135-7146`，對照鎖內 `test_interactive_open_command_expired_before_submit_zero_tx`）；延伸 `open_command_sent_after_open` |
| #36 | B:D-74 | `send_checks_order` 延伸至提交點拒絕 | 映射同 06 Task 14 #36（`INTERACTIVE_EXPIRED`／`AUTOBOOT_QUIET`／`SESSION_BUSY`） |
| #56 | B:D-60、D-66 | `tick_close_restore_returns_session_interactive_busy` | D-22(2) 例外：tick 到期 PostClose 還原結算前，`interactive_open`／interactive 命令／human 救援回 `SESSION_INTERACTIVE_BUSY`、`console_attach` 回 `{"ok":false,"error_code":"SESSION_INTERACTIVE_BUSY","selector":<COM>}`；還原結算後 open 成功、`interactive_owner=="agent:new"`（逐字對照鎖內 `test_tick_blocks_new_lease_until_owner_restore_finishes`）；fg_finish 來源的 hold 仍回 `SESSION_BUSY`（`interactive_rpc_busy_during_replay_hold` 不變） |
| #41 | B:E-115 | `attached_unhealthy_not_ready` 消費端 | 使用 06 Task 12 #41 定義的 `serial_alive`／`running` |
| #108 | B:D-61、D-101 | `windows_tcp_attach_console_does_not_open_lease`、`transfer_suspend_defers_existing_human_window` | Windows TCP `attach_console` 不開 lease（`:6855-6864`）；file 傳輸 suspend human 時既有 human owner 視窗先 defer（`:7970,8001`） |

---

## Task 8：PAL console backends（PTY／TCP／ConsoleMux／com0com）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s22-pal-console.md`（`type: feat`、`scope: rust-pal`：「PAL 新增 PTY console（raw termios＋keepalive）、127.0.0.1 TCP console listener、ConsoleMux 多工與選配 com0com console」）。
**擁有者 host 可執行（OD-15，原 BLOCKED-OWNER）：** Spike 1 基本資格已在擁有者 host 通過（com0com 3.0.0.0 已安裝、HVCI 執行中；既有 DUT-side pair `CNCA0=COM20`／`CNCB0=COM21`）。`cap_windows_com__console_com0com_*` 三案屬 Spike 1 剩餘項「Console-side pair」（OD-01 選配），需在同一 host 另建第二組 pair（以 `setupc install` 或 P4 Task 19 的 SC-36 helper `--pair`，需管理員一次），並以 `SW_TEST_CONSOLE_PAIR=<A>:<B>` 由 orchestrator 經 WSL interop 執行；結果未知前不得預標 PASS，`no_modem_forward` 案同時是 Spike 1 剩餘項「控制線隔離」的證據。HVCI 開啟環境下的功能自測紀錄併入該次報告（§7.3、§15.8.3）。

**Files:**
- Modify: `crates/sw-pal/src/console_pty.rs`、`crates/sw-pal/src/console_pty/posix.rs`（06 T2 已建 openpty／raw termios／keepalive；本 Task 只加 `mux_handle()` 與 conformance，不重建，增補 #22）；Create: `crates/sw-pal/src/console_pty/windows.rs`（`create_pty` 回 `Unsupported`）
- Modify: `crates/sw-pal/src/console_tcp.rs`；Create: `crates/sw-pal/src/console_tcp/posix.rs`、`crates/sw-pal/src/console_tcp/windows.rs`
- Modify: `crates/sw-pal/src/console_mux.rs`；Create: `crates/sw-pal/src/console_mux/posix.rs`、`crates/sw-pal/src/console_mux/windows.rs`
- Modify: `crates/sw-pal/src/console_com0com.rs`；Create: `crates/sw-pal/src/console_com0com/windows.rs`、`crates/sw-pal/src/console_com0com/posix.rs`
- Modify: `crates/sw-pal/src/proc_scan.rs`（`held_slave_paths` 的 `/proc/<pid>/fd` 掃描）
- Modify: `crates/sw-pal/src/lib.rs`（`native()` 依 SC-28 選 `ConsoleKind`；`Capabilities.com0com_console`）
- Modify: `crates/sw-pal/src/fakes/console.rs`、`crates/sw-pal/src/conformance/console_suite.rs`、`crates/sw-pal/src/testutil.rs`（Task 1 骨架的本體）
- Modify: `docs/rust-refactor/plans/rust-architecture.md` §4.7（`ConsoleIo::mux_handle`、`ConsoleListener::mux_handle`、`MuxHandle`、`ConsoleMux`；D-04）
- Create: `crates/sw-pal/tests/console_conformance.rs`
- Modify: `scripts/rust/win-runner.sh`（`WSLENV` 加 `SW_TEST_CONSOLE_PAIR`；arch §9.4 第 5 步原本只轉送 `SW_TEST_COM_PAIR`，否則 `cap_windows_com__console_com0com_*` 在 Windows 端永遠讀不到此變數）、`docs/rust-refactor/plans/rust-architecture.md` §9.4 同步；`tests/acceptance/` 的 Windows 能力 manifest 註明 `SW_TEST_CONSOLE_PAIR` 的角色為 Console-side pair（與 DUT-side 的 `SW_TEST_COM_PAIR` 分開）

**Interfaces:**
- Consumes：arch §4.7 trait；`crossbeam-channel`（com0com reader→mux）。
- Produces（追加於 arch §4.7）：

```rust
// crates/sw-pal/src/console_mux.rs
pub struct MuxHandle(/* PAL 私有：Posix(RawFd) | WinSocket(usize) | Com0com(u64) | Fake(u64) */);   // 只能由 sw-pal 建構
pub trait ConsoleIo { /* arch §4.7 既有 + */ fn mux_handle(&self) -> MuxHandle; }
pub trait ConsoleListener { /* arch §4.7 既有 + */ fn mux_handle(&self) -> MuxHandle; }
pub type MuxId = u64;
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MuxEvent { Readable(MuxId), Writable(MuxId), Hangup(MuxId), Acceptable(MuxId), Woken }
pub trait ConsoleMux: Send {
    fn add_io(&mut self, id: MuxId, io: Box<dyn ConsoleIo>) -> PalResult<()>;        // 以 io.mux_handle() 取得 fd／SOCKET／channel；不同 PAL 的 handle → Err(Unsupported)
    fn add_listener(&mut self, id: MuxId, l: Box<dyn ConsoleListener>) -> PalResult<()>;
    fn remove(&mut self, id: MuxId) -> Option<MuxEntry>;
    fn set_write_interest(&mut self, id: MuxId, on: bool);
    fn wait(&mut self, timeout: Duration) -> PalResult<Vec<MuxEvent>>;        // POSIX poll(2)；Windows WSAPoll
    fn read(&mut self, id: MuxId, buf: &mut [u8]) -> PalResult<ReadOutcome>;  // 非阻塞；EAGAIN → TimedOut
    fn try_write(&mut self, id: MuxId, data: &[u8]) -> PalResult<usize>;      // EAGAIN → Ok(0)
    fn accept(&mut self, id: MuxId) -> PalResult<Option<(Box<dyn ConsoleIo>, String)>>;
    fn waker(&self) -> Arc<dyn IoWaker>;
}
pub enum MuxEntry { Io(Box<dyn ConsoleIo>), Listener(Box<dyn ConsoleListener>) }
pub trait ConsoleTransportFactory { /* arch §4.7 既有 + */ fn new_mux(&self) -> PalResult<Box<dyn ConsoleMux>>; }
pub fn select_console_kind(env: Option<&str>, has_pty: bool) -> (ConsoleKind, Option<&'static str /* WARNING 文字 */>);   // SC-28
```

- [ ] **Step 1：寫失敗測試**

`crates/sw-pal/src/conformance/console_suite.rs` 定義一組 `fn(&Platform)` 斷言，`crates/sw-pal/tests/console_conformance.rs` 分別以 `fakes`、native POSIX、native Windows 呼叫；需要資源者依 arch §9.1 命名 `cap_<resource>__<name>` 並 `#[ignore = "cap:<resource>"]`。

```rust
// crates/sw-pal/tests/console_conformance.rs（節錄）
#![cfg(feature = "test-fakes")]
use std::time::Duration;
use sw_pal::{ReadOutcome, ConsoleIo, ConsoleListener};
fn tempfile_dir(tag: &str) -> std::path::PathBuf {          // 每次呼叫建立唯一暫存目錄（pid＋計數器），測試結束不需清理
    static N: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let d = std::env::temp_dir().join(format!("{tag}-{}-{}", std::process::id(), N.fetch_add(1, std::sync::atomic::Ordering::Relaxed)));
    std::fs::create_dir_all(&d).unwrap(); d
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__pty_slave_is_raw_and_master_nonblocking() {
    let p = sw_pal::native();
    let pair = p.console.create_pty().unwrap();
    assert!(sw_pal::testutil::is_dev_pts_path(&pair.slave_path));
    let t = sw_pal::testutil::tcgetattr_path(&pair.slave_path).unwrap();
    assert_eq!((t.iflag, t.oflag, t.lflag), (0, 0, 0));
    assert_eq!(t.cflag & (t.CREAD | t.CLOCAL | t.CS8), t.CREAD | t.CLOCAL | t.CS8);
    assert_eq!((t.vmin, t.vtime), (1, 0));
    assert!(sw_pal::testutil::master_is_nonblocking(&pair));
    let run_dir = tempfile_dir("sw-p6-run");                     // 測試自建暫存目錄，不依賴外部 env
    assert!(sw_pal::testutil::no_symlink_points_to(&pair.slave_path, &[run_dir.as_path()]));   // D-01
}

#[test]
#[ignore = "cap:posix_pty"]
fn cap_posix_pty__external_close_does_not_eof_master() {   // keepalive（06 §7.1）
    let p = sw_pal::native();
    let mut pair = p.console.create_pty().unwrap();
    let ext = std::fs::OpenOptions::new().read(true).write(true).open(&pair.slave_path).unwrap();
    drop(ext);
    let mut buf = [0u8; 16];
    assert!(matches!(pair.master.read(&mut buf, Some(Duration::from_millis(200))).unwrap(), ReadOutcome::TimedOut));
}

#[test]
fn tcp_console_binds_loopback_ephemeral_and_peer_eof() {  // 兩平台都可跑（Windows 經 win-runner）
    let p = sw_pal::native();
    let mut l = p.console.bind_tcp_console().unwrap();
    let ep = l.endpoint();
    let (host, port) = ep.rsplit_once(':').unwrap();
    assert_eq!(host, "127.0.0.1"); assert_ne!(port.parse::<u16>().unwrap(), 0);
    let c = std::net::TcpStream::connect(&ep).unwrap();
    let (mut io, peer) = sw_pal::testutil::wait_accept(&mut *l, Duration::from_secs(2));
    assert!(peer.starts_with("127.0.0.1:"));
    assert!(io.has_external_peer(None));
    drop(c);
    let mut buf = [0u8; 8];
    assert!(matches!(io.read(&mut buf, Some(Duration::from_secs(2))).unwrap(), ReadOutcome::Eof));
}
```

| 測試名 | 平台／標記 | 斷言 |
|---|---|---|
| `cap_posix_pty__held_slave_paths_sees_external_holder` | posix_pty | 子行程 `sleep 5 < slave` 持有時集合含 `slave_path`；自身 pid 持有不算 |
| `cap_posix_pty__try_write_full_returns_zero` | posix_pty | 無人讀 slave 時持續 `try_write(8192 bytes)`，最終回 `Ok(0)`，不回 Err |
| `cap_posix_pty__has_external_peer_semantics` | posix_pty | `held=None` → true；`held` 不含 → false；含 → true |
| `tcp_accepted_socket_has_nodelay` | 兩平台 | `getsockopt(TCP_NODELAY)==1`（`testutil::nodelay(&io)`） |
| `tcp_listener_reuseaddr` | 兩平台 | `testutil::reuseaddr(&l)==true` |
| `tcp_try_write_never_blocks` | 兩平台 | 對端不讀時 `try_write` 在 10 ms 內回傳 |
| `mux_waker_wakes_wait` | 兩平台 | 另一 thread 5 ms 後 `wake()`，`wait(5 s)` 於 100 ms 內回 `[Woken]` |
| `mux_reports_readable_and_hangup` | 兩平台 | TCP io 收到資料 → `Readable(id)`；對端關閉 → `Hangup(id)` 或 `Readable` 後 `read` 為 `Eof` |
| `mux_write_interest_toggles` | 兩平台 | `set_write_interest(id,true)` 後 `Writable(id)` 出現；false 後不再出現 |
| `cap_posix_pty__mux_mixed_pty_and_tcp` | posix_pty | 同一 mux 內 PTY 與 TCP 各自觸發 `Readable` |
| `select_console_kind_table` | 純函式 | `(None,true)→Pty`；`(Some("tcp"),true)→TcpTelnet`；`(Some(" PTY "),true)→Pty`；`(Some("pty"),false)→(TcpTelnet, Some(WARNING))`；`(Some("bogus"),true)→Pty`；`(Some(""),false)→TcpTelnet` |
| `com0com_unsupported_on_posix` | 只在 posix 編譯產物跑（由 `caps.com0com_console==false` 判斷，不用 cfg） | `open_com0com_console("COM21")` 回 `Err(kind=Unsupported)` |
| `cap_windows_com__console_com0com_roundtrip` | windows_com | `SW_TEST_CONSOLE_PAIR=COMa:COMb`：開 COMa 為 console，對 COMb 寫 `b"hi"` → mux `Readable` 並讀到；console 端 `try_write(b"ok")` → COMb 讀到 |
| `cap_windows_com__console_com0com_exclusive` | windows_com | 開啟後再以 share 0 開同埠失敗（`ERROR_ACCESS_DENIED`） |
| `cap_windows_com__console_com0com_no_modem_forward` | windows_com | 對 COMb 改 baud 與 DTR／RTS 後，console handle 端不呼叫任何設定 API（以 `testutil::comm_api_calls(&io)` 計數器斷言 `set_comm_state==1`（僅開啟時一次）、`escape_comm_function==0`） |
| `mux_handle_from_foreign_pal_rejected` | 兩平台 | 把 `fakes::console` 建立的 io 加進 native mux → `Err(Unsupported)` |
| `tcp_wouldblock_maps_to_zero` | 兩平台 | 對端不讀直到送出緩衝滿：`try_write` 回 `Ok(0)`（Windows `WSAEWOULDBLOCK`、POSIX `EAGAIN`），不回 Err |
| `listener_readable_reports_acceptable` | 兩平台 | 有連線排隊時 `wait` 回 `Acceptable(id)`（Windows 以 `POLLRDNORM` 判定） |
| `winsock_error_message_format` | Windows（由 `caps.console_kind==TcpTelnet && !caps.pty` 判斷） | 連線到已關閉 port 的錯誤 `PalError.py_message` 以 `[WinError N] ` 開頭 |
| `fake_console_suite` | memory_only | 同一 suite 跑在 `fakes::console`（不計原生 gate） |

- [ ] **Step 2：執行並確認 FAIL**

Run: `scripts/rust/cargo_nonzero.sh -p sw-pal --features test-fakes --test console_conformance -- --include-ignored`；orchestrator 另執行 `scripts/rust/win-test.sh --package sw-pal --features test-fakes --test console_conformance`（以 `--test` 選 integration binary，不加 libtest filter，避免像 `-- console` 這種過濾字串漏掉 `mux_waker_wakes_wait` 等案例）
Expected: FAIL（`not yet implemented: P6 Task 8`）；Windows 報告該 exe `failed>0`。

- [ ] **Step 3：實作**

1. POSIX PTY（`console_pty/posix.rs`，06 T2 已實作，本 Task 只核對並加 `mux_handle()`）：`openpty` → master `fcntl(F_SETFL, O_NONBLOCK)`、`FD_CLOEXEC`；slave `tcgetattr` 後 `c_iflag=0, c_oflag=0, c_lflag=0, c_cflag=CREAD|CLOCAL|CS8, c_cc[VMIN]=1, c_cc[VTIME]=0`、`tcsetattr(TCSANOW)`，不設 speed（uart_io.py:220-230）；`slave_path = ttyname(slave)`；`PtyPair.slave_keepalive` 持有 slave fd 直到 `close`；`ConsoleIo::read` 為 `poll([master, wake])`；`try_write` 單次 `write`，`EAGAIN` → `Ok(0)`。Windows `console_pty/windows.rs`：`create_pty` 回 `Unsupported`。
2. `held_slave_paths`：掃 `/proc/<pid>/fd/*` 的 readlink（排除 `getpid()`），`/proc` 不可讀回 `None`（uart_io.py:463-490）；Windows 回 `None`。
3. TCP（`console_tcp.rs` 以 `std::net::TcpListener` 為主體；`SO_REUSEADDR` 在 bind 前經 `posix.rs`／`windows.rs` 的 `setsockopt` 設定）：`bind 127.0.0.1:0`、`listen(8)`（`CONSOLE_TCP_BACKLOG`；std 預設 128，故改用 socket2 等價的原生呼叫在 sub module 內 `listen(fd, 8)`）、nonblocking；accept 後 `set_nodelay(true)`（失敗忽略）、nonblocking；peer 字串 `"<ip>:<port>"`；`has_external_peer` 恆 true；`read` EOF → `ReadOutcome::Eof`。
4. `ConsoleMux`：POSIX 以 `libc::poll` 管理 `Vec<pollfd>`（含 eventfd／self-pipe waker）；Windows 以 `WSAPoll`，waker 為一對 loopback TCP socket（`127.0.0.1:0` 自連），`wake()` 送 1 byte、`wait` 讀乾。com0com io 不是 socket：PAL 為其起內部 reader thread（`serialwrap-com0com-<port>`），讀到的資料放入有界 channel（64）並喚醒 mux，`wait` 回 `Readable(id)`。
5. com0com（`console_com0com/windows.rs`）：`CreateFileW(\\.\COMn, GENERIC_READ|GENERIC_WRITE, 0, NULL, OPEN_EXISTING, FILE_FLAG_OVERLAPPED)`；`SetCommState` 一次（115200 8N1，不作用於 DUT）、`SetCommTimeouts`（`ReadIntervalTimeout=MAXDWORD` 等，同 arch §4.2）；不呼叫 `EscapeCommFunction`；`has_external_peer` 恆 true。POSIX 版回 `Unsupported`。`Capabilities.com0com_console = true` 只在 Windows native。
6. `native()`：讀 `SERIALWRAP_CONSOLE_BACKEND`，以 `select_console_kind(env, has_pty)` 決定 `caps.console_kind`；回傳 WARNING 時寫 stderr 一行（D-11 文字）。
7. Windows 專屬（原列於 Task 12，移到本 Task，使 Step 4 的 Windows 預期成立）：`WSAPoll` 對 listener 以 `POLLRDNORM` 判定 `Acceptable`；loopback waker 的重入（`wake()` 連呼叫只留 1 byte、`wait` 讀乾）；Winsock 錯誤映射到 `PalError.py_message`（`[WinError N] …`）；nonblocking `WSAEWOULDBLOCK` → `Ok(0)`。

- [ ] **Step 4：執行並確認 PASS**

Run: `scripts/rust/cargo_nonzero.sh -p sw-pal --features test-fakes --test console_conformance -- --include-ignored cap_posix_pty__ && scripts/rust/cargo_nonzero.sh -p sw-pal --features test-fakes --test console_conformance && python3 scripts/rust/check_cfg_boundary.py`；orchestrator 另執行 `scripts/rust/win-test.sh --package sw-pal --features test-fakes --test console_conformance`
Expected: Linux 全部 PASS；Windows 報告非 `windows_com` 案例 PASS（`passed` 等於預期的非 cap 測試數）、`cap_windows_com__*` 為 ignored 並標「證據不足」；cfg 檢查 exit 0。

- [ ] **Step 5：commit**

```bash
git add crates/sw-pal/ scripts/rust/win-runner.sh docs/rust-refactor/plans/rust-architecture.md tests/acceptance/ changelog.d/rust-s22-pal-console.md
git commit -m "feat(rust-pal): 新增 PTY／TCP console、ConsoleMux 與選配 com0com console" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

#### parity-amendments-2026-10-26 追加（Python 1:1 承接）

> 來源：`docs/rust-refactor/plans/parity-amendments-2026-10-26.md`（稽核 A／B 與 P6 審查）。依 OD-14 #1（外部可觀察行為與 Python 1:1，只可新增、不可修改），下列為本 Task 的**必要**測試列與完成條件：紅燈先行，新增 nodeid 於本 Task 紅燈 commit 以 P2 `regen_lock.py --l1-add-only` 登記到所列集合；斷言值以 Python 基線實測為準（不得自訂）。

| # | 稽核來源 | 測試／要求 | 精確斷言或完成條件 |
|---|---|---|---|
| #40／#64 | B:E-109 | 步驟 2 console reaper／`held_slave_paths()` | 必須使用 06 Task 2 #64 的 PAL `same_tty`（`(st_dev,st_ino)` 身分；devpts 不以 rdev 判定），跨 mount namespace 同名 `/dev/pts/N` 不計為 peer；移植 `TestUartBridgeHeldPathsNamespace` 5 案為 `console_reaper_ignores_other_namespace_same_name_pts` 等（`uart_io.py:1430-1471`） |
| #109 | B:E-15 | `tcp_accept_sets_nodelay_and_label_shape` | TCP accept 設 `TCP_NODELAY`；label／`vtty` 形狀照 `uart_io.py:1327-1344`（`tcp://127.0.0.1:<port>#<n>` 等以 Python 實測為準） |

---

## Task 9：ConsoleHub runtime 與 RX fan-out

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s22-console-hub.md`（`type: feat`、`scope: rust-console`：「每 session 一條不阻塞的 ConsoleHub thread：RX fan-out、Telnet 協商、peer 掃描與跨重連保留 console」）。
**BLOCKED-OWNER evidence:** 實板長時 RX 洪流下慢速 minicom 不拖累 WAL／agent（realhw `p1-con-fanout`；L3 `lr-mixed` 屬 Slice 2.4）；Console 按鍵至回顯 SLO 的實體 loopback 量測（BLOCKED-OWNER 總表 #7）。

**Files:**
- Modify: `crates/sw-core/src/human_console/hub.rs`
- Modify: `crates/sw-core/src/uart_bridge.rs`（reader：WAL append 後 `try_send` 到 hub，再進 data lane；`ReaderCtx.sentinel` 一律 `None`，增補 #21）
- Modify: `crates/sw-core/src/actor.rs`（執行 Task 6／7 的 console effects；建立 primary 哨兵 console；啟停 hub）
- Modify: `crates/sw-core/tests/rx_reader.rs`（06 的 `sentinel_receives_raw_bytes_and_eagain_drops`、`sentinel_input_discarded` 改寫為 `reader_sends_to_hub_after_wal`、`reader_hub_full_does_not_block`）
- Modify: `crates/sw-core/tests/support/hub.rs`（Task 1 簽章的本體）
- Create: `crates/sw-core/tests/console_hub.rs`、`crates/sw-core/tests/console_echo_latency.rs`

**Interfaces:**
- Consumes：Task 2 `TelnetFilter`、`escape_iac`、`TELNET_GREETING`；Task 5 `OutRing`；Task 8 `ConsoleMux`、`MuxHandle`、`ConsoleTransportFactory`；Task 6 事件與 effects；06 `Mailbox` 的 control／data sender、`Clock`、`ReaderCtx`。
- Produces：

```rust
// crates/sw-core/src/human_console/hub.rs
pub enum HubCmd {
    AdoptPty { client_id: String, client_lifetime: u64, label: String, pair: PtyPair },
    StartListener,                                             // TcpTelnet：bridge 起來時
    StopListener,                                              // bridge 停止時（ephemeral port，UIO-12.8-c）
    OpenCom0com { port: String },                              // D-12
    Out { client_id: String, item: OutItem },
}
#[derive(Default)] pub struct HubCritical { pub close: Vec<String> /* ≤ console 數 */, pub close_all: bool, pub shutdown: bool }
pub struct HubHandle { pub cmd: crossbeam_channel::Sender<HubCmd> /* bounded(CONSOLE_HUB_CTRL_QUEUE) */,
                       pub rx: crossbeam_channel::Sender<Arc<[u8]>> /* bounded(CONSOLE_HUB_RX_QUEUE) */,
                       pub critical: Arc<Mutex<HubCritical>>, pub waker: Arc<dyn IoWaker>, pub status: Arc<HubStatus>,
                       pub input_epoch: Arc<AtomicU64> /* = CoreDeps.input_epoch_pub */ }
impl HubHandle { pub fn close(&self, cid: &str); pub fn close_all(&self); pub fn shutdown(&self); }   // 寫 critical＋wake，永不阻塞
pub struct HubStatus { pub endpoint: Mutex<Option<String>>, pub listener_up: AtomicBool, pub rx_dropped_chunks: AtomicU64, pub out_dropped: AtomicU64 }
impl HubStatus { pub fn vtty_alive(&self, primary_vtty: Option<&str>, fs_exists: &dyn Fn(&str) -> bool, kind: ConsoleKind) -> bool; }
   // POSIX：primary 的 vtty 路徑存在；TcpTelnet：listener_up（06 §7.7）
pub fn spawn_hub(com: String, platform: &Platform, control: ControlSender, data: DataSender, clock: Arc<dyn Clock>,
                 input_epoch: Arc<AtomicU64>) -> HubHandle;    // thread 名 "serialwrap-console-<COM>"
```

- [ ] **Step 1：寫失敗測試**

`crates/sw-core/tests/console_hub.rs`（`#![cfg(feature = "test-fakes")]`；`HubHarness`（Task 1）；`FakeConsoleFactory` 可注入「每次只吃 n bytes」「永遠 EAGAIN」的 io；FakeClock）：

```rust
#![cfg(feature = "test-fakes")]
mod support;
use std::{sync::Arc, time::{Duration, Instant}};
use support::hub::*;
use sw_core::human_console::{hub::*, ConsoleTransportKind};
use sw_core::mailbox::{ControlEvent, DataEvent};
use sw_proto::constants::{telnet::TELNET_GREETING, CONSOLE_OUT_RING_BYTES};

#[test]
fn greeting_written_before_any_fanout_bytes() {
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    let client = t.fake.connect();                         // fake accept
    for _ in 0..50 { assert!(t.reader_push(b"RXDATA")); }
    t.settle();
    let got = client.written();
    assert_eq!(&got[..12], &TELNET_GREETING[..]);
    assert!(got[12..].starts_with(b"RXDATA"));
    assert!(matches!(&t.control_events()[..], [ControlEvent::ConsoleConnected { kind: ConsoleTransportKind::TcpTelnet, .. }]));
}

#[test]
fn slow_console_does_not_block_reader_or_other_console() {
    let t = HubHarness::pty("COM0");
    let slow = t.adopt_pty_with(FakeIo::stalled());        // try_write 永遠 Ok(0)
    let fast = t.adopt_pty_with(FakeIo::unbounded());
    let start = Instant::now();
    for _ in 0..384 { t.reader_push(&[b'x'; 8192]); }      // 3 MiB；reader 側一律 try_send
    assert!(start.elapsed() < Duration::from_millis(200), "reader 不得因 console 背壓阻塞");
    t.settle();
    assert_eq!(fast.written_len(), 384 * 8192 - t.status().rx_dropped_chunks.load(std::sync::atomic::Ordering::Relaxed) as usize * 8192);
    assert!(t.ring_display_bytes(&slow.client_id()) <= CONSOLE_OUT_RING_BYTES);
}

#[test]
fn full_mailbox_pauses_reading_without_blocking_hub() {   // D-17(1)
    let t = HubHarness::tcp("COM0");
    t.hub.cmd.try_send(HubCmd::StartListener).unwrap();
    let c = t.fake.connect();
    t.block_mailbox(true);                                  // data lane try_send 一律 Full
    for _ in 0..200 { c.push_input(b"k"); }
    let start = Instant::now();
    t.hub.close_all();                                      // critical 旁路仍被處理
    t.settle();
    assert!(start.elapsed() < Duration::from_millis(500));
    assert!(c.closed());
    assert!(t.data_events().len() <= 64, "pending 有界");
}
```

| 測試名 | 情境 | 期望 |
|---|---|---|
| `telnet_reply_sent_even_in_flash` | TCP client 送 `IAC WILL NAWS` | client 收到 `IAC DONT NAWS`；filter 後無資料 → 不送 `ConsoleInputBytes` |
| `telnet_input_filtered_before_lane` | 送 `b"ls\r\n"` | data lane `ConsoleInputBytes{bytes:b"ls\r"}` |
| `input_tagged_with_epoch_snapshot_before_wait` | `input_epoch` 設 7 → client 送 `a` → 在 hub 已取快照、尚未讀取時把 epoch 改 8 → 讀出 | 事件 `input_epoch==7`（快照在 `mux.wait` 之前，D-17(5)；core 會把它當 stale 丟棄） |
| `display_escaped_for_telnet_only` | RX `[0xFF,0x01]`，一個 TCP、一個 PTY | TCP 得 `[0xFF,0xFF,0x01]`、PTY 得 `[0xFF,0x01]` |
| `echo_out_escaped_for_telnet` | `HubCmd::Out{Display(b"\xff")}` 給 TCP client | 寫出 `[0xFF,0xFF]` |
| `tcp_eof_emits_disconnected_on_data_lane` | client 先送 `b"ab\r"` 再關閉 | data lane 依序為 `ConsoleInputBytes{b"ab\r"}`、`ConsoleDisconnected{client_id,lifetime}`（D-17(3)） |
| `pty_io_error_emits_disconnected` | fake PTY read 回 `Err(Io)` | 同上（Python `_drop_console_client` 路徑） |
| `peer_scan_every_3s` | `clock.advance(1 s)` 九次，每次後 `settle()` | 恰 3 個 `ControlEvent::ConsoleScan{source: Hub}`，掃描時刻為 3.0／6.0／9.0 s（hub 的掃描到期一律以注入的 Clock 計算） |
| `listener_restarted_with_new_port_per_bridge` | Start→Stop→Start | 兩次 endpoint port 不同；既有 TCP client 保留、其 `vtty` 仍為舊 endpoint 字串（UIO-12.8-c） |
| `consoles_survive_conn_epoch_change` | adopt c1；模擬 IoWorker 重建（06 的 epoch 變更） | c1 仍在、slave_path 不變、無 close |
| `close_all_on_release` | `hub.close_all()` | 所有 fake io `closed()==true` |
| `critical_close_when_cmd_queue_full` | 先塞滿 64 筆 `Out`；`hub.close("c1")` | c1 仍被關閉；之後的 `Out` try_send 失敗只增加 `out_dropped` |
| `no_peer_pty_display_cleared_after_grace` | PTY 無 holder、attach 後 2.5 s 的 peer scan | 該 console ring Display 清空（D-05），Control 保留 |
| `control_overflow_closes_console` | TCP client 只送不讀，連送 30000 次 `IAC WILL NAWS`（每次回 3 bytes） | Control 超過 65536 bytes 時該 console 被關閉、data lane 有 `ConsoleDisconnected`、WARNING 一筆；其他 console 不受影響 |
| `telnet_accept_paused_at_32` | 連入 33 個 client | 前 32 個收到 greeting；第 33 個在任一 console 關閉前未被 accept；關閉一個後被 accept；WARNING 一筆 |
| `hub_rx_queue_full_drops_and_counts` | hub 卡住不消費，reader `try_send` 300 次 | `rx_dropped_chunks==300-256`；WARNING 一筆（每次由未滿轉滿一筆） |
| `loop_quota_bounds_drain` | `rx` channel 持續有資料時同時送 `close_all` | `close_all` 在 2 輪迴圈內生效（每輪每 channel 最多 64 筆，D-17(2)） |
| `com0com_open_failure_logs_and_continues` | `fake.fail_next_com0com()`；`OpenCom0com` | WARNING（D-12 模板），hub 續行 |
| `vtty_alive_semantics` | PTY primary 路徑存在／不存在；TCP listener up／down | true／false／true／false |

`crates/sw-core/tests/rx_reader.rs`（06 檔案；增補 #21）：`reader_sends_to_hub_after_wal`（每個 chunk 先有 WAL 紀錄，hub `rx` channel 才收到同一 bytes）、`reader_hub_full_does_not_block`（hub `rx` 滿時 reader 仍在 10 ms 內把下一 chunk 送進 data lane）。

`crates/sw-core/tests/console_echo_latency.rs`（`ActorRig`＋FakeSerial loopback：DUT 端收到的每個 byte 立即回送為 RX）：PTY c1 為 raw owner，送 500 個可識別按鍵（每個間隔 5 ms），量測「寫入 c1 master」到「c1 master 讀回同一 byte」的延遲；斷言 p99 < 10 ms（規格 §1.2 SLO 的 in-process 回歸門檻；實體 115200 loopback 屬 BLOCKED-OWNER #7）。另在同一檔加 `console_input_flood_keeps_control_latency`：c1 以 1 MiB/s 送入時，`get_state` 的 control 事件延遲 p99 ≤ 20 ms（arch §5.2 目標值，比照 06 T21 的量測方法）。

- [ ] **Step 2：執行並確認 FAIL**

Run: `scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test console_hub --test rx_reader --test console_echo_latency`
Expected: FAIL（`not yet implemented: P6 Task 9`；`rx_reader` 新案例斷言失敗）。

- [ ] **Step 3：實作**

1. Hub 迴圈：(a) 取 `epoch_snapshot = input_epoch.load(Acquire)`（D-17(5)）；(b) `mux.wait(min(下一次 peer scan 剩餘（以注入的 Clock 計算）, pending 非空 ? 10 ms : 200 ms))`；(c) 先處理 `critical`；(d) `cmd` 與 `rx` channel 各最多處理 `CONSOLE_HUB_LOOP_QUOTA` 筆（`rx`：每 chunk 對每個 console，Telnet → `Display{escape_iac(chunk), iac_escaped:true}`，PTY／com0com → `Display{chunk, false}`）；(e) 處理 mux 事件；(f) 把 `pending` 依 FIFO 以 `try_send` 送出，送不出就停；(g) 對 ring 非空者 `try_write(front)`→`consume(n)`，仍非空則 `set_write_interest(id,true)`。
2. Accept（TcpTelnet）：console 數已達 `TELNET_CONSOLES_PER_SESSION` 時，listener 的讀取 interest 關閉並寫 WARNING，數量降下時恢復。`client_id = uuid4_hex()[..12]`；`ring.push(Control(TELNET_GREETING))` 並**立即** `try_write`（在把該 client 加入 fan-out 集合之前），再加入集合；送 control lane `ConsoleConnected{label: peer, vtty: endpoint 或 peer, kind: TcpTelnet}`（ownership 由 core 的 `on_stream_accept` 處理）。
3. 輸入：`pending` 為空時才讀取 console io（否則暫停讀取，背壓留在 OS）；`mux.read`（`SERIAL_READ_CHUNK=8192`）；Telnet client 先 `TelnetFilter::feed`，reply 非空就 `push(Control(reply))`，Control 累計超過 `CONSOLE_CONTROL_MAX_BYTES` 時關閉該 console（D-05）；filter 後 bytes 非空時排入 `pending`：`DataEvent::ConsoleInputBytes{client_id, client_lifetime, input_epoch: epoch_snapshot, bytes}`。`Eof`／`Err` → `remove`，並把 `DataEvent::ConsoleDisconnected` 排入同一個 `pending`（與輸入同序）。`pending` 達 `CONSOLE_HUB_PENDING_EVENTS` 時停止讀取，並寫一筆 WARNING。
4. Peer scan：每 `HUMAN_PEER_GRACE_S` 呼叫 `platform.console.held_slave_paths()` → control lane `ConsoleScan{scan, source: Hub}`（經 `pending`）。core 在此事件內做 reap（06 §7.5 `reap_stale_consoles` 規則：非 internal、PTY、非 owner／suspended、已滿 `STALE_CONSOLE_GRACE_S`、不在 held → `ConsoleClose`＋`deferred.remove_client`＋primary 轉移給 `consoles` 的第一個）與 self-heal（Task 7）；`held=None` 時視為全部仍被持有（不誤剪）。hub 對 PTY console 依同一次掃描執行 D-05 的 Display 清空。
5. 哨兵 primary：session 第一次有 bridge、且沒有任何保留 console 時，actor 以 `create_pty()` 建 `label="primary"`、`internal=true` 的 console（TcpTelnet kind 不建，06 §7.2），以 `AdoptPty` 交給 hub，core 端 `primary_client_id` 設為它。06 reader 不再持有哨兵（`ReaderCtx.sentinel: None`）。
6. actor 對 hub：`ConsoleOut` → `cmd.try_send(Out)`，`Full` 時丟棄並 `out_dropped += 1`；`ConsoleClose`／`ConsoleCloseAll`／shutdown → `HubHandle::close`／`close_all`／`shutdown`（寫 critical＋wake，不經 channel）。
7. `uart_bridge.rs` reader：順序固定為 `WalWriter::append(RX)` → `hub.rx.try_send(Arc<[u8]>)`（`Full` → `status.rx_dropped_chunks += 1`，每次由未滿轉滿寫一筆 WARNING）→ `hub.waker.wake()` → data lane `UartRxChunk`（06 §4.1 的 WAL→console 順序；P8 的 flash observer 插在 WAL 與 console 之間）。

- [ ] **Step 4：執行並確認 PASS**

Run: `scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes --test console_hub --test rx_reader --test console_echo_latency && scripts/rust/cargo_nonzero.sh -p sw-core --features test-fakes`
Expected: PASS。

- [ ] **Step 5：commit**

```bash
git add crates/sw-core/src/human_console/hub.rs crates/sw-core/src/uart_bridge.rs crates/sw-core/src/actor.rs crates/sw-core/tests/ changelog.d/rust-s22-console-hub.md
git commit -m "feat(rust-console): 新增不阻塞的 ConsoleHub thread、RX fan-out 與 peer 掃描" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

#### parity-amendments-2026-10-26 追加（Python 1:1 承接）

> 來源：`docs/rust-refactor/plans/parity-amendments-2026-10-26.md`（稽核 A／B 與 P6 審查）。依 OD-14 #1（外部可觀察行為與 Python 1:1，只可新增、不可修改），下列為本 Task 的**必要**測試列與完成條件：紅燈先行，新增 nodeid 於本 Task 紅燈 commit 以 P2 `regen_lock.py --l1-add-only` 登記到所列集合；斷言值以 Python 基線實測為準（不得自訂）。

| # | 稽核來源 | 測試／要求 | 精確斷言或完成條件 |
|---|---|---|---|
| #37 | B:E-18 | `hub_mux_wait_error_backoff_20ms_then_rebuild` | `mux.wait` 回 Err：stop 已設 → 離開；否則 20 ms 退避後重整 fd 集合、不結束 hub（`uart_io.py:1386-1390`） |
| #89 | B:E-19 | `console_rx_handler_panic_isolated` | 單一 console 處理 panic 寫 WARNING、其他 console 照常 |
| #110 | B:E-117、E-118、E-119 | 既有 SC-31 列 | Python 側無測試；Rust 依 SC-31(a)(c)(d) 只驗絕對契約（`absolute_only`），A/B 比較器排除；不另立 Python 特性測試 |

---

## Task 10：RPC 表面、LeaseIndex 路由與 SessionPublic 欄位

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s22-console-rpc.md`（`type: feat`、`scope: rust-daemon`：「Rust daemon 承接 session.console_attach／console_detach／console_list，interactive 方法改經 LeaseIndex 路由」）。
**BLOCKED-OWNER evidence:** 無。

**Files:**
- Modify: `crates/sw-daemon/src/service.rs`（新增 3 個 console 分支；06 已有的 4 個 interactive 分支改經 `route_interactive`；位置與參數讀取順序照 service.py:820-869）
- Modify: `crates/sw-core/src/session_manager.rs`（selector 路由、`LeaseIndex`、`console_scan_snapshot`、`create_pty` 前置與 `ConsoleAttachReq` envelope）
- Modify: `crates/sw-core/src/actor.rs`（`ConsoleAttachReq` 拆 envelope：`PtyPair` 存入 executor 的 `pending_pty: HashMap<u64, PtyPair>`，core 只收 `PtyInfo`；`ConsoleAdopt` → `HubCmd::AdoptPty`，`ConsoleDiscard` → drop `PtyPair`）
- Modify: `crates/sw-core/src/session_core.rs`（`PublicCtx` 由 `HumanConsoleState` 產生；`vtty_cache` 更新點，D-20）
- Modify: `crates/sw-proto/src/config_types.rs`（`windows.serial_console`，D-12）
- Modify: `crates/sw-core/src/lib.rs`（`pub const SLICE: &str = "2.2"`）
- Modify: `crates/sw-daemon/tests/support/mod.rs`（`deferred_methods(yaml, delivered)`、`ServiceRig::with_console_backend`、`uart_writes`）、06 的 `deferred_methods_return_method_not_found_without_side_effects` 測試（改傳 `sw_core::SLICE`）、`tests/contract_blackbox/slice21/test_slice_scope.py`（`test_deferred_methods`、`test_deferred_methods_windows` 只檢查 `slice` 晚於 `"2.2"` 的列）（增補 #24；`tests/acceptance/slice21_scope.yaml` 不改）
- Create: `crates/sw-daemon/tests/rpc_console.rs`

**Interfaces:**
- Consumes：Task 6／7 的 `SessionRequest` 新變體與回應；06 `SessionManager` selector 路由、`Params`、`pydict!`、`SessionCore::public(now, &PublicCtx)`；P3 `RuntimeConfig`。
- Produces：

```rust
// crates/sw-daemon/src/service.rs（dispatch 內，與 Python 同序）
"session.console_attach"     => self.console_attach(p),     // selector 鏈 → label=py_str 若 truthy 否則 None → selector 空 → INVALID_ARGS
"session.console_detach"     => self.console_detach(p),     // selector 鏈、client_id=str(… or "") → 任一空 → INVALID_ARGS
"session.console_list"       => self.console_list(p),
"session.interactive_open"   => self.interactive_open(p),   // 06 既有；selector、owner=str(… or "agent")、timeout_s=float(… or 60.0)、command=str(… or "")、
                                                            // allow_attached=py_truthy(get("allow_attached", false)) → selector 空 → INVALID_ARGS
"session.interactive_send"   => self.interactive_send(p),   // 06 既有；改經 route_interactive
"session.interactive_status" => self.interactive_status(p), // 06 既有；screen_chars=int(… or 2048)；改經 route_interactive
"session.interactive_close"  => self.interactive_close(p),  // 06 既有；改經 route_interactive
// crates/sw-core/src/session_manager.rs
impl SessionManager {
    pub fn console_scan_snapshot(&self) -> ConsoleScanSnapshot;                       // RPC handler thread 呼叫（D-09）
    pub fn route_interactive(&self, interactive_id: &str) -> Option<SessionRef>;      // LeaseIndex（含 tombstone）
    pub fn send_with_scan(&self, s: &SessionRef, req: SessionRequest, timeout: Duration) -> Result<PyDict, PyErr>;   // 同 sender 先送 ConsoleScan 再送 request
}
// crates/sw-proto/src/config_types.rs
pub struct SerialConsoleMap(pub IndexMap<String /* DUT by_id */, SerialConsolePair>);
pub struct SerialConsolePair { pub console_port: String, pub peer_port: String }
pub struct RuntimeConfig { …, pub windows_serial_console: Option<SerialConsoleMap> }
```

- [ ] **Step 1：寫失敗測試**

`crates/sw-daemon/tests/rpc_console.rs`（`#![cfg(feature = "test-fakes")]`；in-process `ServiceRig`；`call(method, params_json)` 回 `PyDict`，以 `RPC_WIRE` 比對；不含 `id`）：

| 測試名 | 請求 | 期望 wire（`<S>`＝當下 SessionPublic JSON、`<V>`＝version） |
|---|---|---|
| `attach_invalid_args` | `console_attach {}` | `{"ok":false,"error_code":"INVALID_ARGS","version":"<V>"}` |
| `attach_selector_alias_chain` | `{"alias":"dut"}` | ok（`selector→session_id→com→alias` 鏈，02 §5.3） |
| `unknown_selector_shapes` | `console_attach`／`console_detach`（帶 client_id）／`console_list`／`interactive_open` 的 selector 為 `"COM9"` | 四者皆 `{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM9","version":"<V>"}`（`session_manager.py:3947`、`3978`、`4002`、`4020`；不走 06 generic resolve 的 `SESSION_NOT_FOUND`） |
| `attach_label_coerced` | `{"selector":"COM0","label":5}` | `"label":"5"` |
| `attach_label_falsy_uses_client_id` | `{"selector":"COM0","label":""}` | `label == client_id` |
| `attach_pty_shape_first` | 首個 PTY console | `{"ok":true,"client_id":"<12hex>","label":…,"vtty":"/dev/pts/N","interactive_session_id":"<32hex>","interactive_owner":true,"session":<S>,"version":"<V>"}` |
| `attach_pty_shape_second` | 第二個 | 無 `interactive_session_id`／`interactive_owner` 兩鍵 |
| `attach_then_send_has_no_false_not_found` | attach 後立即以回傳的 `interactive_session_id` 呼叫 `interactive_status` | ok（LeaseIndex 在 Reply 前更新） |
| `attach_interactive_owner_false_when_fg_busy` | 前景忙時 attach | `"interactive_owner":false`、lease 仍開（owner 進 pending） |
| `attach_interactive_owner_false_during_replay_hold`（R2-F4，D-22(2)） | 前景命令剛結束、其 HumanReplay 以 FakeDevice `PauseBeforeSubmit` 停在 OS 提交前（回放 hold 存在、`foreground` 已清）時 attach；之後 `release_pause()` | 回應與上列相同：`"interactive_owner":false`、lease 仍開（owner 進 pending），因 Python token 在回放完成後才清；`release_pause()` 且回放結算後 `session.get_state` 的 `foreground_busy==false` |
| `attach_tcp_shape` | `ServiceRig::with_console_backend("tcp")` | `{"ok":true,"client_id":null,"label":null,"vtty":"127.0.0.1:P","endpoint":"127.0.0.1:P","transport":"tcp","protocol":"telnet","session":<S>,"version":"<V>"}`，不開 lease |
| `attach_not_ready_states` | DETACHED／RECOVERING／RELEASED／FLASHING／ATTACHING | `{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM0","version":"<V>"}`（Python 只接受 READY／ATTACHED）；`uart_writes()` 不變 |
| `attach_rejected_discards_pty` | 不合格狀態 | fake factory 記錄的 PTY 已關閉（`ConsoleDiscard` → drop） |
| `attach_request_timeout_closes_pty` | actor 卡住使 control lane 送出逾時 | RPC 回 `timeout` 類錯誤；`PtyPair` 隨 `SendTimeoutError` 回到 handler 並被 drop（fd 不洩漏） |
| `detach_missing_client_id` | `{"selector":"COM0"}` | `INVALID_ARGS` |
| `detach_unknown_client` | `client_id:"nope"` | `{"ok":false,"error_code":"CONSOLE_NOT_FOUND","client_id":"nope","version":"<V>"}` |
| `detach_unknown_client_still_closes_own_lease` | c1 持 human lease；c1 的 io 已斷線（console 已移除、lease 尚在）；detach c1 | `CONSOLE_NOT_FOUND`，且 session `interactive_session_id` 變 null（ERR-7.5-b） |
| `detach_ok_closes_own_lease` | detach c1 | `{"ok":true,"client_id":"c1…","session":<S>,…}`；`interactive_session_id` 變 null |
| `list_and_detach_without_bridge` | DETACHED | `console_list`／`console_detach` 皆 `{"ok":false,"error_code":"SESSION_NOT_READY","selector":"COM0","version":"<V>"}` |
| `list_sorted_by_label_then_id` | 三個 console（含哨兵 `primary`） | `consoles` 依 `(label, client_id)` 排序，每列 `{"client_id","label","vtty","interactive_owner"}` |
| `list_prunes_stale_without_peer` | 非 primary、>2.0 s、無 holder | 不出現在列表（RPC 前的 `ConsoleScan` 生效） |
| `open_coercion_float_error_before_invalid_args` | `{"timeout_s":"x"}`（無 selector） | `{"ok":false,"error_code":"EXCEPTION","message":…}`（無 version） |
| `open_timeout_zero_uses_default` | `timeout_s:0` | lease `timeout_s==60.0` |
| `open_allow_attached_string_false_is_truthy` | `allow_attached:"false"` | 視為 true（`bool("false")`） |
| `send_routes_by_lease_index` | 兩 session 各一 lease | send 到正確 session（WAL TX com 正確） |
| `status_screen_chars_coerced` | `screen_chars:"10"` | screen 最多 10 字元 |
| `status_screen_chars_negative` | `-3` | 回 `rx_text` 去掉開頭 3 字元的結果（`tail_py`，02 §11 H-08） |
| `status_not_in_retry_whitelist` | `Method::parse("session.interactive_status").is_retryable_readonly()` | false；`session.console_list` 為 true |
| `session_public_vtty_is_cached` | attach h1（`vtty` 為 None → 設為 h1）；attach h2 | `session.list` 的 `vtty` 仍是第一個設定值，不隨 h2 改變；detach 後改為 `bridge.vtty_path`（D-20） |
| `session_public_console_fields_pty` | `session.list` | `console_count` 含哨兵；無 `console_endpoint` 鍵 |
| `session_public_console_fields_tcp` | tcp backend `session.list` | 有 `console_endpoint`（位置在 `drain_timeout_epoch` 之後、最後一個 session 欄位） |
| `serial_console_ports_excluded` | config `windows.serial_console {"COM5":{"console_port":"COM40","peer_port":"COM41"}}` | `DeviceSelection.exclude_coms` 含 `COM40`、`COM41` |
| `serial_console_bad_shape_ignored` | `windows.serial_console: [1,2]` | 整個鍵被忽略、WARNING 一筆，其他設定照常 |
| `console_methods_no_longer_deferred` | 06 deferred 測試以 `delivered="2.2"` 執行 | `session.console_*` 不再回 `METHOD_NOT_FOUND`；`event.*`、`file.*` 仍回 |

- [ ] **Step 2：執行並確認 FAIL**

Run: `scripts/rust/cargo_nonzero.sh -p sw-daemon --features test-fakes --test rpc_console`
Expected: FAIL（`METHOD_NOT_FOUND` 或 `not yet implemented: P6 Task 10`）。

- [ ] **Step 3：實作**

1. 三個新分支與四個既有分支，照 service.py:820-869 的參數讀取順序：先完成所有轉型（任何 `PyErr` 直接成為 framing `EXCEPTION`），再檢查 `INVALID_ARGS`。selector 查無 session 時，這四個方法（`console_attach`、`console_detach`、`console_list`、`interactive_open`）回 `SESSION_NOT_READY{selector}`，**不走** 06 的 generic resolve。
2. session 類：RPC handler thread 先 `console_scan_snapshot()`，經 `send_with_scan` 在同一個 sender 上先送 `ControlEvent::ConsoleScan{source: Rpc}`，再送 request（D-09）；`session.list`／`get_state`／`activity`／`health.status` 的 session 摘要同樣先送 scan。`console_attach` 在 `ConsoleKind::Pty` 時先 `create_pty()`，以 `ControlEvent::ConsoleAttachReq{label, pty: Some(pair), reply}` 送出（送出逾時時 `PtyPair` 隨錯誤回來並被 drop）；actor 層把 `pair` 存進 `pending_pty[token]`，以 `SessionRequest::ConsoleAttach{label, pty: Some(PtyInfo{token, slave_path})}` 交給 core；core 回 `ConsoleAdopt` 時交給 hub、回 `ConsoleDiscard` 時 drop。`interactive_send／status／close` 以 `route_interactive(id)` 找 session；查無時回 `{"ok":false,"error_code":"INTERACTIVE_NOT_FOUND","interactive_id":…}`。
3. `PublicCtx`：`console_count` 為該 session 目前 console 數（已依 RPC 前的 scan prune）；`vtty` 取 `HumanConsoleState.vtty_cache`，依 D-20 的更新點維護，無 bridge 時取保留 console 的 primary；`console_endpoint` 鍵只在 `ConsoleKind::TcpTelnet` 或 hub 有 endpoint 時輸出（02 §6.1、`tests/test_session_console_endpoint.py`）。
4. `config_types.rs`：`windows.serial_console` 以 `yaml11` 讀入；非 mapping 或缺欄位時忽略整個鍵並寫 WARNING（不影響其他設定）；`DeviceSelection` 組裝時把所有 `console_port`／`peer_port` 加入 `exclude_coms`。
5. 增補 #24：`sw_core::SLICE = "2.2"`；`support::deferred_methods(yaml, delivered)` 只回 `slice > delivered` 的列；`tests/contract_blackbox/slice21/test_slice_scope.py` 的兩個 deferred 測試改為依 `bb_harness.is_rust()` 時只檢查 `slice > "2.2"` 的列。

- [ ] **Step 4：執行並確認 PASS**

Run: `scripts/rust/cargo_nonzero.sh -p sw-daemon --features test-fakes --test rpc_console && scripts/rust/cargo_nonzero.sh --workspace --features test-fakes`
Expected: PASS；06 的 sw-daemon 測試除增補 #24 的期望調整外無新失敗。

- [ ] **Step 5：commit**

```bash
git add crates/sw-daemon/ crates/sw-core/src/ crates/sw-proto/src/config_types.rs tests/contract_blackbox/slice21/test_slice_scope.py changelog.d/rust-s22-console-rpc.md
git commit -m "feat(rust-daemon): 承接 console RPC 並以 LeaseIndex 路由 interactive 方法" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 11：黑箱契約與既有測試改指 Rust（Linux L1.5）

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s22-console-blackbox.md`（`type: change`、`scope: contract-blackbox`：「新增 console／lease／Telnet 黑箱契約，可對 Python 與 Rust artifact 執行」）。`type` 必須是 `TYPE_TO_SECTION` 的鍵，`test` 不在其中，`collate` 會丟 `FragmentError`。
**BLOCKED-OWNER evidence:** 同一批黑箱在 Windows host 對 Python 與 Rust daemon 的 A/B（BLOCKED-OWNER 總表 #1，Task 12）；L2 F5／F6／F8 實機版本（#4）。

**Files:**
- Create: `tests/contract_blackbox/slice22/__init__.py`、`tests/contract_blackbox/slice22/conftest.py`（fixture `console_daemon`、`unready_daemon`、`telnet_daemon`；以 06 的 `slice21_daemon_factory` 與 P3 `bb_faketarget.FakeTarget`（06 T15a 擴充版）為基礎）
- Create: `tests/contract_blackbox/slice22/console_helpers.py`（stdlib only；只放 console 專用小工具）
- Create: `tests/contract_blackbox/slice22/test_console_pty.py`、`test_console_telnet.py`、`test_interactive_lease.py`、`test_console_bc02.py`、`test_slice22_family_mirrors.py`
- Modify: `tests/contract_blackbox/bb_faketarget.py`（P3／06 T15a `FakeTarget` 追加兩個 keyword-only 選用能力：`scripts: dict[bytes, tuple[float, bytes]]`（收到指定行時延遲回覆）、`ready: bool = True`（False 時不回 ready nonce）、`hold_reply(on: bool)`（暫停所有回覆，用來把 session 卡在 RECOVERING／ATTACHING）；預設行為不變。**測試資產變更，依規格 §10.4 需獨立審閱**）
- Modify: `tests/test_human_agent_coexist.py`、`func-test/lib/daemon_harness.py`、`func-test/lib/cli_client.py`（只在 P2／P3 尚未讀 `SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE` 時依 arch §9.3 加入，預設值不變）

**Interfaces:**
- Consumes：P3 `tests/contract_blackbox/bb_harness.py`（`is_rust`、`isolated_env`、`run_cli`、`daemon_prefix`、`cli_prefix`）與 `conftest.py` 的 `rust_only`、`cap_posix_pty` marker；P3 `bb_faketarget.FakeTarget`（06 T15a：`slave_path`、`start`、`stop`、`written`、`emit`）、`tools/uart_stdio_bridge` scenario runner；06 `tests/contract_blackbox/slice21/harness.py`（`slice21_daemon_factory`、`Slice21Daemon.sw(...)`、WAL 讀取）；env `SERIALWRAP_CLI_EXE`、`SERIALWRAP_DAEMON_EXE`、`SERIALWRAP_AB_LABEL`；Task 8 的 SC-28 env。
- Produces（`tests/contract_blackbox/slice22/console_helpers.py`）：

```python
def open_vtty(path: str) -> int: ...                    # os.open(O_RDWR|O_NOCTTY|O_NONBLOCK)
def read_for(fd: int, seconds: float) -> bytes: ...
def telnet_connect(endpoint: str) -> socket.socket: ...
def b64_payload(record: dict) -> bytes: ...            # base64.b64decode(record["payload_b64"])
def tx_payload(wal: list[dict], source: str) -> bytes: ...   # 同 source 的 TX payload 依 seq 串接
def wait_state(d, selector: str, state: str, timeout_s: float) -> None: ...   # 輪詢 session list
def wait_rx_contains(target, needle: bytes, timeout_s: float) -> bool: ...    # 對 FakeTarget.written()
```

- [ ] **Step 1：寫失敗測試**

```python
# tests/contract_blackbox/slice22/test_console_pty.py（節錄；其餘依下表）
from __future__ import annotations

import os
import re
import time

import pytest

from .console_helpers import b64_payload, open_vtty, read_for, tx_payload, wait_rx_contains

pytestmark = pytest.mark.cap_posix_pty


def test_first_console_raw_second_line_buffered(console_daemon) -> None:
    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    assert a1["ok"] is True
    assert re.fullmatch(r"[0-9a-f]{12}", a1["client_id"])
    assert re.fullmatch(r"/dev/pts/\d+", a1["vtty"])
    assert re.fullmatch(r"[0-9a-f]{32}", a1["interactive_session_id"]) and a1["interactive_owner"] is True
    a2 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h2")
    assert "interactive_session_id" not in a2 and "interactive_owner" not in a2
    rows = d.sw("session", "console-list", "--selector", "COM0")["consoles"]
    assert [r["label"] for r in rows] == sorted(r["label"] for r in rows)
    assert [r["client_id"] for r in rows if r["interactive_owner"]] == [a1["client_id"]]
    fd1 = open_vtty(a1["vtty"])
    os.write(fd1, b"\x1b[A\t")
    assert wait_rx_contains(target, b"\x1b[A\t", 2.0)
    assert tx_payload(d.wal_records(), f"human:{a1['client_id']}") == b"\x1b[A\t"


def test_deferred_bytes_flushed_after_agent_command(console_daemon) -> None:     # f5-deferred-bytes-flushed（#78）
    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0")
    fd1 = open_vtty(a1["vtty"])
    sub = d.sw("cmd", "submit", "--selector", "COM0", "--cmd", "sleep 2", "--cmd-timeout", "8")
    assert sub["ok"] is True
    time.sleep(0.5)
    os.write(fd1, b"X")
    time.sleep(0.5)
    assert b"X" not in target.written(), "agent 命令期間 human 位元組必須進 deferred"
    assert wait_rx_contains(target, b"X", 5.0)
    recs = [r for r in d.wal_records() if r["dir"] in ("TX", "RX")]
    seq_cmd = min(r["seq"] for r in recs if r["dir"] == "TX" and r.get("cmd_id") == sub["cmd_id"])
    seq_ok = min(r["seq"] for r in recs if r["dir"] == "RX" and b"sleep 2: ok" in b64_payload(r))
    seq_x = min(r["seq"] for r in recs if r["dir"] == "TX" and r["source"] == f"human:{a1['client_id']}")
    assert seq_cmd < seq_ok < seq_x
```

`console_daemon` fixture：`FakeTarget(prompt="root@prplOS:/# ", boot_banner="", scripts={b"sleep 2": (2.0, b"sleep 2: ok\r\n"), b"bootcmd": (0.5, b"U-Boot 2023.01\r\n")})` 接到 06 `slice21_daemon_factory`，等 `COM0` 為 READY 後 yield `(daemon, target)`。

黑箱案例表。雙邊案例必須對 Python 基線先 PASS，對 Rust artifact 在實作前 FAIL；標「Rust-only」者加 `@pytest.mark.rust_only`（P3 marker），Python 端為 skipped，報告列為不適用而非 PASS。凡期望為拒絕的案例，另斷言 FakeTarget 的 `written()` 長度不變（§11.3 零 TX）：

| 檔案::測試 | 對照來源 | 關鍵斷言 |
|---|---|---|
| `test_console_pty::test_first_console_raw_second_line_buffered` | `test_interactive_raw` 前兩案、`f5-second-console-linebuffer`、`p1-con-second` | 見上 |
| `test_console_pty::test_second_console_line_runs_after_owner_detached` | `test_agent_defer_tx::test_console_input_is_line_buffered_for_broker_queue` | detach h1 後，h2 寫 `b"echo L2\r"` → h2 讀回 `b"echo L2\r"`；DUT 收到 `b"echo L2\n"`；WAL TX source `human:<h2>` 且 `cmd_id` 非 null |
| `test_console_pty::test_backspace_local_echo` | `test_agent_defer_tx::test_console_input_supports_backspace_and_local_echo` | h2（無 lease 擋）寫 `b"ab\x7fc\r"` → 回顯 `b"ab\x08 \x08c\r"`、DUT 收到 `b"ac\n"` |
| `test_console_pty::test_second_console_blocked_while_human_lease` | 05 §5.1 第 8 步、G-02 | h1 持 lease 時 h2 寫 `b"ls\r"` → 3 s 內 DUT 未收到 `b"ls\n"`；source `human:<h2>` 的 TX 為空 |
| `test_console_pty::test_deferred_bytes_flushed_after_agent_command` | `f5-deferred-bytes-flushed`、`test_interactive_raw::test_human_input_deferred_during_agent_then_flushed` | 見上 |
| `test_console_pty::test_raw_ownership_survives_agent_rounds` | `f5-raw-ownership-survives-agent-rounds` | 5 輪 `cmd submit echo R<n>`，每輪後 console-list 中 h1 `interactive_owner` 為 true；最後寫 `b"\t"` 立即到 DUT |
| `test_console_pty::test_rx_fanned_out_to_all_consoles` | `test_agent_defer_tx::test_rx_is_fanned_out_to_all_consoles`、`p1-con-fanout` | DUT `emit(b"FANOUT\r\n")` → h1、h2 皆讀到 |
| `test_console_pty::test_unread_console_does_not_block` | `test_unread_console_does_not_block_human_line_submission` | h3 從不讀取；DUT 連送 2 MiB 後 `cmd submit echo OK` 在 5 s 內 done |
| `test_console_pty::test_detach_releases_owner` | `test_interactive_raw::test_detach_console_releases_interactive` | detach h1 → session `interactive_session_id` 為 null、console-list 無 owner |
| `test_console_pty::test_detach_unknown_client` | 02 §7.6 | `{"ok":false,"error_code":"CONSOLE_NOT_FOUND","client_id":"nope"}`（另含 version） |
| `test_console_pty::test_invalid_args_and_alias_selector` | 02 §7.6、§5.3 | `rpc("session.console_attach", {})` → `INVALID_ARGS`；`{"com":"COM0"}` ok；selector `COM9` → `SESSION_NOT_READY`＋`selector` |
| `test_console_pty::test_peer_gone_console_pruned` | `f5-console-peer-gone-recycled` | attach h2 從不開啟，2.5 s 後 console-list 不含 h2；h1（開著）保留 |
| `test_console_pty::test_peer_loss_grace_then_teardown` | `test_interactive_raw::test_peer_gone_past_grace_tears_down`、`p1-con-orphan` | h1 寫 `b"a"`（human active）→ 關 fd → open agent → `SESSION_INTERACTIVE_BUSY`；3.5 s 後再 open → ok 且無 `soft_preempted`；h1 不在 console-list |
| `test_console_pty::test_vtty_stable_across_recover` | `test_agent_defer_tx::test_bridge_restart_reuses_existing_console_vttys`、`t5_bind_idempotent_preserves_console` | `session recover --force`（或 `session clear`）回 READY 後，h1 的 `client_id` 與 `vtty` 不變，fd 仍可讀到新 RX |
| `test_console_pty::test_release_closes_consoles_before_reply` | §11.3「舊 worker 交接前不得回覆 release 成功」 | `device release` 回 ok 時：回應 `closed_consoles` 等於 release 前的 console 數；回應回來之後 h1 的 fd 讀到 EOF／`EIO`（不會在回覆後才關）；`session list` 為 `RELEASED` |
| `test_console_pty::test_human_line_interactive_mode_opens_lease` | `test_service_human_console` | 無 lease 時 h2 寫 `b"vim\r"` → console-list 中 h2 `interactive_owner` 為 true |
| `test_console_pty::test_line_overflow_warning` | SC-16 | h2 寫 16390×`a`＋`\r` → DUT 未收到 `aaaa`；daemon stderr 含 h2 的 `client_id` |
| `test_console_pty::test_last_line_before_disconnect_submitted` | D-17(3) | h2 以單次 `os.write(fd, b"echo LAST\r")` 後立即 `os.close(fd)` → DUT 收到 `echo LAST\n` |
| `test_console_pty::test_session_list_has_no_console_endpoint` | `test_session_console_endpoint::test_key_absent_on_pty_platform` | `session list` 每列無 `console_endpoint` |
| `test_console_pty::test_session_vtty_cached` | D-20 | attach h1、h2 後 `session list` 的 `vtty` 等於第一次設定的值 |
| `test_interactive_lease::test_open_send_status_close_shapes` | 02 §7.6 | open → `recovery_mode:false`、無 `soft_preempted`；send `echo I1\n` → `bytes:8`、DUT 收到、WAL TX source `agent`；status → `owner:"agent"`、`status:"active"`、screen 含 `I1`；close ok；再 status → `INTERACTIVE_NOT_FOUND` |
| `test_interactive_lease::test_open_twice_busy` | `p1-con-busy` | 第二次 `SESSION_INTERACTIVE_BUSY`、`interactive_session_id`＝第一個 id |
| `test_interactive_lease::test_soft_preempt_restore_and_replay` | `p1-con-softpreempt`、#53 | h1 未鍵入 → open agent 得 `soft_preempted:true`；h1 寫 `b"Q"` 未到 DUT；close → DUT 收到 `Q`、h1 `interactive_owner` 為 true |
| `test_interactive_lease::test_expiry_tick_restores_without_access` | SC-13（P1 T12） | open agent timeout 1（soft preempt）；h1 寫 `b"W"`；3.0 s 內不發任何 RPC → DUT 收到 `W`；status → `INTERACTIVE_EXPIRED`；再 status → 仍 `INTERACTIVE_EXPIRED`；close → `INTERACTIVE_EXPIRED` |
| `test_interactive_lease::test_lazy_expiry_parity` | 05 §10.2 | timeout 1、等 1.05 s（Tick 前）、send → `INTERACTIVE_EXPIRED`；再 send → `INTERACTIVE_NOT_FOUND`。lazy 與 tick 的先後不屬契約，若 Tick 先到期，第二次為 `INTERACTIVE_EXPIRED` 也接受（兩種結果都由 Python 基線驗證） |
| `test_interactive_lease::test_status_touch_extends` | 02 §7.6、§4.2 | timeout 2；1.5 s 時 status；再 1.0 s 時 status 仍 ok；之後 3.5 s 不碰 → `INTERACTIVE_EXPIRED` |
| `test_interactive_lease::test_send_encodings` | 05 §10.2 | key `ctrl-c` → `bytes:1`、DUT `\x03`；base64 `aGk=` → `bytes:2`、DUT `hi`；encoding `hex` → `{"ok":false,"error_code":"EXCEPTION",…}` 且無 `version` |
| `test_interactive_lease::test_open_rejections_zero_tx` | §11.3 #1 | RECOVERING（`unready_daemon` 以 `hold_reply` 卡住 recovery）→ `SESSION_RECOVERING`；已有 lease → `SESSION_INTERACTIVE_BUSY`；兩者都帶 `command:"help"`，DUT 未收到 `help` |
| `test_interactive_lease::test_attached_rescue_boot_interrupt` | `test_bootloader_recovery::test_allow_attached_opens_lease_on_autoboot_countdown` | `unready_daemon`（`FakeTarget(ready=False)`）→ session `ATTACHED`；DUT `emit(b"Hit any key to stop autoboot:  3\r\n")` → `interactive-open --allow-attached` 得 `recovery_mode:true`、`boot_interrupt:true`、無 `login_required` |
| `test_interactive_lease::test_attached_rescue_login_required` | `test_login_recovery_lease` | DUT `emit(b"\r\n(none) login: ")` → `login_required:true` |
| `test_interactive_lease::test_attached_not_bootloader` | `test_allow_attached_rejects_when_neither_prompt_nor_banner` | DUT `emit(b"random\r\n")` → `SESSION_NOT_READY`＋`error_detail:"NOT_BOOTLOADER"` |
| `test_interactive_lease::test_attached_without_allow_rejected` | `test_default_rejects_attached_state` | `SESSION_NOT_READY`＋`selector` |
| `test_interactive_lease::test_stash_dropped_does_not_replay`（Rust-only，SC-31(b)） | D-16 | soft preempt 後 h1 寫 `b"Q"`；關 h1 fd 並等 3.5 s（peer 消失超過 grace）；close agent lease → DUT 從未收到 `Q` |
| `test_console_bc02::test_banner_during_suspend_drops_deferred` | BC-02、§11.3 | `cmd submit --cmd bootcmd --cmd-timeout 3`；腳本 0.5 s 後回 `b"U-Boot 2023.01\r\n"`、之後不回 prompt；banner 之前 h1 寫 `b"Z"`；輪詢 `cmd get` 直到命令進入終態，再等 3 s → DUT `written()` 從未出現 `Z`，WAL 無含 `Z` 的 TX |
| `test_console_bc02::test_input_during_recovery_not_replayed`（Rust-only，SC-31(a)） | D-13 | `target.hold_reply(True)` 後 `session recover --force`；`wait_state(COM0, "RECOVERING"／"ATTACHING")` 確認進入恢復窗後 h1 寫 `b"R"`；`hold_reply(False)` 讓它回 READY → 3 s 內 DUT 未收到 `R`。Python 會回放此輸入（SC-31 的理由），所以只對 Rust 執行 |
| `test_console_bc02::test_normal_suspend_loses_no_keystroke` | Review Focus 1、§11.3「禁止來源交錯」 | 10 輪 agent 命令期間 h1 共寫 `b"0123456789"`（每輪 1 byte）→ DUT 最終依序收到全部 10 bytes，且每個 byte 都不落在任何 agent 命令 payload 的 bytes 之間（以 WAL TX seq 斷言） |
| `test_console_telnet::test_greeting_and_attach_shape`（Linux 上 Rust-only） | `test_telnet_filter::test_greeting_bytes`、02 §7.6 | `telnet_daemon`（`SERIALWRAP_CONSOLE_BACKEND=tcp`）：attach 回 `client_id:null`、`transport:"tcp"`、`protocol:"telnet"`、`vtty==endpoint`；連線後前 12 bytes＝greeting |
| `test_console_telnet::test_first_raw_second_line`（Linux 上 Rust-only） | 06 §8.2、`test_refine_ownership::test_tcp_accept_during_operation_gate_does_not_get_raw_owner` | T1 送 `b"ab"` → DUT 收到 `ab`；T2 送 `b"ls\r\n"` → T2 收回 `b"ls\r"`、DUT 收到 `b"ls\n"` |
| `test_console_telnet::test_iac_escape_outbound`（Linux 上 Rust-only） | 06 §8.4 | DUT `emit(b"\xff\x01")` → T1 收到 `b"\xff\xff\x01"` |
| `test_console_telnet::test_inbound_nvt_and_binary_refusal`（Linux 上 Rust-only） | `test_telnet_filter` | T1 送 `IAC IAC` → DUT 收到單一 `0xFF`；送 `IAC WILL BINARY` → T1 收到 `IAC DONT BINARY`、DUT 無新 byte；`IAC` 與 `WILL NAWS` 分兩次 send → 回 `IAC DONT NAWS` |
| `test_console_telnet::test_disconnect_removes_console`（Linux 上 Rust-only） | 06 §8.3 | 關閉 T2 → 1 s 內 console-list 不含 T2 |
| `test_console_telnet::test_session_public_has_console_endpoint`（Linux 上 Rust-only） | `test_session_console_endpoint::test_key_always_present_on_non_pty_platform` | `session list` 每列有 `console_endpoint` |
| `test_slice22_family_mirrors::test_f4_interactive_line_cmd_defined` | `f4-interactive-line-cmd-defined` | `cmd submit --mode interactive --cmd vim` 回 `execution_mode:"interactive"`、`interactive_session_id` 非 null |
| `test_slice22_family_mirrors::test_f6_ping_during_long_op` | `f6-ping-during-long-op` | `sleep 5` 執行中 `daemon status` 往返 < 1.0 s |
| `test_slice22_family_mirrors::test_f6_two_boards_no_starvation` | `f6-two-boards-no-starvation` | 兩個 target、兩 session 交錯 20 個命令全部 done |
| `test_slice22_family_mirrors::test_f8_second_daemon_detected` | `f8-second-daemon-detected` | 第二個 throwaway daemon（`profiles: {}\ntargets: {}\n`）啟動後 15 s 內 `daemon status.multi_open` 為 true |
| `test_slice22_family_mirrors::test_f8_tty_holders_reported` | `f8-tty-holders-reported` | `daemon status.foreign_holders` 含 target slave 路徑對應 daemon pid |

`test_console_telnet.py` 的 skip 條件：Linux 上 `@pytest.mark.rust_only`（Python 沒有 SC-28 env，此為信心較低的 Rust-only 驗證）；Windows 上不加 marker，兩版 daemon 原生都是 Telnet，同一批案例由 Task 12 做 A/B。

既有測試改指 Rust：`SERIALWRAP_CLI_EXE=…/serialwrap SERIALWRAP_DAEMON_EXE=…/serialwrapd python3 -m pytest -q tests/test_human_agent_coexist.py`（t1～t8）；`func-test` 的 8 個 2.2 案例 `co-01-line-buffering`、`cowork-01-agent-bad-cmd`、`ha-01-human-then-agent`、`hk-01-special-keys-raw`、`hk-02-agent-preempt-human`、`hk-03-deferred-input-flush`、`hk-04-minicom-exit-cleanup`、`rc-01-interactive-mode-switch`（oracle 已由 P2 依 `TST-D-9` 修復）以 `python3 func-test/runner.py -c <case>` 在同兩個 env 下執行；L1.5 情境 `l15-multi-source-contention-console` 以 `python3 -m tools.uart_stdio_bridge.scenario run tools/uart_stdio_bridge/scenarios/l15-multi-source-contention-console.yaml --artifact-label rust --evidence-dir target/l15/<run_id>` 執行（`no_forbidden_interleave` 判定）。

- [ ] **Step 2：執行並確認 FAIL**

Run:
```bash
cargo build --release -p sw-cli -p sw-daemon
python3 -m pytest -q -rs tests/contract_blackbox/slice22
SERIALWRAP_AB_LABEL=rust SERIALWRAP_CLI_EXE=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrap SERIALWRAP_DAEMON_EXE=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrapd \
  python3 -m pytest -q -rs tests/contract_blackbox/slice22
```
Expected: 第一條（Python 基線，P1 已交付 BC-02／SC-13／SC-16）雙邊案例全部 PASS，`rust_only` 案例（`test_console_telnet.py` 全部、`test_input_during_recovery_not_replayed`、`test_stash_dropped_does_not_replay`）skipped；第二條（Rust）FAIL。

- [ ] **Step 3：實作**

依失敗訊息逐項修正 Task 6～10 的整合缺口（不得修改 oracle）：常見點為 effects 執行順序（`LeaseIndex` 在 `Reply` 前）、RPC 前的 `ConsoleScan` 是否送出、哨兵 primary 是否建立、WAL TX 的 `source`／`cmd_id`、hub 對 Telnet echo 的 IAC 逸出、hub 的 `input_epoch` 快照時點。每個修正都附對應的 Rust L1 回歸測試，放在負責該模組的 `crates/*/tests/` 檔中。

- [ ] **Step 4：執行並確認 PASS**

Run: Step 2 的三條命令，再加
```bash
export SERIALWRAP_CLI_EXE=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrap SERIALWRAP_DAEMON_EXE=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrapd
python3 -m pytest -q tests/test_human_agent_coexist.py
for c in co-01-line-buffering cowork-01-agent-bad-cmd ha-01-human-then-agent hk-01-special-keys-raw hk-02-agent-preempt-human hk-03-deferred-input-flush hk-04-minicom-exit-cleanup rc-01-interactive-mode-switch; do
  python3 func-test/runner.py -c "$c" || exit 1
done
python3 -m tools.uart_stdio_bridge.scenario run tools/uart_stdio_bridge/scenarios/l15-multi-source-contention-console.yaml --artifact-label rust --evidence-dir target/l15/$(date +%y%m%d-%H%M%S)
SERIALWRAP_AB_LABEL=rust python3 -m pytest -q tests/contract_blackbox/slice21     # 前一切片黑箱在 2.2 artifact 上不得退步
unset SERIALWRAP_CLI_EXE SERIALWRAP_DAEMON_EXE
python3 -m pytest -q tests/
```
Expected: 全部 PASS（Python 與 Rust 兩組；`tests/` 全套無新失敗）。

- [ ] **Step 5：commit**

```bash
git add tests/contract_blackbox/slice22/ tests/contract_blackbox/bb_faketarget.py tests/test_human_agent_coexist.py func-test/lib/ crates/ changelog.d/rust-s22-console-blackbox.md
git commit -m "test(contract-blackbox): 新增 console、lease 與 Telnet 黑箱契約並改指 Rust artifact" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 12：Windows 原生 console 與選配 com0com

**Test author model:** haiku-xhigh　**Implementer model:** codex-gpt-6-luna-max
**Changelog fragment:** `changelog.d/rust-s22-windows-console.md`（`type: feat`、`scope: rust-windows`：「Windows Telnet console 原生測試與選配 com0com Serial console（OD-01）」）。
**擁有者 host 可執行 gate（OD-15）與 BLOCKED-OWNER evidence:**
1. Windows-native Console L1.5（總表 #1，OD-15 起為可執行 gate）：`scripts/rust/win-blackbox.ps1 -Subset console` 需要 Windows 側的 DUT serial。DUT 端接線：`SW_TEST_COM_PAIR=COM20:COM21`（OD-08：Spike 1 已在擁有者 host 通過的 com0com DUT-side pair；直連 Windows 的實體 USB-UART loopback 對僅作備援）；daemon 以 OD-11 隔離實例管理 `COM20`（`SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20`），由 orchestrator 經 WSL interop 代跑，`COM21` 由 P2 `tools/uart_stdio_bridge` 的 Windows pySerial 端點接 DUT 模擬器（`local-process` 模式）。Python 與 Rust daemon 各跑一輪（Sequential A/B，§15.9：先停 A、確認 `COM20` 釋放，再啟 B；兩輪都用新的模擬器實例）。
2. Console-side com0com（OD-01 選配，總表 #2；OD-15 起在擁有者 host 可執行、結果未知）：驅動已安裝（Spike 1 基本資格通過，HVCI 執行中、Secure Boot 關閉已記錄）；另建第二組 pair 後以 `SW_TEST_CONSOLE_PAIR` 提供的 pair 執行 `cap_windows_com__console_com0com_*` 與 Tera Term Serial 逐字元互動手動程序（§7.3、§15.8.3）。
3. Tera Term／PuTTY Telnet 互動桌面驗證（`cap_interactive_desktop`，總表 #3）：方向鍵、Tab、Ctrl-C 即時透傳、第二視窗 line-buffer；程序見 Task 13 的 `windows-telnet-frontends.md`。

**Files:**
- Create: `crates/sw-daemon/tests/windows_console_native.rs`
- Modify: `scripts/rust/win-blackbox.ps1`（加 `-Subset console`：設定 `SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE`、啟動 `COMb` 端的 bridge＋DUT 模擬器、執行 `tests/contract_blackbox/slice22/test_console_telnet.py` 與 `test_interactive_lease.py`；證據寫到 `target/win-blackbox/<run_id>/{python,rust}/`：`junit.xml`、bridge 的 `bridge.v1` 紀錄、`daemon.stderr`，另產生 `ab_summary.json`）
- Modify: `tests/contract_blackbox/slice22/conftest.py`（Windows 上 `telnet_daemon` 改用 DUT adapter：`FakeTarget` 換成經 `tools/uart_stdio_bridge` 接在 `COMb` 的模擬器，`SW_TEST_COM_PAIR` 缺席時以 `windows_com` 能力 SKIP 並標「證據不足」）
- Modify: `tests/contract_blackbox/slice22/console_helpers.py`（`console_kind(attach_resp)` 依 `transport` 判斷，使同一案例在 Windows 走 socket client）
- Create: `docs/rust-refactor/procedures/windows-serial-console.md`（com0com console 手動驗證程序：前置、設定 `windows.serial_console`、Tera Term 步驟、期望、證據檔清單）

**Interfaces:**
- Consumes：Task 8 Windows 實作（含已移入 Task 8 的 Winsock 修正）、Task 10 RPC、Task 11 fixtures；arch §9.4 `win-runner.sh`／`win-test.sh`；P2 `tools/uart_stdio_bridge` Windows pySerial 端點。
- Produces：`windows_console_native.rs` 的測試入口；`win-blackbox.ps1 -Subset console`；手動程序文件。

- [ ] **Step 1：寫失敗測試**

`crates/sw-daemon/tests/windows_console_native.rs`（`#![cfg(feature = "test-fakes")]`；以 `caps.console_kind==TcpTelnet` 判斷適用，不用 cfg；Linux 執行時以 `SERIALWRAP_CONSOLE_BACKEND=tcp` 跑同一組，Windows 經 win-runner 原生執行）：

| 測試名 | 斷言 |
|---|---|
| `telnet_hub_end_to_end_with_fake_serial` | in-process daemon＋FakeSerial DUT＋原生 TCP console：attach 回 TCP shape；client 前 12 bytes＝greeting；送 `b"ab"` → FakeSerial 收到 `ab`、WAL TX source `human:<cid>` |
| `telnet_second_client_line_mode` | 第二 client `b"ls\r\n"` → echo `b"ls\r"`、FakeSerial 收到 `b"ls\n"` |
| `telnet_listener_ephemeral_per_bridge` | 觸發 drain(Recovery) 後 READY：endpoint port 改變；舊 client 保留 |
| `telnet_slow_client_bounded` | client 不讀，FakeSerial 灌 4 MiB RX → 該 console 的 `ring_display_bytes() <= 1 MiB` 且 `dropped_total() > 0`（經 `ServiceRig` 的 hub 診斷 accessor，不量測行程 RSS）；agent `cmd submit echo OK` 5 s 內 done |
| `cap_windows_com__serial_console_end_to_end` | `#[ignore="cap:windows_com"]`：`windows.serial_console` 指向 `SW_TEST_CONSOLE_PAIR`；peer 端寫 `b"x"` → FakeSerial 收到；`device list` 不含 pair 兩端 |

- [ ] **Step 2：執行並確認 FAIL**

Run: `SERIALWRAP_CONSOLE_BACKEND=tcp scripts/rust/cargo_nonzero.sh -p sw-daemon --features test-fakes --test windows_console_native`；orchestrator 另執行 `scripts/rust/win-test.sh --package sw-daemon --features test-fakes --test windows_console_native`（以 `--test` 選 integration binary；不使用與測試名無關的 libtest filter）
Expected: FAIL（RPC 回 `METHOD_NOT_FOUND` 或 `not yet implemented: P6 Task 10`）；Windows 報告該 exe `failed>0`。

- [ ] **Step 3：實作**

整合 Windows 路徑（PAL 層的 Winsock 修正已在 Task 8 完成）：com0com 在 `ConsoleHub` 的開啟時機（bridge 起來後、listener 之後）；`win-blackbox.ps1 -Subset console` 的 DUT adapter 與 A/B 流程；撰寫 `windows-serial-console.md` 手動程序。

- [ ] **Step 4：執行並確認 PASS**

Run: `SERIALWRAP_CONSOLE_BACKEND=tcp scripts/rust/cargo_nonzero.sh -p sw-daemon --features test-fakes --test windows_console_native`；orchestrator 另執行 `scripts/rust/win-test.sh --package sw-daemon --features test-fakes --caps windows_com --test windows_console_native`
Expected: 非 cap 案例兩平台 PASS（Windows 報告 `passed==4`）；`cap_windows_com__*` 在未提供 `SW_TEST_CONSOLE_PAIR` 時為 ignored，報告標「證據不足」（不得記為 PASS）。

- [ ] **Step 5：commit**

```bash
git add crates/sw-daemon/tests/windows_console_native.rs scripts/rust/win-blackbox.ps1 docs/rust-refactor/procedures/windows-serial-console.md tests/contract_blackbox/slice22/ crates/sw-pal/ changelog.d/rust-s22-windows-console.md
git commit -m "feat(rust-windows): 補齊 Windows Telnet console 原生測試與選配 com0com console" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 13：案例證據對照、文件、全套 gate

**Test author model:** agy-gemini-3.8-flash-high　**Implementer model:** haiku-xhigh
**Changelog fragment:** `changelog.d/rust-s22-docs-acceptance.md`（`type: docs`、`scope: rust-console`：「Slice 2.2 案例證據對照、BLOCKED-OWNER 程序與 console 文件更新」）。本 Task 動 `tests/`（屬 `code_paths`）與 README，fragment 必要。
**BLOCKED-OWNER evidence:** 本 Phase 全部 BLOCKED-OWNER 項目的程序與證據 schema（見文末總表）；L2 F5／F6／F8 與 L3 Console／Lease P1 的實機執行。

**Files:**
- Modify: `tests/acceptance/slice22_evidence_map.yaml`（Task 0 骨架；本 Task 為每個凍結 case 填 evidence，**不得增刪鍵**）
- Create: `tests/test_acceptance_slice22.py`
- Create: `scripts/rust/s22_gate_check.py`、`tests/test_s22_gate_check.py`（R5-F3：`AUTOMATED` 條目與 L1 段的執行證據判定；`tests/test_acceptance_slice22.py` 只驗對照表靜態一致，函式存在不等於已執行且通過）
- Modify: `tests/acceptance/l1_required.lock.json`（只以 P2 `regen_lock.py --l1-add-only` 登記 `s22_gate:cargo_workspace`、`s22_gate:console_conformance_pty`、`s22_gate:win_test`、`s22_gate:blackbox_rust`，linux／windows 依段；R5-F3）
- Create: `docs/rust-refactor/procedures/windows-telnet-frontends.md`（Tera Term／PuTTY／`telnet.exe` 互動程序：前置、逐步驟、證據檔名、PASS 條件）、`docs/rust-refactor/procedures/cross-reboot-console.md`（實板 U-Boot autoboot 中斷與 reboot 期間鍵入不回放）、`docs/rust-refactor/procedures/console-echo-loopback.md`（§1.2 Console 按鍵至回顯 SLO：115200 實體 loopback、500 個可識別樣本）
- Modify: `contracts/behavior_changes.yaml`（SC-28、SC-29、SC-31 `status: IMPLEMENTED`，依 P1 慣例只遞增檔案 `revision`（不寫 `contract_revision` 鍵））
- Modify: `README.md`（English 與繁體中文兩段同步：human console 段加 Telnet backend 選擇與 Windows Serial console 選配說明、OD-12 信任範圍）
- Modify: `docs/regression-plugin.md`（#155 評估紀錄：本 Phase 不新增 `regression/` case，理由見下）

**Interfaces:**
- Consumes：P2 `tests/acceptance/cases.yaml`、`cases.lock.json`、`regen_lock.py`；Task 0 的 `slice22_evidence_map.yaml`；Task 2～12 的測試 ID。
- Produces：填妥的 `slice22_evidence_map.yaml`。每筆格式為 `{status: AUTOMATED, tests: ["<檔案>::<函式>", …]}`、`{status: BLOCKED-OWNER, procedure: "<路徑>", evidence_schema: [<檔名>, …], blocked_item: <總表 #>}` 或 `{status: NOT_APPLICABLE, reason: "<能力依據>"}`，最後一種只允許 `windows_status: gap` 且有 P2 `windows_gap_reason` 的平台組合。

- [ ] **Step 1：寫失敗測試**

```python
# tests/test_acceptance_slice22.py
from __future__ import annotations

import json
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))
EMAP = yaml.safe_load((ROOT / "tests/acceptance/slice22_evidence_map.yaml").read_text(encoding="utf-8"))
INVARIANT_TESTS = {   # §11.3 不變量 → 至少一個具名自動化測試
    "zero_tx_on_reject": "crates/sw-core/tests/session_core_lease.rs::open_state_matrix",
    "no_forbidden_interleave": "tests/contract_blackbox/slice22/test_console_bc02.py::test_normal_suspend_loses_no_keystroke",
    "no_stale_context_replay": "tests/contract_blackbox/slice22/test_console_bc02.py::test_banner_during_suspend_drops_deferred",
    "no_reply_before_handoff": "tests/contract_blackbox/slice22/test_console_pty.py::test_release_closes_consoles_before_reply",
    "no_auto_resend": "crates/sw-core/tests/human_tx_lane.rs::human_raw_partial_not_resent",
    "insufficient_evidence_not_pass": "tests/test_acceptance_slice22.py::test_blocked_owner_cases_are_not_pass",
}


def _fn_exists(ref: str) -> bool:
    path, _, name = ref.partition("::")
    text = (ROOT / path).read_text(encoding="utf-8")
    return re.search(rf"\b(def|fn) {re.escape(name.split('::')[-1])}\b", text) is not None


def test_map_keys_equal_frozen_slice22_set() -> None:
    assert EMAP["frozen_lock_sha1"]
    assert set(EMAP["cases"]) == set(LOCK["sets"]["slice_2.2"]), "分母以凍結鎖檔為準，不得縮小"


def test_no_pending_entries() -> None:
    assert [k for k, v in EMAP["cases"].items() if v["status"] == "PENDING"] == []


def test_automated_entries_point_to_existing_tests() -> None:
    for cid, v in EMAP["cases"].items():
        if v["status"] == "AUTOMATED":
            assert v["tests"] and all(_fn_exists(t) for t in v["tests"]), cid


def test_blocked_owner_cases_are_not_pass() -> None:
    for cid, v in EMAP["cases"].items():
        if v["status"] == "BLOCKED-OWNER":
            assert (ROOT / v["procedure"]).is_file() and v["evidence_schema"] and v.get("result") is None, cid
    assert set(v["status"] for v in EMAP["cases"].values()) <= {"AUTOMATED", "BLOCKED-OWNER", "NOT_APPLICABLE"}


def test_section_11_3_invariants_have_named_tests() -> None:
    for inv, ref in INVARIANT_TESTS.items():
        assert _fn_exists(ref), inv


def test_lock_only_grew_since_task0() -> None:
    assert set(LOCK["sets"]["slice_2.2"]) >= set(EMAP["cases"]), "鎖檔只能 add-only（regen_lock.py --add-only）"


def test_automated_tests_are_in_frozen_l1_sets() -> None:   # R5-F3：每個 AUTOMATED 測試都必須在某個凍結 L1 集合內，gate 才會要求它執行且通過
    l1 = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))["sets"]
    frozen = {n for sid, plats in l1.items() if sid.startswith("s22_gate:") for p in plats.values() for n in p["nodeids"]}
    for cid, v in EMAP["cases"].items():
        if v["status"] == "AUTOMATED":
            for t in v["tests"]:
                path, _, name = t.partition("::")
                if not path.endswith((".py", ".rs")):
                    continue        # func-test／scenario 執行紀錄另由 L1.5 報告判定，不是 L1 nodeid
                nid = t if path.endswith(".py") else f"cargo:{path.split('/')[1]}::{pathlib.Path(path).stem}::{name}"
                assert nid in frozen, (cid, t)
```

`tests/test_s22_gate_check.py`（`core_portable`、`memory_only`；合成 fixture，不讀最新報告；R5-F3，failing-first）：`scripts/rust/s22_gate_check.py --l1-results <dir> --map tests/acceptance/slice22_evidence_map.yaml [--lock <l1_required.lock.json>] --out <json>` 讀 `<dir>/<section>/<platform>/resource-report.json`（`l1-results.v1`），對每個 `s22_gate:*` 集合以 P2 `l1_judge.judge` 判定，並要求每個 `AUTOMATED` 條目的每個測試在對應平台的報告中恰一列 `passed`；輸出 `{"sections":[{"set_id","platform","verdict","reasons"}],"cases":[{"case_id","status":"PASS|FAIL|INSUFFICIENT","missing_tests":[…]}],"result":"PASS|FAIL|INSUFFICIENT"}`，exit 0 只在 `PASS`。

| 測試名 | 斷言 |
|---|---|
| `test_existing_function_not_executed_is_insufficient` | 對照表某 `AUTOMATED` case 指向 `tests/contract_blackbox/slice22/test_console_pty.py::test_first_console_raw_second_line_buffered`（函式存在），但 `blackbox_rust/linux` 報告沒有該 nodeid → 該 case `INSUFFICIENT`、`missing_tests` 含之、`result == "INSUFFICIENT"`、exit 1 |
| `test_cargo_nonzero_count_is_not_enough` | `cargo_workspace/linux` 報告 200 列 `passed`，但凍結集合中的 `cargo:sw-core::human_tx_lane::human_raw_partial_not_resent` 不在其中 → 該 section `INSUFFICIENT`（`cargo_nonzero.sh` 的「執行數 >0」會放行） |
| `test_required_ignored_or_duplicate_insufficient` | `console_conformance_pty` 的必要 `cap_posix_pty__*` 列為 `skipped` → `INSUFFICIENT`；同 nodeid 兩列 → `INSUFFICIENT`；一列 `failed` → `FAIL`、exit 1 |
| `test_runner_exit_nonzero_insufficient` | 全部必要 nodeid `passed` 但 `exit_code == 101` 或 `finished == false` → `INSUFFICIENT` |
| `test_cap_windows_com_absent_is_blocked_not_pass` | `win_test/windows` 報告的 `cap_windows_com__*` 為 `skipped`：這些 nodeid 不屬 `s22_gate:win_test` 集合（BLOCKED-OWNER #1、#2 承接），故該 section 仍可 `PASS`；但若把它們登記進集合 → `INSUFFICIENT`（不得以 ignored 充數） |
| `test_all_present_pass` | 每個集合恰覆蓋、每個 `AUTOMATED` 測試恰一列 `passed`、`exit_code 0`、`finished true` → `result == "PASS"`、exit 0 |

- [ ] **Step 2：執行並確認 FAIL**

Run: `python3 -m pytest -q tests/test_acceptance_slice22.py tests/test_s22_gate_check.py`
Expected: FAIL（`test_no_pending_entries` 列出全部 PENDING 鍵；程序文件不存在；`s22_gate_check.py` 不存在、`l1_required.lock.json` 尚無 `s22_gate:*` 集合）。

- [ ] **Step 3：實作**

1. `slice22_evidence_map.yaml`：逐一填寫凍結集合中的每個 case：L1／L1.5 指向 Task 2～12 的實際測試（例：`f5-second-console-linebuffer` → `tests/contract_blackbox/slice22/test_console_pty.py::test_first_console_raw_second_line_buffered`；func-test 八案 → Task 11 的 `func-test/runner.py -c <case>` 執行紀錄與對應黑箱；`l15-multi-source-contention-console` → Task 11 的 scenario 執行；`bc02-*`、`sc13-*`、`sc15-*`、`sc16-*`、`sc28-*`、`sc29-*`、`sc31-*` → 對應的 L1＋黑箱測試）；L2 F5（4 案）、F6（2 案）、F8（2 案）為 `BLOCKED-OWNER`，procedure 為 `testpilot run serialwrap_regression --family F5,F6,F8`（testbed `serialwrap_exe` 指向 Rust bundle `bin/serialwrap`）；L3 `p0-console-raw`、`p0-blog-clean`、`p1-con-*`、`p1-cmd-modes`、`p1-wal-reset`、`p1-wal-fullrun` 為 `BLOCKED-OWNER`；`win-telnet-*` 四案為 `BLOCKED-OWNER`（總表 #1，Task 12 程序）；Stage 0 與 Slice 2.1 繼承的必要案例（P0 八案、F1～F4／F12、2.1 的 P1 等，§11.6）沿用 06 的證據條目並重跑於 Rust 2.2 artifact（Step 4）。`p0-blog-clean` 在 Windows 為 P2 已登記的 gap，該平台組合填 `NOT_APPLICABLE`。
2. 程序文件三份：每份含「前置條件」「逐步驟（含命令）」「證據檔名清單」「PASS 條件」「失敗時記錄」五節；證據檔名與總表一致。
3. `behavior_changes.yaml`：Step 4 全部通過後才把 SC-28、SC-29、SC-31 改為 `IMPLEMENTED`。
4. README 兩語段同步；明載 Telnet console 只綁 `127.0.0.1`、同機任何本機使用者都能連線（OD-12），不得寫「同使用者隔離」；說明 `SERIALWRAP_CONSOLE_BACKEND` 與 `windows.serial_console`。
5. `docs/regression-plugin.md` 加 #155 評估：本 Phase 修正的行為都能由 L1／L1.5 覆蓋；需要實機的部分（autoboot 中斷、實板 reboot 期間鍵入、Windows Telnet frontend）已有 F5／F9 與 realhw p1_console 案例承接，Windows Telnet human frontend 屬 Stage 0 §15.8.4 交付，本 Phase 不新增 `regression/` case。

- [ ] **Step 4：執行並確認 PASS**

Run（Windows 步驟由 orchestrator 執行，見「執行順序」第 5 點）：
```bash
python3 -m pytest -q tests/test_acceptance_slice22.py tests/test_contracts_slice22.py tests/test_contracts_catalog.py
python3 tests/acceptance/regen_lock.py --check
G=target/s22-gate/$(date +%y%m%d-%H%M%S)                                  # R5-F3：出口以逐案 l1-results 判定，cargo_nonzero.sh 只留作開發迴圈防呆
python3 scripts/rust/cargo_l1_results.py --platform linux --out $G/cargo_workspace/linux/resource-report.json -- --workspace --features test-fakes          # 含 06 全部 L1 測試
python3 scripts/rust/cargo_l1_results.py --platform linux --out $G/console_conformance_pty/linux/resource-report.json -- -p sw-pal --features test-fakes --test console_conformance -- --include-ignored cap_posix_pty__
scripts/rust/win-test.sh --features test-fakes --caps windows_com          # 報告的 l1_results 複製到 $G/win_test/windows/resource-report.json
python3 scripts/rust/check_cfg_boundary.py
cargo build --release -p sw-cli -p sw-daemon
SERIALWRAP_AB_LABEL=rust SERIALWRAP_CLI_EXE=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrap SERIALWRAP_DAEMON_EXE=$PWD/target/x86_64-unknown-linux-gnu/release/serialwrapd \
  SW_RESOURCE_REPORT=$G/blackbox_rust/linux/resource-report.json \
  python3 -m pytest -q tests/contract_blackbox/slice21 tests/contract_blackbox/slice22 --junitxml=$G/blackbox_rust/linux/junit.xml   # 前一切片黑箱在 2.2 artifact 上重跑
python3 scripts/rust/s22_gate_check.py --l1-results $G --map tests/acceptance/slice22_evidence_map.yaml --out $G/s22-gate.json
python3 -m pytest -q tests/
python3 -m policy_check --repo .
```
Expected: 全部 PASS；`s22_gate_check.py` exit 0、`s22-gate.json` 的 `result == "PASS"`（每個 `s22_gate:*` 集合的凍結 nodeid 恰執行一次且 `passed`、每個 `AUTOMATED` 條目的測試都在報告中 `passed`）；`win-test` 報告中 `cap_windows_com__*` 為 ignored／證據不足（不計 PASS，且不屬 `s22_gate:win_test` 集合）；`tests/` 全套 0 failed；policy_check 無 FAIL（R-18 docs 對齊已滿足）。四個 `s22_gate:*` 集合以 P2 `regen_lock.py --l1-add-only` 凍結（R5-F3），時點依 P2 Task 13（R5 審查後續）：P6 各 Task 在自己的紅燈 commit 登記新增 nodeid，本 Task 只登記 gate 自身測試。

- [ ] **Step 5：commit**

```bash
git add tests/acceptance/slice22_evidence_map.yaml tests/test_acceptance_slice22.py scripts/rust/s22_gate_check.py tests/test_s22_gate_check.py tests/acceptance/l1_required.lock.json docs/rust-refactor/procedures/ contracts/behavior_changes.yaml README.md docs/regression-plugin.md changelog.d/rust-s22-docs-acceptance.md
git commit -m "docs(rust-console): 完成 Slice 2.2 案例證據對照並更新 console 文件" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

PR（`wt/rust-refactor/p6-slice22` → `feature/rust-refactor`）描述（R-11／R-17／#155）：勾選 Policy Checklist；`Closes` 只在有對應 issue 時使用，無 issue 時上 `policy-exempt:issue-link`；記錄 #155 評估結論（同上第 5 點）；附 Task 0 的 `cases.lock.json` sha1；列出本 Phase 全部 BLOCKED-OWNER 項目與其程序及證據 schema 位置。

#### parity-amendments-2026-10-26 追加（Python 1:1 承接）

> 來源：`docs/rust-refactor/plans/parity-amendments-2026-10-26.md`（稽核 A／B 與 P6 審查）。依 OD-14 #1（外部可觀察行為與 Python 1:1，只可新增、不可修改），下列為本 Task 的**必要**測試列與完成條件：紅燈先行，新增 nodeid 於本 Task 紅燈 commit 以 P2 `regen_lock.py --l1-add-only` 登記到所列集合；斷言值以 Python 基線實測為準（不得自訂）。

| # | 稽核來源 | 測試／要求 | 精確斷言或完成條件 |
|---|---|---|---|
| #187 | 稽核 A／B 全體 | 切片出口條件 | `s22_gate_check.py` 另讀 `parity-amendments-2026-10-26.md` 中承接 Phase 為 P6 的列，逐列標 `PASS`／`BLOCKED-OWNER`／`OWNER-DECISION-PENDING`；`s22_gate:py_parity`（Task 0）納入 gate 必要集合；P6 審查 F1～F9、(h) 的已實作追加修正須在進入 Task 6 前完成（F1／F2 至少已明文化 BLOCKED-UPSTREAM） |

---

## BLOCKED-OWNER 總表（本環境無法產出，不得標 PASS）

OD-15 更新：#1 已轉為擁有者 host 可執行 gate（`COM20`／`COM21`，orchestrator 經 WSL interop 代跑）；#2 在同一 host 可執行但屬 Spike 1 剩餘項、結果未知；#4／#5 交 bench 前先以 P2 Task 17 `scripts/docker/run-l15.sh` 兩容器拓樸跑同一批情境並附 `L1.5-docker` 報告（不計為真板 L2／實機 L3）；#3、#6、#7 不變。#7 的 SLO 量測仍不得以 docker 取代。

| # | 項目 | 需要的資源 | 程序與證據 schema | 對應 Task／案例 |
|---|---|---|---|---|
| 1 | Windows-native Console L1.5（Telnet console＋Windows 側 DUT serial），Python 與 Rust daemon Sequential A/B（OD-15：可執行 gate） | OD-08：Spike 1 已通過的 com0com DUT-side pair `COM20:COM21`＋`tools/uart_stdio_bridge` Windows pySerial 端點（擁有者 host）；直連 Windows 的實體 USB-UART 為備援 | `scripts/rust/win-blackbox.ps1 -Subset console`；證據 `target/win-blackbox/<run_id>/{python,rust}/junit.xml`、`bridge.v1` 紀錄、`ab_summary.json` | Task 12；`win-telnet-raw-owner`、`win-telnet-linebuffer`、`win-telnet-suspend-resume`、`win-telnet-deferred` |
| 2 | Console-side com0com Serial console（OD-01 選配）：驅動安裝、HVCI／Secure Boot 狀態紀錄、`cap_windows_com__console_com0com_*`、Tera Term Serial 逐字元互動，以及 DTR／RTS／baud 不外洩 | 擁有者 host（驅動已裝；另建第二組 pair 需管理員一次）、`SW_TEST_CONSOLE_PAIR`；Spike 1 剩餘項（Console-side、控制線隔離） | `docs/rust-refactor/procedures/windows-serial-console.md` | Task 8、12；`sc29-com0com-serial-console` |
| 3 | Tera Term／PuTTY／`telnet.exe` 互動桌面驗證（方向鍵、Tab、Ctrl-C 透傳；第二視窗 line-buffer） | 互動桌面（`cap_interactive_desktop`） | `docs/rust-refactor/procedures/windows-telnet-frontends.md` | Task 2、12 |
| 4 | L2 F5（4 案）、F6（2 案）、F8（2 案）以 Rust bundle 在 live bench 執行 | TestPilot `serialwrap_regression`、live bench、bench 互斥鎖 | `testpilot run serialwrap_regression --family F5,F6,F8`；證據為 TestPilot 原始結果＋外層 envelope（§11.4） | Task 13 `slice22_evidence_map.yaml` |
| 5 | L3 realhw `p0-console-raw`、`p0-blog-clean`、`p1-con-fanout`、`p1-con-defer`、`p1-con-busy`、`p1-con-softpreempt`、`p1-con-liveness`、`p1-con-orphan`、`p1-con-second`、`p1-cmd-modes`、`p1-wal-reset`、`p1-wal-fullrun` | realhw bench（two_boards、tmux） | realhw 既有程序；證據為 `engine_result`＋`release_gate_result` | Task 13 |
| 6 | 跨 reboot 實板情境：真實 U-Boot autoboot 倒數中 `boot_interrupt` 救援、實板重開期間 human 鍵入不回放（BC-02／SC-31） | 實板＋可觸發 reboot 的授權 | `docs/rust-refactor/procedures/cross-reboot-console.md` | Task 4、7 |
| 7 | 規格 §1.2 Console 按鍵至回顯 SLO（p50 ≤3 ms、p99 ≤10 ms；115200 bps 實體 loopback、500 個可識別樣本，不得以 Docker／Python bridge 取代） | 實體 USB-UART loopback | `docs/rust-refactor/procedures/console-echo-loopback.md`；證據 `echo_latency_samples.csv`（500 列：樣本 id、送出 ns、回顯 ns）、`summary.json`（p50、p99、環境） | Task 9（in-process 回歸門檻為 `console_echo_latency.rs`） |
