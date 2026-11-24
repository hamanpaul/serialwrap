=== DESIGN ===

**1. 事實核對**

查核基準為本機 `HEAD=origin/main=e5cee8c3c5242f25b9aca4fb0d8b6485de9ff112`；工作目錄無變更。以下為讀碼結果，未執行測試或實機重現。

| 項目 | 現行行為與證據 |
|---|---|
| 終態阻擋重探 | [reprobe.rs:127](/home/paul_chen/prj_pri/serialwrap-worktrees/v051-design/crates/sw-core/src/reprobe.rs:127) 對 `CREDENTIALS_UNRESOLVED` 直接早退；這是必須保留的帳密保護。 |
| STALL 仰賴 probe 失敗 | [transport_stall.rs:16](/home/paul_chen/prj_pri/serialwrap-worktrees/v051-design/crates/sw-core/src/transport_stall.rs:16) 僅接受指定 prompt 錯誤；必須 probe 期間零 RX、曾有 RX、最後 RX 距今至少 30 秒才精煉為 `TRANSPORT_STALL`。正式接線在 `session_core.rs:5543` 的 `refine_login_failure()`。 |
| self-test 的遮蔽 | [session_core.rs:8886](/home/paul_chen/prj_pri/serialwrap-worktrees/v051-design/crates/sw-core/src/session_core.rs:8886) 的 ATTACHED 分支沒有被動停流判定，最後回 `ATTACHED_NOT_READY／console_attach`（`:8949`）。issue 所稱「Rust 找不到此字串」已過期。 |
| 現有觀測可沿用，但不能直接當新證據 | `session_core.rs:1829` 已輸出 RX／TX age；`:1530` 在收到 `UartTxCompleted` 時更新 `last_tx_mono`，沒有先要求 `confirmed_bytes>0`。因此新診斷不得直接以 `last_tx_age_s` 認定確實送出 bytes。 |
| 原始 RX 與 actor 有時序差 | [uart_bridge.rs:526](/home/paul_chen/prj_pri/serialwrap-worktrees/v051-design/crates/sw-core/src/uart_bridge.rs:526) 先更新 `RxShared`，之後才將 RX 排入 data lane。只讀 actor 的 `last_rx_mono`，可能把尚未 dispatch 的 RX 誤判成停流。 |
| 從未 RX 不能靠時間戳辨識 | `uart_bridge.rs:124` 建立 `RxShared` 時將 `last_rx_mono` 設為建立時間。新判定須有本 bridge 世代的非空 RX 計數，不能用時間戳非零代表曾收資料。 |
| 平台能力 | [device_source/mod.rs:18](/home/paul_chen/prj_pri/serialwrap-worktrees/v051-design/crates/sw-pal/src/device_source/mod.rs:18) 的 `UsbTopology` 只提供 busid；POSIX 解析 sysfs，Windows 回 `None`。它不提供 URB STALL 佐證。 |
| 終態契約 | [session_last_errors.yaml:156](/home/paul_chen/prj_pri/serialwrap-worktrees/v051-design/contracts/session_last_errors.yaml:156) 明定 `CREDENTIALS_UNRESOLVED` 為終態、不自動重探、`detail=null`，且禁止放入 RPC error-code namespace。 |
| 輸出契約 | `contracts/rpc_schema.json:65` 定義 `SessionPublic`，`:493` 禁止額外欄位；`:6540` 起以分支定義 self-test。目前沒有獨立 transport 健康物件或 transport self-test 分類。 |

既有測試：

- `crates/sw-core/tests/transport_stall.rs`：鎖住原 probe 判定矩陣、30 秒邊界、提示文字、從未 RX、計數倒退與告警去重。
- `crates/sw-core/tests/transport_stall_wiring_p7.rs:70` 起：驗證 probe 失敗到 actor／WAL 的接線；沒有終態下的獨立被動檢測。
- `crates/sw-core/tests/lease_selftest.rs:641`：分類優先序；`:782` 起涵蓋 passthrough、quiet、probe 等路徑。
- `crates/sw-daemon/tests/session_rpc_p7.rs:160`：已有缺帳密的 `ServiceRig` 夾具，可沿用其設定方式建立本包測試。

