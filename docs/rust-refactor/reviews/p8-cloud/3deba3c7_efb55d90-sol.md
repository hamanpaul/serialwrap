## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-ea9e03d-1 | High | 已修 `2a9a9bc4` | 0 | actor 發布綁定世代、manager 接線及兩項 RPC 逾時回歸仍保留，本輪未改動。 |
| P8-b3ffd2a-1 | High | 已修 `5928c192` | 0 | 正式 CLI 更新、產品交接鏈及繼任者驗證仍保留，本輪未改動。 |
| P8-0f9ec82-1 | Medium | 已修 `0095a2dd` | 0 | 測試專用提前返回仍已移除，交接走共用迴圈，本輪未改動。 |

## B 新發現

NO FINDINGS。

已審查 `3deba3c7..efb55d90` 唯一提交 `efb55d90` 的全部三個變更檔案。

新增測試經正式 `Service::rpc("command.submit")` 接線，分別檢查 FakeSerial 實收 bytes、相同 `cmd_id` 的完整 WAL TX、前景忙碌狀態及狀態查詢零額外副作用；缺少 UART 或 WAL 接線會使測試失敗。未修改產品碼、契約或既有測試斷言。

驗證結果：

- `gpb_command_submit_reaches_uart_and_wal`：**1 passed**。
- L1 lock：`p7_gate:cargo-test-workspace.linux` 新增 **412** 節點，`s21_core.linux` 新增 **10** 節點；既有節點、selector 與其他欄位均未更動。
- 對新增節點所屬的 21 個測試 target 建置並收集：**412 個相異新增節點全部存在，無 ignored 節點**；此項僅收集，未執行整套測試。
- `regen_lock.py --check --base 3deba3c7`、`git diff --check 3deba3c7..efb55d90`：均通過。

建置與測試使用指定 `CARGO_TARGET_DIR`。未修改來源檔案，工作樹維持乾淨；未跑完整 pytest、Docker／bundle，未接觸 production daemon、正式設定／狀態或實體 UART。本輪取得的是 L1 記憶體替身接線證據，完整整體驗收仍由編排者的 host 基線承擔。

## 既有碼待辦（不計入結論）

- 沿用前輪持久化缺口：`crates/sw-core/src/session_manager.rs:6609` 的 `?` 仍可能在 bind RPC 逾時時提前返回，略過後續 `Unreleased`／`Binding` 提交。本輪未改動，未另行實測。
- 沿用前輪 pre-take／post-take 窗口驗收待辦；新增的 command 接線測試不能證明這兩個窗口已修復。

## 對裁決的異議（不計入結論）

無。

結論：可放行