# Sequential A/B 執行程序

本程序說明 `python3 -m tools.stage3 ab` 的 manifest、三種 suite adapter 與失敗處置。A/B 是「Python 基線（A）→ Rust 候選（B）」的依序執行：A 側確認釋放之前不啟動 B 側，B 側一律使用全新實例目錄。

Stage 3 的整體順序見 `stage3-runbook.md`。本文件的命令可被 `build_parser()` 解析，但實際執行需要真實 artifact 與 bench，不得以範例 manifest 直接代替。

## 1. 命令

```bash
python3 -m tools.stage3 ab verify-manifest --manifest "$AB_MANIFEST" --repo-root .
python3 -m tools.stage3 ab run --manifest "$AB_MANIFEST" --out "$AB_OUT" --repo-root .
```

| 子命令 | 行為 | 結束碼 |
|---|---|---|
| `verify-manifest` | 載入 manifest，重算所有凍結輸入、bundle 元件與案例涵蓋。不執行任何受測命令 | 0＝無違規；1＝有違規（`violations` 列出） |
| `run` | 依 manifest 執行 A/B，寫出 `$AB_OUT/ab-report.json` | 0＝PASS；1＝FAIL 或 INCOMPLETE（中斷時若報告已記錄 FAIL，同樣為 1）；3＝INFRA_ERROR、BASELINE_INVALID 或 ABORTED（中斷且報告不可讀時亦為 3） |

`run` 的 `--out` 必須不存在，已存在即拒絕（`HARNESS_MANIFEST_INVALID`），以免覆寫舊證據。每份 `ab-report.json` 再以 `evidence ingest --kind ab-report` 登錄。

## 2. manifest 欄位

manifest 為 JSON（`tests/acceptance/ab_manifest.schema.json`），範本見 `tests/acceptance/examples/` 下的三個 `ab_manifest.*.example.json`。範例僅供格式參考（`example_only`），其雜湊值為佔位，不得用於真實執行。

| 欄位 | 說明 |
|---|---|
| `schema_version`、`run_id` | 版本與唯一執行識別。同一 `run_id` 不得重跑 |
| `layer` | `l1.5`、`l2` 或 `l3`，決定 suite adapter |
| `platform`、`stage` | `linux` 或 `windows`；`stage` 為 `1A`、`1B`、`2.1`～`2.4` 或 `3` |
| `contract_revision` | 契約版本 |
| `case_list`、`case_lock` | `cases.yaml` 與 `cases.lock.json` 的路徑與 sha256 |
| `required_case_set`、`case_ids` | 必要案例集合，`verify_frozen` 核對涵蓋 |
| `tools` | 納入凍結的工具路徑與 tree sha256（含 `tools/stage3`） |
| `allowed_differences` | `contracts/behavior_changes.yaml` 的路徑與 sha256，登記 A/B 允許差異 |
| `acceptance` | 僅 L3：`realhw/acceptance/longrun_acceptance.yaml` 的路徑與 sha256 |
| `dut` | DUT 種類、transport、`com_pair`、`image`、`image_digest`、`boards`、重置與指紋命令（`reset_argv`、`fingerprint_argv`），以及必填的 `expected_fingerprint`（物件，鍵值為字串，須與 `fingerprint_argv` 的實際輸出相符） |
| `authorization` | `allow_destructive`、`decision_ref`、`boards`。破壞性案例必須有授權 |
| `test_device_allowlist` | Windows com0com 實例的允許裝置清單，對應 `SERIALWRAP_TEST_DEVICE_ALLOWLIST` |
| `profile_files` | 受測設定檔的 `FrozenFile` 陣列（每項 `path` 與 `sha256`）；無則為空陣列 |
| `seed` | 非負整數，隨機種子，寫入報告以利重現 |
| `platform_settings` | 物件，平台特定設定（例如 Windows com0com 參數）；無則為空物件 |
| `sides` | 兩側（A、B）：`label`、`role`（baseline／candidate）、`bundle_dir`、`release_manifest`（path 與 sha256）、`cli_exe`、`daemon_exe`、`activate_argv`、`deactivate_argv` |
| `suite` | `adapter`、`timeout_s`、`selftest_report`、`testbed_template`、`realhw_config_template`、L3 的 `tier`、`extra_args` |
| `endpoint` | L2／L3 的明確 endpoint；L3 不得是佔位值 |
| `release` | 釋放驗證參數：`timeout_s`、`poll_s`、`endpoints`、`device_paths`、`lock_paths`、`device_open_probe`、`accept_unreadable_holders` |
| `reverify` | 複驗：`enabled` 與 `order`。是否複驗必須在 manifest 預先宣告，不得依第一輪結果決定 |
| `failure_retention` | L3 固定為 `on-demand`，見第 5 節 |