**Python 0.4.x 基線已對照**：目前 checkout 的凍結 `sw_core/transport_stall.py:27–69` 同樣依賴 probe 失敗；`sw_core/session_manager.py:2199` 對缺帳密早退，`:7801` 回 `ATTACHED_NOT_READY／console_attach`。本次未另外 checkout 歷史 0.4.1，亦未重跑 issue 的 FTDI 情境。

issue 對其他終態的推論須修正：現行 Rust `reprobe.rs:76–89` 已依 DF-27 納入 `POST_LOGIN_CMD_TIMEOUT` 重探，不能再將它一概描述為「不自動重探」。本包診斷應獨立於重探 allowlist。

**2. 方案**

**本包最小可交付：一個子項 `i267-i1`，完成被動觀測、加法欄位與 self-test 接線。** 不納入核心日誌讀取、通知或復原操作。

建議新增條件式欄位 `transport_health`：

```json
{
  "status": "stall_suspected",
  "evidence": ["rx_silence", "confirmed_tx_without_rx"],
  "rx_age_s": 65.0,
  "tx_wait_s": 12.0,
  "bridge_generation": 1,
  "busid": "1-3",
  "corroboration": "not_collected",
  "hint": "觀測到長時間無 RX，且既有 TX 後仍無 RX；尚未證實 USB STALL。……"
}
```

- `status` 本輪只有 `rx_quiet`、`stall_suspected`。
- 單純長時間無 RX：`rx_quiet`，`tx_wait_s=null`，不宣稱故障。
- 有符合資格的既有 TX，之後仍零 RX：`stall_suspected`。
- 正常、尚未達門檻、從未 RX、無有效 bridge：省略欄位。**省略不代表 transport 已通過健康驗證。**
- `session.list` 放在每個 session 內；self-test 的巢狀 `session` 沿用同物件。新 transport 分類分支另於頂層附相同 `transport_health`。
- 不變更 `last_error`、`last_error_detail`、readiness、重探預算或持久化狀態。

預計修改位置：

| 檔案／函式 | 修改內容 |
|---|---|
| `crates/sw-core/src/transport_stall.rs` | 新增被動觀測型別、純判定函式 `classify_passive_transport()` 與 `passive_transport_hint()`；保留現有 probe helper 與文字。 |
| `crates/sw-core/src/uart_bridge.rs`：`RxShared`、`append()`、`IoWorker::start_observed()` | 新增診斷專用、帶 epoch 的觀測資料與一次取樣介面；worker 啟動前初始化。非空原始 RX 立即更新並清除 TX witness；不改既有 RX 計數、tail 或清除語意。 |
| `crates/sw-core/src/tx_writer.rs`：`spawn_tx_writer()`、實際 `write_gated()` 周邊 | 注入被動觀測 hook；只記錄已確認送出的 TX，不改提交、授權、receipt、WAL 或重送行為。既有直接呼叫端可傳無 hook。 |
| `crates/sw-core/src/session_core.rs`：`public()`、`self_test_pre_probe()` | 從同一份觀測快照產生診斷；在一般 ATTACHED 分類及送 probe 之前處理疑似停流。 |

資料流：

1. RX reader 收到非空 bytes，更新當前 epoch 的原始 RX 計數、時間與序號。
2. writer 在真正寫入前取得 RX 序號；寫入結果有已確認 bytes、且不屬 unknown outcome 時，再比較序號。期間已有 RX 就不建立「TX 後無 RX」witness。
3. witness 記錄完成觀測時間；持續 TX 保留本次 RX 靜默期間的**第一筆**有效 witness，不延後判定期限。
4. `public()`／self-test 讀取一致快照後純計算結果，不另外啟動 timer、probe、開埠或掃描 WAL。
5. RX 恢復即清 witness；下一次查詢自動移除停流標記。所有晚到的舊 epoch 更新均不得影響新 bridge。

觀測鎖只涵蓋取樣與更新，不跨 UART I/O、WAL 寫入、sysfs 查詢或 RPC 等待持有。

這是**查詢時評估的被動診斷**；沒有新增背景警報或主動通知。

工作包交界：

