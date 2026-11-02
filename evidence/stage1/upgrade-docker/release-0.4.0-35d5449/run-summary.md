# REL-040-r2 Docker 升級重跑摘要

- 執行日期：2026-11-03
- 候選分支／HEAD：`feature/release-0.4.0`／`35d5449bd4afa7cd82151c04f54b0328c15f746c`
- 執行入口：`scripts/docker/run-upgrade.sh`，情境 `upgrade`
- 舊／新版本：0.3.2 → 0.4.0
- 三模式共用 release bundle manifest SHA256：`113f66ee9858eae114220bdb56afbf612ab4f594a3bd2d2ae1a435d7179263f0`

| 模式 | 範圍 | 結果 | OD-48 legacy handoff | `no_two_reader` |
|---|---|---:|---|---:|
| systemd-user | full | 19/19 PASS | `systemctl_stop`，等待 5342 ms，舊行程已結束 | 1430 samples，0 overlap |
| systemd-system | `legacy_direct_setup_success_path` | 13/13 PASS | `systemctl_stop`，等待 5103 ms，舊行程已結束 | 1595 samples，0 overlap |
| on-demand | full | 19/19 PASS | `rpc_daemon_stop`，等待 5424 ms，舊行程已結束 | 1408 samples，0 overlap |

每個模式各執行一次；driver 均回傳 `verdict=PASS`、`failed=[]`，因此沒有觸發同條件重跑。每次腳本正常結束並執行 EXIT cleanup，移除本次建立的容器與升級映像。

## 候選條件

release bundle 以 `/usr/bin/python3.12`（3.12.3）建立；runtime `pyvenv.cfg` 的 `home = /usr/bin`、`include-system-site-packages = false`。精確字串掃描顯示 `SW_TEST_UPDATE_PAUSE_AFTER`、`SW_TEST_UPDATE_FORCE_VERIFY_FAIL` 均不存在，但 `bin/serialwrap` 含 1 個 `SERIALWRAP_TEST_DELEGATE_GROUP` 字串，`bin/serialwrapd` 含 0 個。故 Docker 升級驗證通過；「不含測試鉤子」候選條件仍未通過（既有 S1X-b-4 Low，本項未改產品碼）。
