---
type: feat
issue: 203
scope: remote
---
`sw_core/bench_registry.py` 新增 `to_remote_argv(entry)`：把 `BenchEntry` 展開成等價 `serialwrap remote -L` 的 argv（`--remote-socket`、逐項 `--ssh-opt`、`--autossh`、`user@host:port`），供 `connect` 重用既有 spawn 路徑而不重造隧道建構邏輯。
