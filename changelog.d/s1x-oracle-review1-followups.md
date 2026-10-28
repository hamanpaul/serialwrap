---
type: fix
scope: rust-refactor
---

依非實作者 oracle 審查（`docs/rust-refactor/reviews/s1x-oracle-review-1.md`）後續：Spike 3 主／oplock 分開記錄案例的替身改由第一次 rename 請求觸發 break（修正高負載下的 INSUFFICIENT 誤判）；SC-18 被拒請求的零 TX 檢查前確保距被拒回傳已過 wire 落盤等待上限並 settle，並新增「被拒請求延遲落盤＋最後一次送出遺失」必 FAIL 的回歸；L1 新增 `autostart_resend_carries_original_command_params`，斷言 SC-18 重送的 `command.submit` params 與原請求相同且請求數為 1。
