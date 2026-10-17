---
type: fix
scope: uart
---

每次 UART 寫入都在實際送出前重新驗證命令仍有效，避免送出已取消、quiet、release、lease 換手或 human 輸入上下文失效的命令與按鍵。自動登入、密碼與 ready probe 在提交前偵測到開機 banner 或登入流程已被取代時不再送出；`session.self-test` 的 nonce probe 在 quiet 期間改回 `AUTOBOOT_QUIET`（`recommended_action: wait`），不送 UART、不等待 nonce。
