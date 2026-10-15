---
type: feat
scope: contracts
---
新增 `contracts/wal_record.schema.json`（WAL 行欄位、型別、crc32 格式、排序序列化、rotate 門檻與 seq 語意）、`contracts/wal_meta_events.schema.json`（META 事件唯一 schema，涵蓋 credentials_unresolved、transport_stall_suspected、command_completion 與 BC-01 核准的重建事件與缺口範圍規則）與 `contracts/state_file.schema.json`（state.json 五個頂層 key 與列結構），並以實際 WalWriter／SessionManager 輸出驗證。
