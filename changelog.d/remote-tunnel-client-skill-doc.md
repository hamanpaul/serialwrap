---
type: feat
scope: skill
---
`SKILL.md` 的 Remote Support 章節補上 `--remote-socket` 硬化模式下的 agent 端連線格式（`unix:///path/to.sock`）、`remote_hint` 欄位在該模式下不會更新為 unix endpoint 的已知落差，以及給「隧道對端」agent 的操作提醒（`--source` 標記、共用硬體先 `session self-test`、隧道非常駐服務需回報而非自行繞過）。
