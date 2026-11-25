> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-5a35dc6-1 | High | 已修 `41211d5c` | 0 | 複本重啟後的 doctor 斷言保留；本輪依裁決改為固化解析後的路徑。Windows 路徑的新邊界見 B。 |
| V051-5a35dc6-2 | High | 已修 `413e02f3` | 0 | 本輪未見撤銷 symlink 與 `..` 的解析修正。 |
| V051-413e02f-1 | High | 已修 `41211d5c` | 0 | 不同工作目錄與不同實體 WAL 目錄的 daemon／doctor 測試保留。 |
| V051-95ad179-1 | High | 已修 `5444fea5` | 0 | 依裁決移除來源紀錄的讀寫；唯讀寫回失敗、改值及改回後重啟的定向測試通過。 |
| V051-95ad179-2 | Medium | 已修 `5444fea5` | 0 | 移除來源鍵辨識、清除與寫入；定向測試確認既有鍵在複製和重啟後保留。 |
| V051-95ad179-3 | Medium | 已修 `5444fea5` | 0 | 紅燈 log 記錄 HEAD、空的產品差異統計與測試檔雜湊；綠燈產品碼及測試檔雜湊均與本提交相符。 |
| V051-d0b341a-1 | High | 已修 `5444fea5` | 0 | 依裁決不再辨識或覆寫來源形狀的使用者鍵；碰撞案例定向測試通過。 |

## B 新發現

1. **V051-5444fea-1 [High]** [daemon_main.rs:1630](crates/sw-daemon/src/daemon_main.rs:1630) — Windows 跨磁碟機的相對 `wal_dir` 在複製後仍是相對路徑。

   例如 system config 為 `C:\ProgramData\serialwrap\config.yaml`，設定 `wal_dir: 'D:archive\wal'`。Windows 路徑 join 遇到不同磁碟機時會回傳 `D:archive\wal`，程式隨即把它寫入複本；它仍依 D 槽的工作目錄解析。若首次啟動時 D 槽工作目錄是 `D:\one`，重啟時變成 `D:\two`，WAL 會分流，違反本輪裁決要求複製時寫入絕對路徑並固定目錄。

   **修法：**依 Windows 原生磁碟機相對路徑語意，將複製當下實際使用的 WAL 路徑解析為完整絕對路徑後再寫入；解析失敗時不得寫入仍依賴工作目錄的複本。補上跨磁碟機相對路徑、變更工作目錄後重啟的回歸測試，並保留 symlink／`..` 的檔案系統語意。這是靜態追查及 `ntpath` 反例；未在 Windows 原生環境實跑。

定向驗證：`daemon_process` 的 `system_config_` 三項 **3 passed**；未執行完整 pytest、Docker、bundle 或 Windows 原生測試。工作樹保持乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正