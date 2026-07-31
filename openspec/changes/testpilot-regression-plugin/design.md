# Design: testpilot-regression-plugin

## Context

#155 已完成 62 個 closed issue 盤點與四項裁示，定案 10 個 Scenario Family（F1–F10）。既有基礎：realhw（Case/CaseResult、preflight、benchlock、drivers）與 serialwrap-reliability plugin（testpilot 薄殼模式、三大契約陷阱實證）。bench：COM0=prpl（BGW720，U-Boot 2024.04、autoboot 3s 已實證）、COM1=bcm；部署版 serialwrap 0.2.4；testpilot venv 現綁 stale 0.2.1（#154，OPEN）。權威設計全文見 `docs/superpowers/specs/2026-07-31-testpilot-regression-plugin-design.md`。

## Goals / Non-Goals

**Goals:**
- 已修 bug 的實機回歸防線：分鐘～十幾分鐘級、可常跑（改動後／發版前）。
- 與 reliability 完全平行（目的、case 來源、discover 互不混淆），共用 realhw harness 基礎。
- #154 類 client 歪斜不得汙染本 plugin 的判定（pin exe＋版本 gate）。
- 破壞性 case 受 gate 管控；U-Boot 情境唯讀護欄由 harness 層強制。

**Non-Goals:**
- 不做 soak／長跑（#12 歸 reliability）；不測 shell/passthrough/Airoha/Windows/MCU（bench 不具備）；不重造 runner／報表／分診（testpilot 為殼）；不關閉 #154（僅 ops 升級＋防線）。

## Decisions

1. **平行薄殼重用 realhw 引擎**（vs 塞 realhw 新 tier／全新 harness）：#155 明示兩 plugin 不合併；realhw 已解 benchlock、恢復、preflight，不重造。
2. **自有 registry 與 Case dataclass**（vs 共用 realhw 全域 `REGISTRY`）：避免兩 plugin discover 互相撈到對方 case；自有模型才放得下 `family`／`issues` 欄位（回歸可追溯的核心）。
3. **SwCli 注入 exe 路徑**（upstream 小改、預設不變）＋ **preflight 版本對齊 gate**（vs 只修 venv）：venv 誰都可能再裝舊版，防線要在 plugin 內；ops 升級另做（三重防線）。
4. **destructive gate 讀 testbed `allow_destructive`**（vs CLI flag）：testpilot `run` 無自訂 flag 通道，testbed 是既有組態面；false→SKIP（非 FAIL）保持「改動後快跑」語意。
5. **U-Boot 護欄做成型別 API**（`UBootConsole` 只暴露 interrupt／白名單唯讀／leave；vs 約定俗成寫在 case 裡）：手滑即持久化，禁令必須由 harness 強制且可單測。
6. **F10 用 ThrowawayDaemon＋device release**（vs 改 prod profile 製造帳密失敗）：裁示「不算動到本機設定」；prod config 完全不碰。
7. **testpilot 契約沿 reliability 實證**：remediation `enabled:true`＋`max_attempts:1`＋不覆寫 decision hooks（snapshot 通道）；`retry.max_attempts` 顯式 1；execute_step 恆 success、判決集中 evaluate。
8. **命名 `serialwrap_regression`**（vs issue 工作名 serialwrap-plugin）：與 `serialwrap_reliability` 平行對照、直指定位。

## Risks / Trade-offs

- [agy 產出品質不穩] → cg review＋主 agent 整合；契約敏感層（plugin.py/guards）不派工。
- [F9 把板子留在 U-Boot／不回 READY] → 護欄強制 `leave`＋`ensure_ready` 收尾；實測排 bench 空檔。
- [實機時序 flaky] → 沿 realhw hints（line race、foreground busy、settle 拍）設計；case 內給足等待。
- [與 prod daemon 互擾] → 非破壞 case 走正常 RPC（產品本來承諾共存）；benchlock 與 reliability/wifi_llapi 互斥；F10 隔離 sandbox。
- [COM1 U-Boot 具備性未確認] → 首次 reboot 時確認；沒有則該 case 對 COM1 SKIP。

## Migration Plan

dev-only editable 安裝（testpilot venv `pip install -e regression/`），release wheel 零改動；回退＝`pip uninstall serialwrap-regression`。ops：venv serialwrap 0.2.1→0.2.4（記錄於 #154）。

## Open Questions

無阻斷項。COM1 U-Boot 具備性與實際 case 數（估 29–31）於實作／首輪實測收斂。
