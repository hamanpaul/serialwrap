---
type: fix
scope: rust-daemon
---
issue 306／307：修正 self-test 在前景操作、有效 queue、login／Recover、agent interactive lease 與進行中 background capture 時仍發出 probe 的安全問題；一般忙碌回 `SESSION_BUSY`、有效 agent lease 回 `SESSION_INTERACTIVE_BUSY`，兩者皆建議 `wait`，且不送 probe TX、不 suspend human、不更新 probe 時間、不 confirm ready。Recover 只要 side-op 仍登錄、連線 epoch 有效且 `SystemRecoverKey` permit 仍可提交就算忙碌；FSM 世代推進本身不撤銷 Recover。human console attach 推進 owner 世代不會取消進行中的 Recover，已撤銷或 Rebound 的舊 waiter 不得遮蔽 bridge 狀態。此修正只改 self-test 忙碌判定，未改動通用命令、Recover 或 human lease 政策。
