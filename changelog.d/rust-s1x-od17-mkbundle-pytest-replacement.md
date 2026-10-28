---
type: change
scope: test-infra
---

依 OD-17（擁有者裁決，2026-10-22）將 mkbundle 相關的 `#[ignore]` cargo L1 列改由 pytest 替換登記：新增 L1 集合 `s1_pytest_full`（linux，selector 為 Stage 1 pytest 全套 `-q tests/ -p junit_provenance`），收錄 `tests/test_rust_mkbundle.py::test_mkbundle_respects_cargo_target_dir_override` 與 `tests/test_rust_release_manifest_schema.py::test_manifest_schema_accepts_new_optional_fields` 兩個替換案例（兩者內含 `--ignored --exact` 驅動並斷言 `1 passed`），再逐名移除 `s1a_cargo`／linux、`s1b_cargo`／linux、`s1b_cargo`／windows 中對應的 `real_mkbundle_dev_output_verifies` 與 `mkbundle_respects_cargo_target_dir_override` 列（共 4 處、2 個唯一 nodeid）。`tests/acceptance/regen_lock.py --l1-add-only` 全程 add-only，移除以 `--allow-removal` 逐名核准；`.github/workflows/tests.yml` 與 `tests/test_resource_report.py::test_workflow_ci_l1_gates` 的核准清單同步。

Stage 1 出口 gate（`scripts/rust/stage1a_gate.py`）新增 `s1_pytest_full`／linux 列：報告讀 `<--l1-results>/linux/pytest-full/resource-report.json`，junit 取 `--pytest-junit-report`，每個凍結 nodeid 須在通過 provenance 驗證的 JUnit 中恰出現一次且為 passed（CHG-S1X-OD17-GATE-01，收緊，未新增放行規則）；缺報告、集合不存在、INSUFFICIENT 或 INVALID 一律為證據不足，FAIL 仍為 FAIL，未新增放行規則。`check_p4_cases.py` 未接線，以避免循環依賴。尚未執行 Stage 1 pytest 全套，故該列在產生報告前不得標為 PASS。
