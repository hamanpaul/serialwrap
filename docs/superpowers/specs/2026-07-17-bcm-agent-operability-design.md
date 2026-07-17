# bcm/BDK 板 agent 操作健壯性：帳密解析觀測性（#140）＋ autoboot 倒數窗可開 recovery lease（#114 最小半）

> 日期：2026-07-17
> 分支：`feature/bcm-agent-operability-114-140`
> 關聯 issue：Closes #140、Closes #114
> 範圍：純 POSIX/共用邏輯側，不動 Windows 後端（`rpc_win`/`lock_win`/`device` win/console TCP）。

## 背景與動機

2026-07-17 一次實機 debug（一塊 BDK/Broadcom 板換到既有 FTDI 線的 COM1）暴露兩個獨立但同屬「agent 操作 bcm/BDK 板」的健壯性缺陷：

1. **#140 帳密解析靜默失敗**：brcm-template（`platform: bcm`）換對 profile 後仍 `Login incorrect`，追查發現 daemon 對 `Login:`/`Password:` 送的是**空字串**（非設定的帳密），因為 `env_file: brcm.env` 於 `sw_core/config.py:_resolve_opt_path` 相對 daemon profile-dir 解析（`/etc/serialwrap/profiles/brcm.env`），該檔不存在（只有 `brcm.env.example`），`sw_core/auth.py` 僅 `log.warning` 後回空帳密照樣送出 → 無限 `Login incorrect`，操作者無可辨識訊號（`session list` 只顯示泛用 `LOGIN_REQUIRED`）。

2. **#114 agent U-Boot 自救的「刻意進 U-Boot」半**：#130（v0.2.3）已交付「板子在 `=>` 時 agent 可用 `interactive-open --allow-attached` recovery lease 逐字驅動 U-Boot」（給 prpl-template 補了 `bootloader_prompts`）。但若壞 fw 會 autoboot 載入卻 hang，agent 需**刻意中斷 autoboot 停在 `=>`**——而 autoboot 倒數當下 RX 最後一行是 `Hit any key to stop autoboot: N`，不在 `bootloader_prompts`（只有 `=>` prompt 行），`interactive-open --allow-attached` 於此刻回 `NOT_BOOTLOADER`、agent 搶不到窗。

## 目標（本 PR 範圍）

- **#140 core**：profile 宣告了帳密來源但解析為空時，**不送空帳密**，回明確 `error_code`，一次性清楚警告含實際解析路徑與原因。
- **#114 minimal**：讓 `interactive-open --allow-attached` 在 autoboot 倒數窗（boot banner）命中時也能開 recovery lease，agent 得以連打按鍵中斷 autoboot 進 `=>`。

## 明確排除（defer，非本 PR）

- #114 的 reboot-guard first-class primitive（封裝 reboot→中斷→交還 agent）。
- #140 的 `doctor`/`session self-test` 帳密解析檢查、env_file XDG fallback、attach 時重讀 env_file（issue #140 保留為後續）。
- 不動 #130 boot quiet window 的既有 gate 邏輯、不碰 #139 的 state 降級。

---

## 設計 A — #140 帳密解析觀測性（core）

### A.1 auth 層：回傳解析狀態

`sw_core/auth.py`：
- 保留 `SessionAuth` frozen dataclass 不破壞既有欄位。
- `resolve_session_auth()` 額外回報**解析狀態**（reason enum，任一）：
  - `ok`：帳密成功解析（user/pass 非空，或 profile 本就不需帳密而合法）。
  - `env_file_missing`：profile 宣告 `env_file` 但解析到的絕對路徑不存在。
  - `env_file_unreadable`：檔存在但讀取失敗（權限/格式）。
  - `key_absent`：env_file 可讀但缺 `user_env`/`pass_env` 指定的 key（或值為空）。
  - `not_configured`：profile 未宣告任何帳密來源（`user_env`/`pass_env`/`env_file` 皆無）——合法的 passwordless/auto-login 情境。
- 實作方式：`resolve_session_auth` 回 `(SessionAuth, AuthResolution)`（或在 `SessionAuth` 旁附一個輕量 result 物件），並帶「實際解析到的 env_file 絕對路徑」供訊息使用。呼叫端相容：既有只取 `SessionAuth` 的呼叫點以 tuple 解包或 helper 取。

### A.2 login FSM：宣告帳密但解析空 → 不送空、回明確錯誤

`sw_core/login_fsm.py` + `sw_core/session_manager.py`：
- 「宣告了帳密來源」的判準：profile 有 `user_env` 或 `pass_env` 或 `env_file`。
- 當板子出現 login prompt（`login_regex` 命中）、且該 profile **宣告了帳密來源**、但 `AuthResolution.reason != ok`（解析為空）：
  - **不送空 user/pass**，中止本次登入嘗試。
  - 回 `error_code=CREDENTIALS_UNRESOLVED`，set 進 session `last_error`（與「板子還沒到 login prompt」的 `LOGIN_REQUIRED` 明確區分）。
  - 一次性 `log.warning` + WAL 事件：`env_file 帳密解析失敗：<reason>；解析路徑 <abs_path>；session <COM>`（不印帳密值本身）。
