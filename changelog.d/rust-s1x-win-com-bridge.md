---
type: fix
scope: rust-refactor
---

修正 Windows COM 橋接自測與黑箱腳本的 Stage 1 出口缺陷：`WinSerialEndpoint` 原以單一狀態鎖包住阻塞的讀取與寫入，寫入阻塞（對端不讀、緩衝區滿）時會連帶阻住讀取端，bridge 無法排空接收端，即 ST-03(a) 所描述的雙向死鎖。現改為狀態鎖只保護 handle／generation／timeout 設定，讀與寫各以獨立串行鎖進行、彼此並行。ST-03(a) 的 Windows 分支回到計畫定義的「一次寫 2 MiB 且不讀，之後再讀」，寫入與讀取皆有界期限（逾時即 FAIL，不無限等待，不回傳部分資料當成功）；先前的並行 helper 已移除，改以端點層級測試（`tools/uart_stdio_bridge/tests/test_selftest_win_round_trip.py`）驗證。Windows 實機結果於 Stage 1 證據中另行記錄，未通過即 FAIL，不得以測試改寫放行。`win-blackbox.ps1` 快取 Process Handle，ExitCode 為 null 一律判定失敗；B 輪 junit 與 `SW_RESOURCE_REPORT` 改寫入 staging `l1\windows\blackbox\` 後再複製回 OutDir。bridge 工具樹的 `APPROVED_REVISION.json` 已還原為 HEAD（先前由實作者自行 re-pin 的內容不得保留）；新工具樹須由非實作者獨立審查後才可 pin。
