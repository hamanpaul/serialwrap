> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-1ec1992-1 | High | 已修 `914fe072` | 0 | 本輪未改 Python 必要集合、selector 或 Rust 集合。 |
| V051-1ec1992-2 | High | 已修 `914fe072` | 0 | 前輪核對的 20 次驗收證據仍在提交內；本輪未重跑。 |
| V051-1ec1992-3 | Low | 已修 `914fe072` | 0 | 撤回的 Python 觀測器描述未重新加入 changelog。 |
| V051-914fe07-1 | Medium | 已修 `f06eb10b` | 0 | JUnit 缺檔或無法解析時回報 `GAP`；本輪未修改該判斷。 |

## C 完整性核對

| 項目 | 狀態 | 證據 |
|---|---|---|
| 逐項處理 `V051-22685ed-2`～`-5`，先紅後綠或記錄證據缺口 | 部分完成 | `854e1bb8` 有外部行程掃描、時間界線及 artifact 紅燈；console 根因未定已列 SPEC-GAPS。掃描判準仍有 B-1 缺陷。 |
| 不放寬斷言、不刪除或改名測試、不擴大範圍 | 部分完成 | nodeid 與 lock 本輪未變；但 `854e1bb8` 修改了 brief 禁止修改的 Rust 產品路徑，見 B-2。 |
| finding 對照表及可追溯 commit 的驗收 | 部分完成 | 實作者有對照表；update 綠燈紀錄的 `HEAD` 仍是 `f06eb10b`，未完整綁定當時產品碼與 bundle，見 B-3。 |
| 確認 f1 的 OD-115 撤回已在分支 | 完成 `1ec19922`、`914fe072` | `rust_only`、DF-23 紀錄與 Python 必要集合調整均在起點之前。 |
| 依 rest brief 準備 bundle、使用指定 A/B 入口及整檔驗收 | 部分完成 | update 整檔紀錄為 9 passed、1 個指定 deselect；正確版本配對的證據及其 commit 綁定有 B-3 所述限制。 |
| Windows 狀態讀取競態：先紅後綠，期限與斷言不變 | 完成 `854e1bb8` | monkeypatch 紅燈可查；本次定向執行 bridge 測試為 **3 passed**。 |

## B 新發現

1. **V051-854e1bb-1 [High]** [test_cli_mgmt_update.py](tests/contract_blackbox/test_cli_mgmt_update.py:1040) — **掃描證據仍可能把連結變更前開始的掃描當成變更後的掃描。**正式案例以 `action_mark`，而非已完成的連結變更，作為掃描下界；preload 又在 `real_opendir()` 返回後才讀取連結狀態。若執行緒在 `opendir` 返回後被排程暫停、bridge 隨後移除連結，恢復後的 `lstat` 會記為「連結不存在」，原本已開始的掃描即可滿足新階段。`wait_scan_after` 也未要求 open 與 close 的 TID 相同。本次以受控事件序列驗證，不同 TID 的 open／close 確實被接受為同一次掃描。這會讓背景掃描覆蓋出現假綠。

   **修法：**建立可證明掃描開始晚於連結實際變更的邊界，例如以 bridge action 完成時間作保守下界；同時依 daemon PID、watcher TID 與同一次目錄 handle 配對 open／close。補上「open 已開始後才改連結」及跨 TID 配對的受控反例，並接入正式案例。

2. **V051-854e1bb-2 [High]** [version.rs](crates/sw-proto/src/version.rs:6) — **超出 brief 的修改範圍。**rest brief 明確禁止修改 `crates/` 產品路徑；本提交改動 `sw-proto` 的正式版本來源，也修改了 `sw-cli` 產品檔。實作者雖描述了修改，SPEC-GAPS 未記錄此範圍違反及理由，因此完整性項目「不擴大範圍」未完成。

   **修法：**撤回未授權的產品路徑變更，改以授權範圍內可驗證的雙版本 artifact 完成測試；若確實無法做到，停止並將所需產品修改與影響交編排者裁決。

3. **V051-854e1bb-3 [Medium]** [test_cli_mgmt_update.py](tests/contract_blackbox/test_cli_mgmt_update.py:962) — **update 整檔與 20 次驗收無法完整追溯到所稱的提交及 artifact。**綠燈 log 記錄的 `HEAD` 是 `f06eb10b`，而當時測試檔已是未提交修改；測試檔雜湊與 `854e1bb8` 相符，但該兩份 log 沒有同時綁定當時 Rust 產品碼及實際 bundle 的雜湊。因此，9 passed 與 20/20 不能單憑現有紀錄證明執行的是 `854e1bb8` 對應的完整程式與 artifact。

   **修法：**在修正後的提交上重新建立或明確核對兩套 artifact，記錄 commit、工作樹狀態、相關原始碼與 CLI／daemon 雜湊、manifest 配對及實際執行路徑，再執行要求的定向與整檔驗收。

4. **V051-854e1bb-4 [Medium]** [test_console_pty.py](tests/contract_blackbox/slice22/test_console_pty.py:254) — **ownership 異常時，新增診斷本身會提前中止證據收集。**例如 h2 寫入超長行途中取得 raw ownership，迴圈內的斷言立即失敗；後面的 DUT TX、WAL 與 stderr 診斷都不會執行。這正是原始 `aaaa` 問題需要辨識的交錯，失敗訊息因此仍不足以判定資料是否到達 DUT。

   **修法：**先記錄 ownership 異常，在可靠的清理或診斷路徑擷取 DUT TX、WAL、stderr 與 client ID，再以保留的原斷言判失敗；補一個受控 ownership 變化案例驗證失敗時證據仍完整。

## 既有碼待辦（不計入結論）

- console 原始 `aaaa` 到達 DUT 的根因仍未定；實作者已列 SPEC-GAPS，現有 A/B 通過不能證明原始問題消失。
- 前輪列出的 `test_od72_slice21_nodes_keep_their_assigned_owner` DF-20 鎖集合問題仍待處理。

## 對裁決的異議（不計入結論）

無。

本次僅執行與本輪相關的 Windows bridge 定向測試及觀測器受控事件驗證；未執行完整 pytest、Docker、bundle 或正式出口 gate。工作樹維持乾淨。

結論：需修正