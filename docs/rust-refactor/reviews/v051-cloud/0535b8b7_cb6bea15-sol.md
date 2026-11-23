> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-5914e1c-1 | Medium | 已修 `ef2cebf3` | 0 | 共同 template 結構檢查未受本輪變更影響。 |
| V051-5914e1c-2 | Medium | 已修 `ef2cebf3` | 0 | `profiles` 非 mapping 的分類未受本輪變更影響。 |
| V051-5914e1c-3 | High | 已修 `b4b2c0dd` | 0 | 相關 Rust 程式與測試未受本輪變更影響。 |
| V051-ef2cebf-1 | Medium | 已修 `b4b2c0dd` | 0 | fixture 錨點及內容斷言仍在。 |
| V051-3d208a9-1 | High | 已修 `e1353c4b` | 0 | 判定器仍讀取 check 頂層 payload。 |
| V051-3d208a9-2 | High | 已修 `e1353c4b` | 0 | 共同及完整物化 template 的集合斷言仍在。 |
| V051-3d208a9-3 | High | 已修 `e1353c4b` | 0 | 缺漏映射仍只列有缺漏的 template。 |
| V051-3d208a9-4 | High | 已修 `e1353c4b` | 0 | 本輪未修改相關判定器。 |
| V051-e1353c4-1 | Medium | 已修 `557b625d` | 0 | 受版控 fixture、摘要及集合斷言仍在。 |
| V051-557b625-1 | Medium | 已修 `ac793dfc` | 0 | fixture 與 LF Git 屬性未受本輪變更影響。 |
| V051-0535b8b-1 | High | 已修 `7fc2c40e` | 0 | system 入口選用 `materialized`，並有完整物化 payload 的 phase PASS 測試。 |
| V051-0535b8b-2 | High | 未修 | 1 | 新增的檢查沒有確認 Rust CLI 內嵌資產；且文字掃描可把 YAML 字串內容誤認成 live 欄位。見 B-1、B-2。 |
| V051-0535b8b-3 | Medium | 未修 | 1 | 已容許全域 revision 前進，但未驗證 BC-08 的核准 revision 是最後變更 revision；見 B-3。 |
| V051-0535b8b-4 | High | 已修 `7fc2c40e` | 0 | 已補缺 check、錯政策、損壞 payload 及各入口實際 verdict 斷言，仍呼叫真實判定器。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| ITEM 4：BC-08、doctor 契約、coverage、P4 映射及 Linux 鎖 | 部分完成 | 登錄與映射見 `1c6823bb`、`39bef5ae`、`0535b8b7`；本輪 revision 測試的缺口見 B-3。 |
| ITEM 5：A/B 唯一差分、Rust-only 黑箱及公開說明 | 完成 `1c6823bb`、`5d9213ae` | 保留原 nodeid、19 項共同基線及 bundle 配對；實作者紀錄 bundle 黑箱 `5 passed`。 |
| ITEM 6：各入口政策傳遞、遷移後判定與 driver 反例 | 部分完成 | `7fc2c40e` 補齊 system 政策及 phase 測試，但遷移 gate 仍有 B-1、B-2 的假綠情境。 |
| ITEM 6：紅綠燈、定向驗收及交付紀錄 | 完成 `1c6823bb`、`7fc2c40e`、`cb6bea15` | 實作者記錄紅綠燈與驗收結果；p290 整合、正式 OD-33 及 Windows 原生驗收列於 SPEC-GAPS，由編排者安排。 |

本次以 `--noconftest`、停用 pytest 快取執行相關定向測試，結果為 **13 passed、252 deselected**；另以記憶體替身驗證下列反例。未執行完整 pytest、Docker 或 bundle 測試。

## B 新發現

### V051-cb6bea1-1 [High] [upgrade_checks.py:468](scripts/docker/upgrade_checks.py:468) — 文字掃描可把 YAML 字串誤判為遷移欄位

`_profile_template_has_field()` 以行首文字判斷欄位，沒有判斷該行是否位於 YAML 多行字串內。記憶體反例中，live `prpl-template` 的多行字串包含一行 `timeout_recovery_keys: [CTRL_C]`，但解析後 template 實際沒有該 key。配合缺該欄位的舊版內嵌資產，實際 driver 回 **PASS**，違反遷移後缺欄必須 FAIL 的 gate。

同一掃描也會拒絕合法的 quoted key：`"timeout_recovery_keys": [CTRL_C]` 經 YAML 解析確實存在，但 migration 判 **FAIL**。

**修法：**用能正確識別 YAML 結構的方式檢查共同 template 的實際頂層 key；保留容器執行環境的依賴約束。補多行字串假欄位必須 FAIL、quoted key 必須依實際結構判定的 driver 反例。

### V051-cb6bea1-2 [High] [run_upgrade_container.py:398](scripts/docker/run_upgrade_container.py:398) — 檢查 bundle 內 Python 資產副本，未確認 doctor 使用的內嵌資產

Rust doctor 使用 [編譯時內嵌資產](crates/sw-cli/src/assets.rs:11)，新增 gate 卻只檢查 bundle 的 `py/sw_core/assets/profiles/default.yaml`。若 CLI 仍內嵌缺 `timeout_recovery_keys` 的舊資產，而 bundle 的 Python 資產副本已有該鍵，doctor 對舊 live profile 仍只報預期的 `boot_done_regex`；記憶體反例中，實際 driver 判定為 **PASS**。

**修法：**建立候選 CLI 實際內嵌資產與受驗證 p290 資產的一致性證據，或直接驗證候選 CLI 所使用的資產；不一致或無法確認時 FAIL。補「CLI 內嵌舊資產、bundle 副本為新資產」的反例。

### V051-cb6bea1-3 [High] [test_contracts_catalog.py:1382](tests/test_contracts_catalog.py:1382) — revision 修正放寬了原有核准值斷言

新斷言只要求 `0 < approved_at_revision <= revision`。BC-08 最後一次變更的 revision 是 12，但以記憶體突變將 `approved_at_revision` 改成 `1` 後，實際執行 `test_bc08_doctor_profile_template_fields_registered` 仍通過。這使錯誤核准來源 revision 無法由原測試攔截。

**修法：**允許全域 revision 繼續前進，同時把 BC-08 內容與其最後變更時的核准 revision 綁定；在 BC-08 本身變更時同步更新證據。補「全域 revision 前進而 BC-08 不變」應通過，以及「BC-08 核准 revision 被改成較早值」必須失敗的反例。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正