- **p287**：維持「從未 RX、probe 確實送出後失敗」的 detail 範圍。本包須曾有當前世代 RX，兩者互斥；不以本包 witness 假裝執行過 probe。
- **i268**：不調整缺 secret 的登入決策；即使其後允許其他驗證，本包仍不依賴登入是否執行。
- **i300**：不監測 DETACHED 的開埠重試、不增加 attempt、不改退避。
- **i306**：沿用其忙碌／quiet／操作權保護；疑似停流不得成為送 probe 的例外。
- **i311**：TX 觀測只需結果、時間及序號，不接觸或保存 payload，不受 secret 是否寫入 WAL 影響。

**3. 設計決策**

下列建議均待依 OD-112 記入裁決後實作。

| 決策 | 選項／建議／理由 |
|---|---|
| 【需編排者定案】診斷位置 | **選項**：併入 `last_error_detail`／獨立物件。**建議**：條件式 `transport_health`。**理由**：保留帳密終態及既有 detail 契約；避免每個正常回應都增加欄位。 |
| 【需編排者定案】取樣門檻 | **選項**：沿用 probe 的 30 秒／獨立被動門檻。**建議**：RX 靜默 **60 秒**標 `rx_quiet`；另有有效 TX 且完成觀測後 **10 秒**仍無 RX，才標 `stall_suspected`。**理由**：被動觀測證據較弱；60 秒是保守預設，並非經真機校準的最佳值。既有主動判定仍為 30 秒。 |
| 【需編排者定案】只有靜默的分類 | **選項**：超時即判 STALL／只呈現靜默。**建議**：無 TX witness 只標 `rx_quiet`，不以 transport 分類取代既有 self-test。**理由**：一般 shell 可無限期靜默。這將需求「凍結就標記」落為可見的靜默標記，並非無條件故障分類；若驗收要求無 TX 也必須回 STALL，須明確接受誤判代價。 |
| 【需編排者定案】疑似與已佐證 | **選項**：以 TX 無回應視為已證實／區分被動證據與 USB 原因。**建議**：本輪永遠 `corroboration=not_collected`，只回疑似。**理由**：TX 無回應仍可能是 DUT 關機、程式不回應或 echo 關閉；busid 存在也不是 USB STALL 佐證。 |
| 【需編排者定案】self-test 輸出 | **選項**：維持原分類只加 detail／新增 transport 分支。**建議**：疑似時 `ok=true`、`classification=TRANSPORT_STALL`、`recommended_action=check_host_transport`，不附 `probe_ok`。**理由**：診斷成功與 DUT 健康分開；這是 classification／action 值，沒有新增錯誤碼。提示明列「疑似」。 |
| 【需編排者定案】「不改既有欄位」的解讀 | **選項**：連 classification 值也不變／保留欄位形狀、修正目標情境的值。**建議**：採後者。**理由**：需求同時要求 self-test 不再回 `ATTACHED_NOT_READY／console_attach`，兩者若按字面解讀即矛盾。BC-21 明列此值的變更。 |
| 【需編排者定案】適用範圍與優先序 | **選項**：覆蓋所有狀態／只覆蓋有效的 ATTACHED、READY 非 passthrough bridge。**建議**：採後者；保留 RELEASED、RECOVERING、strict human lock、device／bridge／vtty 故障及既有 busy／quiet 分支優先序。**理由**：不把交接、停機或 passthrough 正常沉默重解釋成 STALL。其他分支仍可從巢狀 session 查看被動觀測。 |
| 【需編排者定案】清除與保存 | **選項**：持久化 latch／目前 bridge 的暫態觀測。**建議**：RX 恢復、bridge 更換或退出監測狀態即清；不寫 `state.json`。**理由**：舊設備或 daemon 重啟前的證據不能沿用；清除診斷不清帳密終態。 |
| 【需編排者定案】平台降級與指引 | **選項**：Linux 專用／共通被動判定、平台別提示。**建議**：Linux 有可信 busid 時附人工 re-enumeration 指令，先要求核對 host 證據；無 busid 不猜路徑。Windows 提供裝置及 USB 連線檢查，不輸出 Linux 指令。**理由**：純計數判定可攜，復原能力並不相同。 |

誤判控制的邊界：即使符合 60／10 秒及 TX witness，仍只支持「疑似」。本輪不推斷週期性輸出、不自動學習 cadence，也不因 human 已 attach 而排除觀測。

**4. 契約與相容性**

