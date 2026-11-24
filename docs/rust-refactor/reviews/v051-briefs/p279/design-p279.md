=== DESIGN ===

1. **事實核對**

**證據基準**

- 唯讀副本的 `HEAD` 與本機 `origin/main` 均為 `e5cee8c3c5242f25b9aca4fb0d8b6485de9ff112`，detached、工作樹乾淨。未查網路；「最新」僅指提供的本機 ref。
- 作者為 GitHub `weisheng-lin-227`，Git commit 作者為 `weisheng_lin`。PR 279 包含 `e56ed46a39bd7ca2dba3f89162d1f145aa3c88d5` 與 `17d673c110d09a4f85b6e78ced0520729c61f744`，本機 `origin/fix/275-tcp-console-lease` 指向後者。
- 已讀指定裁決、共同規則、issue 盤點、PR／issue 全文與外部稽核 §3.2。裁決已配 **BC-23**；OD-111 要求凍結 Python，OD-114 要求本包合成一個實作子項、一次 terra 審查。
- 本輪僅讀碼與 Git object，未編譯、未跑測試或 gate；以下競態判斷屬靜態分析。

**Rust 現況與缺口**

| 面向 | 已核對的行為與位置 |
|---|---|
| TCP 接入 | `crates/sw-core/src/session_core/console.rs:709` 的 `console_connected()` 登記 client，於 `:743` 呼叫 `on_stream_accept()`，沒有建立 lease。 |
| raw owner | `crates/sw-core/src/human_console/ownership.rs:119` 依狀態將首位人類放入 `interactive_owner`、`suspended_owner` 或 `pending_owner`。第二條連線不會自動搶走現有 owner。 |
| 回報來源 | `crates/sw-core/src/lease.rs:166` 的 `lease_context()` 從 session lease 推導 `interactive_owner`／`human_attached`／`human_active`；沒有 lease 就看不到 TCP raw owner。 |
| 無法自癒 | `crates/sw-core/src/session_core/lease_p6.rs:193` 的 refresh 在沒有 lease 時返回；`:259` 的 self-heal 遇已有 owner／suspended owner 就返回。 |
| POSIX 對照 | `crates/sw-core/src/session_core/console.rs:920` 的 PTY attach 會 refresh，無 lease 時於 `:927` 建 human lease。TCP `console_attach_tcp()` 在 `:833` 只回 endpoint。 |
| agent 仲裁 | `crates/sw-core/src/session_core/lease_p6.rs:347` refresh 後，`:364` 對閒置 human soft preempt，活躍 human 回 `SESSION_INTERACTIVE_BUSY`。TCP 缺 lease，無法進入相同仲裁。 |
| foreground | `crates/sw-core/src/session_core/console.rs:34` 的 `fg_begin()` 只有看見 human lease 才 suspend；`human_suspend_begin()` 在 `:59` 亦同。 |
| reprobe | `crates/sw-core/src/session_core.rs:6159` 同時檢查 bridge、human lease、peer、實際 raw owner 與 60 秒活動窗。補回報欄位本身不能修好此路徑。 |
| socket EOF | `crates/sw-core/src/human_console/hub.rs:1002` 將 EOF／讀取錯誤轉成斷線；`session_core/console.rs:749` 先核對 lifetime，再清該 client 的 ownership、deferred、lane 與紀錄。 |
| 錯清新 owner | `lease_p6.rs:208` 的普通 human lease 有 peer grace；一般 close 於 `:94` 清 owner。直接讓鏡像走此流程，可能清掉後來接入的 B。 |
| stash 收尾風險 | `lease_p6.rs:105` 還原 stash；失效時於 `:116` 清 owner，並於 `:148` 在 resume 後再清一次。A 被 preempt 後斷線、B 在 suspend 中接入時，也必須覆蓋此路徑。 |

因此，PR 內「Rust console／lease 尚未實作」已過期；Rust 已有對應實作，也有同類缺口。

**Linux 可以測 TCP console**

