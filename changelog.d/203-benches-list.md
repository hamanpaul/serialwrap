---
type: feat
issue: 203
scope: remote
---
新增 `serialwrap benches`：列出 `benches.yaml` 的各 bench 代號、`connect` 記住的 endpoint 與 tunnel alive 狀態，讓操作者不必自己去翻 `benches.state.json`。

- `bench_registry` 新增 `load_configured_benches()`，供 CLI 直接取全部代號（`resolve()` 改為呼叫它，行為不變）。
- endpoint 記憶改為嚴格驗證：非字串／空值／非 `tcp://`／非 loopback host 一律視為記憶損壞並回明確錯誤，不再靜默當成「未 connect」。
- alive 判定改為比對記住的 endpoint 實際 port，而非 `benches.yaml` 宣告的 `local_port`。
- `connect --code --close` 同步改用記住的 port 拆隧道；tunnel 仍存在時不清除 endpoint 記憶。
- README 內嵌的 `serialwrap --help` 區塊同步更新（R-16）。
