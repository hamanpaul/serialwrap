# reliability-testpilot-plugin Specification

## Purpose
TBD - created by archiving change serialwrap-reliability-plugin. Update Purpose after archive.
## Requirements
### Requirement: plugin 註冊與 dev-only 安裝
plugin SHALL 以 `reliability/pyproject.toml` 為獨立發行單位（dist 名 `serialwrap-reliability`），宣告 `dependencies=["testpilot-core>=0.3.4,<1.0"]` 與 entry point `[project.entry-points."testpilot.plugins"] serialwrap_reliability = "serialwrap_reliability.plugin:Plugin"`，`api_version` SHALL 為 `"1.1"`。此 dist MUST NOT 進 release wheel、MUST NOT 上傳任何 index；唯一支援的安裝方式為 editable install（`pip install -e <repo>/reliability`）。主 `pyproject.toml` 與 release wheel MUST 零改動。plugin SHALL 於 editable 環境下以 repo 相對位置（`__file__` 上溯至 repo root 後插入 `sys.path`）import `realhw`，MUST NOT 要求 realhw 被打包。

#### Scenario: testpilot 發現 plugin
- **WHEN** 於 testpilot venv `pip install -e <repo>/reliability` 後執行 `testpilot list-plugins`
- **THEN** 列出 `serialwrap_reliability`；`testpilot list-cases serialwrap_reliability` 列出全部 case（含 tier/destructive metadata）

#### Scenario: release wheel 不受影響
- **WHEN** 以主 pyproject 建 wheel
- **THEN** wheel 內容與本 change 之前相同（不含 realhw 與 serialwrap_reliability）

### Requirement: 生命週期映射（Thin Adapter）
plugin SHALL 將 testpilot 生命週期映射到 realhw 引擎：`prepare_run()` 執行 realhw preflight 當 gate（任一 suite-refuse 缺項 → 拋出終止整場，仿 PreflightGate）；`discover_cases()` 自 realhw REGISTRY 產 case dicts（tier/destructive/requires 進 metadata；destructive case 預設過濾、僅 `--case` 顯式點名才納入）；`execute_step()` black-box 呼叫 realhw `case.run(ctx)`；`evaluate()` 依 CaseResult verdict 回傳布林並於 FAIL/SKIP 時把 `category`/`reason_code`/evidence 抄進 `case["_last_failure"]`；`teardown()` 執行板卡恢復與 tmux/docker 殘留清理。plugin MUST NOT 以 testpilot transport 執行 case 動作（tmux/usbipd/systemd/CLI 皆由 realhw drivers 自理）。核心邏輯 SHALL 位於不 import testpilot 的模組（`core.py`），`plugin.py` 僅做 PluginBase glue。

#### Scenario: 分類抄寫落桶
- **WHEN** 某 case 回 `CaseResult("FAIL", category="environment", reason_code="docker_unavailable")`
- **THEN** `evaluate()` 回 False 且 `_last_failure.category=="environment"`，core 產 `diagnostic_status=="FailEnv"`，trace 含 `reason_code`

#### Scenario: 破壞性 case 選擇期排除
- **WHEN** 未帶 `--case` 直接 `testpilot run serialwrap_reliability`
- **THEN** destructive case 不進 run、不出現在報表；`--case p1-rst-daemon` 顯式點名時才執行

### Requirement: 組態來源與雙來源等價
plugin SHALL 自 testpilot `testbed.yaml` 讀 bench 事實（boards 的 com/serial/busid/platform/profile、`usbipd_exe`、Windows 端 serialwrap.exe 路徑、timeouts、longrun 參數）並合成 realhw cfg dict；realhw SHALL 提供單一 loader 介面使 config.json（standalone）與 testbed.yaml（plugin）兩來源對同一 bench 事實產出等價 cfg。

#### Scenario: 雙來源等價
- **WHEN** 同一組 bench 事實分別寫入 config.json 與 testbed.yaml
- **THEN** 兩條路合成的 cfg dict 相等（單測斷言）

### Requirement: 執行策略與 longrun checkpoint 模型
plugin SHALL 宣告 `execution_policy` 為 `{mode: "sequential", max_concurrency: 1}`；`agent-config.yaml` SHALL 顯式設 `retry.max_attempts: 1`（core 預設 2，缺省即真機重跑）。remediation SHALL 設 `enabled: true` **僅作 failure_snapshot 擷取通道**（core 於 disabled 時不寫 snapshot，所有 FAIL 會退化 Inconclusive），並鎖死實際修復動作：`max_attempts: 1` 使 on_retry 永不 dispatch；plugin MUST NOT 覆寫 remediation decision hooks（預設回 None——decision 恆 None 為**唯一真正生效的阻擋點**；實地考證：core 對空 `allowed_actions` 集合為 falsy、白名單檢查被短路不攔截，故 MUST NOT 依賴空白名單當防線）；`hooks.enabled_hooks` MUST 含 `on_failure`（漏列同樣導致 snapshot 不落地）。longrun SHALL 為單一 case、於 `discover_cases()` 依 duration/interval 合成 N 個 checkpoint step，step 判準 always-pass（不讓 core 中斷長跑），最終判決集中於收尾（讀 longrun-analysis：daemon 死亡→`test/daemon_died`、RSS 超閾→`test/rss_leak`、快照斷流→`environment/snapshot_gap`、乾淨→Pass）；長跑進度監控來源為 realhw 增量寫出的 `snapshots.ndjson`（testpilot agent_trace 於 case 結束才落盤）。

#### Scenario: 長跑不被 retry 重跑
- **WHEN** longrun case 收尾判 FAIL
- **THEN** 不觸發第二次 attempt（max_attempts=1），diagnostic_status 依收尾分類

### Requirement: 報表與報告身分
plugin SHALL 覆寫 `create_reporter()`／`report_formats()`（`["md","json"]`），報表重用 realhw 產出（report.md/report.json，長跑另含 longrun-analysis.md）。報告身分 SHALL 以 deployed serialwrap 版本為準（preflight 自 `serialwrap --version`／daemon status 取得並烙進 run meta）；板卡 fw 記為環境 metadata。

#### Scenario: 報告身分烙印
- **WHEN** 任一 run 完成
- **THEN** run meta 含 deployed serialwrap 版本、repo HEAD sha、板卡 fw；報表檔名/內容以 deployed 版本識別

### Requirement: 雙前端一致性
同一部署上，standalone `python3 -m realhw` 與 plugin 對同一批非破壞性 case 的逐案 verdict SHALL 一致；不一致 SHALL 先歸因（adapter 缺陷 vs 真機偶發）並重跑確認，無法歸因者視為 adapter 缺陷。

#### Scenario: 一致性驗收
- **WHEN** 於同一部署先後以兩前端各跑 P0＋P1 非破壞性
- **THEN** 逐案 verdict 相同；差異需附歸因紀錄

