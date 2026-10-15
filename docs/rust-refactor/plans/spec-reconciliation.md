# 規格 ↔ 程式碼調和表（Spec Reconciliation）

> 本表逐項裁決 `docs/rust-refactor/inventory/01～10` 所列的差異、quirk、缺口與矛盾，作為各 Phase 實作依據。除文末「擁有者確認清單」外，不需再向擁有者確認。
> 共用架構見 `rust-architecture.md`。Phase 代號沿用 `00-master-plan.md`：P0 契約、P1 Python BC、P2 測試基礎、P3 Stage 1A、P4 Stage 1B、P5 Slice 2.1、P6 2.2、P7 2.3、P8 2.4、P9 Stage 3。

## 0. 裁決規則與代號

| 代號 | 意義 | 是否登記 `contracts/behavior_changes.yaml` | Python 與 Rust |
|---|---|---|---|
| **KEEP** | 保留 Python 基線，凍結為契約（規格 §1.3、§5.3 末段） | 否 | Rust 逐字承接 |
| **SPEC** | 定稿規格明文要求不同行為 | 是，`SC-xx` | Python 於 Stage 0 先改，Rust 承接；標「Rust-only」者除外 |
| **FIX** | 缺陷或安全修正 | 是，`DF-xx` | 兩邊都修 |
| **NEW** | 規格要求、Python 尚不存在的功能 | 是，`SC-xx` | 依列內註明，分雙邊或 Rust-only |
| **BC** | 已核准的 BC-01～BC-05 涵蓋 | 已有 | 兩邊都做 |
| **DOC** | 只修規格或文件勘誤，行為不變 | 否 | — |
| **TEST** | 修測試資產；依規格 §10.4 需獨立審閱 | 否 | — |
| **IMPL** | 內部實作差異，不構成對外契約 | 否 | Rust 自由實作 |

**needs-owner 標 yes 的條件**：變更會改到對外可觀測的契約，而且定稿規格沒有明文要求。標 yes 的項目一律先套用文末列出的預設值往下做，不因等待確認而停工。

**通則**（適用全表，不逐列重複）：

- **G1**：`EXCEPTION` 的 `message` 文字不屬契約；契約只比對 code。
- **G2**：Python 中順序未定義的集合（如 set 差集），契約定為集合，比較器以集合比對。
- **G3**：OS 錯誤訊息照 Python `str(OSError)` 的格式產生（arch §4）。
- **G4**：argparse help 以 Python 3.12 的輸出為 golden。
- **G5**：Python 印 traceback 的路徑，若本表未另行裁決，則保留 exit code，stderr 文字不屬契約。

---

## 1. 登記項目總表（寫入 `contracts/behavior_changes.yaml`）

### 1.1 SC：依定稿規格推導（含規格要求的新功能）

| ID | 內容 | 規格依據 | 範圍 |
|---|---|---|---|
| SC-01 | `RECOVERING` 狀態下的 CmdSubmit 與 InteractiveOpen 回 `SESSION_RECOVERING`（現況為 `SESSION_NOT_READY`） | §5.3 | 雙邊 |
| SC-02 | `FLASHING` 狀態下的 CmdSubmit 與 InteractiveOpen 回 `FLASHING_BUSY`（現況為 `SESSION_NOT_READY`） | §5.3 | 雙邊 |
| SC-03 | execute-time 與 file.* 經 manager gate 回 `AUTOBOOT_QUIET` 時也附 `retry_after_s` | §5.3 | 雙邊 |
| SC-04 | Drain 可觀測面：斷線、I/O error、release、clear、bind 的 detach 期間轉為 `RECOVERING`。release 必須等 handle 關閉回條後才回成功；超過 3000 ms 時設 `drain_stalled:true` 與 `drain_timeout_epoch`（取 `bridge_generation`），release 回 `SESSION_RECOVERING`。SessionPublic 新增 `drain_stalled`、`drain_timeout_epoch` 兩欄，位置在 `capture` 之後 | §5.2、§5.3、§9.4 | 雙邊 |
| SC-05 | `device.attach` 同步重新取得獨占：失敗回 `DEVICE_STILL_HELD{pids,holder_unknown}` 或 `DEVICE_NOT_PRESENT`；成功才回 `ATTACHING`。`force` 只略過開檔前預檢，不得略過已識別的外部 holder：POSIX 開檔在 `TIOCEXCL` 後、任何 I/O 前做鎖後 holder 重掃（未取 flock 的外部程式先開埠時 open／flock／TIOCEXCL 仍會成功），`pids` 非空即 `DEVICE_STILL_HELD`、零 TX；初次／熱插自動 attach 同樣適用（`ATTACH_FAILED:DeviceHeldError`） | §5.3、§6.2 交接、§6.2 POSIX「拒絕已識別的外部衝突」 | 雙邊 |
| SC-06 | `CMD_PENDING_MAX` 的計數納入「已 cancel 但 worker 仍在執行」的命令 | §5.4 | 雙邊 |
| SC-07 | TX WAL 只記錄 OS 已確認寫出的 bytes，順序等於實際寫入順序 | §8.1 TX、ADR-05 | 雙邊 |
| SC-08 | UDS 權限預設 `0600`，新建目錄為 `0700`；只有明確設定 `SERIALWRAP_SOCKET_GROUP` 時才用 `0660` 加 chgrp | ADR-04 | 雙邊（owner #3） |
| SC-09 | 設定探索順序：`SERIALWRAP_CONFIG`（指定 config.yaml）→ `CONFIG_DIR/config.yaml` → 系統目錄（`/etc/serialwrap/config.yaml` 或 `%ProgramData%\serialwrap\config.yaml`，唯讀 fallback） | §6.1 | 雙邊 |
| SC-10 | lock 與 socket 路徑在比較時先正規化（abspath、normpath，並去掉 `unix://` 前綴）；Windows Tier 1 改用 `LockFileEx`（與 `msvcrt.locking` 互斥相容） | §6.2 | 雙邊 |
| SC-11 | 有序關機：drain 所有 worker、關閉 handle、WAL sync，之後才 exit | §6.1 更新流程、§8.1、BC-01 | 雙邊 |
| SC-12 | 監管 reconcile 必須確認舊 daemon 已結束（ping 不通且行程消失，上限 10 s）才啟動新 daemon | §6.1 | 雙邊 |
| SC-13 | Interactive lease 到期除了存取時判定，也由 1 s tick 主動處理（還原 human／回放），並保留 tombstone | §5.1.3、§5.3 | 雙邊 |
| SC-14 | Flash probe 的資格加上：不在 RELEASED、RECOVERING 或 drain 中，且沒有前景操作 | §5.3 | 雙邊 |
| SC-15 | 每個 console 的出向 ring 為 1 MiB，滿時 drop-oldest。顯示資料原本就允許丟棄，契約只寫「可丟、不阻塞」 | §5.4 | Rust-only |
| SC-16 | console line buffer 上限 16385 B；被拒的整行（`CMD_TOO_LONG`、`SESSION_QUEUE_FULL`、`SESSION_NOT_READY`）寫 WARNING log 供診斷 | §5.3、§5.4 | 雙邊 |
| SC-17 | Windows 的 daemon 多開偵測與 CLI 跨 socket 預檢以行程掃描實作；Windows 的 holders 一律回 `unknown` | §6.2 | Rust-only |
| SC-18 | Windows `on-demand` 模式「首次需求啟動」：本機預設 endpoint 連不上時自動 `daemon start` 並重送一次；明確指定 endpoint、bench 或 remote 時不觸發 | §6.1 | Rust-only（1B） |
| SC-19 | OD-11 測試裝置 allowlist，細節見 §2 `GAP-G-07` | OD-11、§9.4 | 雙邊（P1） |
| SC-20 | 新子命令 `daemon restart`：systemd 與 windows 模式轉為 `service restart`；on-demand 模式執行 stop、等待結束、再 start（同一 endpoint） | §4.2 #14 | 雙邊 |
| SC-21 | `remote stop <port\|all>` 作為 `remote close` 的別名，原名照常可用 | §4.2 #17 | 雙邊 |
| SC-22 | `supervision-mode set <mode> [--force] [--with-sudo]`：輸出 JSON，走與 setup 相同的 reconcile；不帶參數時維持輸出純文字 | §4.2 #16 | Rust-only（1B） |
| SC-23 | `setup --rollback`、版本目錄、Current／Previous、規格 §12.1 的七步更新流程；第 2 步交接安全檢查不受 `--force` 影響（Flash／FileTransfer 等不可安全中止操作進行中一律拒絕，不停止監管或 daemon） | §4.2 #19、§12.1 | Rust-only（1B） |
| SC-24 | 監管模式 `windows-task`（`ExecutionTimeLimit=PT0S`）與 `windows-service` | §6.1、OD-03、OD-12 | Rust-only（1B） |
| SC-25 | release manifest，以及 doctor 的 `version_pairing` 檢查 | §9.5、OD-13 | Rust-only |
| SC-26 | Windows event handler：`exec` 走 CreateProcess 加 Job；`shell` 走 `cmd.exe /d /s /c`；timeout 時 TerminateJobObject；env 白名單另加 `SYSTEMROOT`、`COMSPEC`、`PATHEXT`、`TEMP`、`TMP`、`USERPROFILE` | §5.5 | Rust-only（owner #6） |
| SC-27 | BC-05 的 Windows remote 範圍延伸到 `connect` 與 `benches`，移除這兩者的 `REMOTE_NOT_SUPPORTED` | BC-05 | 雙邊（owner #7） |

