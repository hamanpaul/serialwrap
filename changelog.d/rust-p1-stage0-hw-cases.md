---
type: change
scope: regression
---

新增 Stage 0 行為變更的實機回歸 case（F10 `f10-release-drain-receipt`、`f10-release-aborts-inflight-command`，F5 `f5-deferred-dropped-after-reboot`；擁有者開立 issue 前以 `pending_issue` 標示待補），並新增 Windows WAL／EDR Spike 3 腳本、release 持久化前後 kill、Windows remote 三拓樸與 Spike 1 虛擬 COM 資格的實機程序 runbook；隔離實例下 `SERIALWRAP_TEST_KILL_AFTER_SAVE` 測試鉤子僅於 release 持久化落盤後生效（建構子不觸發），`SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE` 提供 commit 前的確定性暫停點；非隔離實例只寫警告。
