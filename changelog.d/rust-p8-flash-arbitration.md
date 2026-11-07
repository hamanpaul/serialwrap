---
type: feat
scope: rust-core
---
P8（Slice 2.4）Task 8：Rust daemon 的 SessionCore 承接 MCU flash 仲裁的介面與失敗測試。`TxPermit` 新增 `probe_id`（既有建構點一律 `None`）；`ControlEvent` 新增 `FlashSyncProbeStart`／`FlashProbeReleased`／`FlashSyncMatched`／`FlashHolderGone`，型別 `ProbeId`、`FlashReject`、`ProbeGrant`、`FlashReply`；`SessionManager` 新增 `FlashCandidate` 與 `collect_flash_candidates_and_mark`／`unmark_flash_critical`／`flash_critical`。新增 `flash_session_core` 測試（CoreHarness 狀態機斷言與 ActorRig 黑箱零 TX 證據）與 `tx_writer` 的 flash fence 規則表測試。
