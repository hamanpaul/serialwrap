## ADDED Requirements

### Requirement: 可選且相容的 CLI trace
CLI SHALL 使用專用 serialwrap.cli_trace logger（propagate=False、冪等 handler），INFO event 在預設 WARNING 不輸出。-v/-vv 的 INFO/DEBUG 優先於有效 SERIALWRAP_LOG_LEVEL，未知 env 安靜回 WARNING。不得改 root 或既有 serialwrap logger。

#### Scenario: 預設及重複呼叫
- **WHEN** main() 多次執行，先有 -v 再無 -v
- **THEN** 預設 stdout/stderr 契約不變，trace 不重複也不殘留。

### Requirement: 單次來源與白名單
trace SHALL 僅輸出 endpoint_transport、endpoint_id、endpoint_source、method、elapsed_ms、error_code、errno、errno_name、retry_count、timeout_s。Unix path 全部雜湊成固定長度 SHA-256 ID；TCP 僅 literal loopback host+port 可見，其他雜湊；不得加入 params、command、UART、secret、owner 或完整 response。

#### Scenario: fallback 与 event
- **WHEN** config endpoint 失聯而採 canonical fallback，或送 event.rule_set
- **THEN** trace 與當次實際解析的 endpoint/source、真正 RPC method 一致，無第二次 probe。

### Requirement: 不增加操作副作用
trace SHALL 保留既有 retry/TIMEOUT enrich 的次數及順序，elapsed 計入其耗時，errno 僅取最後主請求 OSError，不污染 stdout dict。

#### Scenario: retry 後成功及 timeout enrich
- **WHEN** 主請求先 OSError 後成功，或主請求 timeout 而 enrich 發生 OSError
- **THEN** 成功或 timeout trace 的 errno 為 null；trace on/off 的 RPC 序列完全相同，不重送 mutating RPC。
