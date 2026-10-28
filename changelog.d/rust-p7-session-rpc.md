### Added

- Rust 重構 P7 Task 17：session RPC（self_test／recover／attach／log_*）的 Slice 2.3 行為回歸鎖（參數轉型順序、`force` 依 Python 真值、passthrough attach 不 probe、log 回應鍵序、session 公開欄位）。

### Fixed

- Rust daemon `session.attach` 在 bridge 存在的 FLASHING 與 reboot recovery（RECOVERING、無 drain）狀態下，回應改為 Python `_attach_state_result` 的 `{"ok":false,"error_code":…,"session":…}`（不再多帶 `selector` 或 drain 欄位）。
