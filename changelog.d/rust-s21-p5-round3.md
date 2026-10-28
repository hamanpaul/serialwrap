---
type: fix
scope: rust-s21
---
Slice 2.1 第三輪審查修正：sw-daemon 行程測試的 `wait_ready` 逾時改為輸出 `last_error` 與假 DUT PTY 的外部持有者、自行 spawn 的子行程以守衛於 panic 時回收（F91／F111：外部行程搶開測試 PTY 時 daemon 依 #82 正確拒開，原本只剩狀態名且孤兒子行程會卡住 `cargo test`）。其餘處理見 `docs/rust-refactor/reviews/p5-fix-responses.md` 第三輪段。

state.json 含孤立 surrogate（surrogatepass 位元組或 `\uXXXX` 跳脫）時，Rust daemon 比照 Python 載入成功、於啟動存檔重現 `UnicodeEncodeError`（rc 1、原檔不動、不產生 `.corrupt`、不 attach），不再改名損毀備份並丟掉 RELEASED；UTF-16／32 解碼錯誤訊息位置含 BOM。release 存檔前暫停點的 OSError 與 state.json 提交失敗的 RPC `EXCEPTION` message 改為 Python `str(OSError)` 形式。Slice 2.1 出口 gate：`case_uptake` 依承接率斷路器判定（tripped／OD-02 → FAIL、證據不足 → EVIDENCE_MISSING）、非測試 section 須明確帶判定、OWNER-DECISION-PENDING 列須對應登記的裁決項、L1.5 缺合格 bridge 自測報告時記為證據缺失。

L1.5：func-test expect 新增 `list_contains`；`s21-reboot-minimal-guard` 依 #162 等待再確認後才送命令；exclusive-conflict 情境斷言 `DEVICE_STILL_HELD.pids` 含外部行程。gate 新增 `--evidence-run`（凍結檢查未過仍收集證據、判定恆 INCOMPLETE），L1.5 仍有待裁決缺口時 `l15_ab` 不計 PASS。

測試支援：sw-daemon 測試經 `tests/common/mod.rs::data_root()` 定位 FakeTarget 與 repo 檔（不再依賴 cwd，使 `cargo_l1_results.py` 收集結果正確）；gate 的 Rust 黑箱改帶 Rust CLI。