- `crates/sw-pal/src/console_pty.rs:206` 的 native factory 可呼叫 TCP listener。
- POSIX 實作 `crates/sw-pal/src/console_tcp/posix.rs:15` 綁定 `127.0.0.1:0`。
- `crates/sw-daemon/tests/support/service_rig.rs:469` 已有 `ServiceRig::with_native_tcp_console()`；`:262`、`:276` 接上原生 listener 與 mux，其 UART 仍是 fake serial。
- `crates/sw-daemon/tests/windows_console_native.rs:92` 已使用這條接線；檔案沒有整檔 Windows 限制。

可在 Linux 驗證 TCP socket、Telnet、ConsoleHub、actor、RPC、fake UART／WAL 的整條路徑；這不等於 Winsock、Windows COM 或 TeraTerm 實機證據。

**Python 0.4.x 基線已對照**

- `sw_core/uart_io.py:1347` 的 `_accept_console_conn()` 在 `:1384` 起直接授 active／pending／suspended owner。
- `sw_core/session_manager.py:7159` 的 TCP attach 因 `client_id=None` 只回 endpoint；PTY 分支於 `:7171` 開 lease。
- `_human_active_locked()` 在 `:2141` 要求 session lease；自癒於 `:2554` 跳過已有 owner；`_refresh_interactive_locked()` 在 `:6098` 無 lease 即返回。
- `_lease_context()` 在 `:6138` 只以 lease 判斷 human presence。
- PR 新增的 Python getter／鏡像修正不在目前凍結基線內，本包不採用其 Python 程式及測試變更。

**既有契約與測試**

- `contracts/rpc_schema.json:495` 定義既有四鍵 `LeaseContext`，不需要增加 wire 欄位。
- `openspec/specs/session-interactive/spec.md:203` 定義閒置 human soft preempt；`:224` 定義 peer liveness。
- `session_core_lease.rs` 已測 soft preempt、BUSY、stash、peer grace、self-heal、human-active reprobe。
- `console_hub.rs::tcp_eof_emits_disconnected_on_data_lane` 已測 EOF 事件。
- `rpc_console.rs::attach_tcp_shape` 鎖住「只查 endpoint、不建立 socket client 或 lease」；修正後應原樣通過。
- `session_core_console.rs:274` 的 `owner_change_updates_fence_before_tx` 要求一次實際 owner 改變只增加一次 `owner_gen`，鏡像簿記不能額外增加。

另有兩項不能照搬作者敘述：

1. **file RPC 的 bare human 並不因此變成可信 human。**  
   Rust `file_transfer/request.rs:24`、`session_core/file_xfer.rs:50` 與 Python `service.py:1452`、`:1464` 都將 file RPC 視為 agent。PR 宣稱 `file push/pull --source human` 必然 BUSY，與現行程式不符。
2. **部分既有 TCP 測試以「沒有 session lease」為隱含前提。**  
   例如 `session_core_console.rs::line_input_echo_then_submit_order`、`session_core_lease.rs::human_reboot_opens_lease`、`human_timeout_promoted_only_for_internal_origin`、`human_line_timeout_sends_no_rescue_keys`，以及 `windows_console_native.rs::telnet_second_client_line_mode`。補鏡像後，第二條 console 的非 interactive 行命令會碰到既有 human lease BUSY 規則，需明確處理測試前提。

2. **方案**

本包只有一個子項：**TCP raw ownership 與 session lease 同步，連同其仲裁及清理回歸一次完成。**

**修改位置**

