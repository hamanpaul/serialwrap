# P5-T22 Slice 2.1 出口 gate 非 dry-run 實跑（第三輪，2026-10-28）

- 命令：`SERIALWRAP_L15_SELFTEST_REPORT=<posix-pty selftest-report.json> bash scripts/rust/slice21_gate.sh --evidence-run --run-id s21g-261028-045014`
  （§5.3 凍結檢查未過：`event_latency.freeze_status='pending_review_record'`、審閱紀錄未附；`--evidence-run` 照跑各 section 取證，judge 恆 INCOMPLETE 以上）。
- 環境：cloud Linux（root、4 核、ext4／virtio）；`6636694`；identity：CLI sha256 `6eca14be…0885`、daemon sha256 `2ea74890…c509`（release，見 `p5-t20-blackbox-report.md`）。
- 耗時約 47 分鐘；**verdict：FAIL**（gate exit 1）。報告 schema 驗證通過。

## 各 section 判定

| section | 判定 | 數字 | 缺漏／原因 | 處置 |
|---|---|---|---|---|
| cfg_boundary | PASS | rc 0 | — | — |
| linux_l1 | FAIL | 1663 passed／16 failed／27 skipped | 10 支 sw-daemon 因測試支援以 cwd 相對路徑定位 FakeTarget 與 scope 檔，`cargo_l1_results.py` 不在 crate 目錄執行測試 binary（`ModuleNotFoundError: bb_faketarget`）；其餘 6 支為 root 下證據不足 | 已修（`tests/common/mod.rs::data_root()`，本組 commit）；同 collector 重跑兩次：8 → **6 failed**（僅 root 證據不足：`endpoint_written_after_bind`、`wal_dir_readonly_at_start_blocks_login_probe`、`wal_fault_after_ready_splits_agent_and_auto_tx`、`file_eacces_matches_golden`、`unix_fixture_rows_match_python`、`holder_scan_permission`；`setpriv --reuid=65534` 6/6 passed）。root 下仍為 FAIL，L1 收集規則屬 F108（BLOCKED-OWNER） |
| linux_cap_posix_pty | EVIDENCE_MISSING | 340／4／8 | `s21_gate:linux_cap_posix_pty` 不在 L1 鎖 | BLOCKED-OWNER（F50／F57：gate 集合登記須擁有者核可） |
| windows_native_l1 | EVIDENCE_MISSING | runner exit 127 | 無原生 Windows | LOCAL-WINDOWS-TODO（BO-S21-13） |
| windows_cap_windows_com | EVIDENCE_MISSING | — | 無 COM pair | BO-S21-01（允許的豁免） |
| blackbox_python | EVIDENCE_MISSING | 33／10／3 | 集合未登記；10 failed 見 T20 報告 | BLOCKED-OWNER（F57）＋`PA-OD-T20` |
| blackbox_rust | EVIDENCE_MISSING | 0／46 | gate 未設 Rust CLI，conftest 身分檢查 46/46 拒絕（`A/B 標籤與受測 artifact 不符`） | 已修 gate（本組 commit）；以同 artifact 的 T20 兩輪為證：36／8／2 |
| existing_suites_rust | EVIDENCE_MISSING | 4／0；func-test 3/3 | 集合未登記 | BLOCKED-OWNER（F57） |
| l15_ab | FAIL | 8 failed／35 passed（43 項） | Rust 獨有 `l15-delayed-reply`、`s21-reboot-minimal-guard`（reprobe）；兩邊皆非 PASS：`l15-wal-interference`、`s21-wal-unlink-under-traffic`、`s21-event-latency` | `PA-OD-REPROBE`、`PA-OD-L15-WAL`、`PA-OD-L15-GAPS`、`PA-OD-EVLAT` |
| event_latency | EVIDENCE_MISSING | — | 判定器未實作；兩個 daemon 皆無計時欄位 | `PA-OD-EVLAT` |
| wal_s1w_perf | FAIL | append p50 0.023／p95 0.041／p99 0.058 ms（39996 筆） | 規格 §1.2 Linux 門檻 p50≤0.005／p95≤0.01／p99≤0.02 ms：cloud virtio 未達。照實記 FAIL、保留樣本，不改列觀察值 | 待擁有者實機量測（同 BO-S21-14 的正式量測程序；Linux 實機未列 BO，建議追加） |
| case_uptake | EVIDENCE_MISSING | valid_pass 0／49 | `case_verdicts.json` 未彙整；`stage2_estimate_weeks` 未填 | 擁有者填 OD-02 輸入；逐案彙整待 T20／T21 裁決後 |
| pytest_full | EVIDENCE_MISSING | 4719 passed／27 failed／182 skipped | 集合未登記；27 failed 全數屬起點基準（無新增） | BLOCKED-OWNER（F57） |
| policy_check | PASS | fail 0 | — | — |

其餘 verdict 原因：identity 缺 `daemon_version`（`serialwrapd` 依 Python 1:1 不提供 `--version`，gate 取不到；應改由 `daemon status` RPC 取得，F139 deferred）；
parity 105 列：PASS 50、INCOMPLETE 52、OWNER-DECISION-PENDING 1、BLOCKED-OWNER 2（#6／#71 因新規則缺 decision 降為 INCOMPLETE，已補 `decision: PA-OD-1`；
PA-OD-1 已由 OD-21 核准 SC-37，但 SC-37 尚未寫入任何分支的 `contracts/behavior_changes.yaml`，故仍待登記）；§5.3 event_latency 未凍結。

## LOCAL-WINDOWS-TODO

- `windows_native_l1`：`scripts/rust/win-test.sh --package sw-pal --package sw-core --package sw-daemon -- --features test-fakes -- --skip cap_`（原生 Windows runner）。
- Windows 原生黑箱／L1.5（BO-S21-13）、Winsock 慢讀大回應、state.json CRLF、預開 port `configure_port`、overlapped 讀取失敗臂（F09）。
- `win_l1_into` 綁定本次 run（F122）。

## BLOCKED-OWNER

- BO-S21-01～16（gate 報告 `blocked_owner[]` 全列；真板依 OD-27 於 release 後、安裝機上執行）。
- gate 集合 `s21_gate:{linux_cap_posix_pty,blackbox_python,blackbox_rust,existing_suites_rust,pytest_full}` 登記（F50／F57）與 root 下 L1 收集規則（F108）。
- 待裁決：`PA-OD-REPROBE`（Slice 2.1 L1.5 與 2.2 func-test 依賴 P7-T5 reprobe）、`PA-OD-T20`、`PA-OD-L15-*`、`PA-OD-EVLAT`、`PA-OD-F67`、`NC-P5-02`、`PA-OD-1`（已核准、待登記 SC-37）。
- `event_latency` 凍結審閱紀錄（§5.3）、OD-02 `stage2_estimate_weeks`。
