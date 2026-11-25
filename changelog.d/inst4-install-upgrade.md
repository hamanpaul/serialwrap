---
type: fix
scope: install-upgrade
---

Windows 本機管理動作統一解析 endpoint；setup 在內建 TCP 埠不可用時自動選用並保存可用埠，且以 singleton lock 區分未回應與未執行的 daemon。daemon 執行期 bind 失敗會提供 Windows 保留埠診斷。更新、回滾與監管模式切換保留設定的 `socket_path`。

自動選埠的 config 寫入納入安裝交易，supervisor 查詢、切換或健康檢查失敗時還原原始狀態；若安裝前沒有 `config.yaml`，失敗回復會移除交易新建的檔案。寫回只接受可安全編輯的根 block mapping 或獨立空 `{}`；非空根 flow mapping 會 fail closed 並提示手動設定。首次 bundle 安裝使用原始 YAML 格式保留註解，並對新選埠執行 health check。

中斷復原的 journal 綁定交易原本使用的 config 絕對路徑、存在狀態與最後交易內容及 POSIX owner/group/mode；路徑或檔案狀態不符時保留 journal 並安全停止。回復既有 config 時使用 SHA-256 驗證的快照，保留原始 POSIX owner/group/mode。