### 1.2 DF：缺陷與安全修正（兩邊都修）

| ID | 內容 |
|---|---|
| DF-01 | `event.rule_delete` 與 `event.reset` 的 `rule_id` 必須符合 `^[a-z0-9][a-z0-9-]{0,63}\.[a-z0-9][a-z0-9-]{0,63}\Z`，否則不碰檔案。rule_delete 回 `{"ok":true,"deleted":false}`，reset 不計數。修正路徑穿越 |
| DF-02 | RPC 帶入的 `source` 以 `human:` 開頭時，gate 一律以 agent 對待，也不升格成 interactive（owner #5）。真正的 human console 行走 daemon 內部路徑，不受影響 |
| DF-03 | WAL 或 mirror 歸檔同名時加 `.1`、`.2`… 後綴，不覆蓋既有歸檔（rotate 與 reset 都適用） |
| DF-04 | 動態 session 的 RELEASED 要能跨任意次數重啟保留；裝置離線的 tick 不得把 RELEASED 降為 DETACHED |
| DF-05 | `daemon start` 的 daemon 定位順序：manifest launcher → 與 CLI 同目錄的 `serialwrapd` → Python `-m sw_core.daemon` fallback（POSIX 也加）；不再依賴套件內不存在的 `serialwrapd.py` |
| DF-06 | client 收到非 JSON 回應時回 `INVALID_RESPONSE`，exit 2（現況為 traceback） |
| DF-07 | `config.yaml` 有壞 YAML 時：`supervision-mode` 只在 stderr 印一行並 exit 1；`service`、`setup`、`daemon` 印 `{"error_code":"INTERNAL_ERROR","message":…,"ok":false}` 並 exit 1；`setup` 其餘未預期例外比照辦理 |
| DF-08 | `contains` 規則的 `flags` 也做驗證（訊息 `unsupported regex flag: X`），避免已落盤的規則讓下次 daemon 啟動失敗 |
| DF-09 | `RuleRegistry.load_all` 把 `ValueError` 與 `TypeError` 也當成 `FailedLoad`，不讓 `engine.start` 或 reload 崩潰 |
| DF-10 | `setup` 的 system scope 遷移目標改為 daemon 實際使用的 state 路徑 |
| DF-11 | `ensure_wsl_systemd` 只在目標是 systemd 模式且帶 `--with-sudo` 時才執行 sudo；否則把該命令列入 `pending_sudo` |
| DF-12 | setup／reconcile 的 `ran` 欄位填入實際執行過的命令 |
| DF-13 | `serialwrap-minicom-broker` 改找已改名的 `serialwrap-minicom` |
| DF-14 | daemon 啟動順序改為：initial scan → `prepare_dynamic_rank` → watcher.start，消除 #100 殘留的 race |
| DF-15 | RX observer、event engine、background capture 改用該 RX chunk 的精確 WAL seq |
| DF-16 | Windows `setup` 的 skill link 在不能建 symlink 時改為複製目錄 |
| DF-17 | WAL 與 mirror 寫入失敗時只重試失敗的那個檔，不再產生重複 seq 的 WAL 行 |

### 1.3 既有核准：BC-01～BC-05

照規格 §9.1 實作，各列以 **BC** 標示。

---

## 2. 逐項裁決

### 2.1 盤點 01（CLI）

| ID | 事實（現況） | 規格 | 裁決 | 理由 | 階段 | 需擁有者 |
|---|---|---|---|---|---|---|
| CLI-11.01 | 全域選項另有 `--version`、`-v/--verbose` 與 `SERIALWRAP_LOG_LEVEL` | §4.2 只列 5 個 | KEEP＋DOC | 現況就是契約；寫入 `cli_schema.json` | P0/P3 | no |
| CLI-11.02 | `--endpoint` 也接受 `unix:///abs` 與裸路徑 | 只寫 `tcp://` | KEEP | 基線 | P3 | no |
| CLI-11.03 | 重試只對 3 個 transient 碼生效、退避 0.5×2ⁿ 上限 5 s、TIMEOUT 會補診斷欄位、event 不轉送 `--retries`；規格舉例的方法名有誤 | 白名單一致 | KEEP＋DOC | 基線；規格勘誤方法名 | P0/P3 | no |
| CLI-11.04 | 沒有 `daemon restart` | 列有 restart | NEW SC-20（雙邊） | 依 §4.2；Python 也加，讓 1A 委派一致並可做 A/B | P1/P4 | no |
| CLI-11.05 | 只有 `remote close`，沒有 `stop` | 列有 stop | NEW SC-21（雙邊別名） | 名稱不同時做別名，Python 原名保留 | P1/P4 | no |
| CLI-11.06 | 沒有 `setup --rollback` | 要求 rollback | NEW SC-23（Rust-only） | §12.1 必要功能；依賴版本目錄，只有 Rust bundle 有 | P4 | no |
| CLI-11.07 | `supervision-mode` 唯讀、輸出純文字 | 要求查詢與切換 | NEW SC-22（Rust-only） | 依 §4.2；不帶參數的行為保留 | P4 | no |
| CLI-11.08 | `console-attach/detach` 只是 RPC | 歸在 1B 的終端類 | IMPL | 1A 直接原生承接、不委派；只改排程，不改行為 | P3 | no |
| CLI-11.09 | `benches` 不送 RPC，依 remote registry 並會 prune | 歸在 1A 的純 RPC | IMPL | 1A 委派 `benches`／`connect`，1B 原生；`--bench` 解析在 1A 原生 | P3/P4 | no |
| CLI-11.10 | `profile test` 離線執行，依賴 Python `re` | 純 RPC | IMPL＋KEEP | 1A 原生，用 `sw-proto::pyre` | P3 | no |
| CLI-11.11 | `connect <code> [--close]` 沒有子命令 | 寫成「子命令」 | KEEP＋DOC | 基線 | P0 | no |
| CLI-11.12 | `--mode` 沒有 choices；`--cmd` 非必填 | 列出 3 種 mode | KEEP | 基線，daemon 端接受任意值 | P3 | no |
| CLI-11.13 | 多開偵測在 daemon 的 `health()` | 寫在 CLI | KEEP＋DOC | 基線 | P0 | no |
| CLI-11.14 | CLI 另會輸出 10 個清單外的碼 | 78 碼清單 | KEEP | 全部收入 `error_codes.yaml`（OD-06 由資料計數） | P0 | no |
| CLI-11.15 | exit code 不一致（0／1／2／子行程 rc） | 要求凍結 | KEEP | 以 01 §2.4 凍結 | P0 | no |
| CLI-11.16 | `_print` 有 `sort_keys=True`，CLAUDE.md 漏寫 | — | KEEP＋DOC | 修正 CLAUDE.md | P0 | no |
| CLI-11.17 | 委派集合與 spawn 細節 | §4.3 | IMPL | 委派集合見 arch §7.4；spawn 見 DF-05 | P3 | no |
| CLI-11.18 | 預設 timeout 與硬編 timeout | 未寫 | KEEP | 以 arch §3.1 凍結 | P0 | no |
| CLI-7.4 | POSIX pipx 安裝下 `daemon start` 會 spawn 不存在的 `serialwrapd.py` | — | FIX DF-05 | 確認是缺陷（推論可重現 `DAEMON_EXITED`）。01 §12「POSIX 無 `-m` fallback」併入此列 | P1/P4 | no |
| CLI-12.01 | 回應非 JSON 時印 traceback | — | FIX DF-06 | 崩潰改成既有碼 | P1/P3 | no |
| CLI-12.02 | `supervision-mode`、`service`、`setup` 遇壞 YAML 印 traceback | — | FIX DF-07 | 保留 exit 1，改為明確錯誤 | P1/P4 | no |
| CLI-12.03 | setup 遇非 FlashingBusy 例外印 traceback | — | FIX DF-07 | 同上 | P1/P4 | no |
| CLI-12.04 | `event add --file` 讀到非 dict 仍照送 | — | KEEP | daemon 端以 `INVALID_RULE_SCHEMA` 把關 | P3 | no |
| CLI-12.05 | `--socket connect` 被誤判為 JSON 邊界 | — | KEEP | 基線 quirk | P3 | no |
| CLI-12.06 | AF_UNIX 的 `SOCKET_ERROR` message 不含路徑 | — | KEEP | message 照 Python `OSError` 格式（G3） | P3 | no |
| CLI-12.07 | argparse 允許前綴縮寫 | — | KEEP | arch §7.2 | P3 | no |
| CLI-12.08 | `stream tail --from-seq` 預設 0，`log` 預設 None | — | KEEP | 基線 | P3 | no |
| CLI-12.09 | 參數為 null 時送 null 或省略 key，三種處理並存 | — | KEEP | 逐一照抄 | P3 | no |
| CLI-12.10 | `DAEMON_NOT_READY` 時不 kill 子行程；背景 spawn 繼承 stdin | — | KEEP | 基線 | P4 | no |
| CLI-12.11 | bench state 有扁平與巢狀兩種形態；`--bench` 讀取不加鎖 | — | KEEP | 基線 | P3 | no |

