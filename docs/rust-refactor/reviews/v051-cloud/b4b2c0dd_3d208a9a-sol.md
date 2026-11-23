> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-5914e1c-1 | Medium | 已修 `ef2cebf3` | 0 | 本輪未改動共同 template 的結構檢查。 |
| V051-5914e1c-2 | Medium | 已修 `ef2cebf3` | 0 | 本輪未改動 `profiles` 非 mapping 的分類。 |
| V051-5914e1c-3 | High | 已修 `b4b2c0dd` | 0 | 前輪已保存修正前兩支測試的斷言紅燈；本輪未改動相關 Rust 程式與測試。 |
| V051-ef2cebf-1 | Medium | 已修 `b4b2c0dd` | 0 | fixture 已斷言替換發生及指定內容存在；本輪未改動。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 受版控的四階段政策、來源與遷移識別 | 部分完成 | `3d208a9a` 新增政策，0.3.2 資產 SHA-256 與標籤內容相符；但共同 template 集合與實際資產不符，見 B-2。 |
| `devices` 以外不得整類豁免；明確選擇政策 | 完成 `3d208a9a` | [upgrade_checks.py](scripts/docker/upgrade_checks.py:94) 與 `select_profile_policy()`。 |
| 新 check 的形狀、唯一性及精確集合判定 | 部分完成 | 已有判定和六支具名測試，但判定使用的 payload 與空映射格式不符 Rust 輸出，見 B-1、B-3。 |
| 舊版／降級明確不支援模式 | 完成 `3d208a9a` | 政策及定向測試涵蓋兩種模式。 |
| 遷移後仍缺 `timeout_recovery_keys` 須 FAIL | 完成 `3d208a9a` | [測試](tests/test_docker_upgrade.py:951)包含反例；實際 Rust payload 的接收問題仍須依 B-1 修正。 |
| 指定的無 payload 反例先紅後綠 | 部分完成 | 有綠燈，但所附先紅紀錄因函式簽章 `TypeError` 終止，未證明指定的錯誤放行使斷言轉紅，見 B-4。 |
| 本項六支具名測試與 Linux L1 登錄 | 完成 `3d208a9a` | 六個節點已加入兩個 Linux 鎖集合；本次定向執行為 **7 passed、119 deselected**。為避免觸碰正式環境，使用 `--noconftest`，因此有未註冊 marker 警告，不能當作完整 suite 證據。 |

## B 新發現

**V051-3d208a9-1 [High]** [upgrade_checks.py:461](scripts/docker/upgrade_checks.py:461) — 判定器要求 `check["extra"]`，但 Rust 的 [check_to_dict](crates/sw-cli/src/doctor_cmd.rs:1970) 會把 `Check.extra` 的鍵直接展開到 check 物件。輸入合法的 Rust `profile_template_fields` 結果時，實際回 `FAIL: 缺少 extra payload`；現有測試則自行建造巢狀 `extra`，因此綠燈無法證明介面相容。修法：按 Rust 實際的頂層欄位驗證四個鍵，並以符合實際輸出的 fixture 測試可比較與不可比較結果。

**V051-3d208a9-2 [High]** [upgrade_profile_expectations.json:23](scripts/docker/upgrade_profile_expectations.json:23) — 四個情境都只預期 `prpl-template`，但所標示的完整 `v0.3.2` 資產與目前出貨資產共有五個 template：`brcm-template`、`op3-template`、`others-template`、`prpl-template`、`uboot-template`。完整物化則有六個。即使修正 B-1，合法結果仍因 `compared_templates` 不符而 FAIL。修法：以指定的完整來源 fixture 與各目標資產計算並固定各階段的完整共同集合，再用獨立於政策內容的實際 fixture 測試核對；缺漏仍須精確相等。

**V051-3d208a9-3 [High]** [upgrade_checks.py:368](scripts/docker/upgrade_checks.py:368) — 政策強制每個共同 template 都出現在缺漏映射，並將完整物化寫成 `{"prpl-template":[]}`；Rust 的 `matched` 輸出則固定為 `missing_fields: {}`，有缺漏時也只列出有缺漏的 template。將其餘輸入調整成符合政策後，合法的空映射仍被判 FAIL。修法：允許缺漏映射只含有缺漏的共同 template，完整物化政策使用 `{}`；保持對額外欄位、錯誤 template 與非預期缺漏的精確拒絕。

**V051-3d208a9-4 [High]** [test_docker_upgrade.py:868](tests/test_docker_upgrade.py:868) — 本項明定要先以「無 payload advisory 被錯誤放行」取得斷言紅燈，但保存的[先紅輸出](~/b-log/rust-refactor/exec/v051/p289/p289-i3/round-1.share.md:3695)在呼叫舊版函式時先因新增參數 `profile_policy` 發生 `TypeError`；測試未執行到預期的 FAIL 斷言。實作者回報沒有列出此缺項。唯讀重放 `b4b2c0dd` 的舊判定器確認，無 payload advisory 實際回 `PASS`，但這不是指定測試的紅燈證據。修法：以可在修正前後執行的相同回歸測試或明確的相容測試橋接，保留修正前因 `PASS` 與預期 `FAIL` 不符而紅、修正後轉綠的輸出，並讓新測試使用真實 Rust payload 形狀。

## 既有碼待辦（不計入結論）

BC-08、doctor 契約與 P4 映射、A/B 差分與 Rust 黑箱、公開說明、升級 driver 接線及整體驗收，依編排者安排留待第 4～6 項。

## 對裁決的異議（不計入結論）

無。

結論：需修正