| 檔案／函式 | 設計變更 |
|---|---|
| `crates/sw-core/src/lease.rs`／`InteractiveLease` | 新增內部 `mirror_client_lifetime: Option<u64>`；`Some` 表示 TCP 鏡像，owner 沿用既有欄位。一般 lease 預設 `None`，不透出 RPC。 |
| `human_console/mod.rs`／`HumanConsoleState` | 新增唯讀 TCP logical-owner 查詢，依 active → suspended → pending 取得仍存活的 `TcpTelnet` client 與 lifetime。PTY／com0com 不納入。 |
| `session_core/lease_p6.rs` | 新增 `sync_tcp_console_lease()`、只撤簿記的 `forget_tcp_console_mirror()`；接入 refresh、self-heal 與 TCP stash 收尾。 |
| `session_core/console.rs` | 在 accept／有效 EOF／明確 console detach 的既有 ownership 更新後同步鏡像；不改 socket、Telnet 或輸入路由規則。 |
| `session_core.rs` | 在 reprobe 判定前及 READY／ATTACHED 狀態落定後接上同步；確保 RPC refresh 在組回應前完成，`LeaseIndex` 仍先於 Reply。 |
| Rust 測試、契約與文件 | 依第 4、5 節。原生 TCP PAL／Hub production code 預計不需修改。 |

**鏡像不變式**

- active lease 仍只有一筆；agent lease、一般 human lease不被鏡像覆蓋。
- 新建鏡像須有有效 `conn_epoch`、狀態為 `READY`／`ATTACHED`，且不在 stop、shutdown、flash 或 handoff freeze。
- 沿用既有 admission 機制檢查交接窗口；不建立新的鎖，也不以鏡像額外授予 raw owner。
- 鏡像建立／撤除只更新 lease 與 `LeaseIndex`，不呼叫 `human_set_owner()`，不額外增加 `owner_gen`，不持有新的 admission lease flag。
- lifetime 綁定該次 client；舊 EOF 不得撤掉同名新 lifetime 的鏡像。
- suspended／pending 仍代表同一位人類的保留 ownership，refresh 不因 active owner 暫為空就拆鏡像。
- 維持既有 `last_human_input_at` 與 60 秒窗；不新增 per-client 活動計時。

**資料流與轉移**

```text
TCP accept
  → ConsoleHub::ConsoleConnected
  → actor 登記 client、既有 ownership 授權
  → 同事件補鏡像、發布 LeaseIndex

agent 一般命令
  → refresh 保留 TCP 鏡像
  → fg_begin suspend
  → human 輸入沿既有 deferred 路徑
  → fg_finish resume／回放
  → 鏡像仍是同一筆

agent interactive-open
  → refresh
  → human active：既有 BUSY
  → human idle：stash 鏡像、開 agent lease
  → close／expiry：核對原 client lifetime，還原或撤除 stash

socket EOF
  → 既有 Data lane／lifetime 檢查
  → 清該 client 的 ownership、deferred、lane
  → 立即撤其鏡像／失效 stash
  → 後續 B 的鏡像不得被 A 的收尾清除
```

A 在 soft preempt 期間斷線、B 已進 `suspended_owner` 的情境，agent close 只能撤掉 outgoing agent ownership，resume 後保留 B；不得套用無條件的 `clear_owner_after_resume` 清 B。只對 TCP 鏡像來源的分支處理，不改 PTY stash 規則。

新連線若在舊 EOF 處理前已成為第二 client，不額外授予立即接班能力；沿用既有 self-heal 的重授時機，重授後記成鏡像。

**交界**

- **i227** 決定哪些登入失敗保留 bridge、session 狀態及 wrapper 行為。本包只認「有效 bridge＋READY／ATTACHED」，不依錯誤字串自行宣告 transport 健康。
- **i294** 負責 echo／prompt、stdout、partial 與完成判定。本包不改。
- 不處理 issue 264 的板端半行串接；建立 lease 不會撤回已寫到 target 的半行。

3. **設計決策**

