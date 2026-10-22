---
type: fix
scope: test-infra
---

修正 Stage 1 出口 Windows L1.5 producer（`win-func-test.sh`，COM20:COM21）首次實跑未產生 `func-test-report.json` 的六項缺陷（S1X G3）：

- runner 崩潰：`daemon_harness.stop()` 的臨時目錄回收遇 WinError 32 時中止整次執行。改為 daemon 行程以 Popen 確認結束、案例自建的 WAL 讀取 handle 持有者於回收前等待結束、臨時目錄回收有界重試；回收仍失敗者只寫入該案 `harness_diagnostics`，不改變案例判定。daemon 未確認結束或關閉失敗者判為 INSUFFICIENT_EVIDENCE、不得 `gate_eligible`，原已 FAIL 者保留判定。runner 於每案結束後寫出 `func-test-report.json`（原子取代），單一案例的未預期例外記為該案 INSUFFICIENT_EVIDENCE 並繼續其餘案例。
- DUT 開機 banner 遺失（HARNESS_STREAM_MISMATCH）：未修正（BLOCKED，見下）。根因已實測確認：pyserial 開埠會清除該埠尚未讀取的位元組（COM20:COM21 實測），bridge 於 daemon 開啟 COM20 前輸出的 banner 因此遺失。harness 端無法僅以延後 banner 解決：延後 banner 會使 DUT 的 prompt（取自 boot_banner 末行）消失，probe 無法取得 prompt。需 bridge win-serial 端點提供寫入閘門（對應 posix peer_gate），屬 tools/uart_stdio_bridge 變更，本組未修改。（Supersede，2026-10-23：本條之「未修正（BLOCKED）」已由 `rust-s1x-win-serial-peer-gate.md` 所述之寫入閘門取代並完成原生實測；PENDING-OD-S1X-WIN-BANNER 仍 OPEN，待 Windows L1.5 重跑證實。）
- 編碼：wrapper 不再全域強制 `PYTHONUTF8=1`（改以 `PYTHONIOENCODING=utf-8` 僅作用於 runner 自身的 stdio），bridge 的 stdout 明確以 UTF-8（`errors=replace`）解碼，使 bridge 子行程呼叫 git／powershell 時依系統 locale 解碼。
- 基本案例：`win-func-test.sh` 於同一 staging 另跑一次基本案例（不帶 `--scenarios`），輸出 `basic/func-test-report.json`；驅動以第二個 `--l15-report windows=` 交給 gate（gate 5c-iv：cases.lock 宣告的 windows 平台都須有列）。`cases.lock.json` 的 platforms 未變動。
- 測試資源標記：`tests/test_rust_win_func_test.py` 的 13 個項目逐一補上資源 marker（純字串讀取為 `memory_only`，執行 bash 子行程為 `process_rpc`），斷言未改。
- 驅動 `stage1_exit_driver.sh`：`RUN_ID`、`WIN_L15_WRAPPER`、`SPIKE3_WRAPPER` 不再列於 `local`，環境覆寫恢復生效。

補充（報告完整性，S1X 出口收尾）：`func-test/runner.py` 之 `func-test-report.json` 加入 `complete` 旗標；每案中途寫出為 `false`，僅全部案例結束後的最後一次寫出為 `true`。`stage1a_gate.py` 拒收缺少或非 `true` 的 l15-report（整體 FAIL）。