### 2.2 盤點 02（RPC）

| ID | 事實 | 規格 | 裁決 | 理由 | 階段 | 需擁有者 |
|---|---|---|---|---|---|---|
| RPC-D-01 | 有多個經非字面路徑輸出的碼不在 78 碼清單 | §9.2 | KEEP | 全部收錄；`reachable` 與 `interface` 依實況標註 | P0 | no |
| RPC-D-02 | `session.attach` 與 `session.recover`（REPROBE 分支）把 `last_error` 原樣放進 `error_code`，包含 `CREDENTIALS_UNRESOLVED` | §9.3、§15.2「不新增為 RPC error_code」 | KEEP（預設） | 這是既有行為，不是新增；改掉會打斷 agent 與測試。`error_codes.yaml` 只放一筆 `last_error_passthrough` meta 項（`namespace_ref: session_last_errors`），不併入 RPC 計數 | P0 | **yes #1** |
| RPC-D-03 | `stream` 不是真串流，只發一次請求 | 寫「串流讀取」 | DOC | 勘誤 | P0 | no |
| RPC-D-04 | RPC 方法名有誤，規格漏列方法 | §4.2 | DOC | 以 02 §7 的 54 個方法為準 | P0 | no |
| RPC-D-05 | socket 權限為 `0660`，另可經 `SOCKET_GROUP` chgrp | ADR-04 要求 `0600` | SPEC SC-08 | 預設改 `0600`；system 模式明確指定 group 時保留 `0660` | P1/P5 | **yes #3** |
| RPC-D-06 | Windows 用 `msvcrt.locking`；探測與取鎖順序與 POSIX 相反 | §6.2 要求 `LockFileEx` | SPEC SC-10 | 改用 LockFileEx（與原鎖互斥相容）；兩平台各自的順序保留 | P1/P5 | no |
| RPC-D-07 | 路徑未正規化 | §6.2 要求正規化 | SPEC SC-10 | 只在比較時正規化；`config.yaml` 仍寫入原字串 | P1/P4 | no |
| RPC-D-08 | 沒有 `holder_unknown`；`/proc` 不可讀時視為可收回 | BC-04 | BC | BC-04 加上 SC-05 | P1/P5 | no |
| RPC-D-09 | `truncated` 只在 `log.tail_*`；`wal.range` 沒有 | §8.3、§9.4 | KEEP＋DOC | `wal.range` 以回傳筆數是否等於 limit 分頁；勘誤 §8.3 | P0 | no |
| RPC-D-10 | `foreign_holders` 包含 daemon 自身 pid | §9.4 | KEEP | L3 已依此行為設計（p0-multiopen 會排除自身） | P0 | no |
| RPC-D-11 | pending 以「無 `done_at`」計數，cancel 可繞過上限 | §5.4 寫 Accepted/Running | SPEC SC-06 | 規格明文 | P1/P5 | no |
| RPC-D-12 | 白名單內的方法有冪等副作用（prune、expire） | 稱為唯讀 | KEEP＋DOC | 基線 | P0 | no |
| RPC-D-13 | 沒有 peer credential 檢查 | OD-12 | KEEP | OD-12 已裁決接受 | — | no |
| RPC-D-14 | 舊規格寫「只 listen Unix socket」 | — | DOC | 更正 `docs/serialwrap-spec.md` | P0 | no |
| RPC-D-15 | 隱性的 65536 行長上限；executor offload | §4.1 | KEEP＋IMPL | 行長上限凍結；offload 模型不外露 | P0/P5 | no |
| RPC-H-01 | `rule_id` 有路徑穿越 | — | FIX DF-01 | 安全缺陷 | P1/P7 | no |
| RPC-H-02 | 請求行超過 65536 bytes 時斷線、不回應 | — | KEEP | 精確邊界由 Stage 0 測 65535／65536／65537 擷取 | P0/P5 | no |
| RPC-H-03 | 接受 NaN 並原樣回送；lone surrogate | — | KEEP | pyjson 支援 NaN；lone surrogate 轉 U+FFFD，屬非契約差異 | P5 | no |
| RPC-H-04 | `INVALID_JSON`／`INVALID_REQUEST` 用含空格分隔、無 id | — | KEEP | 位元組級保留 | P5 | no |
| RPC-H-05 | client 傳 `source:"human:…"` 就能繞過 gate | §5.2.1 Quiet 屬安全骨架 | FIX DF-02 | 安全 gate 可被偽造繞過 | P1/P5 | **yes #5** |
| RPC-H-06 | falsy 值會換成預設；`bool("false")` 為 True | — | KEEP | Python 轉型語意（arch §3.4） | P5 | no |
| RPC-H-07 | self-test 對 FLASHING session 送 nonce probe | — | KEEP | flash gate／fence 已擋下實際 TX，結果同為 `TARGET_UNRESPONSIVE`；不得 `confirm_ready` | P5 | no |
| RPC-H-08 | 負數參數得到 `EXCEPTION` 或怪異結果 | — | KEEP | code 保留，message 非契約（G1） | P5 | no |
| RPC-H-09 | 對 READY 執行 recover 一律送 CTRL_C 並 flush 佇列 | — | KEEP | 基線 | P7 | no |
| RPC-H-10 | cancel 不中止執行中的命令 | — | KEEP | 計數另見 SC-06 | P5 | no |
| RPC-H-11 | 各種未攔例外回 `EXCEPTION`；release 存檔失敗時記憶體已是 RELEASED | §6.2 | KEEP | 已符合「不得回成功且保持阻擋」 | P5 | no |
| RPC-H-12 | 非 BLOCKING 的方法會阻塞 event loop | — | IMPL | Rust 沒有單一 loop | P5 | no |
| RPC-H-13 | `event.reload` 回傳清單的順序不穩定 | — | KEEP（G2） | Rust 輸出升冪 | P7 | no |
| RPC-H-14 | `event.rule_set` 的 `**raw` 可覆蓋 `ok` | — | KEEP | 基線 | P7 | no |
| RPC-H-15 | server 綁定晚於 attach 開始 | — | KEEP | 啟動順序保留 | P5 | no |
| RPC-H-16 | shutdown 不關 bridge、不寫 state | §6.1、BC-01 | SPEC SC-11 | 規格要求確認 handle 已釋放 | P1/P5 | no |
| RPC-H-17 | `no_profiles_loaded` 只看 explicit target | — | KEEP | 基線 | P5 | no |
| RPC-H-18 | isoformat 在微秒為 0 時省略小數；`next_reprobe_at` 是 monotonic 值 | — | KEEP | 基線 | P5 | no |
| RPC-H-19 | 同一秒內重名的歸檔被覆蓋 | §8.2 要可追蹤 | FIX DF-03 | 資料遺失 | P1/P5 | no |
| RPC-H-20 | Windows `_build_server` 的例外發生在 try 之外 | — | IMPL | exit 1 保留 | P5 | no |

### 2.3 盤點 03（錯誤碼 §7）