- profile **未宣告**帳密來源（`not_configured`）者：行為完全不變（現況 passwordless/auto-login 路徑不誤擋）。
- **避免重試風暴**：進入 `CREDENTIALS_UNRESOLVED` 後，reprobe/自動重探不再反覆送空帳密（該狀態為明確終態，需操作者補帳密後手動 attach/recover）。

### A.3 錯誤碼與文件

- 新 `error_code`：`CREDENTIALS_UNRESOLVED`。README/spec/SKILL.md 補此碼語意與排查（含「env_file 相對 daemon profile-dir 解析、非 XDG config」的明確說明）。

---

## 設計 B — #114 autoboot 倒數窗可開 recovery lease（minimal）

### B.1 授予條件擴充（複用 #130 資產）

`sw_core/session_manager.py` 的 `interactive_open` `allow_attached` 分支（現 `_matches_any_bootloader_prompt` gate，約 :3065）：
- 現況：`session.state == "ATTACHED"` 且 RX tail 命中 `_matches_any_bootloader_prompt(rx_tail, bootloader_prompts)` → 開 recovery lease；否則 `NOT_BOOTLOADER`。
- 擴充：授予條件改為 **`_matches_any_bootloader_prompt` OR `detect_boot_banner(rx_tail)`**（`detect_boot_banner` 為 #130 既有純函式，比對 `BOOT_BANNER_PATTERNS`＝`U-Boot` 版本行／`Hit any key to stop autoboot` 倒數行，單一事實來源）。
- 命中 banner（非 prompt）而授予時，回應標 `boot_interrupt: true`（呼叫端可知是「倒數窗中斷模式」而非「已在 `=>`」）；命中 prompt 者維持現有回應（`boot_interrupt` 省略或 false）。
- lease 一律 `recovery_mode=True`、`owner` 沿用 `--owner`（預設 `agent`）。

### B.2 agent 使用流程（不需新指令）

1. 板子 reboot（agent 送 `reboot` 或壞 fw 自發重開）→ RX 出現 `Hit any key to stop autoboot`。
2. agent `serialwrap session interactive-open --selector COMx --allow-attached`（現在倒數窗即可開，回 `boot_interrupt: true`）。
3. agent `interactive-send` 連打按鍵（Enter/任意鍵）中斷 autoboot → 板停 `=>`。
4. 之後沿用 #130 已通的 recovery lease，`interactive-send`/`interactive-status` 逐字驅動 U-Boot 救 fw。

### B.3 與 #130 boot quiet window 相容性

- lease TX **本就不受** #130 boot quiet window gate（#130 明載 human/lease TX 永不 gate）→ 中斷按鍵送得出去。
- 中斷後停 `=>`（bootloader prompt）→ #130 不把 `=>` 判為開機完成、quiet window 續留（不會有自動 probe 又干擾）；agent 持 lease 續操作，無衝突。
- 不改動 #130 任何既有 gate／解除邏輯。

---

## 資料流與邊界

- A 與 B 互相獨立，無共用狀態；A 在 login/auth 路徑，B 在 interactive-lease 授予路徑。
- 兩者皆為共用純邏輯，Windows daemon 走同一路徑同步受益，無平台分支。
- 對外契約新增：`CREDENTIALS_UNRESOLVED` error_code（A）、`interactive-open` 回應可含 `boot_interrupt`（B）——皆為 additive，不改既有欄位。

## 錯誤處理

- A：解析空帳密 → 終態 `CREDENTIALS_UNRESOLVED`（不無限重試）；例外不穿越 RPC 邊界（維持 `ok:false + error_code`）。
- B：非 bootloader 且非 banner 時仍回 `NOT_BOOTLOADER`（現有行為）；bridge 不健康時回 `SESSION_NOT_READY`（現有）。

## 測試

### A（#140）
- unit（auth）：四種 reason（`env_file_missing`/`env_file_unreadable`/`key_absent`/`not_configured`/`ok`）解析正確，含實際路徑。
- unit（login FSM/session）：宣告帳密但解析空 → 不送空 user/pass、回 `CREDENTIALS_UNRESOLVED`；未宣告帳密 → 行為不變（passwordless 仍可走）。
- PTY fake-target E2E：假 `Login:` prompt + 缺 env_file → session 停 `CREDENTIALS_UNRESOLVED`、WAL **零**空帳密 TX、log 有清楚警告含路徑。
- 回歸：既有有帳密的登入路徑（如 shell/prpl with env_file）不受影響。

### B（#114）
- unit（gate）：allow-attached 在 RX tail 命中倒數行/banner 時授予 recovery lease、回 `boot_interrupt:true`；命中 `=>` prompt 維持現有；皆不命中 → `NOT_BOOTLOADER`。
- PTY fake-U-Boot E2E：假 target 吐 `Hit any key to stop autoboot` 倒數 → agent `interactive-open --allow-attached` 成功（`boot_interrupt:true`）→ `interactive-send` 送鍵 → 假 target 轉 `=>`，lease 仍持有可續操作。
- 相容性：確認 #130 boot quiet window 下 lease TX 仍送得出（沿用既有測試手法）。

## 交付

- 一個 PR，body `Closes #114`、`Closes #140`。
- changelog fragment：`changelog.d/140-credentials-unresolved.md`、`changelog.d/114-autoboot-interrupt-lease.md`（各自 type/issue）。
- 文件同步：README（中英）、docs/serialwrap-spec.md、sw_core/assets/skill/SKILL.md。
