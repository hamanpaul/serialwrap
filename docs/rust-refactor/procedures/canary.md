# Canary 收集與事故程序（OD-10）

Canary 以真實使用累計 serialwrap 的穩定性資料。Linux 與 Windows 各自獨立累計，不得合併，也不得以 Linux 的量掩蓋 Windows 的結果。Canary 不佔用 bench，不是任何 Stage 0～2.4 出口的條件。

## 1. 門檻（每個平台各自）

| 項目 | 下限 | 說明 |
|---|---|---|
| 活躍測試日 | 14 日 | 以 policy 的 `active_day` 判定，需有足量的提交與快照覆蓋 |
| `cmd submit` | 5,000 次 | 只計 arbiter 的 `cmd_id`（`^[0-9a-f]{32}$`），以 `(host_id, cmd_id)` 去重，且必須有終態結果 |
| 干擾／交接循環 | 50 次 | 每種類型有配額，且單一類型不得超過 `cycles_max_type_share` 的上限，不得全由 Recovery 湊滿 |

Windows 不要求 `/dev/ttyMCU`，`flash` 類型的配額必須為 0。

## 2. 部署範圍

- **Windows 只部署在單人使用的測試工作站**（OD-12）。整機本機使用者皆可信；多人共用機在完成多使用者授權前不得部署。
- 本部署不得宣稱「同使用者隔離」。工作站上所有本機使用者都可讀寫同一份帳本與安裝目錄。
- 代表機台為第三方 EDR，Windows Defender 已關閉。不得把防毒行為歸因於 Defender。
- Linux 的 collector 跑在擁有者的日常工作機上，以 systemd user service 常駐。
- 工作站必須以 Stage 3 target 的 release archive 原樣安裝。安裝目錄不得放置任何額外檔案（旁載 DLL、替換資產）。

## 3. 政策凍結前的準備

- 凍結前列出 Canary 期間所有時區與 DST 變更，填入 `utc_offset_changes`（格式為 `[ISO 時間, 分鐘]`，依時間升冪）。政策凍結後不得修改。
- 活躍日以平台本地日期計算。跨時區或跨 DST 的日期切換若未列入 `utc_offset_changes`，會使同一天的紀錄被切到錯誤的本地日。

【BLOCKED-OWNER BO-P9-06】 凍結由 `canary policy freeze` 執行一次，且早於第一筆計數紀錄。活躍日認定、計數與去重、分類與配額等裁決須先完成（見 `stage3-runbook.md` 步驟 (2)）。

```bash
python3 -m tools.stage3 canary policy freeze --policy tests/acceptance/canary_policy.json --decision-id "$DECISION_ID" --frozen-by "$FROZEN_BY" --evidence-owner linux="$LINUX_OWNER" --evidence-owner windows="$WINDOWS_OWNER" --addenda docs/rust-refactor/owner-decisions-addenda.md
```

## 4. Linux：systemd user service

【BLOCKED-OWNER BO-P9-07】 Linux 日常使用期間 collector 常駐，直到累計達標。範本如下，路徑請依實際安裝調整。

