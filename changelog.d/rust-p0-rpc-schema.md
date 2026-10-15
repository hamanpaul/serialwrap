---
type: feat
scope: contracts
---
新增 `contracts/rpc_schema.json`：凍結 NDJSON framing、早退回應位元組、實測的請求行長上限（`contracts/golden/rpc/line_limit.json`）、54 個 RPC method 的參數轉型、selector 別名鏈、成功回應 schema、錯誤碼與判定先後、副作用與重試／阻塞／長逾時旗標，以及 SessionPublic 等共用子結構的欄位順序；CI 安裝 jsonschema 供契約測試使用。
