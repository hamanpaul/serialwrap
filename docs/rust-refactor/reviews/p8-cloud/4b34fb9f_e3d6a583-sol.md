## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-ea9e03d-1 | High | 已修 `2a9a9bc4` | 0 | actor 發布綁定世代、manager 接線與兩項回歸測試均保留，本輪未破壞修正。 |
| P8-b3ffd2a-1 | High | 依裁決不處理 | 5 | [handoff_stop.rs:1352](crates/sw-daemon/tests/handoff_stop.rs:1352) 仍直接送 RPC。依本次範圍裁定，正式 CLI 交接驗證由後續子項恢復；尚未結案，本項不擋放行。 |

## B 新發現

NO FINDINGS。

已逐筆審查 `4b34fb9f..e3d6a583` 的唯一提交 `e3d6a583`。未改動產品碼、既有測試或斷言；L1 鎖僅於三個 Linux 集合各新增同一測試節點，既有內容與順序均保留。

定向驗證使用 `CARGO_TARGET_DIR=/tmp/solrev-p8-rust-refactor-p8-target`：

- `sw-daemon --features test-fakes --test handoff_stop --no-run`：編譯通過。
- `sw-cli --features test-fakes --test update_guard`：**19 passed，0 failed，0 ignored**。
- `sw-cli --test update_guard`：**15 passed，0 failed，0 ignored**。

新增 CLI 子行程案例會寫入工作樹證據目錄並查詢正式 systemd service，因此依本次限制未執行；上述結果不代表該案例已實跑通過。未跑完整 pytest、Docker／bundle 或 Windows 驗收。未編輯檔案，工作樹維持乾淨，未接觸 production daemon、正式設定／狀態或實體 UART。

## 既有碼待辦（不計入結論）

沿用前輪：bind RPC 逾時後若未重試，仍有持久化缺口。[session_manager.rs:6609](crates/sw-core/src/session_manager.rs:6609) 的 `?` 會提前返回，`Unreleased`／`Binding` 提交仍位於其後。本輪未修改，未另行實測。

## 對裁決的異議（不計入結論）

無。

結論：可放行