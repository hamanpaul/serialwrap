---
type: feat
issue: 203
scope: remote
---
新增 provider-neutral 的 bench registry 載入層（`sw_core/bench_registry.py`），讓
`benches.yaml` 可定義 `target`、`remote_socket`、`local_port`、`ssh_opts`、
`autossh`，並強制 schema 驗證：拒絕 provider 專屬欄位、要求 `user@host`、
保留 `ssh_opts` 原樣與 `SERIALWRAP_BENCHES_FILE` 覆寫行為。
