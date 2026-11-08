> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

無。

## B 新發現

**P8X-e66ed8d-1 [High]** [crates/sw-pal/tests/support/wintask.rs:1111](crates/sw-pal/tests/support/wintask.rs:1111) — **缺少 WAL 證據仍判通過**（引入於 `d31858c7`）。

- 具體情境：staging 沒有 `raw.wal.ndjson`，或檔案為空。`wal_last_line_crc_ok()` 均回 `true`，使 runtime 第 118 行「停止後 WAL 最後一筆完整」斷言通過。這放寬了 `cases.yaml:6145` 的必要判準；零 session 裁決沒有豁免 WAL 證據。
- 實測：直接抽取本輪函式編譯測試，輸入不存在的 WAL；「不得判完整」斷言失敗，exit 101。
- 確切修法：只接受非空的 `WalTail::Complete`；透過不開啟 COM 的隔離測試機制，讓受測 daemon 寫入可核對的紀錄，再驗證停止後仍完整。無法取得紀錄時應回報證據不足，不得計為 PASS。

**P8X-e66ed8d-2 [Medium]** [crates/sw-pal/tests/support/wintask.rs:929](crates/sw-pal/tests/support/wintask.rs:929) — **原生停止驗證繞過正式 `graceful_stop`**（引入於 `d31858c7`）。

- `graceful_stop_for_test()` 自行實作停用、RPC、等待、硬停與重新啟用；runtime 第 111 行只呼叫這份副本。計畫 Task 13 明定須呼叫 P4 `graceful_stop`，並以 Rust daemon 驗證整條序列。
- 具體失敗情境：正式 `sw-cli::graceful_stop` 回歸為正常停止後仍執行 `/End`，但此副本維持現況。預期原生案例失敗；實際所有停止斷言仍只觀察副本，無法偵測正式路徑的錯誤。此判斷來自呼叫關係，未做 Windows 突變實跑。
- 確切修法：由測試 harness 呼叫正式 `sw-cli` 停止實作，注入將監管操作限定至 `sw-p8-test-<pid>` 的 `ServiceCtl`；保留凍結案例入口，移除重寫的停止流程，並驗證正式路徑的 `/End` 突變會使案例失敗。

驗證：定向測試 **113 passed**；L1 既有節點與 selector 全數保留，僅追加。未執行 Windows 排程、完整 pytest、docker 或 bundle 測試；工作樹保持乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正