---
type: fix
scope: setup
---

修正共用主機上另有他人（或本人以 systemd-user、不帶 `--socket`）執行的 `serialwrapd` 時，`serialwrap setup` 模式切換與 `serialwrap daemon restart` 在舊 pid 未知時 10 秒後回 `TIMEOUT` 的回歸：舊 daemon 存活掃描改為略過 uid 不同的行程，同 uid 但不帶 `--socket` 的 daemon 以預設 endpoint 比對，讀取失敗只在同 uid 行程上視為掃描不完整（S1X-a-5）。