| ID | 事實 | 規格 | 裁決 | 理由 | 階段 | 需擁有者 |
|---|---|---|---|---|---|---|
| ERR-7.1-a | `DAEMON_ALREADY_RUNNING` 只出現在 stderr 加 exit 2 | §6.2 | KEEP | `interface: proc` | P0 | no |
| ERR-7.1-b | `FLUSHED_BY_RECOVERY` 只在命令紀錄 | — | KEEP | `interface: rec` | P0 | no |
| ERR-7.1-c | `FLASH_TARGET_IS_CONSOLE` 不可達 | — | KEEP | `reachable:false`；Rust 不輸出 | P0 | no |
| ERR-7.1-d | `UNKNOWN_EVENT_CMD` 不可達 | — | KEEP | `reachable:false` | P0 | no |
| ERR-7.1-e | `PROMPT_TIMEOUT_(FORCE_)RECOVERED` 以 `ok:true` 回傳 | — | KEEP | 基線 | P0 | no |
| ERR-7.1-f | 有 5 個碼只在 client 端產生 | — | KEEP | `interface: cli_client` | P0 | no |
| ERR-7.1-g | framing 碼沒有 version，early 回應沒有 id | — | KEEP | arch §6.2 | P0 | no |
| ERR-7.1-h | `DEVICE_STILL_HELD` 沒有 `holder_unknown` | BC-04 | BC | 欄位一律附上 | P1/P5 | no |
| ERR-7.2-a | 16 個直接輸出的碼未列入 | §9.2 | KEEP | 收錄 | P0 | no |
| ERR-7.2-b | 23 個經 last_error 提升的碼 | §9.3 | KEEP | 見 RPC-D-02 | P0 | 同 #1 |
| ERR-7.2-c | `SOCKET_UNAVAILABLE`（PROC） | — | KEEP | `interface: proc` | P0 | no |
| ERR-7.2-d | `CMD_LENGTH_WARNING`／`NOT_BOOTLOADER`／`INVALID_INTERACTIVE_ENCODING` 不是 error_code | — | KEEP | 另立 warning、detail 目錄；最後一項經 `EXCEPTION` 包裝 | P0 | no |
| ERR-7.3-1 | `CREDENTIALS_UNRESOLVED` 外洩成 error_code | §9.3 | KEEP | 見 RPC-D-02 | P0 | 同 #1 |
| ERR-7.3-2 | `RECOVERING` 下 submit 回 `SESSION_NOT_READY` | §5.3 | SPEC SC-01 | 規格明文 | P1/P5 | no |
| ERR-7.3-3 | `FLUSHED_BY_RECOVERY` 的觸發面比規格寬（任何 detach） | — | KEEP＋DOC | 基線 | P0 | no |
| ERR-7.3-4 | `FORCE_RECOVER_TIMEOUT` 只在 force 路徑回出 | §5.2.5 | KEEP | 與規格一致 | P0 | no |
| ERR-7.3-5 | 碼沒有機器可讀的 `retryable` | §9.2 | KEEP | 依 03 主表人工標註入 YAML | P0 | no |
| ERR-7.3-6 | 計數口徑 | OD-06 | KEEP | 由資料產生 | P0 | no |
| ERR-7.3-7 | `INVALID_RULE_SCHEMA` 用 `error` 鍵；`LOG_OPEN_FAILED` 用 `detail` 鍵 | — | KEEP | 回應形狀凍結 | P0 | no |
| ERR-7.3-8 | `AUTOBOOT_QUIET`／`READY_UNCONFIRMED` 有兩種回應形狀 | §5.3 | SPEC SC-03 | 只補 `retry_after_s`；`selector` 與 `session` 的差異保留 | P1/P5 | no |
| ERR-7.3-9 | BC-03 映射：已送出或結果不明時現況回 `PROMPT_TIMEOUT`／`SEND_FAILED` | §5.1.4 | BC | 確認寫出 ≥1 byte → `COMMAND_ABORTED_IN_FLIGHT`；結果不明 → `SEND_FAILED`（沿用既有映射，不擴張語意） | P1/P5 | no |
| ERR-7.4 | 4 處 dead code 或不可達路徑 | — | IMPL | Rust 不實作 | — | no |
| ERR-7.5-a | `USER_ENV_MISSING` 不可達 | — | KEEP | `session_last_errors` 標 `reachable:false` | P0 | no |
| ERR-7.5-b | unassign 失敗仍寫 state；`detach_console` 失敗仍關 lease | — | KEEP | 基線 | P5/P6 | no |
| ERR-7.5-c | `DAEMON_ALREADY_RUNNING_ELSEWHERE` 不印 stderr | — | KEEP | 基線 | P4 | no |
| ERR-7.5-d | `REMOTE_NOT_SUPPORTED` 在不同命令 exit 2 或 1 | — | KEEP | 基線 | P4 | no |
| ERR-7.5-e | `profile test` 恆 exit 0 | — | KEEP | 基線 | P3 | no |
| ERR-7.6 | SKILL／文件未覆蓋全部錯誤碼 | OD-06 | DOC | P0 生成 SKILL 受管區段 | P0 | no |

### 2.4 盤點 04（WAL 與持久化）

| ID | 事實 | 規格 | 裁決 | 理由 | 階段 | 需擁有者 |
|---|---|---|---|---|---|---|
| WAL-D1 | `crc32` 是 8 位小寫 hex 字串 | §8.1 寫 u32 | KEEP＋DOC | schema 定為 `^[0-9a-f]{8}$` | P0 | no |
| WAL-D2 | 條件式 `rotation_failed` 欄位 | — | KEEP | optional | P0 | no |
| WAL-D3 | key 依字母序 | — | KEEP | `DISK_STABLE` | P0 | no |
| WAL-D4 | `wal.range` 沒有 `truncated` | §8.3 | KEEP＋DOC | 同 RPC-D-09 | P0 | no |
| WAL-D5 | unlink META、缺口紀錄都不存在 | §8.2 | BC | BC-01；META schema 見 arch §3.8 | P1/P5 | no |
| WAL-D6 | 每筆開關檔、不 fsync | §8.2 | BC | BC-01 | P1/P5 | no |
| WAL-D7 | `R=67108864`，`>=` 判斷，寫入前檢查 | §1.1.3 | KEEP | 凍結 | P0 | no |
| WAL-D8 | 歸檔名秒級、同秒覆蓋；reset 或重啟後 seq 可重複 | §8.2 | FIX DF-03＋KEEP | 覆蓋屬資料遺失需修；seq 語意以基線凍結 | P1/P5 | no |
| WAL-D9 | 動態 session 的 RELEASED 只撐得過一次重啟 | §6.2 | FIX DF-04 | 安全交接缺陷 | P1/P5 | no |
| WAL-D10 | release 存檔失敗只回泛用 `EXCEPTION` | §6.2 | KEEP | 規格禁止臨時新增碼；行為已符合要求 | P0 | no |
| WAL-D11 | 沒有 `SERIALWRAP_CONFIG`，也沒有系統目錄 fallback | §6.1 | SPEC SC-09 | 規格明文 | P1/P3 | no |
| WAL-D12 | 沒有 windows 監管模式 | §6.1 | NEW SC-24 | 規格要求 | P4 | no |
| WAL-D13 | `BENCH_ENDPOINT_NOT_REMEMBERED` 等碼；部分路徑走 `EXCEPTION` | — | KEEP | 收錄 | P0 | no |
| WAL-D14 | `state.json` 沒有版本欄位、沒有 migration | §9.4、§12.1 | KEEP | Rust 只寫相同的 5 個 key，保證 rollback 後舊版可讀 | P0 | no |
| WAL-D15 | `alias.set` 重啟後不會套回 | — | KEEP | 非規格要求、非安全問題；列入已知缺陷 | P0 | no |
| WAL-D16 | 動態 session 不套用 `defaults.log_dir` | — | KEEP＋DOC | 修正 CLAUDE.md | P0 | no |
| WAL-D17 | system scope 的遷移目標從未被讀取 | — | FIX DF-10 | 遷移等同無效 | P1/P4 | no |
| WAL-D18 | `state_migrate` 只有 setup 使用 | §3.2 放在 sw-core | IMPL | 移到 sw-cli | P4 | no |
| WAL-D19 | `bench_registry` 只有 CLI 使用 | §3.2 放在 sw-core | IMPL | 移到 sw-proto | P3 | no |
| WAL-1.4a | mirror 失敗後重試，造成同 seq 的 WAL 行重複 | — | FIX DF-17 | 資料正確性 | P1/P5 | no |
| WAL-1.4b | 只刪檔、不刪目錄時完全偵測不到 | §8.2 | BC | BC-01 | P1/P5 | no |
| WAL-1.11 | `rows_to_text_lines` 遇 `\r\n` 會多出一行 | — | KEEP | 已知缺陷，逐字承接 | P5 | no |
| WAL-1.3 | meta 內的 float 以 Python repr 輸出 | — | KEEP | pyjson | P5 | no |

### 2.5 盤點 05（Session §16）

