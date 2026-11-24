> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-5914e1c-1 | Medium | 已修 `ef2cebf3` | 0 | 共同 template 結構檢查未受本輪修改。 |
| V051-5914e1c-2 | Medium | 已修 `ef2cebf3` | 0 | `profiles` 非 mapping 的分類未受本輪修改。 |
| V051-5914e1c-3 | High | 已修 `b4b2c0dd` | 0 | 相關實作與測試未受本輪修改。 |
| V051-ef2cebf-1 | Medium | 已修 `b4b2c0dd` | 0 | Fixture 與內容斷言保留。 |
| V051-3d208a9-1 | High | 已修 `e1353c4b` | 0 | 判定器仍讀取 check 頂層 payload。 |
| V051-3d208a9-2 | High | 已修 `e1353c4b` | 0 | 共同及完整物化 template 的集合斷言保留。 |
| V051-3d208a9-3 | High | 已修 `e1353c4b` | 0 | 缺漏映射仍只列有缺漏的 template。 |
| V051-3d208a9-4 | High | 已修 `e1353c4b` | 0 | 相關判定器未受本輪修改。 |
| V051-e1353c4-1 | Medium | 已修 `557b625d` | 0 | 受版控 fixture、摘要及集合斷言保留。 |
| V051-557b625-1 | Medium | 已修 `ac793dfc` | 0 | Fixture 與 LF Git 屬性未受本輪修改。 |
| V051-0535b8b-1 | High | 已修 `7fc2c40e` | 0 | System 入口政策與完整物化 payload 測試保留。 |
| V051-0535b8b-2 | High | 未修 | 3 | 安裝副本 probe 已修，但本輪 YAML 掃描仍可把巢狀欄位誤判為頂層欄位；見 B-1。 |
| V051-0535b8b-3 | Medium | 已修 `a57dee5c` | 0 | BC-08 內容指紋與核准 revision 綁定；定向測試通過。 |
| V051-0535b8b-4 | High | 已修 `7fc2c40e` | 0 | 缺 check、錯政策、損壞 payload 及各入口 verdict 的測試保留。 |
| V051-cb6bea1-1 | High | 已修 `a57dee5c` | 0 | 原多行 quoted scalar 與 quoted key 反例通過；本輪新引入的巢狀 flow 反例見 B-1。 |
| V051-cb6bea1-2 | High | 已修 `ac5360a9` | 0 | 安裝副本與候選 CLI 摘要一致後，以安裝副本執行內嵌資產 probe；相關反例通過。 |
| V051-cb6bea1-3 | High | 已修 `a57dee5c` | 0 | 固定 BC-08 內容指紋及最後核准 revision；定向測試通過。 |
| V051-a57dee5-1 | High | 已修 `ac5360a9` | 0 | 不同路徑的安裝副本可通過摘要與 probe；路徑不一致時的新錯誤分支見 B-2。 |
| V051-a57dee5-2 | Medium | 已修 `ac5360a9` | 0 | 原 plain scalar `[` 與 escaped quoted key 反例的 driver 測試通過。 |
| V051-a57dee5-3 | High | 未修 | 1 | 未見逐項紅燈輸出、修正後驗收對照與附理由的 SPEC-GAPS；見 C 及 B-3。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 逐項處理列出的 finding，先提供紅燈再修正 | 部分完成 | `a57dee5c`、`ac5360a9` 有實作與測試，但 B-1 的結構判定仍有新反例；所提供資料及目前分支未見逐項先紅輸出或附理由的 SPEC-GAPS。 |
| 不放寬既有斷言、不刪除或改名既有測試 | 完成 `ac5360a9` | 本輪 diff 未刪除或改名測試，也未發現既有斷言被放寬。 |
| 先列 finding 對照，再列可追溯 commit 的驗收結果 | 未做 | 所提供資料未包含實作者對照表與驗收回報；分支中亦沒有 `docs/rust-refactor/reviews/v051-fix-responses.md`。見 B-3。 |

定向驗證在 `ac5360a9` 執行八個直接相關 pytest 節點：**8 passed**。另用記憶體替身重現 B-1、B-2；未跑完整 pytest、Docker 或 bundle 測試，未修改工作樹檔案。

## B 新發現

### V051-ac5360a-1 [High] [upgrade_checks.py:610](scripts/docker/upgrade_checks.py:610) — 巢狀 flow sequence 中的欄位被判成頂層欄位

以下合法 YAML 的 `timeout_recovery_keys` 是 `other` 序列中的 mapping key，並非 `prpl-template` 的頂層 key：

```yaml
profiles:
  prpl-template:
    platform: prpl
    other:
      - [one,
    timeout_recovery_keys: [CTRL_C]
      ]
```

以此作為候選 Python 資產、另以有真正頂層欄位的 profile 作為 live 檔，記憶體實測：`a57dee5c` 的 migration 判定為 **FAIL**，`ac5360a9` 則為 **PASS**，且 `target_asset_has_field=True`。因此獨立的 p290 候選資產 gate 會漏掉實際缺欄；若 CLI 內嵌資產與 live profile 都有該欄位，doctor probe 也不會補捉這個候選 Python 資產缺漏。

**修法：**依 YAML 結構辨識頂層 mapping key，正確追蹤巢狀 flow collection；維持容器依賴約束。補上述候選資產缺欄、live 與 CLI 內嵌資產有欄的正式 driver 反例，要求整體 FAIL。

### V051-ac5360a-2 [Medium] [run_upgrade_container.py:411](scripts/docker/run_upgrade_container.py:411) — 候選入口路徑不一致時拋出例外，沒有產生預期 FAIL

當 `bundle/bin/serialwrap` 解析到的檔案與 `bundle/versions/<version>/bin/serialwrap` 不同時，`evidence` 已包含 `configured_cli_path`，第 415 行又以同名參數傳入 `uc.row()`。記憶體替身實測得到 `TypeError: ... got multiple values for keyword argument 'configured_cli_path'`；driver 外層會記為 `INSUFFICIENT`，而不是該候選資產檢查的 FAIL。

**修法：**只傳一次 `configured_cli_path`，補兩個候選路徑不一致時回傳結構化 FAIL 的 driver 測試。

### V051-ac5360a-3 [High] [test_docker_upgrade.py:1383](tests/test_docker_upgrade.py:1383) — 本項要求的逐項紅燈與交付回報仍無法追溯

本輪增加了反例與修正，但可見區間只有同時包含兩者的 `ac5360a9`；所提供資料及目前分支沒有逐項紅燈命令、輸出與所測 commit，也沒有 finding 對照表、修正後驗收結果或附理由的 SPEC-GAPS。因此無法確認 brief 要求的先紅後綠與逐項回報已完成。

**修法：**提供每項 finding 的處置、紅燈命令與輸出、所測 commit、修正後驗收結果及 SHA；無法取得的證據寫入實作者 SPEC-GAPS 並附理由。

## 既有碼待辦（不計入結論）

掃描器對其他合法 YAML 表示法仍有限制：例如 flow mapping 或非 2／4 空格縮排的合法 template 會被判缺欄。這些情況在本輪前已存在。

## 對裁決的異議（不計入結論）

無。

結論：需修正