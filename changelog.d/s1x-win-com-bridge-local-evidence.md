---
type: fix
scope: rust-refactor
---

`win-blackbox.ps1` 的 COM 序列 bridge 改把 `--evidence-dir` 與 stdout／stderr 寫在 Windows 本機 scratch，停止後（啟動失敗時亦然）整份回寫 `OutputRoot\windows-com\bridge` 並逐檔比對 SHA256；回寫不完整時 bridge stop 降為 EVIDENCE-INSUFFICIENT。避免 `\\wsl.localhost`（9P）上 `os.fsync` 失敗（CRT 一律回 EBADF）使 bridge 未建立 `control.json` 即結束。
