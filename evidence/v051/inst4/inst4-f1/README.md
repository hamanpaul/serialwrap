# inst4-f1 驗收證據

- 基準：`fix/inst4-install-upgrade`，起點 `646c04565343877646f842fa8f4705b887b59128`。
- 本目錄保留每份紅／綠燈的命令、HEAD、測試檔 SHA-256 與程式檔 SHA-256；邏輯突變紅燈另有突變程式 SHA-256。

## Finding 對照

| Finding | 證據 |
|---|---|
| V051-f603610-1、V051-646c045-4 持鎖拒絕 | `red-logical-held-lock.txt`、`green-bundle-held-lock.txt`、`green-mgmt-setup.txt` |
| V051-f603610-2、V051-646c045-3 正式 bundle 保留 config 位元組 | `red-bundle-preserve.txt`、`green-mgmt-setup.txt` |
| V051-f603610-2、V051-646c045-4 選埠後健康檢查新埠 | `red-first-install-health.txt`、`green-mgmt-setup.txt` |
| V051-f603610-4、V051-646c045-1 根 YAML 安全寫回 | `red-yaml-nested.txt`、`red-root-flow.txt`、`green-yaml-nested.txt`、`green-root-flow.txt`、`green-endpoint-resolve.txt`、`green-mgmt-setup.txt` |
| V051-646c045-2 supervisor 查詢失敗、切換失敗、健康檢查失敗回復原始 config | `red-supervisor-query.txt`、`red-switch-rollback.txt`、`red-health-rollback.txt`、`green-mgmt-setup.txt` |
| V051-f603610-6、V051-646c045-5 三組邏輯紅燈 | `red-logical-port.txt`、`red-logical-held-lock.txt`、`red-logical-released-lock.txt`；綠燈見 `green-auto-select.txt`、`green-mgmt-setup.txt` |
| V051-f3932f2-1 原始沒有 config.yaml 時失敗回復與中斷復原 | `red-no-config-switch.txt`、`red-no-config-health.txt`、`red-no-config-recovery.txt`；綠燈見 `green-no-config-switch.txt`、`green-no-config-health.txt`、`green-no-config-recovery.txt` |

自然紅燈均在起點 HEAD `646c04565343877646f842fa8f4705b887b59128` 執行新增斷言。三組邏輯紅燈以受控突變執行，突變僅存在於命令完成前的工作檔；目前 `crates/sw-cli/src/setup_cmd.rs` 與起點相同。

## 綠燈與凍結節點

- `green-yaml-nested.txt`：新增巢狀空 mapping 測試 `1 passed`；`green-endpoint-resolve.txt`：endpoint resolve `48 passed`。
- `green-bundle-held-lock.txt`：正式 bundle CLI 持鎖拒絕 `1 passed`。
- `green-mgmt-setup.txt`：mgmt setup `84 passed`。
- `green-crate.txt`：`cargo test -p sw-cli --features test-fakes --no-fail-fast`，45 個測試結果列合計 `815 passed, 0 failed, 3 ignored`。
- 凍結鎖由 `regen_lock.py --l1-add-only` 新增 8 個 Linux nodeid，加入 `p7_gate:cargo-test-workspace`、`s21_core`、`s22_gate:cargo_workspace`；未移除或改名既有節點。新增節點為：
  - `cargo:sw-cli::endpoint_resolve::inst4_selected_port_patch_keeps_nested_empty_mapping`
  - `cargo:sw-cli::mgmt_setup::bundle_setup_selected_port_starts_fake_daemon_and_checks_health`
  - `cargo:sw-cli::mgmt_setup::bundle_setup_on_demand_auto_port_preserves_other_config_bytes`
  - `cargo:sw-cli::mgmt_setup::bundle_setup_nonempty_root_flow_mapping_fails_closed_with_manual_hint`
  - `cargo:sw-cli::mgmt_setup::bundle_setup_auto_port_supervisor_query_failure_keeps_original_config`
  - `cargo:sw-cli::mgmt_setup::bundle_setup_auto_port_switch_failure_keeps_original_config`
  - `cargo:sw-cli::mgmt_setup::bundle_setup_auto_port_health_failure_keeps_original_config`
  - `cargo:sw-cli::mgmt_setup::bundle_setup_ping_timeout_with_held_instance_lock_refuses_second_daemon`
  完整收集清單見 `collect-sw-cli.txt`，regen 輸出見 `regen-lock.txt`。

## 必要檢查

- `gate-cargo-check-windows.txt`：`cargo check --workspace --tests --features test-fakes --target x86_64-pc-windows-gnu`，exit 0。
- `gate-cargo-clippy.txt`：`cargo clippy -p sw-cli --tests --features test-fakes -- --cap-lints warn`，exit 0。
- `gate-cfg-boundary.txt`：`check_cfg_boundary.py`，exit 0。
- `gate-contracts.txt`：契約檢查 `ok: true`；未改 `contracts/`，`contract_revision` 不變，沒有新增 BC。
- `gate-regen-lock.txt`：`regen_lock.py --check` 通過。
- `gate-freeze-provenance.txt`：兩個分母／provenance 測試檔共 `86 passed`。
- 第 1 輪回報記錄 `gate-policy-check.txt`、`gate-policy-check-repo.txt` 為 25 pass、0 fail、2 warn；本輪依相同兩種命令重跑結果不同，詳見第二輪驗收。R-19 workflow 測試 gate 偵測與 R-22 文件懸空引用仍是既有警告，本項未改 workflow 或既有文件。

