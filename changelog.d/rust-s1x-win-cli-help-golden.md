---
type: change
scope: rust-stage1
---

新增 Windows 原生 CPython 3.12.3（embeddable）擷取的 argparse help／usage／錯誤 golden：`contracts/cli_help/windows/` 共 14 筆情境（`--help`、`profile test --help`、10 個錯誤情境，以及 `test_platform_oracle_exists_and_boundary_values_are_native` 強制要求的 2 個平台 oracle 邊界值 `boundary_socket_connect`、`boundary_endpoint_connect`）與 `_meta.json`（`python_version` 3.12.3、擷取時的 git revision）。計畫字面為 12 筆；多出的 2 筆由該測試與凍結 lock 直接要求，須由審查者確認。`scripts/rust/capture_cli_help.py` 補 Windows 擷取可攜性：以擷取用直譯器執行 CLI、子行程輸出以 UTF-8 解碼、golden 一律寫為 LF、固定 env 與黑箱測試 `_env` 一致（`/golden/...`），新增 `--git-revision`，且 Windows 擷取不再改寫共享的 Linux `contracts/cli_help/_meta.json`；Linux golden 位元組不變。

oracle 變更：新增 Windows cli_help oracle（規格 §0.4），須由非實作者的審查者核可後方可凍結進 `s1a_blackbox.windows`。
