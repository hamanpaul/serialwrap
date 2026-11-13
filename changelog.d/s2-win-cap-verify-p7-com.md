---
type: change
scope: rust-p7
---
OD-55 留下的 windows_com 能力列（擁有者 2026-11-12 裁決第 6 點：實際驗證通過才凍結）：新增能力集合 `p7_gate:win-test-sh-caps-windows-com.windows`（P7-LW-9，selector `--package sw-daemon --test cap_windows_com_p7 --features test-fakes -- --ignored cap_windows_com__`），登記 `cap_windows_com__p7_{login_ready,quiet_banner_zero_tx,event_fire}` 3 列。`support::DaemonProc` 現優先使用 `SW_TEST_SERIALWRAPD`，未設定時仍使用 `CARGO_BIN_EXE_serialwrapd`；這讓 Windows runner 可啟動其 staging daemon。擁有者在舊 HEAD `21932ff9` 曾以 COM20:COM21 完成 3 列診斷重跑；本次只套用測試工具修正與原生收集清單，未重跑 Windows／COM，gate 候選版本仍須重取原生執行證據。`sw-pal::console_conformance::cap_windows_com__console_com0com_*` 3 列因 Windows console backend 尚未實作而失敗，不登記。