第一輪未執行完整 pytest、`cargo test --workspace`、Windows 原生測試、Docker、正式 gate evidence driver。第二輪依增量審查要求執行 workspace cargo 測試；本輪結果見下方驗收補記。Windows 原生測試、Docker 與正式 gate evidence driver 仍不在本項執行範圍。

## 第二輪：原始 config 不存在時的回復

- `rollback.rs::restore_original` 依 journal 的 `original.config_sha256` 還原 config：原始值為 `None` 時移除交易建立的 `config.yaml` 並同步目錄；有 SHA 時從原始 snapshot 還原並核對內容。`verify_restored` 也核對檔案存在性與 SHA-256，避免缺檔或殘留新檔被報成已回復。
- 三份紅燈均在起點 `f3932f2b7df5b11f6717a189593cb44b771366b0` 執行，輸出綁定測試檔與產品檔 SHA-256：入口切換失敗、健康檢查失敗各因留下新檔而失敗；中斷案例於第二次正式 CLI setup 未重新選埠而失敗。三份綠燈各為 `1 passed`；中斷案例經 setup CLI 復原 journal、再次選埠，健康檢查失敗後檔案仍不存在且 journal 已清除。
- 新增 L1 節點：
  - `cargo:sw-cli::mgmt_setup::bundle_setup_auto_port_switch_failure_without_config_restores_absence`
  - `cargo:sw-cli::mgmt_setup::bundle_setup_auto_port_health_failure_without_config_restores_absence`
  - `cargo:sw-cli::mgmt_setup::bundle_setup_auto_port_interrupted_recovery_without_config_reselects_port`
- `collect-round2.txt` 記錄依鎖定 selector 的 workspace 收集及 add-only 命令；`collect-workspace-round2.json` 與 `collect-workspace-skipcap-round2.json` 各收集 3764 個 nodeid（53 個 ignored）。L1 鎖僅在 `p7_gate:cargo-test-workspace.linux`、`s21_core.linux`、`s22_gate:cargo_workspace.linux` 各新增上述 3 項，移除 0 項；Windows 集合未更動，新增節點待 Windows 原生收集。

## 第二輪驗收

- `rollback.rs::restore_original` 依原始快照的 `config_sha256` 還原位元組；原始值為 `None` 時移除交易建立的 config 並同步目錄。`verify_restored` 呼叫 `verify_original_config`，核對原始檔案存在性及 SHA-256。
- 新增三個正式 bundle CLI 回歸：入口切換失敗、健康檢查失敗與中斷後復原。三份 `red-no-config-*.txt` 均在 `f3932f2b7df5b11f6717a189593cb44b771366b0` 以原始測試轉紅；對應 `green-no-config-*.txt` 各為 `1 passed`。測試檔 SHA 為 `922a2bd51fca7c49883edf032e8051c6a7d07c10752864334dc81f9a858bdd75`，修正程式 SHA 為 `839b5f726ec42eb940d8a5efbd2cb159ca91a7f516829cd5086a8550eca1bb3a`。
- `green-crate-round2.txt`：`cargo test -p sw-cli --features test-fakes --no-fail-fast`，815 passed、0 failed、3 ignored。
- `case-freeze-tests-round2.txt`：`tests/test_slice21_case_freeze.py` 與 `tests/test_slice24_cases.py` 整檔共 86 passed。為避免 `tests/conftest.py` 在 collection 時探測正式 daemon，使用 `--noconftest`；因此有 19 個 pytest 未知 marker 警告，測試內容全數通過。
- 第一次 `cargo test --workspace --features test-fakes --no-fail-fast` 未設立該 repo 所需的隔離 no-fakes CLI 與 bundle，`handoff_stop` 的 `default_cli_update_scope_cannot_bypass_foreign_daemon`、`handoff_then_successor_never_double_held` 因此前置缺失而失敗。依 `scripts/rust/gate_prereqs.sh` 的 build 命令，在核准的 `/srv/thala-e/serialwrap-cargo/v051-inst4` 範圍內重建 no-fakes CLI、base bundle 與 feature bundle；`handoff-stop-prereq-rerun-round2.txt` 為 23 passed。之後完整 workspace 測試 `cargo-workspace-prereq-round2.txt` 共 3715 passed、0 failed、53 ignored（200 個 test target 結果列）。首次失敗輸出見 `cargo-workspace-round2.txt`。
- `cargo-check-windows-round2.txt`：`cargo check --workspace --tests --features test-fakes --target x86_64-pc-windows-gnu` exit 0。`clippy-sw-cli-round2.txt`：`cargo clippy -p sw-cli --tests --features test-fakes -- --cap-lints warn` exit 0；本項改動行沒有新增 Clippy warning。
- `cfg-boundary-round2.txt`、`contracts-final-round2.txt`、`lock-check-round2.txt` 均通過；未改 `contracts/`，`contract_revision` 不變，沒有新增 BC。
- 新產生的測試與驗收 log 將本機工作樹絕對路徑正規化為 `<worktree>`，保留命令、HEAD、檔案雜湊與測試結果。
- 本輪兩種 policy 命令結果皆為 24 pass、1 fail、2 warn。R-21 顯示 15 個 structural hits；`policy-r21-baseline-round2.txt` 證明其中 13 個不同的路徑均已在起點 HEAD 追蹤且逐位元組相同。本輪新證據路徑正規化後未增加 R-21 hit。未改政策設定或舊證據。R-19、R-22 是既有警告。
- 驗收期間未接觸 production daemon、其設定／狀態、真實序列埠、COM 或容器；未跑完整 pytest、Windows 原生測試或正式 gate evidence driver。
