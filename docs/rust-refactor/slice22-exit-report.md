# Slice 2.2（P6）出口報告

> 依 rulings/p6.md（`docs/parity-amendments` 分支 `docs/rust-refactor/reviews/rulings/p6.md`）P6 OWNER-BATCH #4：流程項 #185 的證據寫入本報告；
> `tests/acceptance/slice22_evidence_map.yaml` 的 `parity_rows['#185']` 以 `PROCESS-EVIDENCE` 引用下方段落（`scripts/rust/s22_gate_check.py` 檢查段落、commit 與紅／綠燈紀錄）。
> 出口 gate 的逐輪實跑數字見 `docs/rust-refactor/reviews/p6-fix-responses.md` 與各輪雲端回報。

## #185 T0 commit 單獨紅燈與終態綠燈

- 稽核：parity-amendments-2026-10-26 #185（P6 審查 F3）：T0 commit `ae33e28`（`chore(contracts): 登記 Slice 2.2 Rust-only 契約並凍結案例集合`）單獨為紅燈；修補分散在後續的 `e5e2c56`（`tests/test_contracts_slice22.py` 補平台／資源 marker）與 `5516137`（`tests/test_slice21_skeleton_guards.py` 的 MIRROR 追加 `resource_limits` 九列）。依計畫不重寫歷史，於此明載「T0 單獨為紅燈、終態綠燈」。
- 紅燈紀錄（2026-10-29 第 5 輪，於 `ae33e28` 的獨立 worktree，以一般使用者執行）：
  - 命令：`python3 -m pytest -q tests/test_resource_markers.py tests/test_slice21_skeleton_guards.py`
  - 結果：**4 failed、9 passed**——
    - `tests/test_resource_markers.py::test_every_item_has_exactly_one_platform_marker`
    - `tests/test_resource_markers.py::test_every_item_has_resource_marker_and_memory_only_is_exclusive`
    - `tests/test_resource_markers.py::test_detected_resources_are_marked`（`('test_contracts_slice22.py', 'test_slice22_case_set_frozen_and_mapped', ['process_rpc'])` 等 marker 缺漏）
    - `tests/test_slice21_skeleton_guards.py::test_resource_limit_rust_mirrors_equal_yaml`（`resource_limits.yaml` 新增 9 列未鏡像）
- 綠燈紀錄（同日，分支 `feature/rust-refactor-p6` HEAD `5ade76c`，同一命令）：**13 passed、0 failed**。
- 結論：T0 單獨為紅燈、終態綠燈；紅燈原因皆為 T0 之後兩個 commit 已修的測試基礎設施（marker／鏡像），非產品行為。
