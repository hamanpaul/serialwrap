---
type: fix
scope: rust-cli
---

`setup` 在重啟 daemon 後輪詢程式碼指紋時，每一輪都重新解析本機 endpoint，與 Python `_resolve_local_endpoint_info` 對齊。reconcile 把 `config.yaml` 的 socket 寫回 canonical 之後，不再沿用 reconcile 前的舊 endpoint。明示的 `--endpoint` 或 `--socket` 仍優先於設定檔。
