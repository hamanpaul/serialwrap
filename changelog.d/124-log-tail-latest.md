---
type: fix
issue: 124
scope: log
---
`log tail-raw` / `log tail-text` 預設改為 **latest 模式**：省略 `--from-seq` 時回傳符合條件的**最新 N 筆**（seq 升冪），修正原本預設 `from-seq=0` 從最舊 seq 起算、`--limit 10` 在 WAL 100 筆時回 seq 1-10 的反直覺行為。回應新增五個 metadata 欄位：`from_seq`（實際使用值，latest 模式為 `null`）、`last_seq`（回傳紀錄最大 seq，無紀錄為 `null`，可作下次增量起點）、`current_seq`（WAL 目前 seq 計數）、`returned`（回傳筆數）、`truncated`（是否還有符合但被 limit 截掉的紀錄；latest 模式指視窗前還有更舊紀錄、range 模式指視窗後還有更新紀錄）。相容性：顯式帶 `--from-seq N`（含 0）仍走舊的 range 增量語意（自 `seq > N` 起最舊 N 筆），老 client 不受影響；legacy `result.tail`（無 cmd_id 的 WAL fallback）與 `wal export` 行為完全不變。
