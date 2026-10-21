---
type: refactor
scope: rust-cli
---
建立 Stage 1B（P4）管理面的介面骨架與測試替身：`sw-pal` 的 `Platform`／`Capabilities` 超集（`ClientPlatform`、`ClientCaps` 保留別名）、`ServiceCtl`／`ProcScan`／`ProcessTunnel`／`RegistryView` 簽章、`fakes` 組裝點替身與 `TestEnv` 測試支援模組；`sw-cli` 的 8 個委派群組（`daemon`、`service`、`supervision-mode`、`remote`、`connect`、`benches`、`setup`、`doctor`）補上宣告式 parser 與 Rust-only 旗標（`supervision-mode set`、`setup --windows-task／--windows-service／--rollback`）及其 help golden；新增黑箱 stub、委派哨兵、P4 案例綁定映射表與 `check_p4_cases.py`。執行期路由仍由 `DELEGATED_GROUPS` 決定，行為不變。
