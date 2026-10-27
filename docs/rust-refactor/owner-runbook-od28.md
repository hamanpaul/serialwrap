# OD-28 擁有者 runbook：`win-task-logon-trigger`（找段落再登出）

依 OD-28（2026-10-27）與其 22:20 補記：`win-task-logon-trigger` 需擁有者登出／登入一次。Windows 登出會終止 WSL（含 driver、本機 Claude 與所有測試），所以證據必須由 Windows 端的 throwaway 排程工作**自行寫檔**，不依賴 WSL 在線；重新登入後由 agent 收取。

**執行時機**：P8 Rust daemon bundle 完成後（plan 09 Task 13）。Stage 1 不建立、不執行任何排程工作；本文件現階段只是程序定義。

## 範圍限制（OD-28）

- 只用 `scripts/stage0/win-task-throwaway.ps1` 建立的 `serialwrap-throwaway-<run_id>`；狀態／執行／設定目錄在隔離根 `%LOCALAPPDATA%\sw-throwaway\<run_id>\` 下。
- 不動正式 `serialwrap` 排程工作；結束必 `-Cleanup`。
- device allowlist 只有 `COM20`（OD-28）。**待 P8 對齊**：目前腳本 `-AllowCom` 只接受 `COM24`（`$throwawayAllowedComs`），與 OD-28 不一致；Task 13 執行前須先依 OD-28 改為 `COM20` 並經審查，不得以現況直接執行。
- 需 `SW_TEST_ALLOW_SCHTASKS=1`。

## 證據契約（排程工作自行寫入）

目錄：`%LOCALAPPDATA%\Temp\sw-od28\<run_id>\`。由 throwaway 工作的 launcher（`launch.ps1`）在 Windows 端寫入，每次觸發附加一列 JSON 到 `logon-trigger.ndjson`，每列至少含：

| 欄位 | 內容 |
|---|---|
| `run_id` | throwaway run id |
| `event` | `launcher_start`（工作被觸發、launcher 開始）／`daemon_started`／`daemon_exit` |
| `utc` | 事件 UTC 時間（ISO 8601） |
| `logon_session_start_utc` | 目前互動登入的開始時間（用來證明是本次登入觸發，而非建立當下手動啟動）。**取值來源固定**：`Get-CimInstance Win32_LogonSession`，只取 `LogonType` 為 `2`（Interactive）、`10`（RemoteInteractive）或 `11`（CachedInteractive），且經 `Win32_LoggedOnUser` 關聯到排程工作主體（互動使用者）的 session，取其中最晚的 `StartTime` 轉 UTC。不得以 launcher 自身啟動時間、工作觸發時間或其他時間代替；取值失敗或找不到符合的 session 時寫 `null` |
| `daemon_pid` | `serialwrapd.exe` PID（`daemon_started` 起） |
| `daemon_sha256` | 受測 `serialwrapd.exe` 的 sha256 |
| `exit_code`／`task_state` | `daemon_exit` 時的 exit code 與排程工作狀態 |

另寫 `SHA256SUMS`（本目錄所有檔案）。launcher 寫入失敗不得影響 daemon 判定以外的行為，但缺少 `launcher_start`／`daemon_started` 列即判「證據不足」，不得記為 PASS。

**待 P8 實作**：現行 `win-task-throwaway.ps1` 的 launcher 尚未寫上述證據檔；Task 13 須新增（例如 `-EvidenceDir` 參數），以 Windows 原生測試驗證後才可進行登出測試。

## 程序

1. agent（WSL）：建置 Rust bundle、以 `SW_TEST_ALLOW_SCHTASKS=1` 執行 `win-task-throwaway.ps1 -RunId <run_id> -Bundle <bundle>` 建立工作，確認 `%LOCALAPPDATA%\Temp\sw-od28\<run_id>\` 尚無 `launcher_start` 列（建立時不手動啟動）。
2. agent：讓本機工作停在段落（commit 進行中的變更、無前景測試），通知擁有者可以登出。
3. 擁有者：於選定時間（不早於指定日 09:00）登出 Windows，再登入；同時通知 thala session 配合。「指定日」由擁有者在 Task 13 執行前另行指定，並登記於 `owner-decisions-addenda.md`；未登記日期前 agent 不得通知擁有者登出，本文件不預設日期。
4. 擁有者重新開啟 WSL 後，agent 收取 `%LOCALAPPDATA%\Temp\sw-od28\<run_id>\` 全部檔案到 `~/b-log/rust-refactor/od28/<run_id>/`，核對 `SHA256SUMS` 與 `daemon_sha256`。
5. 判定：`launcher_start.utc` 晚於 `logon_session_start_utc`，且有 `daemon_started` 與 `daemon_pid` → PASS；`logon_session_start_utc` 為 `null` → 證據不足（不得以其他時間補值後判 PASS）；否則依實際狀況記 FAIL 或證據不足，不得放寬。
6. agent：執行 `win-task-throwaway.ps1 -RunId <run_id> -Cleanup`，確認無殘留工作與 throwaway daemon。
