### Fixed

- Rust 重構 P7 審查第 3 輪：自動重探中遇 quiet arm 不再永久卡住（結果回填為 probe_refused、記 attempt、手動 attach 照常回覆）；reprobe tick 在 probe 被占用時照常排程；pending 中確認失敗累加補 golden；transport stall 計數倒退不再誤判零 RX；USB busid 解析對齊 `realpath(strict=False)`。
