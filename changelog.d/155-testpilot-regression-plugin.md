---
type: feat
scope: regression
---
新增 `serialwrap_regression` TestPilot 回歸 plugin（#155）：把已 CLOSED 且有實際修正、只有實機才驗得到的 bug 固化成 10 個 Scenario Family（F1 命令契約／F2 背壓／F3 失敗可觀測性／F4 狀態語義／F5 console 共存／F6 RPC 不凍結／F7 檔案傳輸／F8 daemon 單一性／F9 開機-U-Boot／F10 登入帳密）約 30 個實機回歸 case。含 #154 防線（testbed pin `serialwrap_exe`＋preflight client↔daemon 版本 gate）、`allow_destructive` gate、U-Boot 唯讀護欄（`UBootConsole` 白名單）、`ThrowawayDaemon` 隔離；realhw `SwCli` 支援注入執行檔路徑（預設行為不變）。文件：README「TestPilot 回歸測試」雙語章節、`docs/regression-plugin.md`（family↔issue 對照＋新增 case SOP）、CLAUDE.md 條件式回歸 case 政策。
