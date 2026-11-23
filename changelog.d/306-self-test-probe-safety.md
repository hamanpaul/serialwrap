---
type: fix
scope: rust-daemon
---
issue 306／307：修正 self-test 在前景操作、有效 queue、login／Recover、agent interactive lease 與進行中 background capture 時仍發出 probe 的安全問題；一般忙碌回 `SESSION_BUSY`、有效 agent lease 回 `SESSION_INTERACTIVE_BUSY`，兩者皆建議 `wait`，且不送 probe TX、不 suspend human、不更新 probe 時間、不 confirm ready。Recover side-op 仍登錄且連線 epoch 有效時，FSM 世代推進與可恢復的 WAL／Fence 暫時限制都不解除 busy；Fence lock 忙碌時保守視為仍可能提交，避免等待 OS submit 阻塞 session actor。human console attach 推進 owner 世代不會取消進行中的 Recover，已撤銷或 Rebound 的舊 waiter 不得遮蔽 bridge 狀態。此修正只改 self-test 忙碌判定，未改動通用命令、Recover 或 human lease 政策。
