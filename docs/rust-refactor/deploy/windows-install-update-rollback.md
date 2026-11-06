# Windows 安裝、更新與回滾作業指南（§12.1）

本文件詳細說明 `serialwrap` 在 Windows 平台上之安裝佈局、監管模式設定、原子更新流程、回滾（`setup --rollback`）與故障復原程序。

---

## 1. 目錄佈局與資產結構

在 Windows 平台中，`serialwrap` 採版本化目錄佈局，安裝根目錄預設位於使用者的 `%LOCALAPPDATA%\Programs\serialwrap\`：

```text
%LOCALAPPDATA%\Programs\serialwrap\
  ├── bin\                          # 執行檔入口目錄（EntryStyle::Copy，含 .new 與 .old.<ts>）
  │   ├── serialwrap.exe
  │   ├── serialwrapd.exe
  │   └── current-release.json      # 當前生效版本指標（記有 bundle_version 與目錄）
  ├── versions\
  │   ├── 0.4.0\                    # 具體版本二進位檔、Python runtime 與清單
  │   │   ├── bin\
  │   │   │   ├── serialwrap.exe
  │   │   │   └── serialwrapd.exe
  │   │   ├── py\                   # 內嵌 Python venv 執行期環境
  │   │   ├── release-manifest.json # 版本配對清單
  │   │   └── SHA256SUMS            # 元件雜湊校驗檔
  │   └── 0.3.9\
  ├── backups\                      # 更新切換前完整備份（含二進位、設定與監管快照）
  ├── supervisor\                   # 排程工作 XML 模板與設定檔
  ├── install-journal.json          # 更新／回滾事務日誌（供中斷復原）
  └── .install.lock                 # 跨行程安裝與更新互斥鎖
```

> [!NOTE]
> Windows 上的 `bin\` 入口採用檔案複製（`EntryStyle::Copy`），切換時依序透過 `*.new` → `*.old.<ts>` → 正式檔名完成原子置換，而非使用符號連結或指標。

---

## 2. 首次安裝

首次安裝由維護者或使用者執行：

```powershell
# 1. 預設使用 Windows Task 排程監管安裝（無須管理員特權）
serialwrap setup --windows-task

