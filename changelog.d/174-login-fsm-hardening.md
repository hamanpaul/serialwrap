---
type: fix
issue: 174
scope: login-fsm
---
login FSM 硬化：`POST_LOGIN_CMD_TIMEOUT` 混淆三種原因且 `last_error_detail` 恆為 `null`、`brcm-template` 的 `prompt_regex` 未錨定行首在洪流／banner 板上可誤配登入成功。

- **送 `post_login_cmd` 前的 login guard**：`login_fsm._finalize_ready()` 送出前先查 rx tail 是否命中 `login_regex`／`password_regex`（`login_fsm.matches_login_or_password()`）——命中就代表 `prompt_regex` 誤配（如 BDK login banner 的 `#####` 裝飾線／CEVENT 洪流被誤配成 prompt）、`_maybe_login` 整段被跳過，此時**絕不**把 `post_login_cmd` 當帳密送進 login prompt，直接回可行動的 `LOGIN_REQUIRED`。
- **失敗分流**：`POST_LOGIN_CMD_TIMEOUT`／`READY_NONCE_TIMEOUT` 逾時後，若 rx tail 已回到 login/password prompt（憑證錯導致板子重印 `login:`，或 guard 仍漏接的邊界情況），改分流為 `LOGIN_REQUIRED`，不再與「登入成功但指令無回應」擠同一個 timeout 碼。`LOGIN_REQUIRED` 依既有 RX_FLOOD 反分類原則永不被遮蔽。
- **`last_error_detail` 帶 rx tail**：`session_manager._refine_probe_failure()`（attach/recover/reprobe/自動登入五處呼叫共用的單一 choke point）在 login FSM 失敗碼（`login_fsm.LOGIN_FSM_DETAIL_ERRORS`）未被翻轉為 `TRANSPORT_STALL` 時，附上失敗當下的 rx tail（`clean_text()` 去控制碼／ANSI 後截尾 300 字元），取代舊行為恆為 `null`。
- **出貨 `brcm-template` 的 `prompt_regex` 錨定行首**：`(?m)[>#]\s*$` → `(?m)^(?:.*[^>#\s])?[>#][ \t]*$`，並排除連續 `#`/`>` 裝飾線；`login_regex` 維持不錨定行首（getty 是 `<hostname> login: `，錨定會打壞這個最常見格式）。
- **login recovery lease**：`session_manager.interactive_open(..., allow_attached=True)` 的 `ATTACHED` 分支，bootloader prompt／boot banner 皆未命中時再檢查 rx tail 是否命中 `login_regex`／`password_regex`——命中則同樣授予 recovery lease，回應標 `login_required: true`（比照既有 `boot_interrupt` 欄位模式），讓 agent／人可用 `interactive-send` 打帳密把 session 救回 `READY`，不必整個 `device release`。三者皆未命中才維持既有 `NOT_BOOTLOADER`。
- **`serialwrap profile test --profile <name> --sample <file> [--profile-dir DIR]`**：離線診斷子命令，不連 daemon、不碰任何 UART，對樣本文字跑 prompt/login/password/bootloader regex，stdout 印 JSON 回報命中結果，供 operator 收緊 regex 前先驗證，不必上板試錯。

canonical 規格見 `openspec/specs/session-interactive/spec.md`（interactive_open 的 login recovery lease 分支）；`README.md` / `docs/serialwrap-spec.md` 同步。

**regression-case 評估**：mock/unit 已覆蓋 guard／分流／last_error_detail／regex 四類樣本／login recovery lease／`profile test` CLI，共 5 個新測試檔＋既有 login FSM／RX_FLOOD／bootloader-recovery 回歸線全綠。「真板 login banner 誤配」（S2'／S4）屬只有實機才驗得到的行為，建議新增 F10（登入帳密 family）realhw case 作 follow-up，不在本 PR 實作。
