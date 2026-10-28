---
type: change
scope: tests
---
收緊 L1 凍結鎖的移除核准粒度（行為變更僅限測試閘門工具）：`tests/acceptance/regen_lock.py` 的 `--allow-removal` 改為 `set_id:platform:nodeid` 三元組，裸 nodeid 與非法格式以用法錯誤（exit 2）拒絕，`--check --base` 與 `--l1-add-only` 皆逐三元組比對。CI 之 `--allow-removal` 清單與釘定清單同步改為 545 筆三元組，逐筆對應真實移除事件（OD-16 (1)(2)、OD-17、既有 Linux 占位與舊裁決），不新增任何放行。`tests/test_rust_bench_tools.py` 之三個 pid namespace／`/proc` 測試移至 `tests/test_rust_bench_tools_posix.py`（模組層 `posix_only`，函式名稱不變）以修正平台分類。
