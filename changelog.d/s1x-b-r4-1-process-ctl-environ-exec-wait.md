---
type: fix
scope: sw-pal
---

修正 `process_ctl` 測試 `spawn_daemon_detached_new_session` 讀 `/proc/<pid>/environ` 的時序競態：原本 spawn 回傳後立即讀取，若子行程尚未完成 exec，會讀到 fork 自測試行程的父環境（缺 `P4_DETACHED_ENV=supplied`、帶 `HOME=`）或尚未發布的空 env 區段。改為先等 `/proc/<pid>/exe` 指向目標執行檔、再等 environ 非空（3 秒有界期限，逾時以 exe／environ 長度／stat 診斷判失敗）後才讀；`P4_DETACHED_ENV=supplied` 與不含 `HOME=` 兩項斷言不變。`spawn_daemon` 正式行為未改動。
