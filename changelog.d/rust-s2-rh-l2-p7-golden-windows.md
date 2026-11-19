---
type: fix
scope: hosted-ci
---

P7 golden 擷取將 profile 與暫存路徑正規化為相同的 `/` 格式，並以 `ntpath` 窮舉模擬 19 組情境；保留 Linux golden，不變更凍結鎖。
