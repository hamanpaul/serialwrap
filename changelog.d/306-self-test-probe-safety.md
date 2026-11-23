---
type: fix
scope: rust-daemon
---
issue 306／307：修正 self-test 在前景操作、有效 queue、login／recover、active agent lease 與進行中 background capture 時仍發出 probe 的安全問題；busy 分支現在回 `SESSION_BUSY` + `recommended_action: "wait"`，且不送任何 probe TX、不 suspend human、不更新 probe 時間、不 confirm ready。此修正只保留 self-test 的忙碌早退，未改動通用命令、recover 或 human lease 政策。