## 3. 三種 suite adapter

adapter 由 `layer` 決定，操作者不得以手動命令替代 adapter 的執行。

| layer | adapter | 實際執行者 |
|---|---|---|
| `l1.5` | L1.5 | P2 func-test bridge 模式：`python3 func-test/runner.py --mode bridge --dut-mode local-process\|docker --report-dir DIR --selftest-report PATH`，以 `SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE` 選 artifact。runner 每個案例自行啟停 daemon 與 DUT |
| `l2` | L2 | `testpilot run serialwrap_regression`，testbed 指定 `serialwrap_exe` 與 `bench_lock_path`。報告目錄由引擎自選 |
| `l3` | L3 | `python3 -m realhw --tier longrun --report-dir DIR`，`realhw/config.json` 指定 `serialwrap_exe`。PID 取 `daemon status.pid` |

adapter 會核對實際受測身分：suite 實際量測的安裝內容必須等於該側凍結的 tree，否則 `HARNESS_ARTIFACT_MISMATCH`。

【BLOCKED-OWNER BO-P9-02】 Linux L2 Sequential A/B（`serialwrap_regression` 全部適用案例，含破壞集）須於擁有者 bench 執行。manifest 的 `authorization` 必須列出授權決策與限定板子（見第 6 節）。

【BLOCKED-OWNER BO-P9-03】 Windows L2 適用案例 A/B 須使用明確的 bench lock 路徑（testbed `bench_lock_path`），於擁有者 Windows bench 執行。

## 4. A→B 釋放驗證失敗

每側結束後，adapter 會以 `release_check` 驗證資源已釋放（串口、endpoint、lock 與程序）。`unknown` 或逾時一律不是 released。

處置規則：

1. A 側未確認釋放前，B 側絕不啟動。`run` 會以 `HARNESS_RELEASE_UNCONFIRMED` 結束該輪，verdict 為 INFRA_ERROR。
2. 不得手動略過釋放驗證，也不得在 A/B 之間手動清理後接續執行同一個 `run_id`。
3. 不得為了通過而改 `release.timeout_s` 後重跑同一 `run_id`。調整任何參數都必須以新的 `run_id` 與新的 `--out` 重跑，並保留舊報告。
4. 先查出釋放失敗的原因（殘留程序、被外部工具佔用的 tty、未回收的 lock），修正後再以新 `run_id` 執行。
5. 失敗報告與 `evidence ingest` 的 INFRA_ERROR 紀錄保留。INFRA_ERROR 不是產品 FAIL，也不阻擋後續有效 PASS；只有目前 target 下有效的 FAIL 紀錄會黏著、不被後續 PASS 抵銷（見 `evidence-index.md` 第 6 節）。

## 5. L3 的故障保留與外部監管

【BLOCKED-OWNER BO-P9-04】 Linux 48h L3 A/B（Python 基線→Rust 候選）。`ab run` 在 B 側啟動前會呼叫 `od09_freeze.register_candidate_start`；OD-09 未凍結即 `HARNESS_OD09_NOT_FROZEN`，B 側不執行。

