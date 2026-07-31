---
type: fix
scope: file_transfer
---
修復 `file.push`／`file.pull` 每段等待逾時寫死常數、與 profile 完全脫鉤（#157）：`SessionManager.file_push()` 原固定 `timeout_s=10.0`（`file_pull()` 固定 30.0），即使 bcm 類慢板已把 `profile.timeout_s` 調大也從未生效。改為未顯式指定時沿用 `profile.timeout_s`（push 夾 5.0s、pull 夾 30.0s 地板），並於 CLI（`--chunk-timeout`）／RPC（`chunk_timeout_s`）開放顯式覆寫。同時將 `DEFAULT_CHUNK_SIZE` 由 2048 降為 512（單行 base64 命令 ~2789 字元在真機 UART console 實證會被截斷——issue 附 WAL 證據；512B → 單行 ~741 字元，~3.8x 安全餘裕；此值為依截斷證據反推的保守估計、非真機量測值），四處各自硬寫的 2048 收斂為 `sw_core/file_transfer.py` 單一常數。`file.push` RPC 對 `chunk_size<=0` 夾 `max(1, ...)` 防呆，避免 `ValueError` 穿越 RPC 邊界。已知殘留缺口（#157 範圍外）：`pull_file` 不分段一次讀全部，受 RX 視窗 128KiB 上限（`sw_core/uart_io.py`）限制，base64 輸出超過上限的大檔 pull 仍會 `PULL_PARSE_FAILED`，`f7-larger-file-not-truncated` 因此仍預期 SKIP，待 follow-up。
