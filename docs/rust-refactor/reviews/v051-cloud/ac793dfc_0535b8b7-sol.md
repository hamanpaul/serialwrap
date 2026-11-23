> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-5914e1c-1 | Medium | 已修 `ef2cebf3` | 0 | 共同 template 結構檢查未受本輪變更影響。 |
| V051-5914e1c-2 | Medium | 已修 `ef2cebf3` | 0 | `profiles` 非 mapping 的分類未受本輪變更影響。 |
| V051-5914e1c-3 | High | 已修 `b4b2c0dd` | 0 | 相關 Rust 程式與測試未受本輪變更影響。 |
| V051-ef2cebf-1 | Medium | 已修 `b4b2c0dd` | 0 | fixture 替換錨點及內容斷言仍在。 |
| V051-3d208a9-1 | High | 已修 `e1353c4b` | 0 | 判定器仍讀取 check 頂層 payload。 |
| V051-3d208a9-2 | High | 已修 `e1353c4b` | 0 | 共同及完整物化 template 的集合斷言仍在。 |
| V051-3d208a9-3 | High | 已修 `e1353c4b` | 0 | 缺漏映射仍只列有缺漏的 template。 |
| V051-3d208a9-4 | High | 已修 `e1353c4b` | 0 | 本輪未修改相關判定器；前輪紅燈證據仍適用。 |
| V051-e1353c4-1 | Medium | 已修 `557b625d` | 0 | 受版控 fixture、摘要及集合斷言仍在。 |
| V051-557b625-1 | Medium | 已修 `ac793dfc` | 0 | fixture 與 LF Git 屬性未受本輪變更影響。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| ITEM 4：BC-08、doctor 契約、coverage、P4 映射及 Linux 鎖 | 完成 `1c6823bb`、`39bef5ae`、`0535b8b7` | BC、schema、兩個 P4 case 與鎖均已登錄；契約 revision 測試的合併問題見 B-3。 |
| ITEM 5：A/B 唯一差分、三個 Rust-only 黑箱及公開說明 | 完成 `1c6823bb`、`5d9213ae` | 保留原 nodeid、19 項共同基線與 bundle 配對；新增三個黑箱及文件。實作者紀錄 bundle 黑箱 `5 passed`。 |
| ITEM 6：各入口政策傳遞與 system 路徑 | 部分完成 | [driver](scripts/docker/run_upgrade_container.py:664) 已接線，但 system 政策與實際物化結果不符，且遷移後政策不能證明 p290 資產存在；見 B-1、B-2。 |
| ITEM 6：driver 反例與判定結果測試 | 部分完成 | [參數化測試](tests/test_docker_upgrade.py:519) 未覆蓋指定的缺 check、錯政策及損壞 payload 的實際 phase 判定；見 B-4。實作者的 SPEC-GAPS 未記載此缺項。 |
| ITEM 6：紅綠燈、定向驗收及交付紀錄 | 完成 `1c6823bb` | 實作者保留 ITEM 4、5、6 紅燈紀錄，並回報 crate、交叉編譯、契約、policy、鎖與 bundle 測試結果；正式 OD-33、跨包整合及 Windows 原生驗收依 brief 留待編排者。 |

本次審查以 `--noconftest`、停用 pytest 快取執行 driver 定向測試，結果為 **7 passed、126 deselected**；另以實際受版控 profile 資料在記憶體中呼叫判定器。未執行 Docker、bundle 或完整測試套件。

## B 新發現

### V051-0535b8b-1 [High] [run_upgrade_container.py:664](scripts/docker/run_upgrade_container.py:664) — system 升級使用錯誤的預期集合

system setup 會將出貨 profile 複製到 `/etc/serialwrap/profiles`，但此入口套用與舊使用者 profile 相同的 `migrated` 政策。完整物化的檔案包含六個共同 template、缺漏為 `{}`；`migrated` 預期五個 template 且缺 `boot_done_regex`，因此正確的 system 升級被判 **FAIL**。以目前出貨資產組成的 payload 呼叫真實判定器，已重現共同 template 不符。

**修法：**在執行 phase 前依已知的 system 物化情境選定受版控的 `materialized` 政策；補實際 system phase 測試，使用完整出貨 profile 的 payload 並斷言 PASS，同時保留非預期缺漏必須 FAIL 的斷言。

### V051-0535b8b-2 [High] [run_upgrade_container.py:875](scripts/docker/run_upgrade_container.py:875) — 未合入 p290 的候選資產可冒充遷移後通過

driver 固定選 `migrated`，但沒有驗證候選出貨資產已包含 `timeout_recovery_keys`。目前 HEAD 的出貨資產和 0.3.2 fixture 都缺此鍵，因此 doctor 比較只回報缺 `boot_done_regex`，恰好等於 `migrated` 預期。以這兩份實際資料呼叫判定器，結果為 **PASS**，儘管 live profile 仍缺要求遷移後存在的鍵。

**修法：**在升級完成 gate 加入可追溯的候選資產與遷移結果驗證：確認 p290 目標資產包含該鍵，且適用的 live template 已具備該鍵；缺一即 FAIL。補「資產與 live 均缺鍵」及「資產有鍵、live 缺鍵」的實際 driver 反例。

### V051-0535b8b-3 [Medium] [test_contracts_catalog.py:1382](tests/test_contracts_catalog.py:1382) — 把全域 revision 固定為 12，會阻擋後續合法合併

其他工作包依裁決登錄 BC 並將 `behavior_changes.yaml` revision 從 12 前進至 13 時，即使 BC-08 未再修改、其 `approved_at_revision` 合法保留 12，此測試仍因 `behavior["revision"] == 12` 失敗。

**修法：**驗證 BC-08 的核准 revision 是有效的最後變更 revision，並允許全域 revision 後續單調前進；補一個全域 revision 增加而 BC-08 不變的測試。

### V051-0535b8b-4 [High] [test_docker_upgrade.py:550](tests/test_docker_upgrade.py:550) — ITEM 6 指定的 driver 反例與成功判定尚未完成

現有參數化測試只在遷移後缺 `timeout_recovery_keys` 時斷言 FAIL；新版缺 check、錯政策、損壞 payload 沒有透過實際 driver phase 驗證。其餘有效情境也只檢查呼叫參數，沒有斷言最終 doctor verdict 為 PASS，因此 system 使用錯誤預期仍能讓這七個測試全綠。這些缺項未列入實作者 SPEC-GAPS。

**修法：**對一般升級、install-sh、system、重新升級及舊版／降級入口，以真實判定器斷言各自的 PASS／FAIL；新增缺 check、錯政策、損壞 payload 與 system 完整物化反例，保留既有精確缺漏判準。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正