## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 978aa1e | 0 | 最終比對前停止 daemon，等待 bridge barrier，再核對 checkpoint 長度及雜湊；延遲重複 TX 已有回歸案例。 |
| 2 | Medium | 已修 51bd8fa | 0 | 改由實際受委派的孫行程寫 ready＋PID，父行程確認存活後才送事件，並確認孫行程結束。 |
| 3 | Medium | 已修 ddcbdde、4e3ba13 | 0 | 出口 driver 已接上逐列判定；只有完整符合核准延後條件的 producer exit 1 才能豁免。 |
| 4 | High | 已修 12b3a92 | 0 | 移除系統 PyYAML 與 pipx system-site-packages；bundle 自帶 PyYAML，舊版依賴由離線 wheelhouse 提供。 |
| 5 | Medium | 已修 12b3a92 | 0 | 正式配方改用 `--release-bundle`，只有 `--dev` 啟用 `sw-cli/test-fakes`。 |
| 6 | Low | 已修 12b3a92 | 0 | 前置條件已改為實際 gate 狀態及核准延後條件。 |

## B 新發現

### S1-4baca8c-1

**[Medium] [crates/sw-cli/src/rollback.rs:806](crates/sw-cli/src/rollback.rs:806) — 首次 pipx 升級在送 stop 前中斷，後續 setup 無法恢復。**

`26a7ff6` 新增 legacy 恢復分支，但仍呼叫要求 bundle 安裝狀態的 `verify_restored`。

具體情境：0.3.2 pipx 安裝沒有 `current-release.json`，daemon 正常執行；在 `legacy_stop:after_capture` 中斷。再次 setup 時，capture 為 `captured`、舊 daemon 仍存活，恢復走到此處；然而 [rollback.rs:3389](crates/sw-cli/src/rollback.rs:3389) 對「沒有 current，但 daemon 存在」一律報錯。實際回 `restore_verify_failed`、保留 journal，之後每次 setup 都重複失敗；預期應撤銷 hold、保留原 daemon 並完成恢復。

現有 crash 迴圈只使用帶 current 的 `legacy_on_demand_update()`，未覆蓋首次安裝。

**確切修法：**為「legacy 已擷取、尚未停止」提供恢復驗證：核對原入口、config、監管狀態及擷取的行程身分，不要求 bundle current 或版本配對；將無 current 的 pipx 首次安裝加入 crash 迴圈。

### S1-4baca8c-2

**[Medium] [crates/sw-cli/src/rollback.rs:823](crates/sw-cli/src/rollback.rs:823) — 跨模式升級中斷後，恢復仍用舊 endpoint 停止新版 daemon。**

`fb56d6c` 已讓新版在 `target_endpoint` 啟動，但本輪修改的 journal 恢復呼叫仍使用第 784 行取得的 `original.supervisor.endpoint`。

具體情境：舊 on-demand daemon 使用自訂 socket X，執行 `setup --user` 後，新 systemd-user daemon 在預設 socket Y 啟動並持有原 UART；於 `start_and_verify:started` 中斷。再次 setup 時，恢復只探測 X，將新版判為不存在，沒有向 Y 送交接；接著 `confirm_released` 掃到新版仍持有 UART，回 `tty_holder_remains`。反覆重試都停不到 Y，無法自動還原。

**確切修法：**在 journal 保存目標模式及 endpoint；恢復時先於目標 endpoint 停止新版，再確認舊 endpoint 與 UART 均已釋放，最後還原。新增 X≠Y、且新版持有 UART 的跨模式 crash 回歸。

本輪檢查 7 個非 merge commit，無 merge commit。上述為靜態路徑反例，未做 Rust 行為重現。契約檢查及 7 支直接呼叫的純函式／靜態檢查通過；policy 為 25 pass、0 fail、2 warn（非 PR 上下文）。凍結鎖各新增 38 筆、無移除；兩份新增報告雜湊相符。受唯讀沙箱限制，未建置或執行完整 pytest／Cargo、Docker、Windows 原生測試。未修改檔案、未接觸 production，工作樹乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正