---
type: change
scope: test-infra
---

S1X G5（sc20-daemon-restart）：`tests/contract_blackbox/test_cli_mgmt_daemon.py::test_daemon_restart_ab` 補上 P0 T14 規定的唯一平台 marker `posix_only` 與資源 marker `process_rpc`（依賴 `/proc` 與 `sh`，Windows 由 cargo `mgmt_daemon::restart_*` 承接 sc20）。該節點因此被 Linux `s1b_20groups` selector（`-m "core_portable or posix_only"`）收集，原先為「兩平台凍結 selector 皆不收集」而一律 FAIL 的狀態解除。

L1 凍結鎖 `tests/acceptance/l1_required.lock.json` 以 `regen_lock.py --l1-add-only` 於 `s1b_20groups`／linux 新增該 nodeid（新增 1 筆、移除 0 筆）；收集輸入限定為既有鎖定節點加該節點，逐字取自實際 `--collect-only` 輸出。windows 集合未變動。未放寬任何 judge、check 或斷言。

同步調整 `tests/test_check_p4_cases.py::test_pass_for_node_outside_every_selector_is_rejected`：原以 `test_daemon_restart_ab` 作為「兩平台皆不收集」之例，該前提因本變更不再成立，改用仍無平台 marker 之 `test_daemon_start_ab_conflict`，斷言不變。
