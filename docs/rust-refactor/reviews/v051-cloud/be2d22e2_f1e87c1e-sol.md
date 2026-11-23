> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-b6f1dd6-1 | High | 已修 680124d0 | 0 | 仍在共同生命週期鎖內重讀模式、檢查 hold，並持鎖至 `/ENABLE`／`/Run` 完成。 |
| V051-b245cbb-1 | Medium | 未修 | 2 | 7141aaf1 已將 BC-07 改為 OD-111，並加入逐編號檢查；但裁決要求先合入基礎 PR、再由本包合入 main。本地 `origin/main` 快照與本分支均不含基礎 commit `2c8e6644`，整合前置條件尚未滿足。 |
| V051-be2d22e-1 | Medium | 未修 | 1 | 新的等待邏輯要求 `health.ping` 含正 PID，但正式 daemon 的回應只有 `ok`、`pong`，因此 daemon 即使已就緒，仍會等到 `DAEMON_NOT_READY`，不重送原 RPC。見 [daemon_cmd.rs](crates/sw-cli/src/commands/daemon_cmd.rs:837) 與 [service.rs](crates/sw-daemon/src/service.rs:873)。修法是以實際 `health.ping` 契約判定就緒，若要顯示 PID 則另查 `health.status`；回歸 fixture 須使用正式回應形狀。 |
| V051-be2d22e-2 | Medium | 已修 f1e87c1e | 0 | 不可序列化模式交回 daemon 入口處理；兩入口的定向交錯測試通過。另有本輪引入的重新讀取競態，列於 B。 |

## B 新發現

**V051-f1e87c1-1 [High]** [test_contracts_catalog.py](tests/test_contracts_catalog.py:1355) — 7141aaf1 移除了既有的 BC-07 核准日期斷言。若 BC-07 的日期由 `2026-11-23` 錯改為格式合法的 `2020-01-01`，原測試會失敗，現在的測試與格式檢查都會通過。這削弱了既有契約測試。**修法：**保留 BC-07 的原日期斷言，同時維持新加入的 OD-111、scope 與 related-decisions 斷言。

**V051-f1e87c1-2 [Medium]** [daemon_cmd.rs](crates/sw-cli/src/commands/daemon_cmd.rs:81) — 鎖內讀到 `on-demand` 後，重新分派會再次讀取設定，沒有使用已讀到的模式。例如第一次重讀為 `on-demand`，釋鎖後、第二次讀取前又切成 `windows-task`，實際會再次取得生命週期鎖並可能執行 `/ENABLE`、`/Run`；依裁決應依第一次重讀的 `on-demand` 重新分派一次。反覆切換也可能造成重複分派。**修法：**將鎖內讀到的模式或解析結果傳給原入口的分派邏輯，不再重新讀取；補受控交錯測試，驗證第二次設定變更不改變本次分派。

定向驗證：`mgmt_daemon` 的 p277 節點 **7 passed**、`mgmt_service` 的 p277 節點 **11 passed**；`tests/test_contracts_catalog.py` **126 passed**、2 個未知 marker 警告。`git diff --check` 通過，工作樹乾淨。上述失敗情境依讀碼判定；未執行完整 pytest、Docker／bundle 或 Windows 原生測試。

## 既有碼待辦（不計入結論）

README 的 Windows task 崩潰自動重啟敘述與 OD-83 不一致；停用工作執行 `/Run` 的實際結果仍待 Windows 實機驗證。

## 對裁決的異議（不計入結論）

無。

結論：需修正