| ID | 事實 | 規格 | 裁決 | 理由 | 階段 | 需擁有者 |
|---|---|---|---|---|---|---|
| SES-16.1 | DeferredBuffer 截頭留尾；跨上下文時不清 | BC-02 | BC | 加上下文標記（owner_gen） | P1/P6 | no |
| SES-16.2 | 沒有三態 TX 結果；`n<=0` 時記整段 payload | BC-03、§8.1 | BC＋SPEC SC-07 | — | P1/P5 | no |
| SES-16.3a | 沒有 drain barrier、receipt、3000 ms timeout | §5.2 | SPEC SC-04 | ADR-06 安全骨架；只有「release 遇 drain 逾時」的回應形狀需確認 | P1/P5 | **yes #4** |
| SES-16.3b | self-test、recover、reprobe、interactive_open 送 TX 不取前景 token | ADR-06 | KEEP | 單一 writer 保證每次寫入不被切開；命令層級的並行維持基線 | P5 | no |
| SES-16.4-01 | `RECOVERING` 下 submit 與 open 回 `SESSION_NOT_READY` | §5.3 | SPEC SC-01 | 規格明文 | P1/P5 | no |
| SES-16.4-02 | `FLASHING` 下 submit 與 open 回 `SESSION_NOT_READY` | §5.3 | SPEC SC-02 | 規格明文 | P1/P5 | no |
| SES-16.4-03 | passthrough 在 ATTACHED 時 submit 回 `PROFILE_NOT_COMMAND_CAPABLE` | §5.3、§5.2.1「核准的例外」 | KEEP（預設） | 例外未經核准，不啟用 | — | **yes #2** |
| SES-16.4-04 | 斷線與 I/O error 走 DETACHED→ATTACHING | §5.3 | SPEC SC-04 | 規格明文 | P1/P5 | no |
| SES-16.4-05 | FlashSyncMatched 時不清 DeferredBuffer | BC-02 | BC | — | P1/P8 | no |
| SES-16.4-06 | `enter_flashing` 沒有 guard | §5.3 | SPEC SC-14 | 規格明文 | P1/P8 | no |
| SES-16.4-07 | lease 到期為被動判定，status 存取會延長 | §5.3 | SPEC SC-13 | 到期事件；touch 語意保留 | P1/P6 | no |
| SES-16.4-08 | 救援流程有 `login_required` 分支；不用 fallback prompts | — | KEEP | 基線 | P6 | no |
| SES-16.4-09 | execute-time 的 gate 沒有 `retry_after_s` | §5.3 | SPEC SC-03 | 規格明文 | P1/P5 | no |
| SES-16.4-10 | reconfirm 還有第三條直接進終態的路徑 | — | KEEP | 基線 | P7 | no |
| SES-16.4-11 | READY 確認失敗時改成 ATTACHED，且不 flush 佇列 | — | KEEP | 規格未明文 | P7 | no |
| SES-16.4-12 | READY 下 open 遇前景忙回 `SESSION_BUSY` | — | KEEP | 基線 | P6 | no |
| SES-16.4-13 | `device.attach` 非同步，另有額外錯誤碼 | §5.3 | SPEC SC-05＋KEEP 碼 | 失敗不得宣稱已收回 | P1/P5 | no |
| SES-16.4-14 | `FORCE_RECOVER_TIMEOUT` 約 10 s；沒有 drain timeout | §5.2.5 | KEEP＋SPEC SC-04 | — | P5 | no |
| SES-16.5-01 | 本區有多個碼未列入 | §9.2 | KEEP | 收錄 | P0 | no |
| SES-16.5-02 | last_error 外洩成 error_code | §9.3 | KEEP | 見 RPC-D-02 | P0 | 同 #1 |
| SES-16.5-03 | `CREDENTIALS_UNRESOLVED` 由 manager 的 gate 合成，狀態為 ATTACHED | §9.3 | KEEP | 依基線凍結 | P0 | no |
| SES-16.5-04 | last_error 完整值集合 | §9.3 | KEEP | 收入 `session_last_errors.yaml` | P0 | no |
| SES-16.6-01 | passthrough 是否 probe 由 `ready_probe` 決定 | — | KEEP＋DOC | 修正 CLAUDE.md | P0 | no |
| SES-16.6-02 | pending 不計 `interactive`；`limits` 未揭露 pending 上限 | — | KEEP | 只做 SC-06 | P5 | no |
| SES-16.6-03 | `priority 0` 變成 10；`human:` 可偽造 | — | KEEP／FIX DF-02 | — | P5 | 同 #5 |
| SES-16.6-04 | `login_regex` 預設值是雙反斜線 | — | KEEP | 已知缺陷，逐字承接 | P0 | no |
| SES-16.6-05 | nonce 會被命令回顯滿足 | — | KEEP | 登入 FSM 基線 | P7 | no |
| SES-16.6-06 | 裝置離線的 tick 把 RELEASED 改成 DETACHED | §6.2 | FIX DF-04 | 交接安全 | P1/P5 | no |
| SES-16.6-07 | dead code | — | IMPL | — | — | no |

### 2.6 盤點 06（UART I/O 與 Console §12）

| ID | 事實 | 規格 | 裁決 | 理由 | 階段 | 需擁有者 |
|---|---|---|---|---|---|---|
| UIO-12.1-a | 沒有 flock 與 TIOCEXCL | BC-04 | BC | 加 flock＋TIOCEXCL；因 TIOCEXCL 不撤銷既有 handle，另加鎖後 holder 重掃以拒絕未取 flock 的外部 holder（含 `--force`）；TIOCEXCL 是 tty 物件旗標、不隨 fd 關閉清除，故正常關閉（I/O 收斂後）與 TIOCEXCL 之後的所有失敗路徑都先 `TIOCNXCL` 再 `LOCK_UN`／close（R0-F3；arch §4.2 關閉序） | P1/P5 | no |
| UIO-12.1-b | `/proc` 不可讀時無法與「無 holder」區分 | BC-04 | BC | `holder_unknown` | P1/P5 | no |
| UIO-12.1-c | Windows 開檔失敗沒有對應錯誤碼 | BC-04 | BC | ACCESS_DENIED → `DEVICE_STILL_HELD` 加 `holder_unknown:true` | P1/P5 | no |
| UIO-12.1-d | PROBE bridge 與正式 bridge 之間無鎖交接 | BC-04 | BC | 依序 open、close 並取得 receipt（receipt 須 `excl_cleared:true`，即 PROBE 關閉前已 TIOCNXCL；否則 PTY master 仍開啟時正式 open 得 EBUSY） | P5 | no |
| UIO-12.1-e | POSIX 忽略 `xonxoff` | — | KEEP | 兩平台的差異收在 PAL 內 | P5 | no |
| UIO-12.2-a | 沒有 drain | §5.2 | SPEC SC-04 | — | P1/P5 | no |
| UIO-12.2-b | `_write_all` 遇 EAGAIN 無限重試、無法取消 | §5.2.3 | SPEC SC-04 | 改為可喚醒、可取消 | P1/P5 | no |
| UIO-12.2-c | 送出前檢查只有兩項 | §5.2.2 | SPEC SC-04 | fence：授權重驗與 OS 實際提交在同一臨界區（Rust `FenceSubmitGate`、Python `_tx_fence_lock` 臨界區內單次非阻塞 `os.write`，`_state_lock` 只做 pre-check），鎖只跨非阻塞提交、不跨完成等待；禁止「驗證→放鎖→write」（R0-F1，arch §5.4 第 3 點；P1 T20、P5 Task 9 測試） | P5 | no |
| UIO-12.2-d | 沒有 TX 三態 | BC-03 | BC | — | P1/P5 | no |
| UIO-12.3-a | TX WAL 記整段或完全不記 | §8.1 | SPEC SC-07 | — | P1/P5 | no |
| UIO-12.3-b | TX WAL 在 write 鎖之外才 append | §8.1 | SPEC SC-07 | — | P1/P5 | no |
| UIO-12.3-c | `send_secret` 不記 WAL | — | KEEP | 秘密不得落盤 | P5 | no |
| UIO-12.3-d | flash gate 靜默丟棄 | — | KEEP | Rust 視為 `NotSubmitted`、不記 WAL | P5 | no |
| UIO-12.4-a | RX 先寫 WAL 再分送（順序正確），但非常駐 handle | BC-01 | BC | — | P1/P5 | no |
| UIO-12.4-b | observer 拿到的 `wal_seq` 不精確 | — | FIX DF-15 | 改用精確值，且仍落在 Python 可能產生的值域內 | P1/P5 | no |
| UIO-12.4-c | 非 OSError 例外會跳過 fan-out | — | IMPL | — | P5 | no |
| UIO-12.5 | DeferredBuffer 截頭留尾 | BC-02 | BC | — | P1/P6 | no |
| UIO-12.6 | 沒有 ring queue（丟尾） | §5.4 | SPEC SC-15（Rust-only） | 顯示資料本就允許丟棄 | P6 | no |
| UIO-12.7 | console PTY 沒有 symlink | §7.1「穩定 symlink」 | KEEP＋DOC | 規格的 symlink 只指 ttyMCU；console 的穩定性靠 PreservedConsoles | P0 | no |
| UIO-12.8-a | greeting 另含 `DO SGA` | §7.2 | KEEP | 12 bytes 位元組級保留 | P6 | no |
| UIO-12.8-b | 對所有非 SGA 的 WILL 回 DONT | §7.2 | KEEP | 符合「拒絕入向 Binary」 | P6 | no |
| UIO-12.8-c | Telnet port 為 ephemeral | — | KEEP | 基線 | P6 | no |
| UIO-12.9-a | 沒有 `probe_id` | §5.1.2 | IMPL | 內部身分 | P8 | no |
| UIO-12.9-b | 認線候選未檢查是否有執行中的操作 | §5.3 | SPEC SC-14 | — | P1/P8 | no |
| UIO-12.9-c | FlashSyncMatched 時不清 Deferred | BC-02 | BC | — | P1/P8 | no |
| UIO-12.9-d | FlashHolderGone 後還原為 `flash_prev_state` | §5.3「回 ATTACHED」 | KEEP | 候選本來就是非 command_capable，實際結果都是 ATTACHED | P8 | no |
| UIO-12.9-e | holder 從未 armed 時等 1800 s；ACK 判定用 substring 並整段回放 | — | KEEP | 基線 | P8 | no |
| UIO-12.9-f | baud 只鏡射一次 | — | KEEP | 基線 | P8 | no |
| UIO-12.10-a | CR 與 LF 分在兩個 chunk 時多送一個空行 | — | KEEP | 已知缺陷 | P6 | no |
| UIO-12.10-b | 每個 chunk 獨立 UTF-8 解碼 | — | KEEP | 基線 | P5 | no |
| UIO-12.10-c | prune 時在鎖內掃 `/proc` | — | IMPL | 效能 | P6 | no |
| UIO-12.10-d | EOF 不觸發 bridge-down；select 錯誤不設 stop | — | KEEP | 基線 | P5 | no |
| UIO-12.10-e | dead code | — | IMPL | — | — | no |
| UIO-12.10-f | `mcu.patterns` 不輸出 `timeout_ms` | — | KEEP | 基線 | P8 | no |
| UIO-12.10-g | 不設 VMIN／VTIME、不做 tcflush | — | KEEP | 基線 | P5 | no |

