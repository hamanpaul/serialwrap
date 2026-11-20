---
type: fix
scope: rust-s2-rh-k8
---
PR #278 第八輪：rust.yml 結構檢查改驗 systemd 停機黑箱的三個 RX 靜定同步點與位移判定；`windows_pid_alive_never_terminates_target` 的 `cmd /c pause` 子行程改持有 stdin 管線，hosted runner 的 EOF stdin 不再讓它提前結束。
