---
type: fix
scope: rust-p7
---
P7 雲端審查修正（第 2 輪）：login run 的 Python 例外（TX 未完整 `TxIncompleteError`、WAL 不健康 `WalUnavailableError`、`Raised`）依 run 種類收斂——attach `DETACHED`＋`ATTACH_FAILED:<型別>`、手動 attach／recover 再探回 RPC `EXCEPTION`（session 不變）、自動重探 `REPROBE_FAILED:<型別>`、reboot recovery `RECOVERY_ERROR`＋WARNING 並續迴圈；pyre 零寬重複上限 0 的擷取群組永不參與、lookaround 本體的可選重複可載入；console unlock 在 quiet 中起再探／reboot recovery 時 fence 保留 quiet（救援鍵與 self-test 仍擋）；detach 不再清除 boot quiet／READY reconfirm（與 Python 同，DUT reboot 期間重新列舉的 attach 不送 probe），passthrough 重掛才清；SessionCore submit／execute gate 改用 `quiet_gate` 的同一組函式。
