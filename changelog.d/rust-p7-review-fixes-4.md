### Fixed

- Rust 重構 P7 審查第 4 輪：重掛 bridge 時延續的 boot quiet 會依原視窗到期解除 fence（self-test／救援鍵不再被永久擋）；自動重探在 probe 期間 banner 才 arm 時依實際結果收斂（ok → READY）；手動再探例外只回覆發起者、併發請求重新 probe；USB busid 解析完整對齊 `realpath(strict=False)`（懸空 symlink）。
