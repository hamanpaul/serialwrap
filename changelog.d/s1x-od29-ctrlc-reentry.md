---
type: fix
scope: sw-pal
---

OD-29 (4) `cap_interactive_desktop__exec_or_wait_ctrl_c`：子行程重入補 `--ignored`（首跑空跑根因）、子行程先寫 ready 標記才送事件、委派前重新啟用 Ctrl+C 使 `CTRL_C_EVENT` 可送達新 process group，並由父／子寫診斷檔；期望值不變。runbook 更新新 exe 與重跑指令；新增 OD-28 登出觸發 runbook。
