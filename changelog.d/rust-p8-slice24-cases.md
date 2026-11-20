---
type: change
scope: acceptance
---
P8（Slice 2.4）Task 1：`tests/acceptance/cases.yaml` 追加 24 筆 `s24-*` 案例（revision 2026-11-08.1；schema `suite` 新增 `rust-l1`、`family` 新增 `S24`，只增不改），鎖檔經 `regen_lock.py --add-only` 重算；`contracts/behavior_changes.yaml` 登記 Rust-only 的 SC-32（Probe／FLASHING 期間 `UartIoError` 進 drain 轉 `RECOVERING`、`DeviceDetached` 延後 reconcile），catalog revision 11。SC-33 依擁有者 OD-79 撤回不登記，原列案例改列 SC-03；`s24-xfer-abort-on-release` 依 OD-62 比照 BC-03。以 `freeze_slice_cases.py` 凍結 Slice 2.4 分母（187 筆，三週固定觀測窗口自 2026-11-08 起，Stage 2 起算日沿用 2026-10-21），並為 2.1～2.3 追加 OD-76 `revision_advance` 修訂；調和表補 SC-32、SC-33 撤回、XFER-ABORT-01（OD-62）、MCU-STATUS-01 與 EVT-D-F4（OD-80）。
