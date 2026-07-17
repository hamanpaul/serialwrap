## Why

實機 debug 一塊 BDK/Broadcom 板（換到既有 FTDI 線的 COM1）暴露兩個「agent 操作 bcm/BDK 板」的健壯性缺陷：(1) profile 宣告了帳密來源但 `env_file` 解析為空時，daemon 靜默對 `Login:`/`Password:` 送**空字串**、無限 `Login incorrect`，操作者無可辨識訊號（#140）；(2) #130（v0.2.3）已讓 agent 在 `=>` 用 U-Boot 救 fw，但 autoboot **倒數當下**無法開 recovery lease（倒數行不在 `bootloader_prompts`），agent 搶不到窗刻意中斷 autoboot（#114 續補半）。

## What Changes

- 帳密解析回報狀態（`ok` / `env_file_missing` / `env_file_unreadable` / `key_absent` / `not_configured`）並帶實際解析路徑。
- login FSM：profile **宣告了帳密來源**但解析為空時，**不送空帳密**、回明確 `error_code=CREDENTIALS_UNRESOLVED`（終態、不無限重試），一次性清楚警告含 env_file 實際解析絕對路徑與原因；未宣告帳密來源（passwordless/auto-login）行為不變。
- `interactive-open --allow-attached` 授予條件擴充：RX tail 命中 autoboot 倒數行/boot banner（複用 #130 `detect_boot_banner`/`BOOT_BANNER_PATTERNS`）時也授予 recovery lease，回應標 `boot_interrupt: true`，讓 agent 於倒數窗連打按鍵中斷 autoboot 進 `=>`。
- 兩者皆 additive（新 error_code、新回應欄位），不改既有欄位；純 POSIX/共用邏輯，不動 Windows 後端。

## Capabilities

### New Capabilities
- `credential-resolution`: session 帳密解析的狀態回報，與「宣告帳密但解析為空時不送空帳密、回 `CREDENTIALS_UNRESOLVED` 終態並清楚示警」的登入行為。

### Modified Capabilities
- `session-interactive`: `interactive-open --allow-attached` 的 recovery lease 授予條件擴充至 autoboot 倒數窗/boot banner（除既有 bootloader prompt 外），並於回應標示 `boot_interrupt`。

## Impact

- 程式：`sw_core/auth.py`（解析狀態）、`sw_core/login_fsm.py` + `sw_core/session_manager.py`（不送空帳密、`CREDENTIALS_UNRESOLVED`、lease 授予條件擴充）、`sw_core/constants.py`（沿用 #130 `BOOT_BANNER_PATTERNS`）。
- 對外契約：新增 `CREDENTIALS_UNRESOLVED` error_code、`interactive-open` 回應可含 `boot_interrupt`（皆 additive）。
- 文件：README（中英）、`docs/serialwrap-spec.md`、`sw_core/assets/skill/SKILL.md`。
- 明確排除（defer）：#114 reboot-guard first-class primitive；#140 doctor/self-test 帳密檢查、env_file XDG fallback、attach 重讀。
- 不動 #130 boot quiet window 既有 gate、不碰 #139 state 降級。