| 項目 | 選項／建議／理由 |
|---|---|
| 共用模型【需編排者定案】 | **選項**：另建 TCP lease 系統／共用 `InteractiveLease`。**建議**：共用，增加內部 mirror lifetime 標記。**理由**：直接沿用 agent 仲裁、LeaseIndex、stash、status 與 tombstone 機制，避免雙重權威。 |
| 建立時機【需編排者定案】 | **選項**：只等週期自癒／actor 接入時建立並於關鍵入口同步。**建議**：accept 同事件建立；refresh、reprobe、自癒與 readiness 落定補同步。**理由**：不能容許第一個 RPC 或 reprobe 先看到「有人 raw、卻無 lease」。 |
| EOF 清理【需編排者定案】 | **選項**：套用 PTY 3 秒 peer grace／TCP 確定 EOF 立即撤鏡像。**建議**：立即撤鏡像，不清其他 owner、不留 expired tombstone；PTY grace 不變。**理由**：EOF 是確定斷線，延遲簿記會造成 ghost human 及 A→B 誤清。 |
| suspend／pending【需編排者定案】 | **選項**：只鏡像 active／包含 suspended、pending。**建議**：三者皆包含，但不繞過既有 TX gate。**理由**：foreground 期間的人類仍存在；session presence 與當下可送 raw bytes 是不同條件。 |
| 活躍與仲裁【需編排者定案】 | **選項**：另定 TCP 政策／沿用 POSIX。**建議**：READY 下，60 秒內活動阻擋 agent interactive-open；閒置允許 soft preempt；一般 agent 命令照既有 suspend/resume。ATTACHED recovery 仍沿用既有 opt-in、分類與 timeout cap，不擴大成一般 READY 語意。 |
| A→B 與 stash【需編排者定案】 | **選項**：只修 active 鏡像／連同鏡像 stash 收尾修正。**建議**：兩者同包。**理由**：否則 agent close／expiry 仍能清掉 B，不能算完成 ownership 修正。 |
| bare human 與 file RPC【需編排者定案】 | **選項**：照作者文字讓 file RPC 也 BUSY／保留 OD-74 現況。**建議**：保留。非 interactive `command.submit` 的可信 bare human 遇 human lease 回 BUSY；`file.*` 仍視為 agent，符合條件時 suspend／傳輸／resume。**理由**：Python、Rust 現行 RPC 一致；改 file 來源政策是另一項產品變更。README、SKILL、fragment 必須修正作者錯誤敘述。 |
| 第二 console 與既有測試【需編排者定案】 | **選項**：新增 TCP 特例繞過 lease／沿用既有 human lease BUSY 規則。**建議**：沿用 BUSY。第二 console 仍可觀察與走 line editor，但有 human lease 時非 interactive 命令受既有仲裁。**測試處理**：上述測 line submission／timeout／reboot 的節點，明確用公開 close 路徑先解除 lease，再驗原有斷言；另新增「鏡像存在→精確 BUSY＋零 TX」反例。不得改名、刪節點或把成功斷言放寬。此 fixture 調整範圍須由編排者核准。 |
| 明確 close【需編排者定案】 | **選項**：所有鏡像 close 都只撤紀錄／區分自動遺忘與使用者明確 close。**建議**：只有 EOF、owner 換人等同步清理走只撤紀錄；明確 `interactive_close`／`console_detach` 保留原有撤權意義。**理由**：避免「close 後立即自動補回」而讓操作失效。 |
| 驗證範圍【需編排者定案】 | **選項**：等待 Windows 才測／Linux 真 TCP＋fake UART 先交付。**建議**：後者；Windows 原生與 TeraTerm smoke 由編排者安排。**理由**：現有設施足以驗證產品接線，且不需接觸實體 COM。 |

以上是建議，尚不視為編排者已核准。兩項需特別寫入裁決：**file RPC 文件修正**、**具名既有測試的 fixture 調整**。

4. **契約與相容性**

**BC-23**

在 `contracts/behavior_changes.yaml` 登錄：

- `kind: BC`
- `scope: rust_only`
- `ab_participation: absolute_only`
- `approval.decision_ref: OD-111`
- `related_decisions` 包含 OD-111、OD-112。
- `applies_to.python: null`；Rust 對應 console Slice 2.2。
- `phases` 建議 `P6`、`P7`，涵蓋 console lease 與 reprobe；coverage owner 為 `P6`。
- 文字明列 presence 回報、active／idle 仲裁、foreground 保留鏡像、EOF／重連／stash 清理，以及來源政策不因標籤改變。
- 實作完成才標 `IMPLEMENTED`；revision 單調前進，`approved_at_revision` 填最後修改該條目時的 revision，不硬填目前版本。

