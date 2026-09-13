### Fixed

- #198：補強同一 session 的 foreground/file transfer admission 與 operation
  epoch cleanup；競爭 writer 會以 `SESSION_BUSY` 拒絕且不產生 UART TX，舊
  bridge callback 不會清除新 epoch 的 busy 狀態。
