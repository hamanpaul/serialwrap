## Context

權威設計見 `docs/superpowers/specs/2026-07-17-bcm-agent-operability-design.md`（已核准）。本檔為 OpenSpec 架構摘要。兩個獨立缺陷同屬 bcm/BDK 板 agent 操作健壯性，共用一個 PR。現況：`sw_core/auth.py::resolve_session_auth` 解析空帳密仍回傳、login FSM 照送空字串；`sw_core/session_manager.py` 的 `interactive_open` allow-attached 分支只認 `bootloader_prompts`（prompt 行），autoboot 倒數行不在內。

## Goals / Non-Goals

**Goals:**
- 帳密解析為空（在 profile 宣告帳密來源時）→ 不送空帳密、回明確 `CREDENTIALS_UNRESOLVED` 終態、清楚示警含實際解析路徑。
- `interactive-open --allow-attached` 在 autoboot 倒數窗/boot banner 命中時亦授予 recovery lease（agent 可中斷 autoboot），回應標 `boot_interrupt`。
- 純 POSIX/共用邏輯，Windows 同步受益、無平台分支。

**Non-Goals:**
- #114 reboot-guard first-class primitive（封裝 reboot→中斷→交還）。
- #140 doctor/self-test 帳密檢查、env_file XDG fallback、attach 重讀。
- 不改 #130 boot quiet window 既有 gate/解除邏輯、不碰 #139 state 降級。

## Decisions

1. **auth 解析狀態**：`resolve_session_auth` 回 `(SessionAuth, AuthResolution)`，`AuthResolution` 帶 `reason`（`ok`/`env_file_missing`/`env_file_unreadable`/`key_absent`/`not_configured`）與實際 env_file 絕對路徑。既有只取 `SessionAuth` 的呼叫點以 tuple 解包相容。理由：不破壞 frozen `SessionAuth`，把「為何空」與「路徑」帶到 login FSM 供決策與示警。
2. **宣告帳密的判準**：profile 有 `user_env` 或 `pass_env` 或 `env_file` 即視為「宣告要帶帳密登入」。`not_configured`（皆無）維持既有 passwordless 行為，不誤擋。
3. **CREDENTIALS_UNRESOLVED 為終態**：進此態後 reprobe/自動重探不再反覆送空帳密（需操作者補帳密後手動 attach/recover）。理由：空帳密狂敲可能觸發板端帳號鎖定，且無資訊價值。
4. **lease 授予條件複用 #130**：allow-attached gate 改為 `_matches_any_bootloader_prompt(...) OR detect_boot_banner(rx_tail)`（`detect_boot_banner`/`BOOT_BANNER_PATTERNS` 為 #130 既有單一事實來源，含倒數行）。banner 命中授予時回 `boot_interrupt: true`，prompt 命中維持現有回應。理由：DRY、與 #130 認知一致；lease TX 本就不受 #130 gate，故中斷按鍵送得出。

## Risks / Trade-offs

- **A 誤擋合法 passwordless**：以「profile 是否宣告帳密來源」為閘，`not_configured` 完全不觸發 → 風險低；加回歸測試固定。
- **A 終態阻斷自癒**：CREDENTIALS_UNRESOLVED 不自動重試 → 操作者補帳密後需手動 attach。取捨明確（避免空帳密風暴），文件載明恢復步驟。
- **B banner 誤授 lease**：`detect_boot_banner` 命中 `U-Boot`/autoboot 倒數才授予；正常 OS 運行不含這些字樣 → 誤授風險低。授予的是 recovery lease（human/agent 顯式操作），非自動 probe，不會自行干擾板子。
- **B 與 #130 張力**：agent 用此 lease 刻意中斷 autoboot（與 #130 保護意圖相反），但透過 lease（顯式）達成、且 #130 不 gate lease TX，兩者機制相容；本 PR 不改 #130 邏輯。