**必要治理變更**

- `contracts/coverage_map.yaml` 新增 `bc:BC-23`，映射第 5 節實際新增且已鎖的 Cargo 節點。
- `l1_only_approvals` 加入裁決指定的固定理由，替換為 `bc:BC-23`。
- `tests/test_contracts_catalog.py` 的 `_V051_BC_IDS`、`_V051_BC_APPROVAL_DATES` add-only 加 BC-23 與實際核准日期。
- `tests/test_rust_contract_coverage.py` 的 `_V051_ADDED_CONTRACT_IDS` add-only 加 `bc:BC-23`，保留原有 269 基數及其他斷言。
- 本包沒有 P4 變更，不增加 P4 case map／lock；不修改 `tests/acceptance/cases.yaml`。
- 不新增錯誤碼，不修改錯誤碼全集或既有 BUSY 優先序。

**Schema、golden、鎖**

- RPC／CLI schema 的鍵、型別、順序不變；mirror 標記不出現在 wire 或 state persistence。
- Python golden、daemon logging 行號與 Python code fingerprint 回到承接前基線；不把作者為 Python 修正產生的機械同步留下。
- 新 Rust 情境以明確斷言驗證，不重錄既有 Python golden。
- `contracts/` 有變動，`contract_revision` 的 tree SHA 會改變；僅由既有工具計算／記錄，不手寫進 YAML。
- 第 5 節新增 Cargo 節點，按既有 selector 以 L1 add-only 加到 Linux 的：
  - `p7_gate:cargo-test-workspace`
  - `s21_core`
  - `s22_gate:cargo_workspace`
- 收集輸入須包含各集合既有節點；不改 selector、不移除節點。Windows 僅列待原生收集名單。

**文件**

- README 英文與繁中 Windows console／human coexistence 段。
- `docs/serialwrap-spec.md` 的 console／lease 概覽。
- `openspec/specs/session-interactive/spec.md`、`session-selftest/spec.md`，補明確標示 Rust 0.5.1／BC-23 的差異說明。
- `sw_core/assets/skill/SKILL_WINDOWS.md` 手寫段落；OD-112 允許修改出貨文件資產。
- 沿用並改寫作者 `changelog.d/275-tcp-console-lease.md`，不得宣稱 Python 已修、file bare human 必然 BUSY，或 Windows 實機已驗證。

5. **測試計畫**

下列名稱為**擬新增節點**。使用既有 `CoreHarness`、fake clock、`ServiceRig`，不另造一套 harness。

