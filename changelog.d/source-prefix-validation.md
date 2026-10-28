---
type: fix
scope: source
---

修正 `--source` 打錯或冒用時默默改變恢復行為的問題。命令的 human／agent 分流與內部強制恢復旗標全靠來源字串前綴判斷：`--source human`（少冒號）過去被當成 agent，命令逾時時多送 CTRL-C／CTRL-D 把 console 登出；外部傳 `--source system:recover` 則可讓逾時命令觸發 force clear + reattach。現在外部入口（`command.submit`／`file.push`／`file.pull`／`device.release`／`session.interactive_open` 的 `owner`，CLI 與 RPC 兩端）統一驗證：bare `human` 與 `human:`（沒名字，前綴不分大小寫）正規化為 `human:cli`，成功回應帶 `source` 與 `source_normalized_from`；`system`／`system:*`（不分大小寫）回 `SOURCE_RESERVED` 並在入列與送 UART 之前拒絕；其餘字串（`agent:<名>`、`codex`、`cli`…）維持原行為。`command.submit` 回應與 `command.get` 記錄新增 `source_class`（`human`／`agent`）。內部路徑（human console 行、`recover_session`、self-test、login probe）不經驗證。skill 與 README 新增「`--source` 的語意」說明 human／agent 在逾時時的差異。
