---
type: fix
scope: rust-device-handoff
---
Rust `device.attach` 遇到 Held／AccessDenied 開埠失敗時，依 Python `attach_device` 回覆鍵序並省略 `session`；force 仍拒絕已識別的外部 holder，維持 RELEASED 且不新增 UART TX。
實際開埠 NotFound 回覆 Python 的 `DEVICE_NOT_PRESENT` 鍵序；一般 RELEASED session 在 OSError 後若 unreleased 落盤失敗，維持 DETACHED 並清除 released provenance；reboot recovery 已有 bridge 時，`device.attach` 冪等回覆 `already_attached`。
Rust `session.bind` 由 actor 原子保存新 binding 並清除舊 RELEASED 列，逾時後 actor 完成仍可落盤；parked handle 關閉期間的重插快照不再被舊拔除事件覆蓋。
Gate 前置腳本會為候選版建置獨立 no-fakes CLI 與 Stage 2 handoff bundles；slice 2.1、2.3、2.4 共用主機預檢，Windows 與 slice 2.2 證據依候選 HEAD 綁定。
