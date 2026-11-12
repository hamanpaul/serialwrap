## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-ea9e03d-1 | High | 已修 `2a9a9bc4` | 0 | actor 發布綁定世代、manager 接線與兩項 RPC 逾時回歸均保留，本輪未改動。 |
| P8-b3ffd2a-1 | High | 已修 `5928c192` | 0 | 正式 CLI 更新、產品交接鏈及繼任者驗證均保留，本輪未改動。 |
| P8-0f9ec82-1 | Medium | 已修 `0095a2dd` | 0 | 測試專用提前返回仍已移除，交接走共用迴圈，本輪未改動。 |

## B 新發現

NO FINDINGS。

已逐檔審查範圍內唯一提交 `3deba3c7`，僅修改 `crates/sw-core/tests/session_manager.rs`。

改指前現在明確等待 `ATTACHED` 與 attach worker 收尾，仍符合計畫「READY 前 real_path 改變」的測試條件。原有新路徑開埠及 sticky 不得寫入的斷言保留，另新增 close 暫停期間零開埠嘗試、舊 handle 關閉一次及新路徑最終 READY 的斷言。移除部分為前輪診斷窗口，未刪除計畫必要測試或放寬斷言；未改產品碼、契約、凍結鎖或 gate。

定向驗證均使用指定 `CARGO_TARGET_DIR`：

- `sticky_persisted_only_when_ready_and_path_unchanged`：連跑 **2/2 通過**。
- `rebind_drains_before_reopen`：**1 passed**。
- `git diff --check 757f80e4..3deba3c7`：通過。

未跑完整 pytest、Docker／bundle，未接觸正式 daemon、設定、狀態或實體 UART；未修改檔案，工作樹維持乾淨。

## 既有碼待辦（不計入結論）

- 沿用前輪持久化缺口：`crates/sw-core/src/session_manager.rs:6609` 的 `?` 仍可能在 bind RPC 逾時時提前返回，略過後續 `Unreleased`／`Binding` 提交。本輪未改動，未另行實測。
- 前輪 pre-take／post-take 窗口的後續進展仍待驗收。本輪驗證的是 attach worker 已收尾後的改指窗口，不能據此宣稱原兩個窗口已修復；依指定分工留待後續接線與整體驗收。

## 對裁決的異議（不計入結論）

無。

結論：可放行