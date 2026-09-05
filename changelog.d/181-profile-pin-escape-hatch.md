---
type: fix
issue: 181
scope: session-manager
---
`session pin` 對既有 session 恢復生效，`others-template` fallback 不再是沒有出口的單向門。

- **根因**：#95 建立的四層優先序（pin > sticky > detect > fallback）只實作在
  `_attach_by_id_dynamic`——也就是「該裝置**從未**建過 session」的路徑。既有 session 走
  `_attach_by_id`，它以 `device_by_id` 找到 session 後就原封不動沿用 `session.profile`；
  而 `clear_session` 只 detach bridge、**不刪 session 物件**，於是 re-attach 又回到
  `_attach_by_id`。結果是 `session pin` 被接受、被寫進 `state.json`（key 正確），然後
  在 attach 時被完全忽略——連 `session clear` 強制重新 attach 也一樣。pin 的說明是
  「最高優先，繞過偵測」，是使用者唯一被告知的逃生口，而它是壞的；配上 fallback 的空
  `ready_probe` 讓 `command_capable` 永久 false，掉進 `others-template` 後**沒有任何 CLI
  途徑**能救回，只能改設定檔再重啟 daemon（#182 描述的代價）。
- **修法**：新增 `_reresolve_profile_on_reattach()`，在 `_attach_by_id` 開 bridge **之前**
  重新解析既有 session 的 profile：
  - **pin**：命中且與現行 profile 不同即改用該 template，`profile_source` 標 `pin`。pin
    存在即為最高優先，命中與否都不再往下偵測（維持「繞過偵測」契約，不開 PROBE bridge）。
  - **fallback 視為暫時分類**：`profile_source == "fallback"` 是**未經量測**的結果
    （attach 當下板子還在噴 boot log、`last_probe_at` 為 `null` 就會掉進來），因此下一次
    attach 以獨立 PROBE bridge 再偵測一次（與 `_attach_by_id_dynamic` 對稱）。
    `yaml-target` / `sticky` / `detected` 是宣告過或量測過的，一律不重解析。
  - 偵測會送 `\r`，故 boot quiet window（#130 U-Boot autoboot 保護）內跳過；session 已有
    bridge 時也跳過（避免再開 PROBE bridge 造成 two-reader）。
- **`_rematerialize_profile_locked()`**：原地 mutate 既有 session 物件而非重建——
  `retained_consoles`（保留給 human minicom 的 PTY）、boot quiet 狀態等執行期欄位都掛在
  該物件上，重建會把正掛著的 human console 一併丟掉。保留 `com` / `act_no` /
  `device_by_id`；alias 只在仍是自動產生的 `<profile_name>+<act_no>` 形式時跟著改名，
  使用者自訂過的一律保留。`session_id` 依新 profile 名重算（`others-template:COM2` →
  `prpl-template:COM2`），並同步搬移 `_sessions`（以 comprehension 重建以保留插入順序，
  #186 的 tiebreak 看得到它）／`_binding_overrides`／`_reprobe_probe_locks`／
  `_loaded_released`／alias registry。新 session_id 已被別的 session 佔用時不動作，
  避免把兩個 session 併成一個。
- **`self-test` 給得出可執行的出口**：`profile_source == "fallback"` 的 PASSTHROUGH 分類
  改為把最近的 RX 餵給所有非 passthrough template 的 `prompt_regex`（`_suggest_profile_from_rx`，
  與 `serialwrap profile test` 同源的判斷），命中則回 `recommended_action: "pin_profile"`
  ＋ `suggested_profile` ＋可直接照抄的兩行 hint，而不是只回 `console_attach`。
  `others-template` 這類 passthrough template 的 `prompt_regex` 是 `.*`、恆真，一律排除在
  建議之外。顯式宣告的 passthrough（`yaml-target`，如 `uboot-template` target）不勸退。
- **`PROFILE_NOT_COMMAND_CAPABLE` 的 hint** 改為寫出可用的動詞（`session self-test` →
  `session pin` → `session clear`），並明講 `command_capable` 純粹由 `ready_probe` 決定、
  **與 session state 或 console 是否被佔用無關**——#181 的排查一開始正是被「COM2 有
  console 佔用所以不能下命令」帶偏（實測 COM1 同樣掛著 console 卻能正常 `cmd submit`）。
- **文件**：`README.md`（中英雙語）的 profile 解析優先序補上「每次 attach 都重新套用」與
  fallback 的暫時性；`session pin / unpin` 段落原本寫「對已存在的 session 下次 daemon
  重啟才生效」已過期，改為 `pin` + `session clear` 即可。`SKILL.md` 補 fallback 出口 SOP
  與 `command_capable` 的判準澄清。

**regression-case 評估**：新增 `tests/test_profile_reresolve_on_attach.py`（22 個 pytest，
修復前全數可重現地 FAIL）——涵蓋 pin 對既有 fallback session 生效、session_id 重算與
COM／act_no／alias 保留、pin 命中不開 PROBE bridge、跨重啟、`yaml-target` 不受影響、
未知 profile no-op、session_id 佔用時不合併、fallback 再偵測、偵測仍失敗時維持原狀、
boot quiet 內不偵測、`detected` 不重偵測、已有 bridge 時不偵測、`_attach_by_id` 確實接上
再解析，以及 `self-test` 的建議與 hint 文字。全部為 in-process session-state 與純函式
邏輯，unit/mock 已完整覆蓋，**不需**新增 `regression/`（TestPilot 實機）case——本修復
不依賴真板 boot 時序、USB 列舉順序或外部工具搶 tty。
