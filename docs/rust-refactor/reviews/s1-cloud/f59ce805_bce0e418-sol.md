> 審查者：pi＋gemini-4-argon（11 月 argon 已花 $45.49）（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

前輪報告所列 6 項 finding 於本分支皆已修復且在 `bce0e418` 維持有效，累計未修輪數皆為 0。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 978aa1e1 | 0 | `_wait_dut_tx` 已移除，改由 `_checkpointed_dut_tx` 要求 passive barrier `settled is True`（`quiet_ms=500`）並核對 LF 結尾前綴之 `wire_len` 與 SHA-256；`test_checkpointed_dut_tx_includes_frames_written_before_barrier_settles` 覆蓋第二筆延遲寫入情境，SC-18 `test_autostart_l15_tx_once` 於 `daemon stop` 後才做最終比對。 |
| 2 | Medium | 已修 51bd8fa5 | 0 | ready 標記改由帶 `DELEGATE_READY_ENV` 的受委派孫行程寫出 `delegated grandchild pid=`；父行程解析該 PID 並確認 `process_is_running` 後才送 `GenerateConsoleCtrlEvent`，若委派前阻塞未建立孫行程則逾時 panic FAIL。 |
| 3 | Medium | 已修 ddcbdde0 | 0 | `stage1_exit_driver.sh` 接通 `scripts/rust/l15_exit_driver.py`，僅在唯一非零步驟為 `win_l15_func_test 1`、gate overall 為 `IMPLEMENTED`／`VERIFIED` 且 `errors` 為空、所有非 PASS 列皆符合核准延後規則且無 `harness_stop_failed` 時才放行出口；`tests/test_rust_stage1_exit_driver.py` 覆蓋整合回歸。 |
| 4 | High | 已修 12b3a929 | 0 | `upgrade.Dockerfile` 移除系統 `python3-yaml`，舊版以隔離 `pipx install --no-index --find-links=/opt/sw-old` 安裝；`mkbundle.sh --release-bundle` 將 PyYAML 封裝進 bundle 隔離 runtime，並以 `python3 -I -B` 驗證由 bundle site 匯入。 |
| 5 | Medium | 已修 12b3a929 | 0 | `mkbundle.sh` 區分互斥的 `--dev` 與 `--release-bundle`，僅 `--dev` 加入 `test-fakes` feature；正式發佈配方改用 `--release-bundle --profile release`，且 `b68b87d5` 進一步將測試守門改為解析 cargo `--features` argv。 |
| 6 | Low | 已修 12b3a929 | 0 | `docs/rust-refactor/release-0.4.0-plan.md` 前置條件已改為 Stage 1A overall 為 `VERIFIED`，或為 `IMPLEMENTED` 且僅剩核准延後之 `BLOCKED-OWNER`、無 `FAIL`／`INSUFFICIENT`。 |

（附註：本輪 `f59ce805..bce0e418` 針對 Stage 1 漏追清單修復之 D2 `b68b87d5`、D5 `9ee4e0fd`、D6 `adbe34a0`、D7 `c2e171cf`、D8 `bce0e418` 五項亦均已核對修復。）

## B 新發現

NO FINDINGS

本輪審查範圍 `f59ce805..bce0e418` 共 5 個非 merge commit（無 merge commit）：
- `b68b87d5`：`tests/test_rust_mkbundle.py` 新增 `_cargo_feature_names` 解析 `--features <val>` 與 `--features=<val>`，防止 `--features` 與 `test-fakes` 分拆為獨立 argv 時繞過字串檢查。
- `9ee4e0fd`：`tests/contract_blackbox/test_bb_l15_wire_payloads.py` 透過 monkeypatch `Path.read_bytes` 同步確認 `wait_wire_contains`／`wait_rx_contains` 已讀到未以 LF 結尾的殘段且仍未返回後，才補寫 `\n` 並驗證返回。
- `adbe34a0`：`tests/test_changelog_fragments.py` 改以 `policy_check.changelog` 的 `load_fragments` 與 `render_section` 驗證 `changelog.d/*.md` 全部 fragment，且相容 collate 清空後的空目錄。
- `c2e171cf`：`README.md` 英文與繁中兩段「從 0.3.x（pipx）升級」同步移除過期的 `TODO(OD-48)`，對齊可在 0.3.x daemon 執行中帶 `--user`／`--system --with-sudo`／`--on-demand` 直接升級的實作，並於 `tests/test_install_docs.py` 新增中英段落回歸測試。
- `bce0e418`：`docs/rust-refactor/reviews/s1-main-sync-observable-changes.md` 更正 `cmd submit --expect-reboot` 之 Rust CLI 對齊狀態，附 `tests/test_s1_observable_expect_reboot.py` 與 `changelog.d/s1m-bd-regression-gaps.md`。

驗證：`tests/test_rust_mkbundle.py`（feature guard 相關 4 項）、`tests/contract_blackbox/test_bb_l15_wire_payloads.py`、`tests/test_changelog_fragments.py`、`tests/test_install_docs.py`、`tests/test_s1_observable_expect_reboot.py` 共 22 passed；`python3 scripts/rust/check_cfg_boundary.py`、`python3 -m tools.contracts check`（`"ok": true`）、`python3 tests/acceptance/regen_lock.py --check`、`python3 -m policy_check --repo .`（25 pass, 0 fail, 2 warn）皆通過；工作樹乾淨。

## 既有碼待辦（不計入結論）

無

## 對裁決的異議（不計入結論）

無

結論：可放行