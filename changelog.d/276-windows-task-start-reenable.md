---
type: fix
scope: cli
---

修正 Windows `windows-task` 監管模式下，`daemon stop` 被中途打斷（Ctrl-C、關閉視窗）後排程工作一直停在停用、之後 `daemon start`／`service start` 只回 `UNKNOWN_ERROR`（schtasks「排程工作已被停用」）的問題。stop 停止期間本來就會暫時停用工作、停完才恢復；現在 `service start`（含 `daemon start` 轉送）只有在監管切換 hold（`autostart-hold.json`）與更新／回滾 journal（`install-journal.json`）都不存在時，才先 `schtasks /Change /ENABLE` 再 `/Run`。任一 hold 存在時只執行 `/Run`，維持原 JSON 回應與 exit code，並在 stderr 說明原因及等待 `serialwrap setup` 完成。start 會在安裝交易鎖及 autostart 鎖下序列化，分別最多等待 5 秒與 15 秒；逾時回既有 `TIMEOUT` 且不執行 `/Run`。涉及 `windows-task` 的 `supervision-mode set` 也會取得安裝交易鎖，逾時可能回 `TIMEOUT`（`phase=install_lock`）。持鎖時 hold 檢查與 schtasks 動作依序執行；setup hold 復原亦使用同一安裝交易鎖，即使 hold 已存在而 `.install.lock` 尚未建立。`service restart` 逾時提示要使用者執行的 `serialwrap service start` 也因此能正常運作（#276）。
