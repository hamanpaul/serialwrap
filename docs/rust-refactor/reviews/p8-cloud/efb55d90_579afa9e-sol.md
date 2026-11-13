## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-ea9e03d-1 | High | 已修 `2a9a9bc4` | 0 | actor 綁定世代發布、manager 接線及兩項 RPC 逾時回歸仍保留，本輪未破壞。 |
| P8-b3ffd2a-1 | High | 已修 `5928c192` | 0 | 正式 CLI 更新、產品交接鏈及繼任者驗證仍保留，相關檔案未變。 |
| P8-0f9ec82-1 | Medium | 已修 `0095a2dd` | 0 | 測試專用提前返回仍已移除，共用交接迴圈未變。 |

## B 新發現

NO FINDINGS。

已逐檔審查 `efb55d90..579afa9e` 唯一提交的四個變更檔案。

Held／AccessDenied 回應已對齊 Python `attach_device`：省略 `session`，鍵序為 `ok → error_code → pids → holder_unknown → selector`。產品碼未改動開埠、force、狀態復原或持久化流程；既有測試移除 `session` 預期與 Python 基線一致。

驗證結果：

- 定向執行 `drain_release`：**191 passed，0 failed，0 ignored**；新增測試涵蓋四種情境、完整序列化 bytes、維持 RELEASED、零新增 TX 與無持久化。
- `regen_lock.py --check --base efb55d90`：通過；L1 鎖僅新增集合與節點。
- `git diff --check efb55d90..579afa9e`：通過。

建置使用指定 `CARGO_TARGET_DIR`；未修改來源檔案，工作樹維持乾淨。未跑完整 pytest、Docker／bundle，未接觸正式 daemon、設定／狀態或實體 UART。本次結果限於 gpc 第 1／4 項。

## 既有碼待辦（不計入結論）

- 沿用前輪 bind RPC 逾時持久化缺口：`crates/sw-core/src/session_manager.rs:6609` 的 `?` 仍可能提前返回，略過後續 `Unreleased`／`Binding` 提交；本輪未改動，未重新實測。
- 沿用 pre-take／post-take 窗口驗收待辦；本輪回應形狀測試不構成該窗口的修復證據。

## 對裁決的異議（不計入結論）

無。

結論：可放行

