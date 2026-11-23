> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-b6f1dd6-1 | High | 已修 680124d0 | 0 | 本輪未修改 start 的生命週期鎖與 hold 檢查路徑；兩個入口仍在共同鎖內檢查 hold，持鎖至 `/ENABLE`／`/Run` 完成。 |

## B 新發現

**V051-b245cbb-1 [Medium]** [behavior_changes.yaml:154](contracts/behavior_changes.yaml:154) — BC-07 的正式核准來源誤標為 `OD-04`。該裁決於 2026-10-15 明確只核准 BC-01～BC-05；BC-07 的實際依據是 OD-111 與編排者 p277 裁決。本輪把原本的 `OD-111` 改成 `OD-04`，而 [test_contracts_catalog.py:1335](tests/test_contracts_catalog.py:1335) 及既有檢查器又強制所有 BC 使用 `OD-04`，造成錯誤歸屬仍通過驗證。查核者依 `approval.decision_ref` 追溯 BC-07 時，找不到該行為的核准依據。

修法：將 BC-07 的 `decision_ref` 恢復為 `OD-111`，並讓測試分別精確斷言 BC-01～BC-05 的 `OD-04` 與 BC-07 的 `OD-111`。既有檢查器也禁止後者；由於裁決禁止為通過驗證而修改驗證器，須先請編排者裁定如何以明確的逐項核准對應更新該規則，保留對未授權來源的拒絕。

定向驗證：五個指定契約測試檔 **256 passed、2 warnings**；`python3 -m tools.contracts check` 與本輪 `git diff --check` 通過。未跑完整 pytest、Docker／bundle 或 Windows 原生測試；工作樹保持乾淨。

## 既有碼待辦（不計入結論）

- 第 2 項已排定處理取鎖後重讀監管模式、nested autostart 自鎖及無效斷言。
- README 既有 Windows task 崩潰自動重啟敘述與規格／OD-83 的差異沿用前輪待辦。停用工作執行 `/Run` 的實際結果仍待 Windows 實機驗證。

## 對裁決的異議（不計入結論）

無。

結論：需修正