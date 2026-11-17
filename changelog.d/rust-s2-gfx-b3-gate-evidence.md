---
type: fix
scope: rust-s2-gfx-b3
---

Slice 2.1 與 Slice 2.2 在缺少 bridge 自測報告時會先核對共用證據，否則自動產生 POSIX PTY 自測報告並記錄路徑；Slice 2.1 在 WSL2 缺少 Python WAL 基線時，會於同一檔案系統自動量測並交由既有 judge 配對。Slice 2.2 拒收共用證據時明確輸出 `EVIDENCE_MISSING`，不變更原有資格判定。
