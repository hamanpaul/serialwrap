## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | High | 已修 `bd62919` | 0 | 回放結算前命令仍停放；一般命令與 reboot 的 Q → B 斷言保留。 |
| 2 | Medium | 已修 `bd62919` | 0 | 延遲樣本仍確認 raw owner、UART TX；本地回顯負對照保留。 |
| 3 | High | 已修 `bd62919` | 0 | release 仍推進 close 世代，拒絕舊世代晚到連線。 |
| P6-e9b5c61-1 | High | 已修 `7370b8f` | 0 | suspend 憑證仍由各自 SelfTest 保存、結算；Recover 不代為 resume。 |
| P6-e9b5c61-2 | High | 已修 `7370b8f` | 0 | 取消停放命令的兩條退出路徑仍結算 registry 在途指標。 |
| P6-e9b5c61-3 | Medium | 未修 | 4 | OD-52 已核准更新，但修訂鏈尚未追加指紋修訂；直接執行 2.1／2.2 指紋斷言，仍因 `l15-release-inflight` 失敗。 |
| P6-e9b5c61-4 | Medium | 未修 | 4 | OD-53 允許保留能力集合，但稽核範圍尚未依裁決調整；直接執行 ignored-node 斷言仍失敗，包含 P6 七個 PTY 節點。 |

## B 新發現

### P6-c6ddda1-1 [Medium] [scripts/rust/s22_gate_check.py:149](scripts/rust/s22_gate_check.py:149) — 其他集合的 passed 會遮蔽同報告重複列

`cd80448` 改用 `any(...)` 後，同一 Linux nodeid 在 `cargo_workspace` 出現兩列 passed、在 `console_conformance_pty` 出現一列 passed，逐案與 parity 判定會回 **PASS**；OD-54 要求同份報告重複仍為 **INSUFFICIENT**。

記憶體反例確認：`08323e8` 回 INSUFFICIENT，HEAD 回 PASS。集合層仍會使整體 gate 為 INSUFFICIENT，影響限於逐案結果誤報。

**確切修法：**保留 failed 優先判定，再拒絕任何報告內的重複 nodeid；新增「一份重複、另一份單列 passed」反例。

### P6-c6ddda1-2 [Medium] [event-latency-od45-rust-20261029.json:2](docs/rust-refactor/reviews/p5-evidence/event-latency-od45-rust-20261029.json:2) — 合入量測報告使必要政策檢查失敗

`7faa3c6` 合入 `3084388` 的報告，`daemon` 欄含主機絕對路徑。實跑 `python3 -m policy_check --repo .` 得 **24 pass／1 fail／2 warn**，唯一 FAIL 為此行觸發 R-21。

**確切修法：**同步 P5 `464adec` 對該欄的路徑遮罩修正，保留量測數值與失敗結果，再確認 R-21 通過。

已逐一檢視範圍內 16 個提交（含 3 個 merge）。11 項靜態斷言、契約檢查與 cfg boundary 通過；L1 鎖只增 12 列、無移除。受唯讀沙箱限制，未建置 Rust、未跑 E2E 或全套 pytest。未修改檔案，工作樹乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正

