---
type: fix
scope: install-upgrade
---

Windows 本機管理動作統一解析 endpoint；setup 在內建 TCP 埠不可用時自動選用並保存可用埠，且以 singleton lock 區分未回應與未執行的 daemon。daemon 執行期 bind 失敗會提供 Windows 保留埠診斷。更新、回滾與監管模式切換保留設定的 `socket_path`。
