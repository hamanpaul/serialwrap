# setup 與既有 TCP endpoint

## 本次修復的範圍

在 POSIX RPC backend 下，若 `config.yaml` 的目前模式與 setup 目標模式都是
`on-demand`，且沒有顯式傳入 `--socket` 或非空的 `--endpoint`，setup 會保留既有、
可解析的 `tcp://host:port` `socket_path`。loopback（例如
`tcp://127.0.0.1:48701` 或 `tcp://[::1]:48701`）同樣保留，因為它可能是
SSH tunnel 的入口。保留與當下能否連線無關：tunnel 暫時斷線不應清除設定。
TCP 格式本身不能證明 daemon 位於遠端，此處是保存既有選擇的政策。

已使用 on-demand TCP 設定的客戶端，更新資產時請明確使用：

```bash
serialwrap setup --on-demand
# 從本機 checkout 重新安裝；install.sh 會把參數轉交給 setup：
./install.sh --on-demand
```

同模式刷新不停止、啟動或重啟 daemon；連續執行後，一般未帶 endpoint
參數的 RPC 仍使用原本的設定。`--force` 保留既有覆寫 profiles／略過 flash
護欄的用途，不是清除 TCP endpoint 的旗標。

## 不在本次修復內的操作

- **一般 `./install.sh`／`serialwrap setup` 的 auto 模式**：偵測到 systemd
  時會選 `systemd-user`。它可能構成模式轉換，不適用上述保留規則。
- **模式轉換**：延續原本的本機監管流程與 canonical socket。既有
  on-demand TCP 設定若可達，轉換流程中的 `serialwrap daemon stop` 可能
  經該設定送到 TCP daemon；這是原有行為，本次沒有修復。遠端客戶端不要
  用不帶模式旗標的安裝流程來假設「只刷新資產」。
- **systemd-user／systemd-system 刷新**：延續本機 unit 的 socket 契約，
  不把 TCP client 設定與本機 daemon 的 endpoint 回寫混用。
- **win backend**：不套用 POSIX 保存例外；測試只覆蓋此排除分支，並非
  Windows 原生端到端驗證。
- **Unix、缺值或無法解析的 endpoint**：仍寫本機預設；本次不是保存所有
  自訂 socket 的通用改動。

顯式 `--socket`／`--endpoint` 仍供 setup 的 daemon／flash 探測使用，並關閉
TCP 保存例外（空字串 `--endpoint ""` 沿用 resolver 的缺值語意，不關閉
保存）；setup 沿用原本「寫回該監管模式 canonical socket」的行為，
**不會把旗標值持久化到 config**。若要換成另一個 TCP endpoint，請直接
修改 `config.yaml`；若要回到本機預設，可先移除其中的 `socket_path`，再
執行 `serialwrap setup --on-demand`。

## 回歸驗證

`tests/test_setup_cli.py` 使用真實 config 讀寫與 reconciler，僅將 RPC
傳輸、endpoint 可達性探測與系統 effects 換成受控替身，不連實際 TCP
endpoint，也不執行真實 systemctl／daemon stop。

涵蓋 loopback／非 loopback／主機名／IPv6、可達與斷線、重跑冪等、後續
一般 RPC 的路由、其他 config 欄位保存、明確旗標、非 TCP／錯誤格式、
模式與 backend 排除、flash 護欄及 force 語意。此問題是設定與接線邏輯，
pytest 即可驗證，不需要新增依賴真板的 regression case。
