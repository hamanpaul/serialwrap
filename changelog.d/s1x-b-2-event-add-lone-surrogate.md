---
type: fix
scope: rust-cli
---

修正 Rust CLI `event add --file`：規則 JSON 含孤立 surrogate 跳脫（如 `"\udc80"`）時，原本依 RPC-H-03 改寫成 U+FFFD 後照送並寫入 daemon（exit 0）；現比照 Python 在連線成功後、送出前以 `INTERNAL_ERROR`（context=`event.rule_set`，訊息 `'utf-8' codec can't encode character '\udcXX' in position N: surrogates not allowed`，連續者為區間）exit 1 結束，daemon 收不到請求行（S1X-b-2）。回應端的 RPC-H-03（孤立 surrogate 轉 U+FFFD）不變；`sw_proto::pyjson` 新增 `loads_keep_lone_surrogates` 供請求端檔案內容使用。
