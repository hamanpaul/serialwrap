---
type: feat
scope: rust-p7
---
P7 Task 3：Rust 全平台 Login FSM——`ensure_ready` 補 bcm／`post_login_cmd`（含 #174 login guard 與逾時分流）、console unlock 被動靜默解鎖（錨定 nonce、CFE 尾行判定、空行辨識 1 s）、`detect_template`、`maybe_reclassify_flood`（RX_FLOOD ≥ 20000 bytes/10 s）；無效 regex 以 `LoginOutcome::Raised("error")` 立即結束、TX 拒絕／未提交／部分寫出以 `LoginOutcome::Aborted` 結束且不再送任何 byte；`LoginIo` 增 `cancel`（`RunCancel`，SC-30）與 `class`、`tx` 改回 `TxReport`；`RxShared::wait_for_regex_cancellable` 以喚醒通道等待（不輪詢）。以 Python 基線 golden `login_fsm_scenarios` 69 列逐列鎖定 TX 序列、結果與虛擬時間。
