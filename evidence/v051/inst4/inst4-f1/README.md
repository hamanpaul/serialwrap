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
- `gate-policy-check.txt`、`gate-policy-check-repo.txt`：含 `--pr-base-ref origin/main` 與一般 repo 兩種命令，皆為 25 pass、0 fail、2 warn。警告為既有 workflow 測試 gate 偵測（R-19）與既有文件懸空引用（R-22 advisory）；本項未改 workflow 或既有文件。

未執行完整 pytest、`cargo test --workspace`、Windows 原生測試、Docker、正式 gate evidence driver；依本工作包界線，這些不是本項驗收命令。