【BLOCKED-OWNER BO-P9-05】 Windows 48h L3 A/B 只能在 Linux 48h 結束且 bench 釋放後開始。

【BLOCKED-OWNER BO-P9-16】 A→B 之間的 bench 獨占由擁有者保留；不得跨 TestPilot 套件或跨主機並行。

- L3 必須 on-demand：`failure_retention` 為 `on-demand`，`endpoint` 為明確值。
- 進入 L3 前，adapter 會檢查 `service status`。若 serialwrap 已註冊為外部監管 unit（systemd 或工作排程器），回報 `HARNESS_FAILURE_RETENTION_UNVERIFIED`，不執行 realhw。
- `lr-mixed` 的故障保留模式不得被外部監管自動重啟抵銷（§15.11.2）。
- 48h 長跑 Linux 與 Windows 依序執行，不可並行（同機同板經 usbipd）。

## 6. 破壞性案例的 authorization

- 含破壞性案例的 manifest 必須 `authorization.allow_destructive` 為 `true`，並提供 `decision_ref`（擁有者裁決編號）與限定的 `boards`。
- 缺漏時 `verify-manifest` 回報違規 `destructive case without authorization: <case_id>`（exit 1）。`ab run` 以 `HARNESS_AUTHORIZATION_MISSING` 拒絕執行（INFRA_ERROR），不執行任何 activate、suite 或 cli 命令。
- 授權只限宣告的板子。不得以同一 manifest 操作未列出的板子。

## 7. HARNESS_ARTIFACT_MISMATCH 的意義

這個代碼表示 suite 實際執行的受測內容與該側 manifest 不符。常見成因：

- 兩側都跑到預設的 Python 基線（B 側未真正切到 Rust 候選）。
- 安裝目錄含有 manifest 未宣告的旁載檔（例如 `bin/version.dll`），造成內容指紋不同。`verify_frozen` 只核對 manifest 宣告的元件，旁載檔要靠 `tree_sha256` 才現形。

此代碼屬於 harness 判定，不是產品 FAIL。處置：找出實際執行的安裝，修正為與 manifest 相符的內容後，以新 `run_id` 重跑。不得把指紋不符的結果改寫為 PASS。

## 8. Windows com0com 實例

- Windows L1.5 A/B 使用 com0com 的 DUT-side pair，或 USB-UART 直連（OD-08）。
- 隔離實例須設 `SERIALWRAP_TEST_DEVICE_ALLOWLIST`，只列出測試用的 COM 號（例如 `COM20`），不得列出 DUT console。
- 擁有者 host 的 COM20／COM21 為 `SW_TEST_COM_PAIR` 的值（serialwrap 端 COM20、bridge 端 COM21）。

【BLOCKED-OWNER BO-P9-01】 Windows L1.5 Sequential A/B（原生 COM）須於擁有者 Windows host 執行。WSL 只能經 interop 代跑，且須擁有者授權。

【BLOCKED-OWNER BO-P9-15】 `WindowsProbe` 於真實 COM 上的釋放判定實測，前置於 Windows L1.5 A/B 的 `ab run`（`windows_com` 資源）。

## 9. 判定與結束碼

- 所有輪次皆 PASS 才為 PASS。否則依嚴重度取最嚴重者：FAIL 優先於 BASELINE_INVALID、INFRA_ERROR、ABORTED 與 PASS，FAIL 不被 INFRA 遮蔽。
- 合併函式 `ab_verdict.combine` 以 `allowed_differences` 登記的差異判定 diff。未登記的差異即 FAIL。
- `layer=="l1.5"` 的差分由 `ab_diff` 判定；`l2` 由 `compare_l2_reports`；`l3` 由 `compare_l3` 比較兩側 `release-gate.json`。

## 10. 收尾

每次 `run` 完成後保存 `ab-report.json` 與各側 `report_dir`，再以 `evidence ingest` 登錄。登錄前不得修改報告內容。報告內容指紋由產生端於執行前後量測，ingest 缺值即拒收。
