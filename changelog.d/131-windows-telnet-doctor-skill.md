---
type: feat
issue: 131
scope: windows
---
Windows human console listener 直接講 **Telnet**（`sw_core/telnet_console.py`）：accept 即主動協商（IAC WILL ECHO／WILL SGA／DO SGA／WILL BINARY），入向 IAC 狀態機過濾（吞協商/子協商、IAC IAC 還原、NVT CR NUL／CR LF 摺疊為單一 CR，狀態跨 recv 邊界存活）、出向 0xFF 逸出——Tera Term／PuTTY 以 **Telnet** 服務連入即得 ssh 般逐字元互動與遠端回顯；POSIX PTY 路徑逐位元組不變。`serialwrap doctor` 平台感知：Windows 檢查 pyserial／PATH／daemon endpoint／SERIALCOMM COM 列舉（dialout 等 Linux 檢查不再誤殺整體 ok），Linux 檢查清單與輸出逐字不變。新增 `serialwrap skill --platform {auto,linux,windows}` 子命令，輸出內嵌操作指南原文（新資產 `sw_core/assets/skill/SKILL_WINDOWS.md`：安裝→daemon start→用戶端→**profile 設定（路徑／三區段結構／Windows targets 綁定範例／no_profiles_loaded 語意）**→console_endpoint→Tera Term Telnet 設定→Windows/Linux 差異對照→MCU 燒錄→疑難排解）。新增 `serialwrap --version`（repo VERSION → pip/pipx metadata → PyInstaller 內嵌 assets/VERSION 三段 fallback，release exe 也能報版本）；`daemon start --profile-dir` 補 help 字串（顯示預設路徑）。
