---
type: feat
scope: rust-cli
issue: null
---
Stage 1B 出口驗證工具：新增 CLI 暖快取往返量測（`bench_cli_roundtrip.py`，nearest-rank 分位數與 §1.2 gate；WSL、容器、平台不符一律 `NOT_STANDARD_ENV`，`--env-manifest` 不得覆寫工具自動量測的身分欄位）、單檔解壓檢查（`check_no_onefile_extract.py`，Linux inotify、Windows `ReadDirectoryChangesW`；共用系統暫存根的事件判 `INSUFFICIENT`，不得判 PASS）、P4 必要案例凍結與完成度檢查（`check_p4_cases.py`；L1 判定唯一委派 P2 `l1_judge`，BLOCKED-OWNER 以 exit 2 分流）。
狀態：全群組 A/B 兩輪執行器、L1.5 黑箱案例、Windows 原生 cargo L1 報告與 `s1b_*` L1 集合登記仍為 BLOCKED-PREREQ（需併入 `feature/rust-refactor`），以明確失敗呈現，未以 xfail 交付。
黑箱 Rust 輪的 `setup`／`supervision-mode` `--help` 改以 Rust-only golden 為 oracle（SC-22／SC-23／SC-24／SC-36 為 absolute_only，與 Python 必然不同），`capture_cli_help.py --rust-only` 擷取寬度擴至與平台 golden 相同的 20～120 九種欄寬。