- 登錄 **BC-21**：`kind: BC`、`scope: rust_only`、`ab_participation: absolute_only`、`decision_ref: OD-111`、`related_decisions` 含 OD-111，`phases: ["P7"]`。記錄 Python 舊行為、條件式欄位、門檻、清除、self-test 新分支及「未證實 USB 原因」。
- `contracts/behavior_changes.yaml` revision 在實際 base 上單調前進；`approved_at_revision` 對應該次變更，不預寫固定數字。
- `contracts/rpc_schema.json` 新增封閉的 `TransportHealth` 定義；`SessionPublic` 新增 optional property 與對應 `x-order`；self-test 新增精確的 transport 分支，保留既有分支斷言。前進 `x-catalog-revision`。
- 不改 `contracts/error_codes.yaml`、`session_last_errors.yaml` 的碼集合及終態設定；不改 CLI 參數，故本包不需改 `cli_schema.json`。
- `coverage_map.yaml` 新增 `bc:BC-21`、`owner_phase: P7`、下節實際登鎖的 Cargo 節點及裁決指定的 `l1_only_approvals`。兩個既有集合 `_V051_BC_IDS`、`_V051_ADDED_CONTRACT_IDS` 只追加本包 ID，不改既有項目或 269 基數。
- 不涉及 P4，不新增 `p4_case_map` 或 `cases.yaml` 案例。
- **保留全部 Python golden**，包括 `transport_stall.ndjson`、`self_test_responses.ndjson`、hint golden。本包用獨立 Rust 測試斷言新行為，不修改舊 oracle 或比較器。
- 契約樹改動後，發布產物的 `contract_revision` 雜湊必須改變並由既有工具重算；不在權威 YAML 新增同名鍵。
- 文件同步：README 中英診斷說明、`docs/serialwrap-spec.md`、`openspec/specs/session-selftest/spec.md`；說明條件式欄位、疑似程度、門檻、清除、平台差異及帳密問題仍待處理。
- 新增 `changelog.d/267-passive-transport-health.md`，`type: fix`、含 `scope`；不改版本號。

新增 Cargo 節點依共同規則加入 Linux 的 `p7_gate:cargo-test-workspace`、`s21_core`、`s22_gate:cargo_workspace`；各 collect 檔須包含集合既有節點。Windows 節點只列待原生收集，不寫入 Windows 鎖。

**5. 測試計畫**

以下為**擬新增測試名**，目前尚未建立或執行。

| 檔案 | 測試名 | 斷言重點 |
|---|---|---|
| `crates/sw-core/tests/transport_health_i267.rs` | `passive_threshold_boundaries` | 60 秒、10 秒前後與等號；只靜默為 `rx_quiet`，兩條件齊備才疑似。 |
| 同上 | `never_rx_and_new_epoch_do_not_inherit_stall` | 本世代從未 RX、mono=0、舊世代 RX／TX、重接後零 RX 均不誤判。 |
| 同上 | `confirmed_tx_witness_uses_first_unanswered_write` | 重複 TX 不延後期限；Rejected、NotSubmitted、unknown 不建立 witness；寫入期間收到 RX 不建立 witness。 |
| 同上 | `terminal_errors_do_not_mask_passive_health` | 表格覆蓋 `CREDENTIALS_UNRESOLVED`、`LOGIN_REQUIRED`、`PASS_ENV_MISSING` 及其他適用錯誤；不變更原錯誤、detail、state 或重探資料。此為注入狀態測試，不聲稱每個組合都有真機證據。 |
| 同上 | `raw_rx_before_actor_dispatch_clears_stall` | reader 已收到 RX、actor 尚未 dispatch 時即不得回疑似；空 bytes 不清除，非空原始 bytes 即清除。 |
| 同上 | `selftest_transport_priority_and_guards` | 疑似時無 `NeedProbe`；既有安全／busy／quiet／passthrough 分支仍符合優先序。 |
| 同上 | `passive_hint_is_platform_specific` | 有 busid、無 busid、Windows；不使用「probe 全程無 echo」等未發生的敘述。 |
| `crates/sw-daemon/tests/transport_health_rpc_i267.rs` | `credentials_terminal_stall_is_visible_through_rpc_without_tx` | 真 `ServiceRig` 進入缺帳密終態，先 RX、再由既有 human console 送 TX、凍結 RX；`session.list` 與 `session.self_test` 都可見診斷及 busid 指引，self-test 分類／action 正確。 |
| 同上 | `rx_resumption_clears_health_preserves_credentials_terminal` | RX 恢復後欄位消失，缺帳密終態與原 detail 不變；再次停止需重新符合條件。 |
| 同上 | `normal_quiet_and_never_rx_are_not_reported_as_stall` | 正常持續 RX、無 TX 的長時間靜默、從未 RX 都不回 STALL。 |
| 同上 | `human_agent_and_interactive_tx_share_passive_observation` | 各正常 TX 入口均接到同一觀測；診斷本身沒有 TX。 |

