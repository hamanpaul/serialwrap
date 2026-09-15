# #198 backlog 優先序與 #182 reload 契約

基準：`cf2aaba9004c18a82a6a5e4e0dcc66ea7211aeb1`（2026-09-12）。本文件是方案與證據盤點，不宣稱已實作、測試、部署或 live。

## 排序結論

排序先看安全／資料正確性，再看已證實的現場阻塞與可驗證性；沒有頻率或對照樣本，**不估效益倍數**。

| 順位 | issue | 建議 | 實際 consumer、成本與下一步 |
|---|---|---|---|
| 0 | [#198](https://github.com/hamanpaul/serialwrap/issues/198) | **P1 gate** | 先守 single-writer、lease、raw evidence 與可診斷恢復；「STOP」只擋無 consumer 的泛化平台／協定／壓測，不撤既有介面。 |
| 1 | [#166](https://github.com/hamanpaul/serialwrap/issues/166) | **P1** | issue comment 有兩板實測：板端缺 `base64`，prpl 約 505 字元單行上限；file push 既有 consumer 目前不可用且有資料正確性風險。先做 decoder 偵測／`openssl` fallback、依 profile 上限推導 chunk，保留 checksum 與 stall fail-closed。 |
| 2 | [#199](https://github.com/hamanpaul/serialwrap/issues/199) | **P1 小修** | Windows backend 是既有 consumer；`st_rdev` 例外可能在 holder 掃描前中止 attach/reclaim。先防禦性取值，但不能把 `/proc` 不可用時的空 `pids` 當成 Windows 排他安全已證明。 |
| 3 | [#171](https://github.com/hamanpaul/serialwrap/issues/171) | **P1 最小切片** | 遠端事故需要控制平面證據；現行有零散 warning，但無 verbosity／`SERIALWRAP_LOG_LEVEL` 契約。Phase 1 記錄當次 CLI endpoint 來源／errno／RPC method／耗時／error code；保留 #172 既有 formatter。Daemon／session trace 放後續；WAL 遺失已由 #189 處理。 |
| 4 | [#182](https://github.com/hamanpaul/serialwrap/issues/182) | **P2** | #181 已讓使用既有 template 的 fallback/pin 修復不必重啟；reload 仍為新增／修改 template、explicit target 與載入版本可見性提供操作成本改善。先做 fail-closed、可驗證的 memory-only contract，不為便利破壞 ownership。 |
| 5 | [#197](https://github.com/hamanpaul/serialwrap/issues/197) | **P2 驗證票** | `remote` 是既有 POSIX CLI／SSH consumer，docker 拓樸已有驗證；尚缺 Cloudflare Quick/Named Tunnel 的 bench 實跑。完成前提與 E2E 證據，不新增 provider SDK 或泛化平台。 |

這不是把 remote、Windows 或 file-transfer 一概 STOP：它們各有既有 consumer；只是 #166/#199 先修正正確性邊界，#197 先做外部環境驗證。上述排序是風險與操作成本排序，不是使用頻率推估。

## 現況證據與 #181 對 #182 的影響

- daemon 只在 `_run_async()` 啟動時 `load_profiles(args.profile_dir)`（`sw_core/daemon.py:128-157`）；`SerialwrapService` 只接收 `profiles/templates`（`sw_core/service.py:219-249`），沒有保存 `profile_dir` 或 `config_refs`。
- `SessionManager` 以 `list(templates)` 保存模板，並在建構時 materialize explicit profiles（`sw_core/session_manager.py:622-705`）；`SessionProfile` 為 frozen dataclass（`sw_core/config.py:46-78`）。目前不存在可 reload 的 profile owner/catalog；實作時由 **SessionManager 單獨持有權威 catalog**，service 只轉送 reload/status，不另存一份可漂移的 templates。
- `load_profiles()` 逐檔讀 YAML，且 `all_templates = ordered_templates` 會保留現有「最後一個檔案的 templates 覆寫」語意（`sw_core/config.py:264-323`）；reload 不得順便改成跨檔 merge。
- #181 PR [#191](https://github.com/hamanpaul/serialwrap/pull/191) 已合併：`_attach_by_id()` 在開正式 bridge 前呼叫 `_reresolve_profile_on_reattach()`（`sw_core/session_manager.py:2427-2463`）。`pin` 會套用候選 template；`fallback` 會以獨立 probe 重偵測；`yaml-target` 明確早退（`sw_core/session_manager.py:2770-2817`）。
- `session clear` 會停止該 session bridge、capture/background 並保留可恢復 console/owner（`sw_core/session_manager.py:1666-1729`）；`recover` 無 bridge 時才重新 attach，已有 bridge 則只做原 profile reprobe（`sw_core/session_manager.py:4431-4484`）。因此 #181 不能被誇大成「所有 target 變更已免 reload」。

## #182 最小可落地契約

### Owner、catalog 與套用邊界

1. `daemon._run_async` 將原始 `profile_dir` 傳給 service，再由 service 轉給 `SessionManager`；**只有 SessionManager 持有** immutable `ProfileCatalog`：`profiles`、`templates`、`max_sessions`、explicit target index，以及每個 YAML 的 `path + mtime_ns/digest` `config_refs`。`daemon status` 透過 manager 回報 refs／generation，讓 operator 知道記憶體版本。
2. `daemon reload` 只重讀、解析、驗證並 atomic swap catalog；不停止 daemon、不關 UART、不改 active `SessionRuntime`、lease、capture、background command。reload 本身的「unaffected」只保證這些現有 runtime；operator 另做 targeted `clear` 時，該 target 的中斷成本要明示。
3. reload 後下一次 targeted `clear`／無 bridge 的 `attach`／`recover` 才允許套用；每個 materialized session 保存所用 template/target revision：
   - `fallback`：以新 catalog templates 重偵測。
   - `pin`：以 persisted pin 名稱查新 template，保留 pin 優先且不 probe；**即使名稱相同**，revision 改變也要重物化（或以 catalog generation 比較），使 timeout／UART／regex 等欄位在下一次 targeted attach 真正更新。
   - `detected`／`sticky`：維持既有 materialized profile，不因 reload 靜默改變已量測分類。
   - `yaml-target`：只有 candidate target index 以同一 owner（至少穩定的 COM + device identity）找到新 merged `SessionProfile` 時，下一次 attach 才套用 target override；目前 `_reresolve...` 早退，**只 swap `_templates` 不得宣稱這項已支援**。
4. explicit target 是 owner；reload 不得把它自動改成 pin/detected/fallback，也不得讓新增 target 搶走已有 dynamic/pinned session。無衝突的新 target 可在 atomic swap 時建立 **DETACHED 的 `yaml-target` SessionRuntime**（不開 UART、不拿 lease；須由下一次 device event 或顯式 `session attach` 才啟動），並受 `max_sessions` 約束。owner 的 device identity／COM 變更、刪除或衝突先回 `PROFILE_RELOAD_CONFLICT`，要求顯式 `session.bind`／release；`yaml-target` 仍拒絕 `session pin`（現行 `PROFILE_IS_EXPLICIT` 語意）。
5. 同一 attach/probe 全流程先在 lock 內擷取 catalog generation，之後只使用該 catalog；在 probe／新 bridge commit 前重驗 generation。若 reload 已交換，關閉尚未交付的 probe/bridge 並以新 generation 重試，禁止新舊 template／target 混用。reload 在 lock 外 parse 時若 generation/`config_refs` 已變，丟棄 candidate 從最新 refs 重建；並發 reload 不得以舊 candidate 覆蓋較新的 swap。

### Validate-before-swap、回滾與可驗收性

- 在 lock 外建立 candidate，檢查 YAML parse/schema、profile/target reference、重複 owner、candidate 與 active owner/pin 的衝突；保留 `load_profiles` 的逐檔覆寫語意。再於 lock 內重驗 refs/ownership 後一次交換；任何 invalid、TOCTOU 或 conflict 都保留舊 catalog，不能半套 templates／targets。
- 未來實作後的 pytest 驗收（本輪不執行、目前不存在 reload tests）：mock `UARTBridge.stop`、human lease、capture、background 與兩個 session，驗證 reload 零 detach/零 TX、bridge/object/lease/capture identity 不變；invalid config rollback；fallback/pin 在 clear 後確實吃 candidate；**同名 pin 的 timeout／UART／regex revision** 在 clear 後更新；explicit target override 在 clear 後確實吃 candidate；無衝突新 target 只建立 DETACHED session；target conflict、stale generation fail-closed；existing bridge `attach/recover` 不假裝套用。
- 真機／TestPilot（另案執行）：兩個以上 session 同時 READY，至少一個 human console/capture；修改單一 YAML → `daemon reload` → status refs/mtime；確認無關 UART、PID/bridge generation、lease/capture 不變；只 clear 目標後確認新 profile/target 生效；再以 invalid YAML 驗舊設定仍可用。這些需要實機時序與 operator 觀察，不能由 pytest 代替。

## 證據邊界與未驗證項

- #166 的缺 `base64`、prpl 行長與 #171 的事故／WAL 歷史，均是 issue／comment 的現場紀錄；本輪未重跑真板。現行 file transfer 仍在 `sw_core/file_transfer.py:20-25,45-70,95-99,161-180` 固定使用 base64／chunk。
- #199 現行 holder path 在 `sw_core/session_manager.py:1834-1849` 先取 `os.stat(...).st_rdev`，再掃 `/proc`；`getattr` 可避免缺欄位例外，但 `/proc` unavailable 回空集合是否足以支援 Windows reclaim 尚無 platform proof。
- #197 的 Cloudflare Quick/Named Tunnel 尚未在本輪執行；README 已有 remote contract，`tests/test_remote_tunnel.py` 是純邏輯／registry 測試，不等於真 NAT／Cloudflare／重開機證據。
- 本輪遵守 contract：未操作 UART/daemon、未跑 pytest、未修改 code/tests；root 本輪只應執行現有 tests，reload 驗收須待實作後新增。故任何 runtime 結論須由 root 依上述驗收獨立驗證。沒有頻率資料，不宣稱節省幾倍人工或降低多少事故。
