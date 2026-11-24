---
type: fix
scope: console
---

修正 Windows TCP console（TeraTerm／PuTTY Telnet）連入後，session 層一直不知道有人類在用的問題。bridge 在 accept 時直接授予 raw 擁有權，不經 SessionManager，所以 session 沒有 lease，`self-test` 的 `human_attached`／`human_active` 恆為 false，人類打字時 reprobe 也不會暫緩。現在 session 層會補記一筆跟著 bridge 走的鏡像 lease：TCP console 斷線或換人時只撤鏡像、不清 bridge owner，避免清掉 grace 窗內新連線的 raw 擁有權。行為因此與 Linux 經 `console-attach` 的 minicom 一致：閒置 soft preempt、活躍時 `interactive-open` 回 `SESSION_INTERACTIVE_BUSY`；連線期間以 bare `--source human` 送的 `cmd submit`／`file push`／`file pull` 回 `SESSION_INTERACTIVE_BUSY`（#275）。
