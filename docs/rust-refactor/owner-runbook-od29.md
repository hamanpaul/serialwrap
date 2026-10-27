# OD-29 (4) 擁有者 runbook：`cap_interactive_desktop__exec_or_wait_ctrl_c`

本測試需要 Windows **互動 console**（stdin 與 stdout 都必須是終端機；`win_ctrl::interactive_console_attached()`），在 WSL interop、排程工作或任何導向輸出的非互動環境下會以「證據不足，非 PASS」失敗。依 OD-29 (4)，由擁有者在 Windows Terminal 執行一次；agent 不得在非互動環境代跑。

## 受測 binary

| 項目 | 值 |
|---|---|
| 來源 commit | `63d9bdb0ae91f0e160991935842704a8edfe8b55`（`feature/rust-refactor`；測試本體 `crates/sw-pal/tests/exec_delegate.rs` 於此後未變更） |
| 建置 | WSL：`cargo test --no-run --target x86_64-pc-windows-gnu -p sw-pal --features test-fakes --test exec_delegate` |
| 放置路徑 | `%LOCALAPPDATA%\Temp\sw-od29\exec_delegate.exe` |
| sha256 | `74f26463e220d658f6fe33a57723eb5874d6b30562176348cbf6347eaa53a95b` |
| 預檢 | `--ignored --list --exact cap_interactive_desktop__exec_or_wait_ctrl_c` 列出恰好 1 個測試 |

測試只在本行程與自身重入的子行程之間送 `CTRL_C_EVENT`／`CTRL_BREAK_EVENT`（子行程以 `CREATE_NEW_PROCESS_GROUP` 建立，事件只送該 process group），孫行程為 `ping -n 30 127.0.0.1`；不碰正式安裝、不開任何 COM。

## 擁有者執行的單一指令

在 **Windows Terminal 的 PowerShell 分頁**（不要從 WSL、不要導向或管線輸出）貼上並執行：

```powershell
$d="$env:LOCALAPPDATA\Temp\sw-od29"; Start-Transcript -Path "$d\ctrl_c-transcript.txt" -Force | Out-Null; & "$d\exec_delegate.exe" --ignored --exact cap_interactive_desktop__exec_or_wait_ctrl_c --nocapture; $rc=$LASTEXITCODE; Stop-Transcript | Out-Null; [ordered]@{test='cap_interactive_desktop__exec_or_wait_ctrl_c'; exit_code=$rc; exe_sha256=(Get-FileHash "$d\exec_delegate.exe" -Algorithm SHA256).Hash.ToLower(); finished_utc=(Get-Date).ToUniversalTime().ToString('o'); host=$env:COMPUTERNAME} | ConvertTo-Json | Set-Content -Encoding utf8 "$d\ctrl_c-result.json"; "exit_code=$rc"
```

預期：畫面出現 `test cap_interactive_desktop__exec_or_wait_ctrl_c ... ok` 與 `1 passed`，最後一行 `exit_code=0`；全程約 5 秒（兩個事件各等 2 秒再送出）。

若 `exec_delegate.exe` 不存在（暫存目錄被清理），請通知 agent 以上表的建置指令重建並重新放置，sha256 會隨之更新於本文件。

## 執行後（agent 處理）

1. 將 `%LOCALAPPDATA%\Temp\sw-od29\ctrl_c-result.json` 與 `ctrl_c-transcript.txt` 複製到 `target/od29/`，核對 `exe_sha256` 等於上表值、`exit_code` 為 0，且 transcript 內有 `1 passed`。
2. PASS：在 `docs/rust-refactor/owner-decisions-addenda.md` 的 OD-29 實跑紀錄列把本項改為「已實證（OD-29）」，附結果檔 sha256；FAIL 照實回報，不得改判或重跑到通過為止。
