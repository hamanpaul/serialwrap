---
type: fix
scope: tests
---
新增 Docker 升級驗證情境 `run-upgrade.sh --scenario attached-legacy`：0.3.2 daemon 以 PTY 假裝置 attach（attached_paths 非空），且 uid 1000 有真實的 `systemd --user` 與 `(sd-pam)`。先確認 fd 掃描因這兩個行程回 Permission，再斷言 0.4.x 的 `setup` 成功切換且耗時有界；補上既有 19／19／13 矩陣漏掉的情境。既有情境與必要檢查不變。
