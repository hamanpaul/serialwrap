---
type: change
scope: test-infra
---

Stage 1A 出口 G3 的 Windows 證據產生器：新增 `scripts/rust/win-func-test.sh`（以 WSL interop 在 Windows 本機 staging 執行 `func-test/runner.py --mode bridge --scenarios --daemon-com COM20 --bridge-com COM21 --transport-mode virtual_com0com`，產出 `--l15-report windows=` 來源）、`scripts/rust/win-spike3.sh`（以同一套 staging 執行 spike3 四案 `win-wal-av-rotate`／`win-wal-av-unlink`／`win-wal-av-reader-handle`／`win-wal-f12-progress`，產出 `--spike3-evidence` 來源），以及共用的 `scripts/rust/win-stage-lib.sh`（HEAD 子集 staging、以 PID＋起始時間監管 Windows 子行程）。EDR 參數只透傳呼叫者提供的值，wrapper 不自填；COM 僅限 COM20:COM21。新增 `tests/test_rust_win_func_test.py` 以靜態與 `--print-argv` 斷言 argv、COM 範圍、EDR 透傳與拒絕路徑。本變更不改 runner、spike3 工具與 gate 的判定規則。
