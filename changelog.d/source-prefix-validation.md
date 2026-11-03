---
type: fix
scope: source
---

修正 `--source` 打錯或冒用時默默改變恢復行為的問題。外部命令入口的 human／agent 權限依 RPC 原始輸入判定：bare `human` 與 `human:`（沒名字，不分大小寫）正規化為 `human:cli`，依 OD-74 享有 human gate 與逾時升格；明寫 `human:<x>`（包括 `human:cli`）仍依 DF-02 比照 agent，不能由 `source_class` 或正規化結果授權。外部傳 `system:recover` 可能觸發 force clear + reattach，因此來源入口（`command.submit`／`file.push`／`file.pull`／`device.release`／`session.interactive_open` 的 `owner`，CLI 與 RPC 兩端）拒絕 `system`／`system:*` 並在入列與送 UART 前回 `SOURCE_RESERVED`。成功正規化的回應帶 `source` 與 `source_normalized_from`；`command.submit` 回應與 `command.get` 記錄另帶僅供顯示的 `source_class`。內部路徑（human console 行、`recover_session`、self-test、login probe）不經外部驗證。skill、README 與 spec 同步說明 raw-source 權限差異。
