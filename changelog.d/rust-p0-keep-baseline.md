---
type: change
scope: contracts
---
新增 `contracts/daemon_logging.yaml`（daemon logging 去向、等級、格式與全部訊息模板）與 `contracts/golden/last_error/transport_stall_hint.json`（TRANSPORT_STALL detail 位元組級 golden），並以特徵化測試凍結 config 載入的已知 quirks（錯誤分流、login_regex 預設值、templates 只取最後一檔、字串 "false" 的 truthiness），供 Rust 重構對照。
