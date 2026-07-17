---
type: fix
issue: 130
scope: session
---
新增 U-Boot autoboot 保護（boot quiet window）：DUT reboot 時 daemon 的自動 probe（reboot recovery／readiness reprobe 送的 `\n`）必然落入 U-Boot autoboot 倒數窗，打斷開機、把板子永卡 bootloader prompt（`=> `），session 從此回不到 `READY`。現在 agent 送出 reboot 類指令當下、或 RX 見到 boot banner（`U-Boot`／`Hit any key to stop autoboot`，涵蓋 DUT 自行重開）即進入 quiet window（預設 180s）：視窗內 gate 所有 `source=system` 的自動 probe TX（reboot recovery 迴圈、reprobe 的 prepare 與寫入前最終驗證、attach probe），純被動等 RX；RX 匹配該 session 的 `login_regex`／`prompt_regex`（開機完成訊號）即刻解除並自動回 `READY`。human console bytes、interactive lease TX 與 agent 顯式命令一律不受 gate（與 #114 刻意進 bootloader 相容）。recovery deadline 延伸涵蓋視窗結束後至少一輪 probe，並設上限防 boot-loop 板無限延命。session 公開欄位新增 `boot_quiet_remaining_s` 供觀測。另 prpl-template 資產補上 `bootloader_prompts`（`^=> $`、`^U-Boot> $`），卡 bootloader 時 `interactive-open --allow-attached` 的正規 recovery lease 可用。
