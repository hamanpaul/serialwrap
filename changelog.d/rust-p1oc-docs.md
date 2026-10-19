---
type: change
scope: docs
---
同步 OD-14 對外文件：`session attach`／`session recover` 失敗時 `error_code` 固定為 `SESSION_NOT_READY`，原診斷值改由頂層 `last_error`、`last_error_detail` 回報；`device release` drain 逾 3 秒改回 `DRAIN_STALLED`（RECOVERING，不得交接）；`platform=passthrough` 不 login、不 probe，靜默 30 秒自動 READY 並建議以 `--completion-regex` 指定完成條件；UDS socket 維持 `0660`（可用 `SERIALWRAP_SOCKET_GROUP` 指定群組）。同步 README（英文與繁體中文）、`docs/command-completion.md`、CLAUDE.md、兩份 SKILL 的未受管段落，並新增 passthrough 實機回歸 case `pt-quiet-ready`（待擁有者開立 issue）。regression case 對帳密解析終態改以 `last_error` 判定。
