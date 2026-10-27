---
type: feat
scope: rust-console
---
interactive lease 承接 human lease、soft preempt、stash 還原、可重複讀的 tombstone 與 ATTACHED 救援的 human 分支（07 Task 7）：`console_attach` 自動開 human lease；READY 閒置 human lease 被 agent soft preempt 時 stash 並 suspend，close／到期依 `_PostCloseAction` 還原並回放（stash 過期或 peer 不在時依 D-16 丟棄該 client 的 deferred）；ATTACHED 救援判定抽出為 `rescue.rs`（bootloader prompt→boot banner→login），並檢查 worker running／serial_alive／vtty；tombstone FIFO 擠出才發 `LeaseIndex(Remove)`；human lease 的 3.0 s peer-loss grace 與 self-heal 隨 hub 掃描執行；drain 後新 epoch 進 READY／ATTACHED 時重開 retained human lease；line editor 來源的 reboot 升格為 `PROMOTE_HUMAN_INTERACTIVE`；tick 到期還原結算前 `interactive_open`／`console_attach` 回 `SESSION_INTERACTIVE_BUSY`（#56）；`RxShared::tail_py` 承接 Python `text[-n:]`。
