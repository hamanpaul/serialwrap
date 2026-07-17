---
type: feat
issue: 114
scope: session
---
`interactive-open --allow-attached` 的 recovery lease 授予條件擴充至 **autoboot 倒數窗**：原本僅在 session `ATTACHED` 且 RX tail 末行匹配 `bootloader_prompts`（如 `=> `）時才授予 lease；現在若未匹配 prompt、但 RX tail 命中 boot banner（`Hit any key to stop autoboot` 倒數行／`U-Boot` 版本行，複用 #130 `detect_boot_banner` 與 `BOOT_BANNER_PATTERNS` 單一事實來源），亦授予相同的 recovery lease（`recovery_mode=True`），並在回應多帶 `boot_interrupt: true`（bootloader prompt 命中則省略此欄位，additive、向後相容）。用途：agent 燒壞 fw 後，若板子會 autoboot 載入壞 image，可在倒數窗搶開 lease → 以 `interactive-send` 連打按鍵中斷 autoboot 停在 `=> ` → 再逐字驅動 U-Boot 救 fw，不必等它先停在 bootloader prompt。與 #130 boot quiet window 相容——lease TX 永不受 quiet window gate，故倒數窗內連打按鍵有效。純 POSIX/共用邏輯，Windows daemon 走同路徑同步受益。README（中英）／`docs/serialwrap-spec.md`／`SKILL.md` 同步。
