## A 前輪 finding 核對

無。

## B 新發現

**P8X-c2d618a-1 [Medium]** [crates/sw-pal/tests/support/wintask.rs:1105](crates/sw-pal/tests/support/wintask.rs:1105) — 移除收尾的 `/End`，可能留下稍後才啟動的 daemon。

- **失敗情境：** 排程批次檔已啟動，但在建立 `serialwrapd.exe` 前暫停超過 20 秒。實跑測試等待逾時後 panic；`cleanup()` 刪除工作，再掃描 daemon，此時掃不到任何 PID，便結束收尾。批次檔恢復後仍會啟動 daemon，留下隔離實例。[Microsoft 文件](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-delete)明定 `/Delete` 不會中斷正在執行的程式。
- **預期／實際：** 中途失敗後應無殘留 daemon；目前可能在收尾結束後新增殘留 daemon，違反 T13 擁有者裁決。
- **確切修法：** 在刪除工作前，對測試工作執行具名 `/End` 並確認已停止，再刪除工作及清理殘留 daemon；調整新增的原始碼斷言，容許收尾守衛使用 `/End`，並加入「批次檔延遲啟動 daemon」的回歸案例。

驗證限制：定向 Cargo 測試因唯讀沙箱無法建立指定 target 的 `.cargo-build-lock`，未能執行；上述為靜態分析，未做 Windows 實跑。凍結鎖確認僅新增，工作樹乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正