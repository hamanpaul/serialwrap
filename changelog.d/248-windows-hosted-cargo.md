---
type: fix
scope: tests
---
修正 hosted Windows Cargo 測試的語系與 ACP 假設：以同機 Python 3.12 取得 Windows 系統訊息及 mbcs 解碼基線，保留完整字串與錯誤碼比對。固定 Rust 內嵌 Unicode 名稱表及安裝資產的 LF 檢出，並加入 autocrlf 位元組回歸測試。
