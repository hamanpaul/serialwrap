# Tasks: serialwrap-reliability-plugin

## 1. Phase 1a — 分類基礎（realhw）

- [x] 1.1 RED＋GREEN：`CaseResult` 增 `category`/`reason_code`（預設空、向後相容）＋report render 分類欄——`tests/test_realhw_harness.py`
- [x] 1.2 既有 29 case 逐案標註 category/reason_code（依裁決線：斷言失敗=test、明確外因=environment、執行期 SKIP=environment、未捕捉例外兜底=空+uncaught_exception）
- [ ] 1.3 本機快速驗證：`--only p1-cmd-file`（SKIP 帶 base64_missing）與任一 PASS case 報表分類欄正確

## 2. Phase 1b — preflight 兩級判決（realhw）

- [x] 2.1 RED＋GREEN：capabilities dict（版本比較、remote 子命令探測、docker 可達）與 requires→執行期 SKIP 映射——`tests/test_realhw_preflight.py`
- [x] 2.2 RED＋GREEN：benchlock（flock＋pgrep 外部 testpilot run；兩進程搶鎖單測）併入 suite-refuse
- [x] 2.3 RED＋GREEN：`WinSwCli` driver（Windows 端 serialwrap.exe JSON 解析→持有清單；純解析單測）＋preflight `windows_daemon` 診斷增強（READY 缺項歸因 windows_daemon_holds_device）
- [x] 2.4 testbed 事實新增 Windows 端 serialwrap.exe 路徑欄位（config.json schema＋文件）

## 3. Phase 1c — hp-cycle 自動救援（realhw）

- [x] 3.1 RED＋GREEN：救援鏈純決策函式（注入探測結果→動作序列：probe→release→retry≤2→fail attended）——`tests/test_realhw_drivers.py`
- [x] 3.2 `p1-hp-cycle` 接上救援鏈（subprocess 薄層）＋evidence 記錄＋FailEnv/windows_daemon_holds_device 標註
- [ ] 3.3 真機驗證：`--only p1-hp-cycle`（含 Windows 端持有情境實測）

## 4. Phase 1d — remote 族（realhw）

- [x] 4.1 `tools/docker/remote_tunnel_test.sh` 加逐拓樸分派參數（`$1`∈{direct,nat_host,dual_nat,gwports,all}，預設 all）
- [x] 4.2 RED＋GREEN：rm-topo verdict 映射（exit code＋log 尾段→CaseResult 分桶）——`tests/test_realhw_drivers.py`
- [x] 4.3 `realhw/cases/remote.py`：rm-topo ×4（shell out 包裝、image 延遲建置、teardown finally 掃殘留）
- [x] 4.4 rm-live ×3（e2e／orphan／cycle：容器 ssh 對端、穿隧道 cmd submit 真板 marker、WAL source 斷言、daemon pid 不變、state dir 淨空；獨立容器名前綴）
- [x] 4.5 harness `--tier remote` 接線＋`--list` 更新＋checklist/README 文件（R-18）
- [ ] 4.6 真機驗收：redeploy 0.2.3+remote（營運前置）後 `--tier remote` 全綠；順帶補驗解鎖的 rst-reboot/bootwindow

## 5. Phase 2a — plugin dist 與核心（reliability/）

- [ ] 5.1 `reliability/pyproject.toml`（dist 名/deps/entry point/api_version 常數）＋`serialwrap_reliability/` 骨架；驗 release wheel 內容不變
- [ ] 5.2 RED＋GREEN：`core.py`（不 import testpilot）：REGISTRY→case dicts 映射（tier/destructive/requires metadata、destructive 預設過濾）、CaseResult→`_last_failure` 抄寫、longrun steps 合成——serialwrap `tests/` 直測
- [ ] 5.3 RED＋GREEN：testbed loader（testbed.yaml→cfg 合成；與 config.json 雙來源等價單測）
- [ ] 5.4 `plugin.py`（PluginBase glue：name/api_version/discover_cases/prepare_run=preflight gate/setup_env=Ctx 一次建置/execute_step=black-box/evaluate/teardown）＋`agent-config.yaml`（sequential、max_attempts=1、remediation=snapshot-only：enabled true＋max_attempts=1＋不覆寫 decision hooks；hooks 含 on_failure）
- [ ] 5.5 `reporter.py`（create_reporter/report_formats=["md","json"]，重用 realhw 報告＋run meta 烙 deployed 版本）

## 6. Phase 2b — bench 整合驗收（plugin）

- [ ] 6.1 editable install → `testpilot list-plugins`/`list-cases` 36 條 → `run --case p0-doctor` 冒煙（agent_trace/diagnostic_status/報表全鏈）
- [ ] 6.2 雙前端一致性：standalone 與 plugin 各跑 P0＋P1 非破壞性，逐案 verdict 比對（不一致歸因）
- [ ] 6.3 分類落桶驗證：停 docker→FailEnv、testbed 錯 serial→FailConfig（serial 為穩定身分鍵；busid 隨插拔變動不宜作 mismatch 測試欄位）、真實 FAIL→FailTest
- [ ] 6.4 longrun 短跑（checkpoint 版 `--duration 15m`）驗步進模型＋reporter
- [ ] 6.5 benchlock 實測（模擬 wifi_llapi run 進行中→整場拒跑）

## 7. 收尾

- [x] 7.1 changelog fragment（R-09）＋docs 對齊（README/checklist，R-16/R-18）
- [ ] 7.2 `python3 -m pytest -q tests/` 無新失敗＋`python3 -m policy_check --repo .`（帶 PR 參數）通過