### 2.7 盤點 07（平台與管理面）

| ID | 事實 | 規格 | 裁決 | 理由 | 階段 | 需擁有者 |
|---|---|---|---|---|---|---|
| PLT-D-01 | 沒有 `SERIALWRAP_CONFIG`，也沒有系統目錄 | §6.1 | SPEC SC-09 | — | P1/P3 | no |
| PLT-D-02 | 只設 `BY_ID_DIR` 不足以隔離（還會掃 by-path） | OD-11 引用 | DOC | 隔離實例必須同時設兩個目錄 | P0 | no |
| PLT-D-03 | 沒有 `daemon restart` | §4.2 | NEW SC-20 | — | P1/P4 | no |
| PLT-D-04 | `supervision-mode` 不能切換 | §4.2 | NEW SC-22 | — | P4 | no |
| PLT-D-05 | 沒有 `remote stop` | §4.2 | NEW SC-21 | — | P1/P4 | no |
| PLT-D-06 | 沒有 `--rollback` | §12.1 | NEW SC-23 | — | P4 | no |
| PLT-D-07 | `benches`／`profile` 分類錯誤 | §4.2 | IMPL | 只改排程 | P3 | no |
| PLT-D-08 | 程式碼沒有 pipe；真正的 POSIX 依賴是 07 §8.6 清單 | BC-05 | BC＋DOC | 以 §8.6 清單作為 BC-05 範圍；延伸到 `connect`／`benches`（SC-27） | P1/P4 | **yes #7** |
| PLT-D-09 | windows 監管模式不存在 | §6.1 | NEW SC-24 | — | P4 | no |
| PLT-D-10 | 使用 `msvcrt.locking`，路徑未正規化 | §6.2 | SPEC SC-10 | — | P1/P5 | no |
| PLT-D-11 | 本區碼未列入 | §9.2 | KEEP | 收錄 | P0 | no |
| PLT-D-12 | 版本 gate 只在測試 harness，CLI 只警告 | §9.4、§9.5 | KEEP＋NEW SC-25 | 警告保留；新增 manifest 與 doctor 配對檢查 | P3/P4 | no |
| PLT-D-13 | 沒有 TIOCEXCL 與 tty flock | BC-04 | BC | 同 UIO-12.1-a（含關閉前 TIOCNXCL） | P1/P5 | no |
| PLT-O-01 | system scope 的 state 路徑不一致 | — | FIX DF-10 | — | P1/P4 | no |
| PLT-O-02 | pipx 安裝下 spawn 失敗 | — | FIX DF-05 | — | P1/P4 | no |
| PLT-O-03 | watcher 與 rank 之間有 race | #100 | FIX DF-14 | — | P1/P5 | no |
| PLT-O-04 | `foreign_holders` 含自身 | — | KEEP | 同 RPC-D-10 | P0 | no |
| PLT-O-05 | socket 以原字串比對 | §6.2 | SPEC SC-10 | — | P1/P4 | no |
| PLT-O-06 | 未帶 `--with-sudo` 也會 sudo 寫 `/etc/wsl.conf` | 程式碼自身的不變量 | FIX DF-11 | 靜默提權 | P1/P4 | no |
| PLT-O-07 | `ran` 恆為 `[]`；rc 被忽略 | — | FIX DF-12 | 欄位意圖明確（FakeEffects 測試已依此斷言）；`applied` 語意不變 | P1/P4 | no |
| PLT-O-08 | 物化後的 broker wrapper 必定 exit 2 | — | FIX DF-13 | 資產缺陷 | P1/P4 | no |
| PLT-O-09 | on-demand 轉 systemd 時有 two-reader 窗口 | §6.1 | SPEC SC-12 | — | P1/P4 | no |
| PLT-O-10 | `REMOTE_BIND_UNVERIFIED` 不重試 | — | KEEP | 基線 | P4 | no |
| PLT-O-11 | close 在 pid 已死時不清 pgid | — | KEEP | 基線（Windows 依 BC-05 改為終止整棵樹） | P4 | no |
| PLT-O-12 | doctor 在 POSIX 對 tcp 恆判可連；兩個 seam 不一致 | — | KEEP | 基線 | P4 | no |
| PLT-O-13 | 壞 YAML 印 traceback | — | FIX DF-07 | — | P1/P4 | no |
| PLT-O-14 | 不看 XDG 變數的固定路徑 | — | KEEP | 基線 | P4 | no |
| PLT-O-15 | unit 檔沒有尾端換行；wsl.conf 被 configparser 重寫 | — | KEEP | 位元組級保留 | P4 | no |
| PLT-O-16 | `exclude_coms` 只在啟動時讀一次 | — | KEEP | 基線 | P5 | no |

### 2.8 盤點 08（測試資產）

