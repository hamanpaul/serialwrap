---
type: fix
scope: release
---

修正 Docker 升級驗證 driver（`scripts/docker/run_upgrade_container.py`）在 on-demand 模式的預熱競態：0.3.2 的 `daemon stop` 先回應、行程稍後才結束，driver 立即 `daemon start` 會得到 `DAEMON_ALREADY_RUNNING_ELSEWHERE`，之後沒有 daemon，使 `seed_user_data` FAIL、`legacy_handoff_direct_setup` INSUFFICIENT。現在每個 `daemon stop` 之後（基線預熱、停舊後 setup、install.sh 前、手動降級、重新升級），先有界等待舊 daemon 行程樹結束、socket 移除，才執行下一步；逾時該步驟判 FAIL，等待結果記錄於 `detail.stop_wait`。判定與必要檢查不變。
