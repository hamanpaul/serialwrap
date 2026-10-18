---
type: feat
scope: daemon
---

新增隔離實例專用的測試裝置 allowlist（`SERIALWRAP_TEST_DEVICE_ALLOWLIST` 或 `windows.test_device_allowlist`，只接受明確列出的 COM 名稱）：僅在 `SERIALWRAP_STATE_DIR`、`SERIALWRAP_RUN_DIR`、`SERIALWRAP_CONFIG_DIR` 三者皆明確設定且不等於預設路徑時生效，正式實例設定時拒絕啟動（`TEST_DEVICE_ALLOWLIST_REJECTED`，exit 2）；生效時 `daemon status` 顯示 `test_device_allowlist`。隔離實例下若 `config.yaml` 存在但無法解析且未以 env 提供 allowlist，同樣拒絕啟動（`invalid config.yaml`）；正式實例的啟動行為不變。不影響獨占鎖與 serial backend。
