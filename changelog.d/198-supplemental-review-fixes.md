---
type: fix
issue: 198
scope: diagnostics
---
#198 補充 review fix：未知 `SERIALWRAP_LOG_LEVEL` 安靜回退 `WARNING`，數字值固定最多
4300 位（不計合法負號），超限不受 Python 整數字串轉換限制設定影響；`setup` 的既有
`health.ping`／`mcu.status` RPC 接入 CLI trace 且保留原 probe 順序、timeout、best-effort
與 `FLASHING_BUSY` 行為；F7 對明確 `CHECKSUM_MISMATCH` 回報
`FAIL/test/binary_roundtrip_mismatch`，不再被工具探測結果降為環境 `SKIP`。
`rpc_call()` 的 trace sink 例外也採 best-effort，不覆蓋既有 RPC 結果或重試／enrich 行為。
