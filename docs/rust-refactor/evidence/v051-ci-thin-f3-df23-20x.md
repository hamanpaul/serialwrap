# ci-thin-f3 DF-23 Rust 受限連跑證據

## 身分與條件

- 受測 commit：`1ec19922df138464b62f178311e583d11795f997`
- 工作樹：開始與結束皆 clean；20 輪期間未修改受測測試檔
- 測試：`tests/contract_blackbox/test_p7_invariants.py::test_release_during_login_waits_for_handle_close`
- 測試檔 SHA-256：`4e91ae21c39adcdcb8f8369f28a5c221f537ab420e72851345ede9e69c351caf`
- Rust CLI：`0ebcb71ff507c09ea88c571c9200148e42da3ab08382e2fb5370c1bac91d7159`
- Rust daemon：`d140cbea33edcbb0a07ca6bbfe4ec698580d2ebf1f58b65f169d6b94473ef044`
- 建置目錄：`$HOME/.cache/serialwrap-cargo/v051-ci-thin`
- CPU 親和性：`taskset -c 0,1`；實際查核為 `pid 86813's current affinity list: 0,1`
- 排程：`heavy-slot serialwrap-v051-ci-thin nice -n 10`，20 輪前景串行，併發測試數為 1
- 忙碌行程：每輪開始時取 CPU 0/1 上 `%CPU >= 10` 的行程數，最大為 2
- 負載：每輪開始的 1 分鐘 loadavg 為 `5.17`～`11.37`，5 分鐘為 `5.93`～`8.60`，15 分鐘為 `6.08`～`7.68`
- 時間：`2026-11-25T08:25:17Z`～`08:40:33Z`，20 輪共 916 秒，低於裁決的 1500 秒上限
- 結果：20/20 passed；每輪 `rc=0`

## 命令

```text
heavy-slot serialwrap-v051-ci-thin nice -n 10 bash -c '
  cargo build -p sw-cli -p sw-daemon --bins
  timeout 180s taskset -c 0,1 env \
    SERIALWRAP_AB_LABEL=rust-p7 \
    SERIALWRAP_CLI_EXE="$CARGO_TARGET_DIR/debug/serialwrap" \
    SERIALWRAP_DAEMON_EXE="$CARGO_TARGET_DIR/debug/serialwrapd" \
    python3 -m pytest -q -p no:cacheprovider \
      tests/contract_blackbox/test_p7_invariants.py::test_release_during_login_waits_for_handle_close
'
```

## 逐輪結果

| 輪 | UTC 開始 | CPU0/1 busy（前） | loadavg 1m（前） | pytest | rc | wall_s |
|---:|---|---:|---:|---|---:|---:|
| 1 | `2026-11-25T08:25:17Z` | 0 | 5.17 | `1 passed` | 0 | 46 |
| 2 | `2026-11-25T08:26:03Z` | 1 | 9.64 | `1 passed` | 0 | 46 |
| 3 | `2026-11-25T08:26:49Z` | 0 | 7.80 | `1 passed` | 0 | 47 |
| 4 | `2026-11-25T08:27:36Z` | 0 | 11.37 | `1 passed` | 0 | 47 |
| 5 | `2026-11-25T08:28:23Z` | 0 | 10.89 | `1 passed` | 0 | 46 |
| 6 | `2026-11-25T08:29:09Z` | 1 | 11.32 | `1 passed` | 0 | 45 |
| 7 | `2026-11-25T08:29:54Z` | 1 | 9.85 | `1 passed` | 0 | 46 |
| 8 | `2026-11-25T08:30:40Z` | 1 | 9.08 | `1 passed` | 0 | 46 |
| 9 | `2026-11-25T08:31:26Z` | 0 | 8.79 | `1 passed` | 0 | 45 |
| 10 | `2026-11-25T08:32:11Z` | 0 | 6.90 | `1 passed` | 0 | 46 |
| 11 | `2026-11-25T08:32:57Z` | 0 | 6.88 | `1 passed` | 0 | 45 |
| 12 | `2026-11-25T08:33:42Z` | 0 | 6.81 | `1 passed` | 0 | 47 |
| 13 | `2026-11-25T08:34:29Z` | 0 | 10.26 | `1 passed` | 0 | 45 |
| 14 | `2026-11-25T08:35:14Z` | 0 | 8.43 | `1 passed` | 0 | 46 |
| 15 | `2026-11-25T08:36:00Z` | 0 | 6.67 | `1 passed` | 0 | 45 |
| 16 | `2026-11-25T08:36:45Z` | 2 | 6.00 | `1 passed` | 0 | 46 |
| 17 | `2026-11-25T08:37:31Z` | 0 | 7.45 | `1 passed` | 0 | 45 |
| 18 | `2026-11-25T08:38:16Z` | 0 | 6.75 | `1 passed` | 0 | 46 |
| 19 | `2026-11-25T08:39:02Z` | 0 | 5.82 | `1 passed` | 0 | 45 |
| 20 | `2026-11-25T08:39:47Z` | 1 | 6.11 | `1 passed` | 0 | 46 |

原始結尾：

```text
REPEAT_RESULT=PASS rounds=20/20 duration_s=916 limit_s=1500
HEAD_AFTER_REPEAT=1ec19922df138464b62f178311e583d11795f997
STATUS_AFTER_REPEAT=clean
```

原始暫存 log SHA-256：`2a3d59da2e8bc3f9213175cd3cebb980caa5b9b0804ea95234f5f197e98566c3`。

## 實際 A/B 與出口判定一致性

同一 commit 與測試內容下，另以前景 `heavy-slot` 串行執行完整 `test_p7_invariants.py`：

- Python A：
  `python3 -m pytest -q -p no:cacheprovider tests/contract_blackbox/test_p7_invariants.py --deselect
  tests/contract_blackbox/test_p7_invariants.py::test_release_during_login_waits_for_handle_close`
  → `7 passed, 1 deselected in 272.32s`，`rc=0`。
- Rust B：
  `SERIALWRAP_AB_LABEL=rust-p7 ... python3 -m pytest -q -p no:cacheprovider
  tests/contract_blackbox/test_p7_invariants.py`
  → `8 passed in 332.98s`，`rc=0`；DF-23 節點實際為 passed。

以該兩份實際 JUnit、L1 report 與 collect-only provenance 建立本案例的聚焦凍結集合後判定：

```text
P7_AB_REPORT_EXIT=0
| p7-df23 | SKIP | PASS | A：Python selector 已明確 deselect rust_only 節點 |
L1_JUDGE_A=PASS
L1_JUDGE_B=PASS
P7_CASE_COVERAGE=PASS
P7_CASE_COVERAGE_DETAIL=B 1／A selector 已明確 deselect rust_only 節點
```

因此案例報告顯示 A selector 明確 deselect、B passed；出口 L1 A/B 與案例 coverage 亦同為 PASS，未放寬
`l1_judge`，且 Rust 必要節點仍由 B 必跑集合承擔。