| ID | 事實 | 規格 | 裁決 | 理由 | 階段 | 需擁有者 |
|---|---|---|---|---|---|---|
| TST-D-1 | L2 實際有 38 case | 規格寫 37 | DOC | 以 38 為準，承接清單據此展開 | P0/P2 | no |
| TST-D-2 | realhw 實際有 37 case（remote 為 8） | 規格寫 36 | DOC | 同上 | P0/P2 | no |
| TST-D-3 | 4 種拓樸加 gwports | 規格寫三拓樸 | DOC | — | P0 | no |
| TST-D-4 | 沒有平台與資源 marker | §11.2 | TEST | P2 於 conftest 註冊 marker 並逐檔標註 | P2 | no |
| TST-D-5 | 實際 1984 個 item | 規格寫 ~1.7k | DOC | — | P0 | no |
| TST-D-6 | f4 哨兵的狀態在三處記載互相矛盾 | §11.4、§15.10 | TEST | 預期 PASS，oracle 不變；Stage 0 bench 若重現紅燈，再登記 XFAIL（#159） | P2 | no |
| TST-D-7 | F12 只比 mtime；`p0-wal-live` 也是 mtime | §8.3、OD-13 | TEST | 兩者都改採 §8.3 的作法：讀回可關聯 marker 的新紀錄並驗證 CRC（同時滿足 OD-13） | P2 | no |
| TST-D-8 | DaemonHarness 隔離不完整，無法指定 artifact | §15.5 | TEST | 補齊 9 個目錄的隔離；讀 `SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE` | P2 | no |
| TST-D-9 | 4 個 func-test case 的 oracle 不成立 | §15.5「保留 oracle」 | TEST | ID 與目的保留；改用既有欄位修復 oracle（`has_interactive` 改看 `consoles[].interactive_owner`；實作 `input_raw`、`any_tx_contains_raw`；`sm-04` 改驗 DETACHED） | P2 | no |
| TST-D-10 | realhw／reliability 沒有 pin，也沒有 #154 gate | §11.6 | TEST | 新增 `serialwrap_exe` 設定與版本 gate | P2 | no |
| TST-D-11 | 版本 probe 的 Python fallback 對 Rust 無效 | — | KEEP | Rust daemon 一定回報 version | P2 | no |
| TST-D-12 | lr-mixed 的 PID 只取自 system systemd；background 回了 `cmd_id` 就算完成 | §15.11 | TEST＋KEEP | PID 改取 `daemon status.pid`；計數口徑不變，以維持基線可比 | P2 | no |
| TST-D-13 | Windows bench lock 路徑寫死 | §15.2 | TEST | 新增 testbed 鍵 `bench_lock_path` | P2 | no |
| TST-D-14 | Windows 上硬性 SKIP | §15.8.4 | TEST | 改為依能力判定（Spike 7） | P2 | no |
| TST-D-15 | acceptance、contracts、bridge 等目錄不存在 | §3.2 | TEST | P0／P2 建立 | P0/P2 | no |
| TST-D-16 | `p1-cmd-modes` 呼叫 `interactive-status` | §15.11.3 | KEEP | 屬功能 case，不是觀測用途 | — | no |
| TST-D-17 | 兩個前端的預設值不同 | — | KEEP | — | — | no |
| TST-D-18 | 13 個檔案使用 `state_iso` | 文件寫 8 檔 | DOC | — | P0 | no |

### 2.9 盤點 09（Event、File、Config 等）

| ID | 事實 | 規格 | 裁決 | 理由 | 階段 | 需擁有者 |
|---|---|---|---|---|---|---|
| EVT-D-S1 | 漏列 3 個 file transfer 碼 | §9.2 | KEEP | 收錄 | P0 | no |
| EVT-D-S2 | 常數碼的分布與規格描述不符 | §9.2 | KEEP＋DOC | — | P0 | no |
| EVT-D-S3 | SKILL 未記載許多碼 | OD-06 | DOC | P0 | P0 | no |
| EVT-D-S4 | `wal_seq` 是全域快照 | §5.5 | FIX DF-15 | — | P1/P7 | no |
| EVT-D-S5 | 單一 matcher queue；LineBuffer 永不重設 | §5.5 | KEEP | 基線 | P7 | no |
| EVT-D-S6 | handler 只能在 POSIX 執行 | §5.5 | NEW SC-26（Rust-only） | Windows 語意必須定義 | P7 | **yes #6** |
| EVT-D-S7 | 本地檔由 daemon 讀寫 | §4.2 #5 | KEEP | 改成 CLI 讀寫會是 BC | P3/P8 | no |
| EVT-D-S8 | stream 是一次性請求 | §4.2 #8 | KEEP＋DOC | — | P0 | no |
| EVT-D-S9 | `log` 群組只有 tail | §4.2 #7 | KEEP＋DOC | — | P0 | no |
| EVT-D-S10 | `skill --platform` 有 windows 版本 | §4.2 #13 | KEEP | 兩份都內嵌 | P3 | no |
| EVT-D-S11 | `event add` 由 CLI 讀檔 | §4.2 #10 | KEEP | — | P3 | no |
| EVT-D-S12 | LineBuffer 切行規則 | §5.4 | KEEP | 凍結細節 | P7 | no |
| EVT-D-S13 | event 處理在 loop 上同步 fsync | §5.4 | IMPL＋KEEP | Rust 不阻塞；語意不變 | P7 | no |
| EVT-D-S14 | config 錯誤有 raise 與靜默兩種分流 | — | KEEP | 凍結 | P0 | no |
| EVT-D-S15 | regex 方言；是否裝 re2 會改變可接受集合 | 規格未提 | KEEP | 以部署預設（無 re2）為基線；用 pyre 重現；無法轉譯的構造列為殘餘差異 | P3/P7 | no |
| EVT-D-D1 | SKILL 寫 chunk 2048 | — | DOC | 改為 512 | P0 | no |
| EVT-D-D2 | SKILL 寫 pull 回傳檔案內容 | — | DOC | 實際是寫到 daemon 的 cwd | P0 | no |
| EVT-D-D3 | README 寫支援 JSON 與 YAML | — | DOC | 只讀 `*.json` | P0 | no |
| EVT-D-D4 | README 的 stdin 欄位名稱 | — | DOC | — | P0 | no |
| EVT-D-D5 | 文件寫 tmpfs 路徑 | — | DOC | — | P0 | no |
| EVT-D-D6 | 文件寫 drop_oldest | — | DOC | 實際丟新 | P0 | no |
| EVT-D-D7 | `match_skipped.reason` 的值 | — | DOC | — | P0 | no |
| EVT-D-D8 | `matched_line`／`bridge_generation` 的描述 | — | DOC | — | P0 | no |
| EVT-D-D9 | CLAUDE.md 的 `log_dir` 優先序 | — | DOC | — | P0 | no |
| EVT-D-D10 | 註解寫 ≈741 | — | DOC | — | P0 | no |
| EVT-D-D11 | `ALL` 只在啟動時展開 | — | KEEP＋DOC | 基線 | P7 | no |
| EVT-D-C1 | `login_regex` 預設值是雙反斜線 | — | KEEP | 已知缺陷 | P0 | no |
| EVT-D-C2 | templates 只保留最後一個檔 | — | KEEP | 已知缺陷 | P0 | no |
| EVT-D-C3 | truthiness 讓 `"false"` 變成 True | — | KEEP | 基線 | P0 | no |
| EVT-D-E1 | `exhausted` 在 dispatch 之前就設定 | — | KEEP | 已知缺陷 | P7 | no |
| EVT-D-E2 | `matched_line` 等於 `matched_text`；`bridge_generation` 恆為 0 | — | KEEP | 基線 | P7 | no |
| EVT-D-E3 | `active_cmd_id` 為字串 `"foreground"` | — | KEEP | 基線 | P7 | no |
| EVT-D-E4 | `last_fire_ts` 記錄 handler 結束時刻 | — | KEEP | 基線 | P7 | no |
| EVT-D-E5 | 帶 flags 的 `contains` 規則會讓下次 daemon 啟動失敗 | — | FIX DF-08 | 可用性缺陷 | P1/P7 | no |
| EVT-D-E6 | `load_all` 遇 `ValueError` 崩潰 | — | FIX DF-09 | 可用性缺陷 | P1/P7 | no |
| EVT-D-E7 | raw 內容覆寫回應欄位 | — | KEEP | 同 RPC-H-14 | P7 | no |
| EVT-D-E8 | `n=0` 回傳全部 | — | KEEP | 基線 | P7 | no |
| EVT-D-E9 | `rule_id` 路徑穿越 | — | FIX DF-01 | 安全 | P1/P7 | no |
| EVT-D-E10 | `reload` 不 disable 已移除規則的 COM | — | KEEP | 基線 | P7 | no |
| EVT-D-E11 | 每行都讀一次 counter 檔 | — | IMPL | write-through 快取等價（daemon 是唯一寫入者） | P7 | no |
| EVT-D-F1 | `chunk_size` 為 0 或負數時的強制轉型 | — | KEEP | 基線 | P8 | no |
| EVT-D-F2 | file 類 CLI 預設 timeout 5 s（#123） | — | KEEP | 已知缺口 | P3 | no |
| EVT-D-F3 | MOVE 失敗不清理暫存檔 | — | KEEP | 基線 | P8 | no |
| EVT-D-F4 | pull 受 131072 字元 RX 視窗限制 | — | KEEP | 基線 | P8 | no |
| EVT-D-F5 | 非 strict base64 解碼 | — | KEEP | `a2b_base64_lenient` | P8 | no |
| EVT-D-F6 | file transfer 不排隊，直接回 `SESSION_BUSY` | §5.2.1 | KEEP | 基線 | P8 | no |
| EVT-D-L1 | 前景執行期間 capture 不寫 log | — | KEEP | 基線 | P5 | no |
| EVT-D-L2 | `result.tail` 回 `EXCEPTION` 而 `log` 回 `INVALID_ARGS` | — | KEEP | 基線 | P5 | no |
| EVT-D-K1 | 有定義但未使用的常數 | — | IMPL | — | — | no |

