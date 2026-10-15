# v5 擁有者裁決紀錄

| ID | 裁決 | 日期 | 備註 |
|---|---|---|---|
| OD-01 | **A**：TCP＋Telnet 為必備基準；com0com Serial（COMx）為選配，僅在驅動可部署的機台啟用 | 2026-10-15 | Spike 1 失敗時不另行採購驅動；OD-08 的 Console-side 測試依此為選配範圍，DUT-side 原生 Windows serial gate 不受影響 |
| OD-02 | **A**：Stage 0／1 出口通過後直接啟動 Slice 2.1，以實測速率校準後續切片 | 2026-10-15 | 附條件承諾：前置 OD-04、OD-10、OD-13 與 Stage 0／1 gate；斷路器（連續 3 週契約測試無實質進展）觸發即退守 Stage 1 |
| OD-03 | **A**：測試團隊工作站預設 `windows-task`（登入自啟、失敗自動重啟、使用者身分、關閉 72 小時執行上限） | 2026-10-15 | `windows-service` 僅限無人值守 bench 且 OD-12 條件成立時採用；`on-demand` 保留為退路；各模式須完成 Spike 2 |
| OD-04 | **BC-01～BC-05 全數核准**（PROPOSED → APPROVED），連同 v5 §9.1 所列核准範圍與必要驗證 | 2026-10-15 | BC-01 的 F12 oracle 修訂仍由 OD-13 配套；BC-04 的平台證據依 OD-05／OD-08；BC-04 使 daemon 持有裝置時無法再直接開 tty（應走 serialwrap console），屬預期行為 |
| OD-05 | **代表機台＝擁有者本機 Windows 主機**：Windows 11 Pro 24H2（registry ProductName 顯示 Windows 10 為已知顯示問題）；HVCI 開啟且執行中、VBS 執行中；Secure Boot **關閉**；使用者可自行以管理員權限安裝；端點防毒為 **第三方企業端點防毒（EDR）（執行中）**，Windows Defender 關閉 | 2026-10-15 | 2026-10-15 自 WSL 唯讀查詢取得。**更正**：v5 中「Defender」相關敘述（含 16.8 ms WAL 歸因、Spike 3）改為「實際端點防毒（本機為 第三方企業端點防毒（EDR））」。代表性風險：若測試團隊機台開啟 Secure Boot，驅動載入條件更嚴，須另行記錄 |
| OD-06 | **A**：規則直接進 paulsha-conventions，為**通用規則、不寫死錯誤碼**：「凡程式會輸出的 error code，都必須在 help／SKILL 有對應說明（意義與處置）」 | 2026-10-15 | 規則本身不得內建任何錯誤碼清單；各 repo 以設定宣告「如何抽出會輸出的 code」（含常數傳遞路徑）與「說明文件位置」。v5 §9.2 的 78 個清單僅為現況盤點，不得成為規則或政策的硬編碼依據；`error_code` 與 session `last_error` 兩個命名空間都適用 |
| OD-07 | **A**：`uart-stdio-bridge` 放在 serialwrap repo 的獨立目錄 | 2026-10-15 | 比照 `realhw/`、`regression/` 模式：獨立依賴、自測與 byte 紀錄 schema；禁止 import 產品程式碼、不得共用產品判定邏輯；驗收時固定工具 revision |
| OD-08 | **A＋C**：擁有者本機 Windows 主機作為自架 runner；Spike 1 通過則以 com0com DUT-side pair 接 bridge，不通過則以實體 USB-UART 直接接 Windows 側（不經 usbipd） | 2026-10-15 | 一般 hosted PR 不裝驅動；動到 PAL 的 PR 與各階段出口必須取得原生 Windows COM handle 證據；記憶體 fake 不得充數。Console-side COM 依 OD-01＝選配 |
| OD-09 | **立即凍結三項**：非預期命令錯誤率 ≤1% 且不高於對應 Python 基線；每板每來源有效完成吞吐 ≥ 基線 95%；`stuck_attached` 總時長與最長連續區段不高於基線。**延後凍結**：RSS／資源預算、最低有效負載與樣本、比較容許誤差——於 Stage 0 修正後 Python 48h 基線完成後、任何 Rust 候選版本長跑開始前凍結 | 2026-10-15 | 安全事件為零與「不得看完結果再改門檻」為不可調原則；既有引擎 FAIL 判準保留為 `engine_result`，發版判定以 `release_gate_result` 表達 |
| OD-10 | **精簡版**：(1) 48h 長跑一律在擁有者 bench 上 Linux／Windows **依序**執行（同機同板經 usbipd，不可並行），由擁有者排程；(2) Stage 0 只跑**修正後** Python 48h 基線（兩平台各一次），修正前以既有 32h 歷史資料參考、BC-01 效能以微基準與短測驗證；(3) Canary 配額照舊（每平台 14 活躍日、≥5,000 次 `cmd submit`、≥50 次干擾／交接循環），但以**真實使用**累計：Windows＝測試團隊工作站、Linux＝擁有者日常使用，不佔 bench | 2026-10-15 | 預估 bench 佔用由約 48 天降至約 2 週；長跑期間 bench 互斥鎖與其他 TestPilot 套件共用，排程需避開；canary 期間非預期重啟仍計事故 |
| OD-11 | **核准（附限制）**：Windows 測試專用虛擬裝置白名單，作為 Linux `SERIALWRAP_BY_ID_DIR` sandbox 的對應機制 | 2026-10-15 | 限制：僅對隔離實例（獨立 state／run 目錄）生效，正式 daemon 設定遇到即拒絕；只接受明確列出的 COM 名稱，不得全域放行 com0com；不關閉獨占鎖、不更換 serial backend；啟用時 `daemon status` 必須顯示 |
| OD-12 | **A**：Windows RPC 與 Telnet console 的信任範圍明定為「整機本機使用者皆可信」，僅部署於單人使用的工作站（`windows-task`） | 2026-10-15 | 多人共用機器（含 `windows-service` bench）在完成多使用者認證／授權（RPC token＋ACL，且 Telnet console 一併納管）前不得部署；規格不得再宣稱已具「同使用者隔離」；`serialwrap remote` 經 ssh 暴露 RPC 時，信任範圍延伸至通過該 ssh 驗證者，須寫入規格 |
| OD-13 | **一併核准並重算工期**：F12 oracle 由 mtime 改為實際 WAL 進展（seq 前進＋檔案大小或讀回最後一筆）；Stage 1 Rust CLI＋Python daemon 版本配對記錄與檢查；效能量測口徑（Workload-S1、取樣、跨 64 MB rotate 停頓上限）；最小 Quiet Gate／Drain／Release／禁止危險重送前移至 Slice 2.1；斷路器「連續 3 週承接率 <10%」須定義分母與起訖 | 2026-10-15 | 不得沿用 v4 工期；Stage 0 起的工期依 OD-07（bridge）、OD-08（自架 Windows runner）、OD-10（bench 依序長跑約 2 週）重新估算 |

## 全部裁決完成（2026-10-15）

OD-01～OD-13 均已裁決；BC-01～BC-05 依 OD-04 改為 APPROVED。待辦：(1) 依本表更新 v5 → v6 規格內容（含 OD-05 的「Defender → 實際端點防毒（第三方企業端點防毒（EDR））」更正、OD-06 的「不寫死錯誤碼」原則）；(2) 重算工期；(3) 定稿前請 Astra 對整份規格做一次對抗審查；(4) 定稿放入 repo。
