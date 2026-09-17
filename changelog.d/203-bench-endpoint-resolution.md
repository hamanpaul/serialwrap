---
type: feat
issue: 203
scope: remote
---
新增全域 `--bench <code>` 參數：以 `connect` 記住的 bench 代號解析 endpoint，讓後續命令免帶 `--endpoint`。

- 端點解析優先序為 `--endpoint` > `--socket` > `--bench` > config fallback；`--bench` 指向尚未 connect 的代號時回明確錯誤，不靜默 fallback 到本機 daemon。
- 同步修補 `daemon start`／`daemon stop`（含 systemd 模式 route）與 `remote` 路徑對 bench endpoint 的定址與錯誤處理。
- README 內嵌的 `serialwrap --help` 區塊同步更新（R-16）。
