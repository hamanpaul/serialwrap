---
type: fix
scope: rust-refactor
---

OD-24：`scripts/rust/cargo_l1_results.py --list-only` 輸出改為 `{"collected":[...],"ignored":[...]}`，`tests/acceptance/regen_lock.py --l1-add-only` 新增時排除 ignored nodeid（鎖中既有 ignored 列不視為移除，只能逐名 `--allow-removal`）；新增防線要求所有 cargo 凍結集合不得含 `#[ignore]` 列，並自 `s1b_cargo` 逐名移除 5 筆 `#[ignore]` 列（CI `--allow-removal` 與 pinned 清單同步列名）。同次真實收集另登記 `start_windows_python_launcher_without_runtime_does_not_fall_through`（linux、windows 各 1 筆）。