| 檔案 | 測試名 | 斷言重點 |
|---|---|---|
| `crates/sw-core/tests/tcp_console_lease.rs` | `tcp_mirror_lifecycle_and_guards` | READY／ATTACHED 建一次鏡像；無 bridge、其他狀態、stop／flash／freeze 不新建；PTY／com0com 不鏡像；既有 agent lease不被覆蓋。 |
| 同上 | `tcp_mirror_tracks_active_suspended_pending` | 三種 owner 位置皆保留同一 mirror ID；第二 client 不取代它；多次 refresh 不重建。 |
| 同上 | `tcp_mirror_does_not_mutate_owner_fence` | 補／撤簿記不改 ownership、admission 或 `owner_gen`；LeaseIndex 在 Reply 前；wire 不含內部欄位。 |
| 同上 | `tcp_mirror_eof_reconnect_preserves_new_owner` | EOF 立即撤 A；B 在舊 grace 窗內仍保有 raw、鏡像及可送鍵能力；舊 lifetime EOF 不傷新 client；涵蓋 B accept 與 A EOF 的兩種順序。 |
| 同上 | `tcp_mirror_agent_open_arbitration` | 從未鍵入／超過活動窗可 soft preempt；活動窗內精確 BUSY；原 client 存活時 close／expiry 還原。 |
| 同上 | `tcp_mirror_preempt_reconnect_preserves_new_owner` | stash A→EOF→B suspended→agent close／expiry；不復活 A、不清 B、不回放 A 的 deferred。 |
| 同上 | `tcp_mirror_foreground_refresh_keeps_lease` | agent 命令在途觸發 self-test／refresh，mirror 不消失；suspend 平衡、結束只回放一次。 |
| 同上 | `tcp_mirror_reprobe_respects_activity_window` | ATTACHED、具備其他 reprobe 條件：59／60 秒零 probe、超過 60 秒才放行；pending／suspended 不繞過 foreground／probe gate。 |
| 同上 | `tcp_mirror_secondary_line_respects_lease_busy` | 第二 console 保有 line editor；非 interactive 命令進 registry 後精確 BUSY、零 UART TX；解除 lease 後原 line 行為可用。 |
| `crates/sw-daemon/tests/tcp_console_lease.rs` | `p279_tcp_socket_selftest_and_arbitration` | 原生 localhost socket accept、鍵入、RPC self-test 回 human 欄位；fake clock 驗 active BUSY／idle soft preempt／restore。 |
| 同上 | `p279_tcp_socket_eof_reconnect` | 真 socket EOF→A 消失→B 接入；B 的 raw bytes 確實到 fake UART 與 WAL。 |
| 同上 | `p279_tcp_socket_foreground_refresh_and_replay` | 真 TCP 在 agent 命令中輸入、查 self-test；命令前景內不插入 human TX，結束後只回放一次。 |
| 同上 | `p279_tcp_rpc_source_policy` | bare human 非 interactive command 遇鏡像精確 BUSY、零 TX；file push／pull 保留 agent 語意並成對 suspend／resume。 |

先在未修 Rust 上執行新測試，保存能指向本缺陷的紅燈；既有 guard 反例可以先綠，不要求每支新測試都紅。修正後以相同命令取綠燈。

**既有回歸**

整檔驗證 `session_core_console`、`session_core_lease`、`human_tx_lane`、`console_hub`、`rpc_console`、`windows_console_native`。至少保留以下斷言：

- endpoint 查詢本身不建立 lease。
- PTY peer grace、stash expiry、D-16／D-22 回放與 handoff freeze 不變。
- `owner_change_updates_fence_before_tx` 仍只增加一次 generation。
- 第二 console 有 human lease 時既有 BUSY 規則不被繞過。

經編排者核准後，僅調整以下節點的無 lease fixture，保留原有斷言與 nodeid：

- `session_core_console::line_input_echo_then_submit_order`
- `session_core_console::last_line_before_disconnect_is_submitted`
- `session_core_lease::human_reboot_opens_lease`
- `session_core_lease::human_timeout_promoted_only_for_internal_origin`
- `session_core_lease::human_line_timeout_sends_no_rescue_keys`
- `windows_console_native::telnet_second_client_line_mode`

其他紅燈先定因，不自行擴充此清單。

**驗收命令**

以下供實作鏈執行，本次未執行；環境、heavy-slot、輸出保存依共同規則。

```bash
heavy-slot serialwrap-v051-p279 nice -n 10 cargo test -p sw-core --features test-fakes --test tcp_console_lease
heavy-slot serialwrap-v051-p279 nice -n 10 cargo test -p sw-daemon --features test-fakes --test tcp_console_lease

heavy-slot serialwrap-v051-p279 nice -n 10 cargo test -p sw-core --features test-fakes --no-fail-fast
heavy-slot serialwrap-v051-p279 nice -n 10 cargo test -p sw-daemon --features test-fakes --no-fail-fast

heavy-slot serialwrap-v051-p279 nice -n 10 cargo check --workspace --tests --features test-fakes --target x86_64-pc-windows-gnu
heavy-slot serialwrap-v051-p279 nice -n 10 cargo clippy -p sw-core -p sw-daemon --tests --features test-fakes -- --cap-lints warn

heavy-slot serialwrap-v051-p279 nice -n 10 python3 -m pytest -q \
  tests/test_contracts_catalog.py \
  tests/test_contracts_slice22.py \
  tests/test_rust_p4_case_map.py \
  tests/test_check_p4_cases.py \
  tests/test_rust_contract_coverage.py

nice -n 10 python3 -m tools.contracts check
nice -n 10 python3 tests/acceptance/regen_lock.py --check
nice -n 10 python3 -m policy_check --repo . --pr-base-ref origin/main
```

