> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-5914e1c-1 | Medium | 已修 `ef2cebf3` | 0 | 本輪未修改共同 template 結構檢查。 |
| V051-5914e1c-2 | Medium | 已修 `ef2cebf3` | 0 | 本輪未修改 `profiles` 非 mapping 的分類。 |
| V051-5914e1c-3 | High | 已修 `b4b2c0dd` | 0 | 相關實作與測試未受本輪修改。 |
| V051-ef2cebf-1 | Medium | 已修 `b4b2c0dd` | 0 | fixture 錨點及內容斷言保留。 |
| V051-3d208a9-1 | High | 已修 `e1353c4b` | 0 | 判定器仍讀取 check 頂層 payload。 |
| V051-3d208a9-2 | High | 已修 `e1353c4b` | 0 | 共同及完整物化 template 的集合斷言保留。 |
| V051-3d208a9-3 | High | 已修 `e1353c4b` | 0 | 缺漏映射仍只列有缺漏的 template。 |
| V051-3d208a9-4 | High | 已修 `e1353c4b` | 0 | 本輪未修改相關判定器。 |
| V051-e1353c4-1 | Medium | 已修 `557b625d` | 0 | 受版控 fixture、摘要及集合斷言保留。 |
| V051-557b625-1 | Medium | 已修 `ac793dfc` | 0 | fixture 與 LF Git 屬性未受本輪修改。 |
| V051-0535b8b-1 | High | 已修 `7fc2c40e` | 0 | system 入口政策與完整物化 payload 測試保留。 |
| V051-0535b8b-2 | High | 未修 | 2 | 新 probe 在正式安裝路徑下無法執行，見 B-1；YAML 掃描另有新反例，見 B-2。 |
| V051-0535b8b-3 | Medium | 已修 `a57dee5c` | 0 | BC-08 的內容指紋與核准 revision 12 綁定，並有全域 revision 前進及較早核准值的反例。 |
| V051-0535b8b-4 | High | 已修 `7fc2c40e` | 0 | 缺 check、錯政策、損壞 payload 及各入口 verdict 的測試保留。 |
| V051-cb6bea1-1 | High | 已修 `a57dee5c` | 0 | 原報告的多行 quoted scalar 與一般 quoted key 反例已補測；新掃描器的其他合法 YAML 失敗情境見 B-2。 |
| V051-cb6bea1-2 | High | 未修 | 1 | 已新增 probe 與舊資產替身測試，但正式安裝路徑會在 probe 前被拒絕，見 B-1。 |
| V051-cb6bea1-3 | High | 已修 `a57dee5c` | 0 | 固定 BC-08 內容指紋及其最後核准 revision，定向測試通過。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 處理 YAML 結構判定與兩個原反例 | 部分完成 | `a57dee5c` 加入掃描器與測試；原反例通過，但合法 YAML 仍可能被誤判，見 B-2。 |
| 驗證候選 CLI 的內嵌 p290 資產 | 部分完成 | `a57dee5c` 加入 doctor probe 與替身測試；正式安裝路徑不符合 probe 的路徑判準，見 B-1。 |
| 綁定 BC-08 內容與最後核准 revision | 完成 `a57dee5c` | [test_contracts_catalog.py](tests/test_contracts_catalog.py:1383) 包含內容指紋、revision 前進及錯誤 revision 反例。 |
| 先紅後綠，逐項回報 finding 對照、輸出與 commit SHA | 部分完成 | 本輪區間只有 `a57dee5c`；指定的 `v051-fix-responses.md` 不存在，提供的資料中也沒有實作者逐項紅燈與驗收回報，見 B-3。 |
| 不放寬既有斷言、不刪除或改名既有測試 | 完成 `a57dee5c` | 本輪 diff 未發現刪除或改名既有測試，也未發現削弱既有斷言。 |

## B 新發現

### V051-a57dee5-1 [High] [run_upgrade_container.py:396](scripts/docker/run_upgrade_container.py:396) — 正式安裝入口與 bundle 來源路徑不同，導致 probe 永遠先失敗

正式 driver 傳入的 bundle 是 `/opt/sw-new`；setup 會將版本複製到使用者的 `versions/<version>`，再讓 `~/.local/bin/serialwrap` 指向該安裝副本。因而 `active_real` 是安裝副本，而 `candidate_real` 是 `/opt/sw-new` 內的來源；兩個正常路徑不相等。記憶體替身依正式路徑執行此方法，結果為 `FAIL：目前 CLI 並非本次候選版本`，連 doctor probe 都未執行。升級與重新升級的 doctor gate 因此無法在正常安裝後通過。

現有 [fake driver](tests/test_docker_upgrade.py:545) 把 active 入口直接指向 bundle 來源，未模擬安裝複製。

**修法：**確認 active 入口確實指向本次安裝的版本，對該 active CLI 執行 probe，並以可信的版本或內容證據連結到候選 bundle；補正式「來源 bundle 與安裝副本為不同路徑」的 driver 測試，涵蓋正常 PASS 與舊內嵌資產 FAIL。

### V051-a57dee5-2 [Medium] [upgrade_checks.py:566](scripts/docker/upgrade_checks.py:566) — 合法 plain scalar 中的 `[` 會遮住後續欄位

例如 `prpl-template` 含 `prompt_regex: ^\[`，接著有 `timeout_recovery_keys: [CTRL_C]`。YAML 解析後欄位確實存在，但新掃描器把 plain scalar 裡的 `[` 當成 flow collection 開始，之後不再辨識 mapping entry，回報缺欄。記憶體反例實際得到 `actual_key=True`、掃描結果 `False`。因此合法 profile 可使遷移 gate 錯誤 FAIL。

同樣，合法的 quoted key `"timeout_recovery_\x6beys"` 被 YAML 解碼為目標欄位，但掃描器未解碼而判缺漏。

**修法：**以能依 YAML 語意取得實際 mapping key 的方式判定，同時遵守容器依賴限制；補上述反例及正式 driver verdict 測試。

### V051-a57dee5-3 [High] [test_docker_upgrade.py:1282](tests/test_docker_upgrade.py:1282) — 無法追溯本項要求的逐項紅燈與實作者驗收回報

brief 要求每項先提供會紅的測試或反例，再修正，並以 finding 對照表、驗收結果及 commit SHA 回報。可見的修正區間只有一個同時包含測試和修正的 commit；指定回應文件不存在，提供的資料也沒有逐項紅燈輸出或附理由的 SPEC-GAPS。因此目前只能確認測試存在及現況結果，無法確認要求的先紅證據與逐項交付回報。

**修法：**補上每個 finding 的紅燈命令、輸出、所測 commit、修正後驗收結果及對照表；若證據無法取得，將缺項及理由列入實作者 SPEC-GAPS。

定向驗證：在 `a57dee5c` 以 `--noconftest`、停用 pytest cache 執行四個直接相關節點，**4 passed**；另以記憶體替身重現 B-1、B-2。未執行完整 pytest、Docker 或 bundle 測試；未修改工作樹檔案。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正