# 2. 選用安裝 com0com 虛擬序列埠驅動（需要系統管理員提權，SC-36，OD-15）
serialwrap setup --with-com0com
```

### 監管模式說明
- **`windows-task`（預設，推薦）**：以使用者排程工作（`schtasks`）常駐執行 `serialwrapd.exe`，設定 `ExecutionTimeLimit=PT0S`（永不逾時），使用者登入時自動啟動，崩潰時自動重啟（`RestartOnFailure`）。
- **`windows-service`（選用）**：註冊為 Windows 服務（`sc.exe`）。依架構決策 OD-12，多人共用機器（含 `windows-service` bench）在完成多使用者認證／授權（RPC token＋ACL，且 Telnet console 一併納管）前不得部署；`serialwrap setup --windows-service` 僅輸出 `pending_sudo` 指令由管理員手動執行，不自動代為提權安裝。

---

## 3. 安全更新流程（§12.1）

更新至新版本時，CLI 會執行原子交接與更新切換，嚴格防範 two-reader 競態與未完成操作中斷：

1. **暫存階段（Staging）**：將新版本解包至 `versions\<new_version>\`，並校驗 `release-manifest.json` 與各元件 sha256 雜湊值（`phase: update_verify`）。
2. **安裝鎖與事務日誌（Journaling）**：取得 `.install.lock`（`phase: install_lock`），將原始入口、監管定義、當前版本快照寫入 `install-journal.json`。
3. **備份階段（Backup）**：依序執行 `backup_started` → 建立完整 Previous 備份 → `backup_complete`（將 manifest sha256 記入 journal）。完整備份提交前絕不切換任何入口。
4. **安全檢查（Safety Check）**：檢查當前 daemon 狀態（`phase: update_safety_check`），若正處於 Flash 燒錄、檔案傳輸（FileTransfer）、活躍寫入或 recovering 狀態，**一律拒絕更新**。此項交接保護**不受 `--force` 影響**（fail-closed）。
5. **抑制自動拉起（Autostart Hold）**：在停止舊 daemon 前，先停用監管自動重啟（`phase: update_autostart_hold`），避免舊 daemon 退出時被排程器搶先拉起。
6. **交接停止（Handoff Stop）與確認釋放（Confirm Released）**：向舊 daemon 送出 `daemon.handoff_stop`，舊 daemon 在原子臨界區內凍結所有新操作並關閉。隨後執行 `confirm_released`（`phase: update_confirm_released`），於 10 秒內確認舊 daemon ping 不通且行程樹完全退出；逾時或裝置仍被持有則拒絕切換。
7. **切換入口（Entries Switching）**：在確認舊版釋放後，切換 `bin\` 下之執行檔與 `bin\current-release.json`（`phase: entries_switching`）。
8. **啟動新版並驗收（Start & Verify）**：解除自動拉起抑制（`phase: update_autostart_release`），啟動新版 daemon，並執行健康檢查與 smoke 測試（`phase: update_start_and_verify`）。
9. **驗收失敗回退（Revert）**：若驗收失敗，停止目前 daemon 僅經 `daemon.handoff_stop{reason:"revert"}`；若 daemon 忙碌或交接不明，則保留 journal 標記為 `revert_pending`，於下次 `setup` 閒置時完成回退。

---

## 4. 版本回滾（setup --rollback）

當新版本運行異常或需要退回上一版本時，使用回滾命令：

```powershell
# 回滾至上一有效版本
serialwrap setup --rollback
```

### 回滾行為原則
- **完整組合切換**：`setup --rollback` 同步切換 Rust CLI、Daemon 執行程式、設定資產與監管排程定義，絕不產生 CLI 與 Daemon 版本不匹配之半切狀態。
- **交接保護**：同樣必須通過安全檢查（無進行中 flash 與傳輸），舊版確認關閉後始切回 Previous 版本。

---

## 5. 異常復原與日誌

若更新程序因系統斷電或中斷，下一次執行 `serialwrap setup` 時會自動偵測 `install-journal.json` 並依日誌進行交易復原（`recover_journal`）：

- **尚未到達切換點（`backup_started` / `backup_complete`）**：復原程序將完整清除暫存檔並確保舊版監管正常。
- **到達或超過切換點（`entries_switching` / `start_and_verify`）**：復原程序**一律回退至原始版本**（Revert to original），絕無自動 roll-forward。
- **交接被拒或復原失敗**：若停止當前 daemon 時交接被拒（例如正處於活躍 flash 或傳輸）或結果不明，journal 檔保持**位元組不變保留**（保留 `revert_pending` 狀態），`setup` 立即以 exit code 2 終止，絕不在未完成復原前執行新的安裝操作。

---

## 6. 錯誤處置對照表

首次安裝與更新常見錯誤碼、發生階段與處置方式：

| 錯誤碼 (`error_code`) | 階段 (`phase`) | 原因說明 | 操作者處置指南 |
|---|---|---|---|
| `SESSION_BUSY` / `FLASHING_BUSY` | `update_safety_check` | Target 正處於 flash 燒錄、檔案傳輸或活躍命令執行中 | 等待當前燒錄或傳輸作業完成後重試；`--force` 不會亦不得覆寫此安全防線 |
| `TIMEOUT` / `DEVICE_STILL_HELD` | `update_confirm_released` | 舊 daemon 未能在 10 秒內完全終止，或裝置控制代碼仍未釋放 | 檢查是否有外部行程持有 COM 埠；確認舊行程退出後重試，切換未發生 |
| `TIMEOUT` | `install_lock` | 無法取得 `.install.lock` 跨行程互斥鎖 | 確認是否有另一 `setup` 實例正在執行；若無請檢查鎖檔殘留狀態 |
| `CHECKSUM_VERIFY_FAILED` | `update_verify` / `rollback_verify` | 安裝包 sha256 雜湊或 release-manifest 校驗失敗 | 重新下載或建立安裝 bundle，確認 bundle 檔案未受損 |
| `DAEMON_EXITED` / `DAEMON_NOT_READY` | `update_start_and_verify` | 新版 daemon 啟動後異常退出或 health.ping 逾時 | 系統已自動觸發回退；檢查新版日誌與設定檔相容性 |
| `INTERNAL_ERROR` / `NEEDS_SUDO` | `update_autostart_hold` | 抑制排程工作或服務自動重啟失敗 | 確認目前使用者具備排程管理權限（若為 service 需提權） |
| `SESSION_BUSY` / `TIMEOUT` | `revert_pending` / `install_recover` | 回退或日誌復原時停止目前 daemon 被拒 | 等待目前 daemon 閒置後再次執行 `serialwrap setup` 完成復原 |

---

## 7. 信任邊界與安全須知（OD-12）

- **本機信任邊界**：Windows 上之通訊（JSON-RPC 預設 `tcp://127.0.0.1:48700` 及 Telnet console）信任範圍為**整機所有本機使用者**。在多人共用主機上不得宣稱同使用者隔離。
- **多人共用機器限制**：依架構決策 OD-12，多人共用機器（含 `windows-service` bench）在完成多使用者認證／授權（RPC token＋ACL，且 Telnet console 一併納管）前不得部署；僅允許部署於單人使用之工作站（`windows-task`）。
- **遠端連線信任邊界**：當使用 `serialwrap remote` 經由 SSH 連線對外暴露或連入時，信任範圍延伸至通過該 SSH 認證之使用者。
