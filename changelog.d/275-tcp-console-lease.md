---
type: fix
scope: console
---

Rust 0.5.1（BC-23）為 Windows TCP console 的 raw owner 在 session lease 層補上 lifetime 鏡像，使 `self-test`、reprobe 與 agent interactive 仲裁能看見 TCP human。EOF 僅撤除對應 client lifetime 的鏡像；A 斷線後 B 接手時，不會清除 B 的 raw owner 或 deferred input。閒置 human 可被 agent soft preempt，close／expiry 後恢復並回放；活躍 human 維持 `SESSION_INTERACTIVE_BUSY`。鏡像只記於內部 LeaseIndex，不產生 handoff 的 `AdmissionFlag::Lease`。已有 human lease 時，預設 line mode 的 bare `--source human` `command.submit` 在執行 gate 終結為 `SESSION_INTERACTIVE_BUSY` 且零 UART TX。`file.push`／`file.pull` 保留 agent 執行政策與既有傳輸 gate。Linux native TCP + fake UART/WAL 覆蓋本次接線；Windows 原生與 Tera Term smoke 待另行驗證。
