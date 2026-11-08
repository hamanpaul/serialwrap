> 審查者：claude-fable-5-1（重型任務，擁有者 2026-11-07 指定）

審查完成。範圍 `8b5cbe47..6c4eb2c4` 共兩個 commit、三個檔案，無 merge commit；定向實測 `daemon_process` 33 passed、`shutdown_order` 11 passed（工作樹未改動）。

## A 前輪 finding 核對

無（brief 未附前輪報告）。

附註：兩個 commit 訊息自稱回應 `P8X-8b5cbe4-1 [Medium]`／`P8X-8b5cbe4-2 [Low]`，但 brief 的前輪報告為「無」，且 `docs/rust-refactor/reviews/` 無 `p8x-fix-responses.md`，故無法逐項核對。此點記為 B 的 Low 文件項，不另立 A 表。

## B 新發現

- **P8X-6c4eb2c-1 [Low]** `crates/sw-daemon/tests/daemon_process.rs:308` — 新測試在 closure 內自寫 `poll`，而 `shutdown_order.rs:116` 與 `event_lifecycle.rs:84` 已各有一份同語意的 `fn poll(limit, f) -> bool`，三份重複。無失敗情境，純可維護性。修法：把 `poll` 搬到 `tests/support/mod.rs` 供三檔共用。
- **P8X-6c4eb2c-2 [Low]** `docs/rust-refactor/reviews/`（缺檔）— commit 86ede2ad／6c4eb2c4 回應的 `P8X-8b5cbe4-*` finding 在本分支無 `p8x-fix-responses.md` 紀錄，後續輪次無法以 repo 內文件追溯「哪個 finding → 哪個 commit → 何種處置」；本輪只能以 commit 訊息與實測交叉驗證。修法：補 `docs/rust-refactor/reviews/p8x-fix-responses.md`，列出前輪編號、嚴重度、處置與 commit。

核對結果（無 High／Medium）：

- **86ede2ad（測試同步修正）**：未削弱任何既有斷言。停機序列 12 個 TraceEvent 與 `SessionsDrained.flushed == [(c2, FLUSHED_BY_SHUTDOWN), (c3, FLUSHED_BY_SHUTDOWN)]` 原樣保留，只前置「COM0 READY → blk `ok:true` → blk `running` → q2/q3 STOP 前仍 `accepted`」四組斷言，且斷言全部移至主執行緒、client thread 逾時只回 false，不會在 STOP 前 panic 造成 daemon 等不到停止要求而 hang（符合 in-process 測試慣例）。改成同步呼叫 `submit(..., "blk")` 的前提是 `command.submit` 於 admission 即回覆（與 Python `arbiter.py:306` 一致），實測確認不阻塞。診斷結論「非 READY 回 `SESSION_NOT_READY`」與 `sw_core/service.py:793` 基線一致，不改產品碼是正確判斷。
- **6c4eb2c4（計數位置）**：`spawned_workers` 遞增移入 `spawned.is_ok()` 分支，唯一消費者是 `shutdown_order.rs:640/682` 在 ShutdownGate 取基準、LockReleased 取差值；遞增仍發生在呼叫端 thread，相對該消費者的可觀察時序不變，無新競態。產品 build 無此欄位。
- **changelog fragment**：`changelog.d/` direct child、`.md`，符合 R-09；內容與兩個 commit 的實際變更相符。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：可放行