L1 以 `scripts/rust/cargo_l1_results.py --list-only` 按各集合既有 selector 收集，再執行 `regen_lock.py --l1-add-only`；只收集、不執行 workspace 全套或正式 gate。

回歸 case 歸屬：本缺陷可用 Linux 真 TCP＋fake serial 重現，建議不新增實體硬體 `regression/` case，PR 明寫此評估。Windows 原生測試與 TeraTerm smoke 列為未取得證據。

6. **風險與不做的事**

- 最大風險是只修 active owner，漏掉 suspended／pending 或 stash 收尾；三者都屬本包必要驗收。
- 鏡像簿記若呼叫一般授權／close，會多改 fence generation、清 B，或失衡 suspend；不得以重授 owner 掩蓋。
- EOF 事件尚未被 actor 消費前，狀態只能反映已處理事件；不承諾跨 Control／Data lane 的全域瞬時排序。
- 不改 POSIX suspend 中 refresh 的既有獨立問題，不順手重構所有 lease。
- 不改 i227 登入失敗分類與 bridge 生命周期，不改 i294 完成判定。
- 不改 com0com、Telnet 協商、listener 埠策略、來源授權、活動窗、實體裝置或正式 daemon。
- 不新增錯誤碼、不放寬斷言、不刪鎖定節點、不修改 Python production code。
- 本包不做額外整包多輪審查。Windows 原生／實機證據與範圍外問題分別列後續；核心鏡像、EOF、仲裁及重連不可留作後續卻宣稱完成。

=== BRIEF ===

0.5.1 工作包 p279：承接外部 PR 279（issue 275），將 Windows TCP console raw ownership 的 session 鏡像 lease 修正交付到 Rust。

起點：分支 `<由編排者填入>`，HEAD `<由編排者填入>`。

作者為 `weisheng-lin-227`／`weisheng_lin`；作者 head 為 `17d673c110d09a4f85b6e78ced0520729c61f744`，包含功能 commit `e56ed46a39bd7ca2dba3f89162d1f145aa3c88d5`。以 merge 承接作者歷史，再以新 commit 撤除 Python 部分，不改寫作者 commit。

=== 背景 ===

- Rust `console_connected()` 已在 accept 時授 raw ownership，卻沒有建立 session lease；self-test、human-active reprobe 與 agent interactive 仲裁因此看不到 TCP 人類。
- 現有 refresh／self-heal 補不上「已有 owner、沒有 lease」。
- 直接套一般 human lease close，會清掉 A 斷線後 B 的 raw owner；agent soft preempt 的 stash close／expiry 也有相同清理風險。
- Linux 已有 native TCP listener 與 `ServiceRig::with_native_tcp_console()`，可以用真 socket＋fake UART 驗證。
- 修正登錄 **BC-23**，只適用 Rust。設計細節採編排者對上方 DESIGN 第 3 節的定案。
- i227 處理登入失敗是否保留 bridge；i294 處理完成判定，均不納入本包。

=== 本包要做的事 ===

1. **保留來源、撤除 Python 修正。**
   - 保留 merge 承接的兩筆作者 commit。
   - 以承接前起點的內容，撤除作者對下列路徑的變更：
     - `sw_core/session_manager.py`
     - `sw_core/uart_io.py`
     - `tests/test_serial_port.py`
     - `tests/test_tcp_console_lease.py`（起點不存在時移除作者新增檔）
     - `contracts/daemon_logging.yaml`
     - `tests/test_contracts_keep_baseline.py`
     - `contracts/cli_help/linux/action_version.json`
     - `contracts/cli_help/linux/action_version_session.json`
     - `crates/sw-cli/tests/argparse_golden.rs`
   - 只撤作者差分，保留起點已有變更；不拿作者舊 base 覆蓋現行 main。
   - README、Windows SKILL 與 fragment 保留可用內容，改成正確的 Rust 0.5.1 說明。

