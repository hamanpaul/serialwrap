---
type: feat
scope: test-infra
---
補齊 `uart_stdio_bridge` bridge.v1 證據契約（P2-T2）：新增 `tests/acceptance/` 的 `bridge_record`／`bridge_control`／`correlation`／`l15_manifest` 四份 JSON Schema（draft 2020-12，`comparison` schema 補 `$id`）、CRC golden vectors 與紀錄／writer 單元測試。紀錄建構改為拒絕未知 observation／META event、空 payload 與 generation／seq <1；`wall_ts` 固定符合 WAL 的 UTC 格式。writer 的 flush／close 有期限、保留失敗碼、在可行時記錄 `evidence_failure` META，並拒絕生命週期外的寫入；Windows `platform_info()` 由 bridge manifest 與 selftest 傳入 endpoint、COM driver 與安裝方式，查詢不足列入 `evidence_insufficient`。
