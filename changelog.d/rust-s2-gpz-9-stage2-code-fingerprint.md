---
type: feat
scope: rust-bundle
---
依 OD-90 支援正式 Linux 與 Windows Stage 2 bundle。Rust daemon 的 `code_fingerprint` 改為其 launcher 檔案 SHA-256 前 12 位小寫十六進位；Stage 1 維持 Python `sw_core` 指紋。setup 的指紋不符警告與受管理服務自動重啟語意維持不變。
