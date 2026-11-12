## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-ea9e03d-1 | High | 已修 `2a9a9bc4` | 0 | actor 綁定世代發布、manager 接線與兩項逾時回歸測試均保留，本輪未改動。 |
| P8-b3ffd2a-1 | High | 已修 `5928c192` | 0 | 正式 CLI 更新、產品交接鏈及繼任者驗證均保留，本輪未改動。 |
| P8-0f9ec82-1 | Medium | 已修 `0095a2dd` | 0 | 測試專用提前返回仍已移除；首次交接走共用迴圈，失敗返回錯誤。 |

## B 新發現

NO FINDINGS。

已逐檔核對唯一提交 `2b3cb83e`。新增同步點要求相同 `cmd_id` 與完整命令 payload 出現在 WAL；原有 probe 拒絕、外部零 TX 與 WAL 無新增 TX 斷言均保留。未改動產品程式、契約、凍結鎖或 gate 判準。

定向驗證：

```bash
CARGO_TARGET_DIR=/tmp/solrev-p8-rust-refactor-p8-target \
cargo test --locked -p sw-core --features test-fakes --test flash_session_core
```

結果：**56 passed，0 failed，0 ignored**。未跑完整 pytest、Docker／bundle，未接觸正式 daemon 或裝置；工作樹維持乾淨。

## 既有碼待辦（不計入結論）

沿用前輪：bind RPC 逾時且未重試時仍有持久化缺口。[session_manager.rs:6609](crates/sw-core/src/session_manager.rs:6609) 的 `?` 提前返回會略過後續 `Unreleased`／`Binding` 提交。本輪未修改，未另行實測。

## 對裁決的異議（不計入結論）

無。

結論：可放行