2. **共用 lease 模型並補 actor 接線。**
   - `InteractiveLease` 增加不透出 RPC 的 TCP mirror lifetime 標記。
   - actor 內查詢 active／suspended／pending 的存活 TCP owner，建立及同步鏡像。
   - 接入 accept、refresh、reprobe、自癒及 readiness 落定；不覆蓋 agent／一般 human lease。
   - 新建鏡像遵守 bridge、狀態、stop、flash、handoff guard。
   - 鏡像簿記不得重授 raw owner、額外增加 `owner_gen` 或新增 admission lease flag。

3. **完成 EOF、重連與 agent 仲裁。**
   - EOF 沿既有 data lane／lifetime 防線，立即清該 client 的鏡像與失效 stash。
   - A→B 重連只撤 A，不能清 B；涵蓋 agent soft preempt 的 close／expiry 收尾。
   - READY 下 human active 回既有 BUSY、idle soft preempt，close／expiry 還原；一般 agent 命令沿用 suspend／resume。
   - agent 命令在途查 self-test／refresh 不得拆鏡像；deferred 只回放一次。
   - 不新增第二 console 的立即接班機制，沿用現有 self-heal。

4. **先紅後綠及既有回歸。**
   - 新增 DESIGN 第 5 節兩個 `tcp_console_lease.rs` 測試檔與具名節點。
   - 至少取得真 socket→Hub→actor→RPC→fake UART／WAL 的接線證據。
   - 既有 line／timeout／reboot 節點如需改無 lease fixture，只限編排者核准的六個節點；保留原斷言及 nodeid，另加鏡像存在時 BUSY＋零 TX 反例。
   - 其他紅燈定因後回報，不自行放寬或擴權修改。

5. **完成契約、L1 與文件。**
   - BC-23：`kind: BC`、`scope: rust_only`、`ab_participation: absolute_only`、`decision_ref: OD-111`。
   - 更新 coverage、l1-only approval、兩個 V051 ID 集合及核准日期。
   - 新 Cargo 節點 add-only 登錄 Linux 三個共同規則集合；Windows 待原生收集清單另列。
   - 不改 schema 形狀、Python golden 或錯誤碼全集。
   - 同步 README 中英、console／self-test 規格、Windows SKILL 手寫段落與作者 fragment。
   - 修正作者「file push/pull bare human 必然 BUSY」敘述：保留現行 file RPC agent 政策。
   - PR 明列來源作者、Python 撤除、BC-23、回歸 case 評估及尚未取得的 Windows 證據。

=== 不得做的事 ===

- 不改 Python production code，不採用作者 Python 測試與其機械同步 golden。
- 不改 POSIX lease／peer grace 的既有語意、com0com、Telnet、來源授權或活動窗。
- 不修 i227、i294、issue 264，亦不擴充 bridge 生命周期。
- 不新增錯誤碼、不刪除或改名既有測試、不放寬斷言、不移除鎖定節點。
- 不把 Linux native TCP 或 Windows cross-check 宣稱為 Windows 原生／COM／TeraTerm 實機通過。

=== 驗收 ===

- 新增測試有可歸因的紅燈與修正後綠燈。
- `sw-core`、`sw-daemon` 含 `test-fakes` 的 crate 測試通過。
- Windows GNU 目標的 workspace tests cross-check 通過。
- 受影響 crate clippy 完成，檢視本包檔案的診斷。
- DESIGN 第 5 節列出的五個契約治理 pytest 檔、`tools.contracts check` 通過。
- L1 add-only 登記完成，`regen_lock.py --check` 通過；policy check 為 0 fail。
- 核對作者 head 確實為交付 HEAD 的 ancestor；撤除清單中的基線內容與承接前起點一致。
- 一輪實作、一次 terra 審查；完整性核對涵蓋上述全部工作，不以測試全綠代替交付完整。
- Windows 原生收集與 TeraTerm smoke 由編排者安排；未執行者明列，不預標 PASS。