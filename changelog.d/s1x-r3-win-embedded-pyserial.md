---
type: fix
scope: rust-refactor
---

Windows 黑箱 dev bundle 的內嵌 3.12.3 runtime 依 PyYAML 與 pyserial 實際匯入位置（含 user site）補入 `._pth` 並驗證兩者可匯入；修正 B 輪 on-demand daemon 開 COM20 時 `ATTACH_FAILED:OSError`（SC-18）。
