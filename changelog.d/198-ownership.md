### Fixed

- #198：補強同一 session 的 foreground/file transfer admission 與 operation
  epoch cleanup；競爭 writer 會以 `SESSION_BUSY` 拒絕且不產生 UART TX，舊
  bridge callback 不會清除新 epoch 的 busy 狀態。
- 操作期間的新 POSIX／TCP human console 延後取得 raw ownership；raw write 在
  實際寫入前重驗 admission，已接收輸入進 bounded deferred buffer。收尾在
  manager lock 外回放，保留 agent interactive 接管與 FLASHING 優先丟棄政策。