先以 RPC 主案例在舊行為下取得缺少欄位／錯誤分類的紅燈，再完成實作。零 TX 的基準取在測試安排的人工 TX **完成之後**；診斷前後比對 UART bytes、`write_calls()`、`os_submits()` 與 WAL TX，不只比對回傳值。

由 FakeClock、barrier 和既有 fake serial 控制交錯，不以真實等待 60 秒或加大 timeout 取得綠燈。

供實作鏈使用的驗收命令；依共同規則設好獨立目錄，重負載命令包 `heavy-slot serialwrap-v051-i267 nice -n 10`、前景執行並保存輸出：

```bash
cargo test -p sw-core --features test-fakes --test transport_health_i267
cargo test -p sw-daemon --features test-fakes --test transport_health_rpc_i267

cargo test -p sw-core --features test-fakes --no-fail-fast
cargo test -p sw-daemon --features test-fakes --no-fail-fast
cargo check --workspace --tests --features test-fakes --target x86_64-pc-windows-gnu
cargo clippy -p sw-core -p sw-daemon --tests --features test-fakes -- --cap-lints warn

python3 -m pytest -q tests/test_contracts_catalog.py tests/test_contracts_slice22.py tests/test_rust_p4_case_map.py tests/test_check_p4_cases.py tests/test_rust_contract_coverage.py
python3 -m tools.contracts check
python3 -m policy_check --repo . --pr-base-ref origin/main
python3 tests/acceptance/regen_lock.py --check
```

L1 使用實際收集 JSON，對三個指定集合逐一執行：

```bash
python3 tests/acceptance/regen_lock.py --l1-add-only --set <集合> --platform linux --collect <該集合完整收集JSON>
```

本輪不跑全套 pytest、workspace 全套測試、正式 gate 或 Windows 原生。原生跨平台及真實 FTDI／USB-IP 停流另列證據缺項。

**6. 風險與不做的事**

- **範圍裁決尚缺**：無 TX 的 RX 靜默是否必須改 self-test 分類，以及是否接受本輪只輸出疑似，須由編排者定案；不能由實作者暗中縮減 issue 驗收。
- **核心風險是觀測時序**：必須使用原始 RX、確認 TX 與 epoch；禁止以 actor 接收時間、WAL 是否有 payload 或單獨 age 欄位替代。
- **相容性風險**：忽略新欄位的客戶端可能繼續只看帳密錯誤；嚴格 schema 客戶端須更新。新分類值是 BC-21 的明示變更。
- **測試衝突**：若既有鎖定節點精確要求本票缺陷行為，列出 nodeid 與衝突交編排者裁決，不自行放寬。
- **後續**：自動取得 kernel／sysfs 故障佐證，須另設時間窗、裝置身分、epoch 關聯與權限降級，才能新增「已佐證」呈現。這項成本不適合塞入本輪。
- 不做 USB reset、通知／訂閱、週期學習、新設定入口、Python 修改、帳密補救、自動 TX、attach 重試或重探資格調整。
- 本次只交付設計與 brief；未修改檔案、建立 worktree／分支、commit、push、PR 或部署。

=== BRIEF ===

0.5.1 工作包 i267：修正終態 last_error 遮蔽被動 RX 停流診斷及 self-test 誤導分類。BC 編號 **BC-21**。採單一子項 **i267-i1**。

起點：分支 `<由編排者填入>`；起點 HEAD `<由編排者填入>`。設計讀碼基準為 `e5cee8c3`，實作前核對指定起點與裁決。

**背景**

- `reprobe.rs:127` 對 `CREDENTIALS_UNRESOLVED` 早退，這項帳密保護必須保留。
- 現有 transport stall 判斷僅接在 probe 失敗路徑，因此終態 session 即使原本收過 RX、後來停流，也沒有獨立診斷。
- `session_core.rs:8886` 的 ATTACHED self-test 最後可能回 `ATTACHED_NOT_READY／console_attach`，即使 human console 已經連著。
- Python 凍結。只修 Rust，差異登錄 BC-21；不新增錯誤碼。