### 2.10 盤點 10（缺口與矛盾）

| ID | 事實 | 規格 | 裁決 | 理由 | 階段 | 需擁有者 |
|---|---|---|---|---|---|---|
| GAP-G-01 | daemon 沒有設定 logging（lastResort，WARNING 以上輸出到 stderr） | §9.3「一次性 log」 | KEEP | 去向、格式與等級照現況凍結；systemd 模式由 journald 承接 | P0/P5 | no |
| GAP-G-02 | human 輸入的行被拒時靜默丟棄；line buffer 無上限 | §5.3「可診斷」、§5.4 | SPEC SC-16 | 以 log 提供診斷，不改 WAL 契約 | P1/P6 | no |
| GAP-G-03 | Windows 的 multi_open 與 holder 偵測恆失效 | §6.2、BC-04 | SPEC SC-17＋BC | — | P4/P5 | no |
| GAP-G-04 | 沒有 artifact 身分 | §9.5 | NEW SC-25 | — | P2/P3 | no |
| GAP-G-05a | Windows 的 config 與 state 放在 `%USERPROFILE%\.config` 等位置 | §12.1 | KEEP | 相容既有安裝；程式本體放 `%LOCALAPPDATA%\Programs` | P4 | no |
| GAP-G-05b | Windows release 沒有物化 profiles | §12.1 | NEW（併入 SC-23／SC-24） | Rust setup 會物化 | P4 | no |
| GAP-G-05c | Windows setup 的 symlink 會失敗 | — | FIX DF-16 | — | P1/P4 | no |
| GAP-G-06 | on-demand 不會自動啟動 | §6.1 Windows on-demand 寫「首次需求啟動」 | SPEC SC-18（Rust-only） | Linux 維持現況 | P4 | no |
| GAP-G-07 | 沒有 OD-11 allowlist | OD-11、§9.4 | NEW SC-19 | 定案見本列下方說明 | P1 | no |
| GAP-G-08 | 訊號、SIGPIPE、stdin、help 的程序層行為 | §4.3 | KEEP | 凍結 | P3 | no |
| GAP-G-09 | `TRANSPORT_STALL` 的 detail 文字 | §9.4 | KEEP | 位元組級凍結 | P0 | no |
| GAP-G-10 | 有多處無界資源 | §5.4 | SPEC（可觀測者走 SC-15／SC-16）＋IMPL | 容量見 arch §5.10 | P5 | no |
| GAP-G-11 | 有承接位置未定的模組（util、version、assets、wrapper） | §4.1 | IMPL | 位置見 arch §1.3；wrapper 原樣保留，由 1B 的 setup 物化 | P3/P4 | no |
| GAP-G-12 | 測試 oracle 引用不存在的 `FOREGROUND_BUSY` | §10.4 | TEST | 以工具 PR 移除 | P2 | no |
| GAP-G-13 | `session list` 不評估 reconfirm 是否到期 | §9.4 | KEEP | 凍結：最遲約 1 s tick 翻轉 | P0 | no |
| GAP-X1 | `SERIALWRAP_TCP_PORT` 為空字串時崩潰 | — | DOC＋KEEP | 依原始碼；Rust 保留 exit 1 | P0 | no |
| GAP-X2 | spawn argv 描述有矛盾 | — | DOC | 依原始碼 | P0 | no |
| GAP-X3 | `USER_ENV_MISSING` 是否可達有矛盾 | — | DOC | 不可達 | P0 | no |
| GAP-X4 | `session list` 欄位歸屬有矛盾 | — | DOC | `human_attached` 屬 self-test；`recommended_action` 屬 submit、self-test、recover | P0 | no |
| GAP-X5 | 行號漂移 | — | DOC | — | — | no |

**GAP-G-07（SC-19）定案**：

- 設定位置：
  - env `SERIALWRAP_TEST_DEVICE_ALLOWLIST`：逗號分隔，比對 `DeviceInfo.by_id`（Windows 為 COM 名，POSIX 為 symlink 路徑）。
  - 或 `config.yaml` 的 `windows.test_device_allowlist: [..]`。
  - 兩者都有時以 env 優先。
- 「隔離實例」的判定：`SERIALWRAP_STATE_DIR`、`SERIALWRAP_RUN_DIR`、`SERIALWRAP_CONFIG_DIR` 三者都明確設定，而且解析後都不等於未設定時的預設路徑。
- 非隔離實例設定了 allowlist：daemon 拒絕啟動，stderr 印 `serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: <reason>`，exit 2。
- 啟用時 `health.status` 多出 `"test_device_allowlist":{"enabled":true,"coms":[...]}`。
- allowlist 只影響裝置的發現與選取，不改變獨占鎖或 backend。

---

## 3. 擁有者確認清單（needs-owner=yes；不等確認，先依預設實作）

| # | ID | 議題 | 預設 |
|---|---|---|---|
| 1 | RPC-D-02（另含 ERR-7.2-b、ERR-7.3-1、SES-16.5-02） | `session.attach` 與 `session.recover` 把 `last_error`（含 `CREDENTIALS_UNRESOLVED`）放進 `error_code` | **保留**現況；`error_codes.yaml` 只放一筆 passthrough meta 項，不計入 RPC 碼，並交叉引用 `session_last_errors` |
| 2 | SES-16.4-03 | 規格中「passthrough ATTACHED 可進 Arbiter」的例外 | **不啟用**，維持 `PROFILE_NOT_COMMAND_CAPABLE` |
| 3 | RPC-D-05／SC-08 | UDS 權限 | 預設 `0600`，新建目錄 `0700`；system 模式明確指定 `SERIALWRAP_SOCKET_GROUP` 時用 `0660` 加 chgrp |
| 4 | SC-04 | release 遇 drain 逾時時的回應 | `{"ok":false,"error_code":"SESSION_RECOVERING","drain_stalled":true,"drain_timeout_epoch":N,"session":…}`。回條遲到時仍完成 release，下次再呼叫回 `already_released` |
| 5 | RPC-H-05／DF-02 | RPC 帶入的 `human:` source | 一律以 agent 對待（gate 與升格都比照 agent）；daemon 內部的 human console 路徑不受影響 |
| 6 | EVT-D-S6／SC-26 | Windows event handler | `shell` 走 `cmd.exe /d /s /c`；用 Job Object 處理 timeout；env 白名單加 `SYSTEMROOT`、`COMSPEC`、`PATHEXT`、`TEMP`、`TMP`、`USERPROFILE` |
| 7 | SC-27 | BC-05 的 Windows remote 是否涵蓋 `connect`／`benches` | **涵蓋**，移除這兩者在 Windows 上的 `REMOTE_NOT_SUPPORTED` |

## 4. 保留的已知缺陷（KEEP；建議另開 issue，不在本次重構範圍）

| ID | 缺陷 |
|---|---|
| WAL-D15 | alias 重啟後不會套回 |
| WAL-1.11 | `log tail-text` 遇 `\r\n` 多出一行 |
| SES-16.6-04／EVT-D-C1 | `login_regex`／`password_regex` 預設值是雙反斜線 |
| EVT-D-C2 | 只採用最後一個 YAML 檔的 templates |
| UIO-12.10-a | CR 與 LF 跨 chunk 時多送一個空行 |
| EVT-D-E1 | exhausted 提早設定 |
| EVT-D-F2 | file 類 CLI 5 s 逾時（#123） |
| EVT-D-F3 | MOVE 失敗殘留暫存檔 |

Rust 依基線逐字承接以上行為。要修正時另開 issue，並登記新的 DF。

## 5. 統計

第 2 節共 296 列（ID 不重複），依「裁決」欄的第一個代號歸類：

| 裁決 | 列數 |
|---|---|
| KEEP（保留基線並凍結；含 KEEP＋DOC 等以保留為主者） | 151 |
| SPEC（依定稿規格變更，SC） | 31 |
| NEW（規格要求的新功能，SC） | 14 |
| FIX（缺陷／安全修正，DF） | 25 |
| BC（既有核准的 BC-01～05） | 19 |
| DOC（只修文件或勘誤） | 26 |
| TEST（修測試資產） | 11 |
| IMPL（內部實作差異） | 19 |
| **合計** | **296** |

登記項目：SC-01～SC-27（27 項）、DF-01～DF-17（17 項），另有既有核准的 BC-01～05。7 列標為需擁有者確認，對應第 3 節的 7 項（第 1 項含 3 列交叉引用），均已附預設值。
