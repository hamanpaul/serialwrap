---
type: fix
scope: rust-s21
---
Rust daemon 修正兩項 Codex sol 增量審查問題：release 持久化改為依 actor 順序同步套用 model delta、工作者只在暫停點後提交當下快照，release 暫停期間完成的 bind 不再被舊 `Released` 列覆寫（重啟不會再成為 RELEASED）；line mode 折行回顯剝除改依原始串流證據（BusyBox 須有 `\r\r\n`、readline 依原始 `" \r"` 標記且標記優先於命令空白），與 Python 主線 842c014 一致，一般 stdout 換行恰好拼出命令時不再被誤刪。