**本包要做的事**

1. **完成同一條被動診斷資料流。**
   - 在 `transport_stall.rs` 新增純判定及被動提示；保留原 probe 判定與 golden。
   - 從 reader 的非空原始 RX、writer 的已確認 TX 取得證據，按 bridge epoch 隔離。
   - 不直接以現有 `last_tx_age_s` 當確實 TX；Rejected、NotSubmitted、unknown 不得建立 witness。
   - TX 寫入期間有 RX 時不建立 witness；持續 TX 不延後第一筆未獲 RX 的觀測期限。
   - 判定資料只存數值與時間，不保存 payload，不改既有 TX／WAL／receipt 語意。

2. **增加診斷輸出並接上 self-test。**
   - 建議門檻：當前世代曾有 RX，靜默 60 秒為 `rx_quiet`；另有確認 TX、其完成觀測後 10 秒仍無 RX，為 `stall_suspected`。
   - `session.list` 的 session 增加條件式 `transport_health`；self-test 使用相同快照。
   - 疑似時建議回 `ok=true`、`classification=TRANSPORT_STALL`、`recommended_action=check_host_transport`，附診斷物件，不附 `probe_ok`。
   - `corroboration=not_collected`；不得宣稱已證實 USB STALL。
   - 有可信 busid 才附該裝置的人工復原指引；Windows 不輸出 Linux 指令。
   - 保留既有安全、busy、quiet 與 passthrough 分類優先序。
   - RX 恢復或 bridge 世代更換即清除診斷；原本的 `last_error`、detail、state、重探資料不受影響。

   **以上門檻、欄位、優先序及「僅疑似」範圍，須以編排者記錄的定案為準。無 TX 的長時間靜默建議只加標記、不回 STALL。**

3. **契約、測試與文件隨實作交付。**
   - BC-21 採 `rust_only／absolute_only／OD-111`，所屬 P7。
   - 修改 RPC schema、coverage_map、l1-only 核准及兩個指定 BC 集合；不修改錯誤碼集合或比較器。
   - 新增 `transport_health_i267.rs` 與 `transport_health_rpc_i267.rs`，先紅後綠；至少有真 ServiceRig 的缺帳密終態接線案例。
   - 覆蓋門檻、正常 RX、從未 RX、RX 恢復、世代交替、RX 尚未 actor dispatch、TX 結果資格及分類優先序。
   - 診斷期間驗證 UART bytes、write、OS submit、WAL TX 均不增加。
   - 新節點加入指定 Linux L1 集合；保留 Python golden。
   - 同步 README 中英、診斷文件、self-test spec 及一份 changelog fragment。

**不得做的事**

- 不覆寫帳密終態，不啟動額外 probe、不自動送任何 bytes。
- 不改 Python、登入 gate、重探 allowlist、i300 退避、release／clear／bind／recover 的既有行為。
- 不新增背景通知、kernel 日誌收集、USB reset、cadence 學習、設定入口或持久化診斷。
- 不放寬既有斷言、刪除／改名鎖定節點或修改驗證器取得綠燈。
- p287、i268、i306、i311 的重疊位置由編排者安排合併順序；不另實作它們的功能。

**驗收**

- 上述新測試先紅後綠，保存輸出；ServiceRig 證明終態仍可看見疑似停流與正確 action。
- RX 恢復後診斷自動消失，缺帳密終態保持原樣。
- sw-core、sw-daemon crate 測試、Windows cross-check、相關 clippy、契約追加測試、contracts check、policy check、L1 check 通過。
- 契約 revision、BC-21 coverage、新增 nodeid 與 Windows 待收集節點交代完整。
- 一輪實作、一次 terra 審查；依 OD-114，本包另做必要的整包驗收重跑與一位審查者核對。
- 真實 FTDI／USB-IP STALL 與 Windows 原生未跑就列為未驗證。kernel 佐證、通知及 USB 復原留後續，不宣稱本輪完成。
- 若既有鎖定斷言或定案與實作需求衝突，明列 SPEC-GAPS 及受影響項目，不以 `REMAINING: none` 隱藏缺項。