```ini
[Unit]
Description=serialwrap Canary collector (linux)

[Service]
WorkingDirectory=%h/serialwrap-stage3
ExecStart=/usr/bin/python3 -m tools.stage3 canary collect --cli %h/.local/bin/serialwrap --ledger-dir %h/canary/ledger-linux --policy tests/acceptance/canary_policy.json --platform linux
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

`WorkingDirectory` 指向提供 `tools.stage3` 模組的工具 checkout，它不決定受測內容。與 Stage 3 target 相符的是 `--cli` 所指向的安裝（即以 target release archive 原樣安裝的 `serialwrap`）。部署後第一次執行單次收集，比對 snapshot 的 `bundle_tree_sha256`／`runtime_tree_sha256` 與 `evidence status` 顯示的 target 值：

```bash
python3 -m tools.stage3 canary collect --cli "$SERIALWRAP_CLI" --ledger-dir "$CANARY_LEDGER_LINUX" --policy tests/acceptance/canary_policy.json --platform linux --once
```

`collect --once` 只輸出計數器，不含指紋。指紋讀自帳本最後一筆 `snapshot` 紀錄，以 Python 讀取（Windows 工作站沒有 jq，見第 5 節）：

```bash
python3 - "$CANARY_LEDGER_LINUX/ledger.ndjson" <<'ZZPY'
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
snap = [row for row in rows if row.get("type") == "snapshot"][-1]
print(json.dumps({key: snap.get(key) for key in ("artifact_id", "bundle_tree_sha256", "runtime_tree_sha256")}))
ZZPY
```

## 5. Windows：sw-canary.pyz 與工作排程器

【BLOCKED-OWNER BO-P9-08】 Windows Canary 在測試團隊單人使用的工作站上進行。collector 以 `sw-canary.pyz` 執行（Python 3.10 以上，stdlib）。Windows 不要求 `/dev/ttyMCU`。

在工作站以工作排程器建立「登入時」觸發的工作，範例（請依實際路徑調整）：

```powershell
schtasks /Create /TN "serialwrap-canary" /SC ONLOGON /RL LIMITED /TR "py -3 C:\canary\sw-canary.pyz collect --cli C:\serialwrap\bin\serialwrap.exe --ledger-dir C:\canary\ledger-windows --policy C:\canary\canary_policy.json --platform windows"
```

部署後同樣執行一次 `collect --once`，再以下列命令讀取帳本最後一筆 `snapshot` 的指紋，與 `evidence status` 顯示的 target 值比對（`collect --once` 本身只輸出計數器）：

```powershell
py -3 -c "import json,sys; rows=[json.loads(l) for l in open(sys.argv[1],encoding='utf-8') if l.strip()]; s=[r for r in rows if r.get('type')=='snapshot'][-1]; print(json.dumps({k: s.get(k) for k in ('artifact_id','bundle_tree_sha256','runtime_tree_sha256')}))" C:\canary\ledger-windows\ledger.ndjson
```

內容指紋不同的工作站安裝不計入配額，只列為 `tree_mismatch`，必須重新安裝為 Stage 3 target 的 archive。

## 6. 計畫性重啟與停機的 note

計畫性重啟或停機前必須寫入 note。`planned_restart` 只抑制 PID 類事故；`planned_downtime` 只抑制不可達事故。note 不會抑制其他類型的事故。

note 的寫入時刻（`t_wall`，由 `canary note` 於寫入當時取得）必須不晚於它所覆蓋的觀測時刻。事後才補寫的 note 不會抑制已發生的 PID 變更或失聯事故：PID 規則以新 PID 的觀測時刻判定，不可達規則以各次失聯快照的時刻判定，兩者都要求 note 已先寫入。已發生的事故若確屬計畫內維護，須以人工分診結案（`canary incident close`），結案紀錄留存於帳本，不得以補寫 note 取代。

```bash
python3 -m tools.stage3 canary note --ledger-dir "$CANARY_LEDGER_LINUX" --kind planned_restart --from "$NOTE_FROM" --to "$NOTE_TO" --reason "$NOTE_REASON" --author "$NOTE_AUTHOR" --platform linux
```

`--from` 與 `--to` 為含時區的 ISO 8601，且 `--from` 必須早於 `--to`。

Windows 工作站以 `sw-canary.pyz` 執行，參數與 `tools.stage3` 不同：`note` 必須額外給 `--host-id`（取自帳本既有 snapshot 紀錄的 `host_id` 欄位，同一工作站固定不變）。

```powershell
py -3 C:\canary\sw-canary.pyz note --ledger-dir C:\canary\ledger-windows --kind planned_restart --from "$NOTE_FROM" --to "$NOTE_TO" --reason "$NOTE_REASON" --author "$NOTE_AUTHOR" --host-id "$HOST_ID" --platform windows
```

## 7. 事故分診表

【BLOCKED-OWNER BO-P9-09】 事故分診與結案由擁有者或證據責任人判斷，須逐筆依下表的必查證據處理。

事故分為自動偵測（由快照、WAL 訊號、擷取異常與 note 重算，不寫入帳本）與人工開案（`canary incident open`）。結案以 `canary incident close` 寫入附加式紀錄。未結案事故阻擋 `canary report` 的 PASS，也阻擋退役。

| kind | 觸發 | 必查證據 | 允許的 resolution |
|---|---|---|---|
| `unexpected_restart` | 非計畫的 daemon 重啟，即使 supervisor 已自動拉起 | supervisor 日誌、快照中的 PID 序列，以及是否有對應 note | `fixed_in_artifact`（需 issue）、`not_product_defect`、`environment`、`duplicate` |
| `unexplained_pid_change` | PID 變更，且無計畫維護紀錄，也無可佐證的主機重開 | 同上，另查 `boot_id` 與 collector 缺口 | 同上 |
| `supervisor_failed_to_start` | supervisor 無法拉起 daemon | supervisor 錯誤輸出與 `service status` | 同上 |
| `daemon_unreachable` | 連續不可達次數達 `unreachable_incident_polls` | collector 的 CLI 錯誤輸出與程序存活狀態 | 同上 |
| `stuck_attached` | 狀態停在 ATTACHED 超過 `stuck_incident_s` | 快照序列中的 session 狀態 | 同上 |
| `wal_unhealthy` | 快照的 `wal_healthy` 為 false（WAL 健康檢查失敗） | WAL 檔與快照序列 | 同上 |
| `wal_loss` | WAL 遺失或重建（`wal_loss`、`wal_recreated` 訊號） | WAL 目錄與 cursor 紀錄 | 同上 |
| `harvest_integrity` | 擷取完整性異常（如 `crc_mismatch`、`truncated`、`seq_regress`） | `harvest_anomaly` 紀錄與原始 WAL。帳本損壞與收割失敗亦屬此類 | 同上 |
| `multi_open` | 同機出現多個 daemon 或多個 tty 持有者 | `daemon status` 的 `multi_open` 與 `foreign_holders` | 同上 |
| `collector_gap` | collector 觀測缺口超過 `gap_incident_s` | collector 執行紀錄與主機睡眠、關機紀錄 | 同上 |
| `safety_violation` | 未授權 TX、跨上下文重送、人機回放等安全訊號（自動偵測） | 對應 WAL 訊號與 cmd_id 脈絡 | 只能 `fixed_in_artifact` 或 `duplicate` |
| `other` | 上列以外 | 人工描述與證據引用 | 同上 |

結案規則：

- `fixed_in_artifact` 必須附 `--issue-ref`。
- `safety_violation` 不得以環境或非產品缺陷結案。
- 同一事故只能結案一次。

```bash
python3 -m tools.stage3 canary incident list --ledger "$CANARY_LEDGER_LINUX/ledger.ndjson" --policy tests/acceptance/canary_policy.json --platform linux
python3 -m tools.stage3 canary incident open --ledger-dir "$CANARY_LEDGER_LINUX" --kind unexpected_restart --platform linux --artifact-id "$LINUX_ARTIFACT_ID" --summary "$INCIDENT_SUMMARY" --evidence "$EVIDENCE_REF"
python3 -m tools.stage3 canary incident close --ledger-dir "$CANARY_LEDGER_LINUX" --policy tests/acceptance/canary_policy.json --id "$INCIDENT_ID" --resolution duplicate --note "$CLOSE_NOTE" --closed-by "$CLOSED_BY"
```

Windows 工作站的 `sw-canary.pyz` 參數不同：`incident open` 需 `--host-id`，結案使用 `--incident-id`，列表使用 `--ledger-dir`。

```powershell
py -3 C:\canary\sw-canary.pyz incident list --ledger-dir C:\canary\ledger-windows --policy C:\canary\canary_policy.json
py -3 C:\canary\sw-canary.pyz incident open --ledger-dir C:\canary\ledger-windows --kind unexpected_restart --platform windows --host-id "$HOST_ID" --artifact-id "$WINDOWS_ARTIFACT_ID" --summary "$INCIDENT_SUMMARY" --evidence "$EVIDENCE_REF"
py -3 C:\canary\sw-canary.pyz incident close --ledger-dir C:\canary\ledger-windows --policy C:\canary\canary_policy.json --incident-id "$INCIDENT_ID" --resolution duplicate --note "$CLOSE_NOTE" --closed-by "$CLOSED_BY"
```

人工開案的 `--artifact-id` 為必填，使事故能歸屬到具體 artifact。工作站的 zipapp 雖允許省略，本程序仍要求填寫。

## 8. 每週報告

每週以目前 Stage 3 target 的兩個 tree 值產生報告。`--bundle-tree-sha256` 與 `--runtime-tree-sha256` 必填，且必須取自 `evidence status` 顯示的 target 值。缺值即 argparse 錯誤，不得以其他值代替。

`canary report` 不在 `sw-canary.pyz` 內。Windows 的報告於含完整 repo 的環境（與 Linux 相同）以帳本副本產生。

```bash
python3 -m tools.stage3 canary report --policy tests/acceptance/canary_policy.json --addenda docs/rust-refactor/owner-decisions-addenda.md --ledger "$CANARY_LEDGER_LINUX/ledger.ndjson" --platform linux --artifact-id "$LINUX_ARTIFACT_ID" --bundle-tree-sha256 "$LINUX_BUNDLE_TREE_SHA256" --runtime-tree-sha256 "$LINUX_RUNTIME_TREE_SHA256" --md
```

結果判定：

- `PASS`：門檻全部達成，且無未結案事故。
- `INCOMPLETE`：有未達門檻的項目或未結案事故。`reasons` 會列出。
- `FAIL`：有 `safety_violation`（不論是否結案），或以 `fixed_in_artifact` 結案者。
- `POLICY_NOT_FROZEN`、`POLICY_CHANGED`：政策未凍結或凍結後被改。

報告只以帳本版本最新的那一份代表目前狀態。事後出現的未結案事故會使舊 PASS 失效；舊報告不得重新匯入。報告的 JSON 版本（`--out`）含 `ledger_through` 與 `ledger_records`，供 ingest 核對。

## 9. 安全與不得呼叫的命令

- Canary collector 與演練觀測不得呼叫 `session.interactive_status`（不得呼叫：會 touch Lease，規格 §10.2、§15.11.3）。
- collector 只呼叫三個唯讀命令：`daemon status`、`session list` 與 `cmd status`（`READONLY_COMMANDS`），外加讀取 WAL 檔。
- 收割 WAL 時只讀取，不得改寫、清理或移動 WAL 檔。

## 10. 工作站的 pack 與安裝

收集器的 zipapp 由 `canary pack-collector` 打包。打包結果必須與 Stage 3 target 的 release 內容分開保存，不得放入工作站的安裝目錄。

```bash
python3 -m tools.stage3 canary pack-collector --out "$DIST_DIR/sw-canary.pyz"
```
