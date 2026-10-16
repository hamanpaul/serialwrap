---
type: feat
scope: test-infra
---
補齊 `uart_stdio_bridge` bridge.v1 證據契約（P2-T2）：新增 `tests/acceptance/` 的 `bridge_record`／`bridge_control`／`correlation`／`l15_manifest` 四份 JSON Schema（draft 2020-12，`comparison` schema 補 `$id`）、CRC golden vectors 與紀錄／writer 單元測試。紀錄建構改為拒絕未知 observation、缺 `meta.event` 與 generation／seq <1；`wall_ts` 一律 `+00:00` 時區（與 WAL 相同）；既有證據檔開檔失敗改以 `HARNESS_EVIDENCE_WRITE_FAILED` 回報；Windows `platform_info()` 補 build／Secure Boot／HVCI／端點防毒／COM driver 唯讀查詢，查詢失敗記 `unknown` 並列入 `evidence_insufficient`。
