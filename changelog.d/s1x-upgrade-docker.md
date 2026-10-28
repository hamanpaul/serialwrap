---
type: feat
scope: rust-refactor
---

新增 0.3.2 正式版 → 0.4.0 候選的 Linux 升級驗證（OD-33）：`scripts/docker/run-upgrade.sh` 在 OD-16 (5) 的 ubuntu:24.04 systemd 容器（`tools/docker/upgrade.Dockerfile`，不連網、不帶裝置）內，對 systemd-user 與 on-demand 兩種監管模式各自執行 pipx 安裝 0.3.2 → bundle 升級 → `setup --rollback` → 手動降級 → 重新升級，檢查入口取代、config／profiles 保留、alias／binding 相容、監管收斂、doctor 與全程無 two-reader，結果寫 `upgrade-report.json`。並新增 `docs/rust-refactor/release-0.4.0-plan.md`（發佈內容、升級／降級程序、已知升級問題與 release notes 草稿）。

升級驗證判準收緊（雲端審查 e270e89#1～#3）：基線必須實際含重綁後的 `shellboard:COM7` by-id（binding 相容檢查不得空轉）；兩個拒絕型檢查除現場未改動外，拒絕原因也須與規格相符（舊 daemon 執行中為 `METHOD_NOT_FOUND`＋`daemon.handoff_stop`、回滾為 `INVALID_ARGS`＋`rollback_unavailable`）；`setup --rollback` 若成功，CLI 與 daemon 皆須回到 0.3.2 且設定逐位元組保留。
