---
type: fix
scope: test-infra
---

隔離 systemd L1.5 測試容器只增加 `SYS_PTRACE` capability，使非 dumpable 行程的 fd holder 可讀；掃描仍對未讀取成功的 PID fail-closed，並在證據中保留 PID 與讀取錯誤細節。
