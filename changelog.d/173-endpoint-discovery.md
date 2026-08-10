---
type: fix
issue: 173
---

修正 daemon endpoint 無法被非 wrapper client 發現的問題（#173）：POSIX 上
`serialwrapd` 過去只有 Windows 後端才會把實際 bind 的 socket 寫回
`config.yaml`（理由是「POSIX on-demand 模式下 CLI 一定算得出同一個
`SOCKET_PATH` 預設值」），但部署 wrapper 若以 `SERIALWRAP_STATE_DIR` 把 socket
搬離 XDG 預設路徑，任何不經過該 wrapper 的 client（例如其他工具內嵌的
venv、CI runner）就永遠連不到健康運作中的 daemon，只會拿到
`SOCKET_ERROR`，且沒有任何診斷指出真正原因；更危險的是，這類「探測失敗」
的 client 若接著自己觸發 on-demand `daemon start`，會在 XDG 預設路徑另外
spawn 出第二個 daemon，兩個 daemon 同時開同一批裝置（two-reader）。

三處修法：

1. **daemon.py**：拿掉 `_write_config_endpoint()` 呼叫外層「僅 Windows」的
   gate，改為 server 啟動成功後全平台無條件寫入 `config.yaml` 的
   `socket_path`（`_write_config_endpoint` 本身不變，仍是 best-effort、失敗
   只記 stderr、不影響 daemon 運行）。讓「daemon 實際綁在哪」有一個與呼叫端
   環境變數無關的單一事實來源。
2. **doctor**：新增 `endpoint_reachable` 檢查（非 advisory，會拉低整體
   `ok`）。以與 CLI 相同的解析 seam 得出本 client 會連上的 endpoint 與其來源
   （`config.yaml` 或平台預設），並掃 `/proc` 找出實際執行中的 `serialwrapd`
   行程與其 `--socket` 引數：沒有任何 daemon 行程時視為 on-demand 模式正常
   （advisory ok）；有行程但本 client 解析到的 endpoint 連不上時明確判定
   `not ok`，`detail` 同時列出本 client 解析到的路徑（含來源）與實際執行中
   daemon 綁定的路徑，方便一分鐘內定位落差，而不是像本次事故那樣耗掉一整個
   下午。
3. **`serialwrap daemon start`（on-demand spawn 防線）**：spawn 新 daemon
   前先掃 `/proc`，若已有 `serialwrapd` 行程且其 `--socket` 與本次 spawn
   目標不同，預設拒絕（`error_code=DAEMON_ALREADY_RUNNING_ELSEWHERE`，訊息
   帶出兩個路徑與修復提示），避免同一批裝置被兩個 daemon 同時開啟；新增
   `--force-spawn` 供已確認情境下跳過此防線。既有「同一個 socket」的冪等
   探測路徑不受影響。

`sw_core/multi_open.py` 的 `detect_multi_open()` 同步擴充：每個偵測到的
daemon 項目現在附帶從 cmdline 擷取到的 `socket`（供上述 doctor 檢查與 spawn
防線共用；擷取不到時為 `None`，呼叫端須視為「無法判定、保守處理」）。

README.md 與 `docs/serialwrap-spec.md` 補充「自訂 wrapper 若以
`SERIALWRAP_STATE_DIR` 搬移 socket，必須讓 config.yaml 同步（本修正後
daemon 啟動即自動寫入，不需再手動處理）」的說明。
