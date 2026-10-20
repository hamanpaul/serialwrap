---
name: serialwrap
description: 透過 serialwrap broker + CLI 進行多 agent UART 存取，提供單寫入者仲裁、RAW logging、session-safe 命令執行、device handoff 與 MCU 韌體燒錄。當任務涉及 UART 命令執行、UART 狀態蒐集、多 agent UART 協調、UART 證據匯出、device release/attach 把 raw UART 暫時交給外部工具（如 MCU 韌體燒錄），或需要在人類 minicom 與 agent 同時運作下保持一致視圖時觸發。
---

# serialwrap Agent Skill

> 本檔為 serialwrap agent 操作的**唯一權威來源**（CLI-first）。所有指令以 `serialwrap <group> <subcmd>` 表達；不存在 MCP tool。語意以 `README.md` 與 `openspec/specs/*` 為準。

## 目的
定義 Agent 在 UART 任務中使用 `serialwrap` CLI 的觸發條件、操作順序與安全邊界，避免直接碰觸實體 UART 造成資料失真或多 writer 衝突。serialwrap daemon（`serialwrapd`）對每條 UART 維持**唯一 reader/writer**，所有 agent 與人類 minicom 共用同一仲裁視圖。

## 何時該使用
- 需要多 Agent 共用同一 UART 並保證單寫入仲裁。
- 需要完整 RAW log（含 timestamp/source/cmd_id/seq/crc）做回放或稽核。
- 需要在人類 minicom／console 與 Agent 任務同時運作下保持一致視圖。
- 需要把 raw UART 暫時交給外部工具獨佔（如 MCU 韌體燒錄），完成後收回（device release/attach）。

## 何時不要使用
- 單次、一次性、無需追溯的本機 serial 測試。
- target 不經 UART 而經 SSH/ADB 等其他通道，且不需 UART 證據鏈。

## 前置條件
- `serialwrapd` 必須啟動（`serialwrap daemon status` 確認）。
- 目標 session 必須是 `READY`（或對 console-only profile 為 `ATTACHED`，見下方 command_capable）。
- profile 與 target 已綁定（`session bind` + `session attach` 至少完成一次）。

## 標準執行順序（Agent 必須遵守）
1. 健康檢查：`serialwrap daemon status`。
2. 探測資源：`serialwrap session list`、`serialwrap device list`。
3. 鎖定目標：`serialwrap session self-test --selector COM0`（回報 classification 與 recommended_action）。
4. 若 session 未 READY、tty 已變更、或 bridge stale：
   - tty 變更／需重綁：`serialwrap session bind --selector COM0 --device-by-id <by-id>` → `serialwrap session attach --selector COM0`。
   - 不健康／`TARGET_UNRESPONSIVE`：`serialwrap session recover --selector COM0`（非 device attach；可加 `--force` 做 clear+reattach+wait-ready）。
5. 提交命令：`serialwrap cmd submit --selector COM0 --cmd '<...>' --source agent:<name> --mode line`（必填 `--selector`，必填 `--source` 以維追蹤性）。
6. `line` 前景命令：`serialwrap cmd status --cmd-id <cmd_id>` 直接讀 stdout。
7. `background` 命令：`serialwrap cmd result-tail --cmd-id <cmd_id> --from-chunk 0 --limit 100` 增量取回 chunk。
8. `interactive` 任務：用 `session interactive-open/-send/-status/-close`，不要拿 `cmd submit` 硬跑全螢幕互動程式。
9. 需要完整證據時：`serialwrap log tail-raw --selector COM0`（預設 latest 模式回最新 N 筆；要增量讀取帶 `--from-seq N`，回應附 `last_seq`/`current_seq`/`truncated` 等 metadata，#124。注意 `truncated` 僅以現行 WAL 檔為範圍——輪替歸檔 `raw.wal.ndjson.<ts>` 不列入，需要更舊紀錄直接讀歸檔檔）／`serialwrap wal export`；只要查目前 WAL seq 用 `serialwrap wal current-seq`。
   - **`error_code: WAL_MISSING`（#189）**：已寫過紀錄但現行 WAL 檔不存在＝稽核紀錄在 daemon 運行期間被刪掉（常見於外部工具 rmtree 掉 WAL 目錄），該區段**無法復原**。不要當成「這段時間沒有輸出」。daemon 會在下一次寫入自動重建目錄續寫；用 `serialwrap doctor` 的 `wal_writable` 檢查確認目錄狀態。所有回應都帶 `wal_path` / `wal_file_exists`，可據此分辨「查得到但沒資料」與「檔案不見了」。

## command_capable 與 READY 判定（#51）
- 一個 session 是否「可下命令」由其 profile 的 `ready_probe` 是否非空決定（`command_capable = bool(profile.ready_probe.strip())`），**與底層是 OS shell 或 bootloader 無關**——只要 `prompt_regex` 對得上、`ready_probe` 能 round-trip 即可進 `READY`（含 U-Boot 之類 bootloader command profile，如 `uboot-template`）。
- **非 command-capable**（無 `ready_probe`，如 `others-template`、未設 `ready_probe` 的 passthrough）：session 維持 `ATTACHED`、**不**轉 `READY`；此時 `cmd submit` 回 `PROFILE_NOT_COMMAND_CAPABLE`。
- `command_capable` **純粹由 profile 的 `ready_probe` 決定**，與 session state 或 console 是否被佔用無關——COM 上掛著 human console 一樣能下命令，看到這個錯誤不要往「session 被佔用」排查（#181）。
- **卡在 `others-template` fallback 時的出口（#181）**：先 `serialwrap session self-test --selector <COM>`；若回 `recommended_action: "pin_profile"` 與 `suggested_profile`（daemon 拿最近 RX 比對過所有 template 的 `prompt_regex`），照它給的兩行套用即可：
  ```bash
  serialwrap session pin   --selector <COM> --profile <suggested_profile>
  serialwrap session clear --selector <COM>   # 下一次 attach 就套用，不必重啟 daemon
  ```
  `fallback` 是**未經量測**的暫時分類（attach 當下板子還在噴 boot log 就會掉進來），下一次 attach 本來也會自動再偵測一次；pin 則是繞過偵測的權威指定。`yaml-target` 的 session 不接受 pin（回 `PROFILE_IS_EXPLICIT`）。
- `SESSION_NOT_READY` 保留給「`command_capable` 為 True 但尚未通過 probe（仍 `ATTACHED`）」的情形——這種等 probe 完成或 `session recover` 即可。
- `serialwrap session self-test` 的最外層 result 直接帶 `command_capable` 欄位，不必鑽進巢狀 dict 判斷。

## 裝置 handoff（把 raw device 暫時交給外部工具，如 MCU 韌體燒錄）（#54）
當外部工具需要對同一 raw `/dev/ttyUSBx` 做**獨佔**存取（例如 `ocp-mcu-upgrade` 燒錄 MCU SBL 二進位協定）時，serialwrap 持有該 FD 會造成 two-reader 競爭、外部工具 timeout。**不要**用 `session clear`（detach 後會自動 re-attach 搶回），改用 first-class handoff：
1. `serialwrap device release --selector COM0 --source agent:flash --reason "flash MCU"`：serialwrap 關閉該 UART FD、clean-slate 清空 console、進入 `RELEASED`，**不會自動搶回**（跨 daemon 重啟亦保留）。
2. 外部工具獨佔該 raw device 完成燒錄。
3. `serialwrap device attach --selector COM0`：收回裝置並重建 primary console。外部仍持有時回 `DEVICE_STILL_HELD`（附 pids）；`--force` 只略過開檔前預檢，鎖後仍識別到 holder 時不會強制搶占，須等外部持有者釋放後再試。
4. 期間 `serialwrap session self-test --selector COM0` 在 `RELEASED` 會回 `external_holder` / `reclaimable` / `recommended_action`（`wait_external_flash` 或 `device_attach`）；屬唯讀偵測，不開 tty、不干擾燒錄。
5. 被遺忘的 release 可由 self-test 的 `reclaimable=true` 辨識（裝置不會永久卡住，但需明確 `device attach` 收回）。

## MCU 韌體燒錄端點 `/dev/ttyMCU`（#55）
serialwrap 另提供原生 MCU flash 端點（與上面 device handoff 互補）：daemon 持續維持 real tty 唯一 reader 的同時，提供 byte-transparent 的 `/dev/ttyMCU`（位於 daemon RUN_DIR 下）給外部 flasher 直接燒錄。
- 查詢可燒的 MCU 家族 pattern（family／probe／expect／baud）：`serialwrap mcu patterns`。
- 查詢 flash 端點狀態（候選 COM port 清單、目前 `is_flashing`、`last_detect`）：`serialwrap mcu status`。
- 燒錄由外部 flasher 直接打端點，例如：`ocp-mcu-upgrade -d <RUN_DIR>/dev/ttyMCU -b 115200 ... -i <fw.bin>`（期望 `Return error code : 0x0`）。
- 端點以非破壞性 **sync-probe** 自動認 MCU 線，偵測階段排除 `command_capable` console（避免燒到 DUT）。
- FLASHING 期間封鎖 console／interactive 注入（`cmd submit` 回 `FLASHING_BUSY`），燒完 session 自動恢復 `ATTACHED`。
- **重要**：清單／狀態查詢一律走 `serialwrap mcu patterns` / `serialwrap mcu status`，**不要**對 `/dev/ttyMCU` idle 寫入查詢字串（會被 flasher 讀成假回應、汙染 SBL sync）。

## human_active 與 soft-preempt（#53）
- serialwrap 追蹤人類在 console 的真實鍵入時間窗（`HUMAN_ACTIVE_WINDOW_S = 60s`）。`self-test` 最外層帶 `human_active`：僅在 human 已 attach 且最後鍵入仍在時間窗內為 `True`；「已 attach 但長時間 idle」不再被當成正在使用。
- `session interactive-open` 在 READY 路徑遇到既有 human lease 但 `human_active=False` 時，會把 human **soft-preempt 降級**（console 不中斷、其鍵入進 deferred buffer），agent 取得控制權並回 `soft_preempted`；agent `interactive-close` 後還原 human lease 並**回放** deferred buffer。human 仍 active 或既有為 agent lease 則維持 `SESSION_INTERACTIVE_BUSY`。
- 解決孤兒 minicom 假性佔用 console 導致 co-work 卡住的問題：死孤兒（console peer 已關）由 self-test 時 detach；活著但 idle 的 console 只降級、不自動 detach（清理交由 agent 主動 `session recover` / `session console-detach`）。
- bootloader recovery（#44）：板子掉進 bootloader 時 session 為 `ATTACHED`，可用 `serialwrap session interactive-open --selector COM0 --allow-attached`（需通過 bootloader prompt 比對）開 recovery lease；READY 下此 flag 無作用。
- bootloader recovery 於 **autoboot 倒數窗**（#114）：`--allow-attached` 的授予條件已擴充——除板子已停在 bootloader prompt（`=> `／`U-Boot> `）外，session 為 `ATTACHED` 且 RX tail 命中 boot banner（`Hit any key to stop autoboot` 倒數行／`U-Boot` 版本行）時也授予 lease，回應多帶 `boot_interrupt: true`。**用途（agent 自救壞 fw）**：燒壞 fw 後若板子會 autoboot 載入壞 image，在倒數窗 `interactive-open --allow-attached` 搶開 lease → 以 `interactive-send` 連打按鍵（如 space）中斷 autoboot 停在 `=> ` → 再逐字驅動 U-Boot 重燒。lease TX 不受 #130 boot quiet window gate，倒數窗內連打按鍵有效。

## FAQ：開機窗連不到 / broker not ready（#69）
- 先跑 `serialwrap session self-test --selector COM0` 與 `serialwrap session list`，看 `classification`、`state`、`last_error`、`reprobe_attempts`、`reprobe_exhausted`。
- `ATTACHED_NOT_READY` + `last_error=PROMPT_UNAVAILABLE` / `PROMPT_TIMEOUT`：通常是 attach 撞 DUT 開機窗、prompt 尚未出現；daemon 會等 RX 閒置後自動重探，成功會回 `READY`。
- `BRIDGE_DOWN` + `DETACHED` + `*_PROMPT_TIMEOUT`：若 device 還在位，daemon 會重新走 attach/probe 路徑。
- `last_error=RX_FLOOD`（#153）：console 正被大量輸出灌爆（session 的 `rx_bytes_last_10s` 超閾 ≥20000B/10s），probe 被洪水淹沒——**不是 console 死了**。self-test 亦回 `classification: "RX_FLOOD"`＋`recommended_action: "wait"`。**等排空勿重建 session**：洪水停止、RX 閒置 3s 後 daemon 自動重探升 `READY`；反覆 recover/attach 只會白等。以 `session list` 的 `rx_bytes_last_10s`／`rx_rate_bps` 觀測排空進度。
- `last_error=TRANSPORT_STALL`（#150）：TX 通、RX 凍——probe 全程連 echo 都沒收到、且 `last_rx_age_s` 已逾 30s。疑似 USB/usbip read-endpoint stall（host `dmesg` 常見 `urb stopped: -32`）。**serialwrap 的 recover／release+attach 無法自復**，需 host 層 USB re-enumeration（`last_error_detail` 附可複製的 authorized 0→1 toggle 指令或 usbipd detach/attach）。**勿先 power-cycle DUT**：也可能是 DUT 斷電/當機，先以 `dmesg` 佐證再動作。
- `last_error=CREDENTIALS_UNRESOLVED`（#140）：profile 宣告了帳密來源（`user_env`／`pass_env`／`env_file`）但解析為空（env_file 缺失／不可讀／缺 key）。這是**終態、不自動重探**——daemon 刻意不再對 login prompt 送空帳密，故反覆 `session recover` 不會成功。**排查**：profile YAML 內的相對 `env_file` 相對 **daemon profile-dir** 解析（systemd-system＝`/etc/serialwrap/profiles/`、pipx/XDG＝`~/.config/serialwrap/profiles/`），**不是** shell CWD 或你的 XDG config——log/WAL 警告會印出實際解析的絕對路徑與原因（不含帳密值）。把帳密檔補到該路徑後，手動 `serialwrap session attach`／`recover`（或重啟 daemon）即重試。
- `minicom_router.sh` 會提示「DUT 可能仍在開機、serialwrap 正在自動重探」；需要阻塞等 READY 時可設 `MINICOM_WAIT_READY=1`。
- 若 `reprobe_exhausted=true` 或等待過久仍未 READY，再手動 `serialwrap session recover --selector COM0`（必要時 `--force`）。
- 懷疑 RX 掉字／狀態被污染：可能是同機多開（two-reader）。`serialwrap daemon status` 的 `multi_open`／`foreign_holders` 欄位與 `serialwrap doctor` 的 `single_daemon` 檢查會掃 `/proc` 報出其他 `serialwrapd` 與 tty 持有者（#101，純偵測）。勿用 `serialwrap daemon start`（systemd 模式會另起非託管 daemon 造成 two-reader）；生命週期用 `serialwrap service ...`。

## Timeout 語意（#123）
- **呼叫端必須自行處理 timeout**：CLI 回 `TIMEOUT` 只代表「CLI 不再等待」，daemon 端操作可能仍在執行、稍後成功（host 過載時尤然）。看到 TIMEOUT 先讀附帶欄位再決定下一步，勿直接重送寫入類命令。
- **長操作自動固定 floor**：`session attach`／`session recover`／`session self-test`／`session console-attach`／`device release`／`device attach`／`session clear`／`session bind` 為 daemon 端阻塞方法（`session console-attach` 的 recover 升級分支可同步跑數十秒）；未指定全域 `--timeout` 時 CLI 自動採固定 45s 的 floor，一般方法維持 5s。顯式指定 `--timeout` 一律照用。floor 為誠實的寬鬆常數——CLI 無從得知 daemon 端 profile 的 `timeout_s`（bcm 類平台常 15s+、多階段 probe），不隨 recover/self-test 的子命令參數縮放（daemon 端對那些參數本就有 2s cap）；仍逾時時改看下一條的診斷欄位與 `session list`。
- **TIMEOUT 錯誤帶診斷欄位**：`daemon_reachable`（1s `health.ping` 探測）分辨「daemon 死亡／斷線」（false → 檢查 daemon／裝置）與「daemon 忙碌」（true → 操作多半仍在跑，稍候以 `session list`／`self-test` 確認結果）；可達時另附 `daemon_busy`（in-flight `commands`／`sessions` 計數）。
- **`--retries N` 僅作用唯讀方法**：只有冪等唯讀白名單（`session list`、`health.*`、`device list` 等查詢類）會在 TIMEOUT／連線失敗／`EMPTY_RESPONSE` 時指數退避重試（0.5s 起 ×2、單次上限 5s）；寫入類（attach／recover／submit…）絕不自動重送。白名單呼叫最壞總耗時約 `(retries+1) × timeout_s + 退避總和`。

## U-Boot autoboot 保護（boot quiet window，#130）
- 對 DUT 下 `reboot` 後 session 停在 `RECOVERING`／`ATTACHED`、且 `session list` 的 `boot_quiet_remaining_s` 有值：**這是正常的 autoboot 保護**，daemon 正在靜默等 DUT 開機（避免自動 probe 打斷 U-Boot autoboot 倒數把板子卡在 `=> `）。**等它自己回 `READY`**（RX 見 login/prompt 即解除、最長 180s），勿反覆下 `session recover`——反覆下也一樣會被 gate 擋下（見下一點），不會提早成功，只會浪費時間。
- 保護 gate 所有自動 probe/按鍵，**含手動觸發的 RPC**：`session attach`、`session recover`（兩者共用同一個 probe 入口）、`session self-test`（回報 `classification: "AUTOBOOT_QUIET"`）、命令逾時後的強制恢復按鍵，在 quiet window 內都會誠實回報「還在等」而不會送 bytes 進 UART。只有 human console bytes 與 interactive lease TX 永遠不受影響——刻意要進 bootloader（先送 reboot 再於 lease 連打按鍵）仍可行。
- **agent 顯式命令的過渡態拒絕（#139；#162 解除改綁 READY 再確認）**：`cmd submit`／`file push`／`file pull` 僅在「quiet 已 arm 且 session 尚未重新確認 `READY`」的過渡態（疑似板卡自發重開機、state 名義上停 `READY`）拿到**可重試的 `error_code: AUTOBOOT_QUIET`**（submit-time 即回、附 `retry_after_s`；或 queue race 時 `cmd status` 終態帶同碼）——命令未送 UART、零副作用，**等 daemon 重新確認 `READY` 後重送即可**，勿當永久失敗、勿反覆 recover。#162 起 quiet 視窗過期**不等於**放行：gate 由 daemon 的確認 probe（nonce）解除，過渡態可看 `session list` 的 `ready_reconfirm_pending`（pending-only 拒絕的 `retry_after_s` 固定 `5.0`，如實照等即可）。**但重試是有上限的**：再確認逾 300s／5 次仍不成功時，同樣三個命令改回**不可重試**的 `error_code: READY_UNCONFIRMED`（不帶 `retry_after_s`、帶 `recommended_action: "self_test"`，`session list` 的 `ready_reconfirm_failed` 為 `true`）。收到此碼請**停止重試迴圈**，改跑 `session self-test` 取分類——若板子卡在 bootloader 會回 `classification: "BOOTLOADER"` ＋ `recommended_action: "recover_interactive"`（session 的 `last_error` 為 `BOOTLOADER_STUCK`），再以 `interactive-open --allow-attached` 打 `boot` 脫困。刻意進 bootloader 的正路是 `interactive-open --allow-attached`（#114，不受 gate），不要用 `cmd submit --mode interactive`（quiet 內同樣會被拒）。
- 若板子已卡在 bootloader prompt（`=> `／`U-Boot> `）：prpl-template 有 `bootloader_prompts`，用 `serialwrap session interactive-open --selector COM0 --allow-attached` 開 recovery lease 打 `boot` 脫困。
- 若板子會 autoboot 載入壞 image、想**刻意中斷** autoboot（#114）：不必等它停在 `=> `——在倒數窗（RX 見 `Hit any key to stop autoboot`／`U-Boot` banner）即可 `serialwrap session interactive-open --selector COM0 --allow-attached` 搶開 lease（回應帶 `boot_interrupt: true`），再以 `interactive-send` 連打按鍵中斷 autoboot 停在 `=> ` 後救 fw。lease 送鍵不受 boot quiet window gate。

## Remote Support 用法（serialwrap remote，ssh-tunnel）

Agent 要從遠端操作本機 UART 時：**在 UART host（daemon 所在機）** 跑一行反向隧道，agent 端照舊用 `--endpoint`。daemon 不重啟、不做預設。

**責任邊界**：`serialwrap remote` 不做 NAT traversal，只在一個**本來就可達**的 SSH target 上管理 forward 的 lifecycle。可達性由外部提供——公網位址、公司 LAN、SSH jump host，或 overlay／private network（Cloudflare Zero Trust、Tailscale、WireGuard、ZeroTier、VPN）皆可。provider 專屬細節寫在 `ssh_config`（host alias、`ProxyJump`）或 `--ssh-opt`，**不要**去找 `--cloudflare`／`--tailscale` 這類旗標，serialwrap 刻意不提供。

```bash
# UART host（有 serialwrapd）：把本機 daemon 反向推到對端（-R 為預設）
serialwrap remote tester@AGENT_OR_RELAY:7777
```

Agent 端連線（依「誰連得到誰」擇一）：

- **direct**（agent host 就是上面 ssh 的對端）：直接
  `serialwrap --endpoint tcp://127.0.0.1:7777 session list`。
  兩端已有 overlay／private network 互相可達時，**即使都在 NAT 後也走這條**——`-R` 直接落在 agent 的 loopback，不需要 relay、不需要成對的 `-L`。
- **agent-pull**（反過來：**agent 連得到 UART host**，例如開發機要連一台在 NAT 後的 bench）：
  **UART host 端什麼都不用跑**，由 agent 主動把對方的 daemon socket 拉回自己的 loopback：
  ```bash
  serialwrap remote -L --remote-socket <UART host 的 serialwrapd.sock> tester@dut:7777
  serialwrap --endpoint tcp://127.0.0.1:7777 session list
  ```
  socket 路徑取自 UART host 上 `serialwrap daemon status` 的值；ssh 帳號要在擁有該 socket 的群組內（0660），否則 forward 建得起來但 `health.ping` 不通、readiness 停在 `starting`。此形狀**只有 `-L`、沒有 `-R`**，不適用「兩端須成對指定同一 `--remote-socket` 路徑」那條（那是 relay 硬化情境的規則）。
- **relay / 雙 NAT（fallback）**（兩端**完全**互不可達，各自對 relay 撥出）：agent 端先
  `serialwrap remote -L tester@RELAY:7777`（回傳 `endpoint`），再用該 endpoint。
  這是「無 overlay、無路由」時的退路，不是跨雙 NAT 的唯一解法。

**`BatchMode=yes` 為強制、`--ssh-opt` 蓋不掉**：不接受任何互動式認證，開隧道當下認證就必須已是非互動的。provider 憑證會過期時（如 Cloudflare Access token），過期會讓 ssh 直接失敗且無有用診斷——無人值守需用 service token 或事先 refresh。

**實例：Cloudflare 當 reachability provider**（雙 NAT、本機不裝 overlay、不自養 relay；agent 在本機、bench 在遠端）——bench 只跑 `cloudflared`、**不跑 `serialwrap remote`**：
- Quick Tunnel（無帳號無網域，hostname 每次重啟換）：bench `cloudflared --config <空檔> tunnel --url ssh://localhost:22`，抄它印出的 hostname（`*.trycloudflare.com`，**去掉 `https://`**——ssh 目標是 hostname 不是 URL）。**`--config` 指空檔不是可選的**：bench 若已跑 Named Tunnel，裸的 `tunnel --url` 會沿用 `/etc/cloudflared/config.yml` 的 `tunnel:`／`credentials-file:`，起出來的是「那條 Named Tunnel 的第二個 connector」而非 Quick Tunnel——照樣印 hostname，但該 hostname 一律 `websocket: bad handshake`，且 production hostname 會被分流到這個 replica。看啟動那行 `Settings: map[...]` 不可含 cred-file。2026-09-21 以 2026.9.1 實跑通過：純 ssh 約 2.4 秒、`remote -L` 一次 `active`（約 2.8 秒）、`cmd submit` 取得真實 UART 輸出。
- Named Tunnel 不開 Access（帳號裡有網域，hostname 固定）：bench 一次性 `cloudflared tunnel login` → `tunnel create <name>` → `tunnel route dns <name> <hostname>` → `~/.cloudflared/config.yml`（ingress `service: ssh://localhost:22`）→ `tunnel run <name>` 或 `sudo cloudflared service install`。兩個實跑坑（2026-09-16，cloudflared 2026.9.1）：`tunnel login` 的瀏覽器頁要**選 zone 再按 Authorize** cert.pem 才落地；`service install` 在 sudo 下 `~` 是 /root 找不到設定，要先把 `config.yml` 與 `<UUID>.json` 搬到 `/etc/cloudflared/` 再 `sudo cloudflared --config /etc/cloudflared/config.yml service install`。

本機兩條路線同一行（`cloudflared` 只是這條 ssh 的 per-connection helper，放 `--ssh-opt`、**不要**寫進 `~/.ssh/config`；`access ssh` 只是子命令名稱、走 Tunnel 就會用到，**不代表**啟用 Access 身份政策）：
```bash
serialwrap remote -L --autossh --remote-socket /run/serialwrap/serialwrapd.sock \
  --ssh-opt=-o --ssh-opt='ProxyCommand=cloudflared access ssh --hostname %h' \
  tester@<hostname>:7777
serialwrap --endpoint tcp://127.0.0.1:7777 session list
```
前提（bench）：sshd 只開金鑰（`PasswordAuthentication no`，hostname 對外可達）、本機公鑰在 `tester` 的 `authorized_keys`、`tester` 在 `dialout`（socket 0660）。`--remote-socket` 取 bench 的 `config.yaml` `socket_path`。**先用純 ssh 驗** `ssh -o 'ProxyCommand=cloudflared access ssh --hostname %h' tester@<hostname> hostname`，通了再接 serialwrap。不開 Access（其瀏覽器 token 會過期、撞 `BatchMode=yes` 靜默失敗）；日後要加請用 service token。完整兩條路線與對照表見 README「實例：以 Cloudflare 當 reachability provider」。

**bench 代號層（#203）**：上面那條長指令可以收成一個代號——一台 bench 一個不變的代號（AnyDesk 形狀）。

```bash
serialwrap connect eit-test                  # 依代號開隧道（內部展開成同一條 remote -L）
serialwrap --bench eit-test session list     # 用代號定址，免帶 --endpoint
serialwrap benches                           # 列代號 + 記住的 endpoint + alive
serialwrap connect eit-test --close          # 拆隧道並清 endpoint 記憶
```

`benches.yaml`（`~/.config/serialwrap/benches.yaml`，`SERIALWRAP_BENCHES_FILE` 可覆寫）每個 bench **五個欄位全部必填**：`target`（`user@host`，**帳號由 host 端自己維護，serialwrap 不推導也不代入當前使用者**）、`remote_socket`（bench 上的 socket 路徑）、`local_port`、`ssh_opts`（字串陣列，原樣傳 ssh）、`autossh`（布林）。loader **拒絕**這五個以外的任何欄位——寫 `cloudflare:`／`tailscale:` 是載入錯誤，schema 在結構上就 provider-neutral（#185）。

endpoint 記憶在 `$XDG_STATE_HOME/serialwrap/benches.state.json`。端點解析優先序 `--endpoint` > `--socket` > `--bench` > config fallback；**未 connect 的代號回明確錯誤、不會靜默 fallback 到本機 daemon**（否則會把命令送錯機器）。

佈署腳本在 `tools/`（provider-specific，刻意不進 CLI）：`tools/bench-issue.sh --domain <domain> <code>`（管理端建 tunnel／設 DNS／產 `handoff/<code>/` bundle／寫 benches.yaml；沒給 domain 即非零退出）、`sudo tools/bench-enroll.sh --bundle <dir>`（bench 端裝 cloudflared／佈署 `/etc/cloudflared`／裝服務／sshd 金鑰限定並 **reload 不 restart**／追加公鑰／跑 CP-1 journal `Registered tunnel connection` 與 CP-2 self-ssh）。bench 端因此**不需要瀏覽器登入、也拿不到管理端的 `cert.pem`**。細節見 README「bench 代號」與「發代號與入列」。

管理：`serialwrap remote`（列隧道）、`serialwrap remote close 7777|all`（拆除）。
回傳 `status`：`active`＝就緒可用；`starting`＝尚未確認（慢速認證／上游未就緒），需再 `remote status` 或重試。

安全：隧道讓對端全權操控 DUT。**只用單租戶／可信 relay**；共享 relay 加 `--remote-socket /path`（遠端改建檔案權限把關的 unix socket）。`-R` tcp 模式若偵測遠端被 `GatewayPorts` 綁到對外，會拒絕（`REMOTE_BIND_UNVERIFIED`）。

### `--remote-socket` 模式：agent 端連線格式與已知落差

硬化後對端**不開 TCP port**，只落地一個檔案權限管控的 unix socket。agent 端連線指令要相應改為：

```bash
serialwrap --endpoint unix:///path/to.sock session list
```

**已知落差**：`serialwrap remote` 回傳 JSON 裡的 `remote_hint` 欄位固定顯示 `tcp://127.0.0.1:<port>`，**不會**因為用了 `--remote-socket` 而更新——照抄會連不上（該 port 根本沒開）。硬化模式下請忽略 `remote_hint`，改用 `unix://` + 該次回傳的 `remote_bind` 欄位組出正確 endpoint。

### 給「隧道對端」agent 的操作提醒

若你是被交接 `--endpoint`（tcp 或 unix）的那一端，而非起隧道的那一端：

- 每筆命令帶 `--source agent:<your-name>`，方便回溯是誰下的。
- 這條隧道等同對端全權操控 DUT／STA（`command.submit`／`file.push`／`daemon.stop` 皆可達，daemon 不做額外身分驗證，安全性完全靠 ssh）。共用硬體時，動作前先 `session self-test` 確認目前狀態、無人在用再下手。
- 隧道是背景 ssh process，不是常駐服務；UART host 重開機或該 ssh process 被殺掉都會讓連線失效。你這端沒有權限重建，遇到 `SOCKET_ERROR` 或連不上時回報給 UART host 那邊的操作者，不要自行嘗試繞過。
- 不要直接碰 `/dev/ttyUSB*`；一切透過 `serialwrap` CLI 走 broker 仲裁（見上方「安全規則」）。

## Event Trigger Engine
用於 UART RX 觸發自動化 handler。**先呼叫 `serialwrap event status`** 確認狀態再 enable/disable，避免假設狀態。
```bash
# 1. 寫 rule 檔案（JSON），含 schema_version/owner/name/kind/selectors/pattern/handler
# 2. 載入
serialwrap event add --file /path/to/rule.json
# 3. 確認狀態（必須在 enable/disable 之前）
serialwrap event status --selector COM0
# 4. 若未啟用，手動啟用
serialwrap event enable --selector COM0
# 5. 查看 fire 記錄
serialwrap event tail --rule-id owner.name -n 20
```
- `auto_enable_com_on_load: true` 的規則在 daemon 重啟後，對具體 COM selector 自動啟用；`selectors: ["ALL"]` 只在 daemon 啟動時展開到當下已知 COM（event engine 啟動早於 device watcher／bootstrap_attach，實務上只含 YAML explicit target），之後熱插或動態偵測的 COM 須手動 `serialwrap event enable --selector COMx`。
- Counter 與 event log 位於 `STATE_DIR/events`（非 tmpfs，主機重開機後保留）；counter 由 disable/delete/reset 清除，daemon 重啟後 counter 保留。

## 透過 UART 推送／拉取檔案
```bash
serialwrap file push --selector COM0 --local ./probe.sh --remote /tmp/probe.sh --source agent:diag
serialwrap file pull --selector COM0 --remote /etc/config/wireless --local ./wireless.bak --source agent:diag
```
- `--remote`／`--local` 必填於 push；pull 省略 `--local` 時寫到 daemon 工作目錄的 `basename(remote)`，回應附 `local_path`。
- `--chunk-size` 預設 512（push）。
- remote 模式下 `--local` 指 daemon 所在 host 的路徑。
- **適用範圍＝小檔（設定檔、腳本、探針等，數十 KB 內）**。UART 115200 baud 的原始上限約 11 KB/s，echo-ACK 節流後更低（1 MB 約 10–17 分鐘）——`file push` 的設計目標本來就不是大檔。
- **大檔走網路通道，不走 UART**：firmware image 這類檔案，只要 DUT 有 SSH／TFTP／HTTP 可達，一律用 SCP／TFTP 傳輸，serialwrap 只負責控制面（以 `cmd submit` 觸發板端的 `scp`／`tftp`／`wget` 並觀察結果）。實測一顆 81 MB image 走 SCP 經 LAN 數秒完成；同一顆走 UART 即使通道完全穩定也需兩小時以上。
- `file push` 用在大檔只能是**確認 DUT 無任何網路通道後**的明確 fallback，並在紀錄裡說明原因。

## 安全規則
- 禁止 Agent 直接寫 `/dev/ttyUSB*` 或 `/dev/ttyACM*`。
- 禁止繞過 broker 自行開多個 serial writer。
- 外部工具需要獨佔 raw device 時，用 `device release` / `device attach` 正式交接（或走 `/dev/ttyMCU` 燒錄端點），**不要**直接 kill daemon 或用 placeholder bind 繞過。
- 長流命令（`logread -f`、`tcpdump`、kernel debug）一律用 `--mode background` 或設足夠長的 `--cmd-timeout`，避免阻塞共享通道。
- 每筆自動化命令必填 `--source`，不可省略，確保追蹤性。
- 卡住時先 `serialwrap session self-test`，再決定是否 `serialwrap session recover`（可加 `--force`）。recover 成功恢復 prompt 時回 `ok: true`（附 `error_code: PROMPT_TIMEOUT_RECOVERED`, `partial: true`），表示 session 可繼續使用。
- session 於命令排隊期間發生 recovery/re-attach（掉出 `READY`）時，尚未啟動的排隊命令會被終結為 `status=error`、`error_code=FLUSHED_BY_RECOVERY`（#128）——代表該命令**未執行**，等 session 回 `READY` 後重送即可；執行中的命令不受影響，仍以真實結果收尾。所有 detach 類路徑（含 clear/release/熱拔/re-attach）皆用 `FLUSHED_BY_RECOVERY`；daemon shutdown 則用 `FLUSHED_BY_SHUTDOWN`，語意相同＝命令未執行、可於 `READY` 後重送。

## 短命令原則（Best Practice）
- **避免 heredoc**：heredoc 經 UART 傳輸時容易遺失字元或打亂 prompt，改用 `echo ... > file` 分步寫入。
- **單行盡量短**：每條命令控制在 2 KB 以內；UTF-8 位元組 > 4 KB 會 warning、> 16 KB 會被 reject（`CMD_TOO_LONG`）。命令不得含 `\n` 換行字元，否則回 `CMD_CONTAINS_NEWLINE`。broker 不截斷；但 broker 上限與 target 端 tty line buffer（常見 4096 bytes）的物理單行限制是兩回事，過長單行仍可能在 target 端被截斷。上限可由 `serialwrap daemon status` 的 `limits` 欄位執行期查詢，不需硬編碼。
- **避免 base64 inline**：不要將整個檔案 base64 編碼塞進 `cmd submit`。小檔（數十 KB 內）改用 `serialwrap file push`；firmware image 這類大檔在 DUT 有網路通道時一律走 SCP／TFTP，不要交給 `file push`（界線見「透過 UART 推送／拉取檔案」）。
- **長命令拆分**：管線命令過長時，先寫成 script 檔再 `source` 或 `sh /tmp/script.sh`。
- **長命令 keepalive**：長時間命令加 `--expected-duration <秒>`，broker 在此期間暫停 prompt timeout 並監控 RX 活動延長等待，避免誤判 PROMPT_TIMEOUT。
- **line vs background**：`line` 命令直接看 `cmd status`，不要用 `cmd result-tail`（那是給 `background` capture 用的）。

## 最小可用範例
```bash
serialwrap daemon status
serialwrap session self-test --selector COM0
serialwrap cmd submit --selector COM0 --cmd 'ifconfig' --source agent:diag --mode line --cmd-timeout 10
serialwrap cmd status --cmd-id <cmd_id>
serialwrap cmd submit --selector COM0 --cmd 'logread -f' --source agent:diag --mode background --cmd-timeout 300
serialwrap cmd result-tail --cmd-id <cmd_id> --from-chunk 0 --limit 100
serialwrap log tail-raw --selector COM0 --limit 200   # latest 模式（預設）：最新 200 筆；--from-seq N 走 range 增量
serialwrap wal current-seq
serialwrap device release --selector COM0 --source agent:flash --reason "flash MCU"
serialwrap device attach --selector COM0
serialwrap mcu status
```

## 錯誤碼與 last_error 處置目錄（自動產生；勿手改）

本節由 `python3 -m tools.contracts skill-section` 依 `contracts/error_codes.yaml` 與 `contracts/session_last_errors.yaml` 產生；`error_code` 與 session `last_error` 是兩個不同命名空間。修改請改 `contracts/` 後重新產生。

### RPC／CLI error_code

<!-- BEGIN: generated-fact marker="error-codes" -->
| code | 介面／平台 | 可重試 | 意義 | Agent 處置 |
|---|---|---|---|---|
| `ALIAS_NOT_FOUND` | rpc／posix、windows | 修正參數後 | 要移除的 alias 不存在 | 以 session list 確認 alias 名稱後再重試 |
| `AUTOBOOT_QUIET` | rpc、rec／posix、windows | 是 | session 名義為 READY，但 boot quiet 視窗進行中或 READY 再確認尚未完成 | command.submit／file.* 直接拒絕依 retry_after_s 等待後重送；execute-time command.get 紀錄未帶 retry_after_s 時每 5 秒重試 |
| `BASE64_DECODE_FAILED` | rpc／posix、windows | 視情況 | 擷取到的 base64 內容無法解碼 | 檢查目標端 base64 工具與擷取內容後再重試 |
| `BENCH_ENDPOINT_NOT_REMEMBERED` | cli_local／posix、windows | 修正參數後 | --bench 指定的 bench 尚未以 connect 記住 endpoint | 先執行 serialwrap connect 記住該 bench 的 endpoint 後再重試 |
| `BENCH_STATE_IO_ERROR` | cli_local／posix、windows | 視情況 | benches.state.json 讀寫失敗（OS 錯誤） | 檢查 benches.state.json 的權限與磁碟狀態後重試 |
| `CHECKSUM_MISMATCH` | rpc／posix、windows | 視情況 | 傳輸後 md5 校驗值不符 | 重新傳輸；若持續不符，檢查來源檔與目標端儲存狀態 |
| `CHECKSUM_VERIFY_FAILED` | rpc、cli_local／posix、windows | 視情況 | 目標端 md5sum 無回應或無法擷取校驗值；Rust setup phases: rollback_verify、update_verify、install_recover | 確認目標端具備 md5sum 工具後重試；若為 setup bundle 驗證失敗則確認 release manifest 與 bundle 檔案完整性 |
| `CMD_CONTAINS_NEWLINE` | rpc／posix、windows | 修正參數後 | 命令字串含換行字元，拒絕送入 UART | 移除命令中的換行並以單行命令重送 |
| `CMD_NOT_CANCELABLE` | rpc／posix、windows | 否 | 命令已是 done 或 error 狀態，無法取消 | 改以 command.get 讀取既有結果，不需取消 |
| `CMD_NOT_FOUND` | rpc／posix、windows | 否 | cmd_id 不存在（已被淘汰或從未提交），或 result_tail 無法取得該命令的結果 | 確認 cmd_id 來源；命令紀錄只保留有限筆數，紀錄不存在不代表命令未執行，不得因此重送；先以 session self-test、log tail 或 WAL 查證目標狀態，是否重新執行由操作者決定 |
| `CMD_TOO_LONG` | rpc／posix、windows | 修正參數後 | 命令的 UTF-8 長度超過硬上限 16384 bytes，拒絕提交 | 縮短命令內容；超過 4096 bytes 的軟上限仍會成功，但會附 CMD_LENGTH_WARNING 警告（非錯誤碼） |
| `COMMAND_ABORTED_IN_FLIGHT` | rec／posix、windows | 否 | 已確認至少寫出 1 byte 後，因 release 或 recovery 而中止 | 命令已部分送出：勿重送；先以 session self-test 確認目標狀態 |
| `COMMAND_FAILED` | rec／posix、windows | 否 | 命令結果為 ok:false 但未帶 error_code 時的後備碼 | 視為失敗，先以 session self-test 確認狀態 |
| `CONSOLE_LINE_LIMIT_TOO_SMALL` | rpc／posix、windows | 否 | profile 的 max_console_line_chars 太小，放不下傳輸必要的命令 | 調大 profile 的 max_console_line_chars 後重試 |
| `CONSOLE_NOT_FOUND` | rpc／posix、windows | 否 | 指定的 console client_id 不存在 | 以 console list 取得現存 client_id 後再操作 |
| `DAEMON_ALREADY_RUNNING` | proc／posix、windows | 否 | daemon 啟動時發現同一實例鎖已被持有，或 socket 已可連線（非 JSON 回應，以 stderr 行與 exit 2 表示） | 勿重複啟動；以 serialwrap daemon status 確認既有實例 |
| `DAEMON_ALREADY_RUNNING_ELSEWHERE` | cli_local／posix | 否 | /proc 發現另一個 serialwrapd 綁定不同 socket（未使用 --force-spawn） | 改用既有 daemon 的 socket，或先停止另一個實例後再啟動 |
| `DAEMON_BINARY_NOT_FOUND` | cli_local／posix、windows | 修正參數後 | Windows 後端找不到 serialwrapd 可執行檔 | 確認 serialwrapd 已正確安裝且位於預期路徑後重試 |
| `DAEMON_EXITED` | cli_local／posix、windows | 視情況 | spawn 後 daemon 行程在就緒前即退出；Rust setup phase 包含 update_start_and_verify | 查看 daemon 的 stderr 並排除原因後再執行 daemon start |
| `DAEMON_NOT_READY` | cli_local／posix、windows | 視情況 | spawn 後等待時間內 health.ping 未成功；Rust setup phase 包含 update_start_and_verify | 以 serialwrap daemon status 確認是否已就緒；未就緒時檢查 daemon 行程，勿盲目重複 spawn |
| `DEVICE_ALREADY_BOUND` | rpc／posix、windows | 修正參數後 | 指定的 by_id 已綁定到其他 session | 以 session list 確認該裝置目前所屬 session，改用其他 by_id 或先解除既有綁定 |
| `DEVICE_NOT_BOUND` | rpc／posix、windows | 修正參數後 | session 未設定 device_by_id，無法 attach | 先以 session.bind 綁定裝置後再 attach |
| `DEVICE_NOT_FOUND` | rpc／posix、windows | 視情況 | 指定的裝置或 selector 解析不到對應裝置 | 以 device list 確認裝置在線且 by_id 正確，裝置出現後再 attach |
| `DEVICE_NOT_PRESENT` | rpc／posix、windows | 視情況 | by_id 不在目前的裝置清單中（未插入或尚未列舉） | 確認裝置已插入且 device list 可見後重試 |
| `DEVICE_STILL_HELD` | rpc、cli_local／posix、windows | 視情況 | UART 獨占開啟或 holder 預檢確認裝置仍被占用；setup phases: update_safety_check、update_confirm_released、rollback_verify、install_recover、supervision_set_safety_check、supervision_set_release | 等待已識別的外部持有者釋放後重試；force 只略過開檔前預檢，不會繞過實際開檔或鎖後 holder 掃描 |
| `ECHO_ACK_UNSUPPORTED` | rpc／posix、windows | 修正參數後 | ack_mode 為 echo，但 bridge 不支援 echo 節流送出 | 改用其他 ack_mode 重試 |
| `EMPTY_RESPONSE` | cli_client／posix、windows | 視情況 | 連線已關閉但未收到任何一行回應 | 僅白名單唯讀方法可由 CLI 重試；其他方法先以 health.ping 確認 daemon 狀態 |
| `ENV_FILE_SOURCE_FAILED` | cli_local／posix、windows | 修正參數後 | env 檔以 bash -lc source 執行失敗（非 0 退出），或 Windows 讀取檔案時發生 OS 錯誤 | 確認 env_file 路徑與內容後重試 |
| `EXCEPTION` | rpc_frame／posix、windows | 否 | handler 拋出未預期例外，framing 層以 message 回傳（回應無 version） | 視為結果不明：先以 command.get 或 session get_state 確認狀態，不可假設未執行而直接重送 |
| `FLASHING_BUSY` | rpc、rec、cli_local／posix、windows | 是 | session 處於 FLASHING 或 flash 臨界區，命令或操作被拒；Rust phase 包含 update_safety_check、supervision_set_safety_check | 等待燒錄結束（session 回到 ATTACHED 或 READY）後再重試 |
| `FLASH_TARGET_IS_CONSOLE` | rpc／posix、windows | 修正參數後 | 燒錄目標是可下命令的 console 裝置，且未指定 force，拒絕執行 | 改選非 console 的燒錄目標；此碼目前 production 不可達，僅作為契約保留 |
| `FLUSHED_BY_RECOVERY` | rec／posix、windows | 是 | 命令尚未執行即因 recovery 或 detach 被清空（未送出，可在 READY 後重送） | 確認 session 已回到 READY 後才可重送 |
| `FLUSHED_BY_SHUTDOWN` | rpc、rec／posix、windows | 是 | daemon 停止時，尚未執行的排隊命令被清空（未送出，可重送）；原子交接凍結後的有副作用 RPC 回 reason:"handoff_frozen" | daemon 重新啟動並確認 session 為 READY 後才可重送 |
| `FORCE_RECOVER_TIMEOUT` | rpc／posix、windows | 視情況 | 強制 recover（clear 後重新 attach）在 10 次、每次 1 秒的窗口內未達 READY | 以 session get_state 確認目前狀態後，再決定重試 recover 或處理裝置 |
| `INTERACTIVE_EXPIRED` | rpc／posix、windows | 否 | 互動 lease 已逾時失效 | 重新 interactive open 取得新的 lease 後再送出 |
| `INTERACTIVE_NOT_FOUND` | rpc／posix、windows | 否 | interactive_id 不存在，或 lease 狀態已不是 active | 重新 interactive open 取得 interactive_id |
| `INTERNAL_ERROR` | cli_local／posix、windows | 否 | CLI 本地流程發生未預期例外；Rust CLI phase 包含 service_disable、service_enable、service_restart_enable、autostart_lock、update_journal、update_autostart_hold、update_autostart_release、install_recover、update_start_and_verify | 回報例外訊息；勿假設操作已失敗而重送寫入型命令 |
| `INVALID_ARGS` | rpc、cli_local／posix、windows | 修正參數後 | 參數缺漏或型別／值不合法（RPC 分派或 CLI 參數驗證） | 依 CLI 或 RPC 說明修正參數後重送 |
| `INVALID_BENCH_CONFIG` | cli_local／posix、windows | 修正參數後 | benches.yaml 驗證失敗 | 修正 benches.yaml 的欄位後重試 |
| `INVALID_BENCH_STATE` | cli_local／posix、windows | 修正參數後 | benches.state.json 結構錯誤 | 檢查 benches.state.json 內容，結構損壞時需人工修復後重試 |
| `INVALID_COMPLETION_REGEX` | rpc、rec、cli_local／posix、windows | 修正參數後 | completion_regex 驗證失敗（空字串、超過 1024 bytes、無法編譯，或可匹配空字串） | 修正 completion_regex 後重送 |
| `INVALID_ENDPOINT` | cli_client、cli_local／posix、windows | 修正參數後 | endpoint 格式錯誤（tcp 缺 host 或 port、unix 路徑非絕對、未知 scheme） | 改用 tcp://host:port 或絕對 unix socket 路徑格式後重試 |
| `INVALID_JSON` | rpc_frame／posix、windows | 修正參數後 | 該行不是合法 JSON，連線保持並續讀下一行（回應無 id 與 version，且為非緊湊分隔符） | 修正請求的 JSON 格式後重送 |
| `INVALID_METHOD` | rpc_frame／posix、windows | 修正參數後 | 請求的 method 欄位不是字串（回應保留 id，無 version） | 以字串形式的 method 重送 |
| `INVALID_REQUEST` | rpc_frame／posix、windows | 修正參數後 | 請求 JSON 不是 object（回應無 id 與 version，且為非緊湊分隔符） | 以 JSON object 格式重送 |
| `INVALID_RESPONSE` | cli_client／posix、windows | 否 | daemon 回應的 JSON 不是 object | 勿直接重送寫入型命令；先以 health.ping 與 session get_state 確認 daemon 與 session 狀態 |
| `INVALID_RULE_SCHEMA` | rpc／posix、windows | 修正參數後 | 事件規則的 schema 驗證失敗 | 依 schema 修正 rule 欄位後重送 event.rule_set |
| `INVALID_TARGET` | cli_local／posix、windows | 修正參數後 | remote target 不是 [user@]host:port 格式，或 port 超出範圍 | 修正 target 格式後重試 |
| `LOCAL_FILE_NOT_FOUND` | rpc／posix、windows | 修正參數後 | daemon 端看不到 local_path（以 daemon 身分判斷檔案是否存在） | 確認路徑存在且 daemon 具讀取權限後重試 |
| `LOG_OPEN_FAILED` | rpc／posix、windows | 視情況 | session log 檔案開啟失敗（OS 錯誤） | 檢查 log 目錄權限與磁碟空間後重試 |
| `METHOD_NOT_FOUND` | rpc／posix、windows | 否 | method 不存在 | 改用 serialwrap 支援的 method 名稱重送 |
| `MOVE_FAILED` | rpc／posix、windows | 視情況 | mv 之後未見 sentinel，無法確認檔案搬移結果 | 先確認目標檔與暫存檔狀態，再決定是否重試 |
| `MOVE_TIMEOUT` | rpc／posix、windows | 視情況 | mv 之後 prompt 逾時，搬移結果不明 | 先確認目標檔與暫存檔狀態，再決定是否重試 |
| `NEEDS_SUDO` | cli_local／posix | 修正參數後 | systemd-system 的 start、stop 或 restart 未帶 --with-sudo；windows-service 的 service_disable phase 未帶 --with-sudo；Rust setup phase 包含 update_autostart_hold | 加上 --with-sudo 後重試（提權動作需人工確認） |
| `NOT_READY` | rpc／posix、windows | 視情況 | session 為 command-capable 且狀態非 READY／ATTACHING，又無 last_error 可回報 | 等待 session 進入 READY 後重試；必要時以 session self-test 取得分類 |
| `NO_ACTIVE_CAPTURE` | rpc／posix、windows | 否 | session 沒有進行中的 log capture | 確認是否已執行 log start；若無進行中的 capture 則無需 stop |
| `NO_SYSTEMD` | cli_local／posix、windows | 修正參數後 | on-demand 或未知的監管模式下呼叫 service 管理動作 | 依 supervision_mode 改用正確的 daemon 管理方式（on-demand 使用 daemon start 與 daemon stop） |
| `PROFILE_IS_EXPLICIT` | rpc／posix、windows | 修正參數後 | session 的 profile 來源為 yaml-target，不可 pin 或 unpin | 改為修改 YAML target 設定，而非使用 pin 或 unpin |
| `PROFILE_NOT_COMMAND_CAPABLE` | rpc／posix、windows | 否 | session 為 ATTACHED 且 profile 不支援命令（command_capable 為 false） | 改用 command-capable 的 profile，或改以 console 進行互動操作 |
| `PROMPT_TIMEOUT` | rpc、rec／posix、windows | 否 | 命令逾時，且 CTRL-C 與 CTRL-D 都未能救回 prompt | 命令結果不明：先以 session self-test 與 session get_state 確認狀態，再由操作者決定是否重送 |
| `PROMPT_TIMEOUT_FORCE_RECOVERED` | rpc／posix、windows | 否 | 命令逾時後，經 force clear 與重新 attach 成功（ok:true 回應，非失敗） | 命令已執行，勿重送；以 session get_state 確認當前狀態 |
| `PROMPT_TIMEOUT_RECOVERED` | rpc、rec／posix、windows | 否 | 命令逾時後 CTRL-C 或 CTRL-D 救回 prompt（ok:true 回應）；命令紀錄不寫入此碼，僅 capture 保留 | 命令已中止且不會自動重送；依 stdout 判斷結果，必要時由操作者決定是否重送 |
| `PULL_PARSE_FAILED` | rpc／posix、windows | 視情況 | 擷取輸出中找不到 sentinel 區段，且無 encoder 失敗 sentinel | 確認目標端 encoder 輸出正常後重試 |
| `READY_UNCONFIRMED` | rpc、rec／posix、windows | 否 | READY 再確認逾 300 秒或已達 5 次仍未確認，拒絕 agent 命令 | 勿重送；執行 serialwrap session self-test 取分類，再依分類處理 |
| `REMOTE_BIND_UNVERIFIED` | cli_local／posix、windows | 視情況 | expose tcp 模式下 relay 的 ss 驗證失敗，或綁定位址非 loopback | 確認 relay 僅綁定 loopback 位址後重試 |
| `REMOTE_NOT_SUPPORTED` | cli_local／posix、windows | 修正參數後 | daemon start 不支援遠端 endpoint；Windows 僅接受 loopback endpoint | 在本機啟動 daemon；Windows 若使用 TCP endpoint，請指定 loopback 位址 |
| `RULE_NOT_FOUND` | rpc／posix、windows | 否 | 指定的 rule_id 不存在 | 以 event rule list 確認 rule_id 後重試 |
| `SAMPLE_READ_FAILED` | cli_local／posix、windows | 修正參數後 | profile test 讀取樣本檔失敗 | 確認樣本檔路徑與讀取權限後重試 |
| `SEND_FAILED` | rec／posix、windows | 否 | send 回呼拋出例外，無法確定命令是否已送出（結果不明） | 勿自動重送；先以 session self-test 與 command.get 確認狀態，再由操作者決定 |
| `SESSION_BUSY` | rpc、rec、cli_local／posix、windows | 是 | 同一 session 的前景操作仍在進行中；setup phases: update_safety_check、update_confirm_released、update_revert、rollback_verify、install_recover、supervision_set_safety_check | 等待前景操作完成後重試 |
| `SESSION_INTERACTIVE_BUSY` | rpc、rec／posix、windows | 是 | 已有不可搶占的 interactive lease，或 SC-13 到期收尾的 ownership restore 尚未完成；相關操作被拒 | 等待 interactive lease 結束或到期收尾完成後重試 |
| `SESSION_NOT_FOUND` | rpc／posix、windows | 修正參數後 | selector 未對應任何 session | 以 session list 確認 selector 正確後重送 |
| `SESSION_NOT_READY` | rpc、rec／posix、windows | 視情況 | session 狀態不符（非 READY，或 bridge 不存在） | 等待 session 進入 READY 後重試；必要時以 session self-test 檢查狀態 |
| `SESSION_QUEUE_FULL` | rpc／posix、windows | 是 | session 已接受或執行中的命令（含已 cancel 但 worker 仍執行）達 256 筆上限 | 等待排隊命令完成後重試 |
| `SESSION_RECOVERING` | rpc、rec／posix、windows | 是 | session 恢復中（RECOVERING），操作暫不可用 | 等待恢復完成（session get_state 不再顯示 RECOVERING）後重試 |
| `SOCKET_ERROR` | cli_client／posix、windows | 視情況 | 連線失敗（connect 或 send／recv 的 OS 錯誤，或平台不支援 AF_UNIX） | 僅白名單唯讀方法可由 CLI 自動重試；其他方法先以 health.ping 確認 daemon 狀態再決定 |
| `SOCKET_UNAVAILABLE` | proc／posix | 否 | socket 路徑存在但 connect 失敗（非 ENOENT 與 ECONNREFUSED），非 JSON 回應，以 stderr 行與 exit 2 表示 | 檢查 socket 路徑的權限與殘留檔案後重試 |
| `SSH_NOT_FOUND` | cli_local／posix、windows | 修正參數後 | PATH 中找不到 ssh 或 autossh | 安裝 OpenSSH 客戶端（ssh 或 autossh）並確認 PATH 後重試 |
| `STATE_CHANGED` | rpc／posix、windows | 視情況 | probe 期間 session 狀態被其他操作改變（進入 FLASHING 或 RELEASED） | 以 session get_state 確認當前狀態後再決定是否重試 |
| `TARGET_DECODER_FAILED` | rpc／posix、windows | 視情況 | chunk 的 sentinel 未見，目標端 decoder 失敗 | 重新傳輸；若持續失敗，檢查目標端 decoder |
| `TARGET_DECODER_MISSING` | rpc／posix、windows | 否 | 目標端沒有 base64 或 openssl decoder | 在目標端安裝 base64 或 openssl 後再重試 |
| `TARGET_ENCODER_FAILED` | rpc／posix、windows | 視情況 | 目標端 encoder 回報失敗（encoder failure sentinel 出現） | 檢查目標端 encoder 後重試 |
| `TARGET_ENCODER_MISSING` | rpc／posix、windows | 否 | 目標端沒有可用的 encoder | 在目標端安裝 encoder 後再重試 |
| `TEST_DEVICE_ALLOWLIST_REJECTED` | proc／posix、windows | 修正參數後 | 隔離測試實例的裝置 allowlist 判定不通過（非隔離實例設定了 allowlist）；stderr 印出 serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED 與原因，exit 2 | 移除 allowlist 設定，或改在三個隔離目錄均明確設定的實例中使用 |
| `TIMEOUT` | cli_client、cli_local／posix、windows | 視情況 | 連線、傳送或接收逾時，或 daemon 管理操作等待舊行程結束逾時；CLI phase 包含 restart_wait_old_exit、reconcile_wait_old_exit、service_stop_wait_stop、service_restart_wait_stop、autostart_lock、install_lock、update_confirm_released、update_start_and_verify、update_autostart_hold、update_revert、install_recover、supervision_set_safety_check；白名單唯讀方法可重試，其他方法不可直接重送 | 非白名單方法不得直接重送；先以 health.ping 與回應中的 daemon_busy 判斷 daemon 狀態 |
| `TRANSFER_ECHO_STALL` | rpc／posix、windows | 是 | echo-ACK 路徑 echo 停滯；換行未送出，命令未執行 | 命令未執行，可重新傳輸 |
| `TRANSFER_PROBE_FAILED` | rpc／posix、windows | 視情況 | 工具探測命令回報 shell 錯誤 | 確認目標端工具可用後重試 |
| `TRANSFER_TIMEOUT` | rpc／posix、windows | 視情況 | 檔案傳輸過程中 prompt 逾時 | 確認目標端狀態後重新傳輸 |
| `TUNNEL_CONFLICT` | cli_local／posix、windows | 修正參數後 | 指定的 port 已有身分不同的 tunnel | 關閉既有 tunnel（connect --close）或改用其他 port 後重試 |
| `TUNNEL_SPAWN_FAILED` | cli_local／posix、windows | 視情況 | ssh 行程在就緒前即結束 | 檢查 ssh 參數與遠端可達性後重試 |
| `TUNNEL_STATUS_ERROR` | cli_local／posix、windows | 視情況 | connect 與 benches 讀取 tunnel 狀態時，回應格式異常或缺少 tunnels 清單（若回應帶 code 則透傳該 code） | 檢查 remote 狀態與 ssh 控制通道後重試 |
| `TUNNEL_STILL_ACTIVE` | cli_local／posix、windows | 視情況 | connect --close 之後 tunnel 仍在運作 | 以 remote status 確認 tunnel 行程狀態後再重試 close |
| `UNKNOWN_BENCH_CODE` | cli_local／posix、windows | 修正參數後 | bench_registry 找不到指定的 bench code | 以 benches.yaml 內存在的 bench code 重試 |
| `UNKNOWN_EVENT_CMD` | cli_local／posix、windows | 修正參數後 | event 子命令未知 | 改用 event 支援的子命令重試 |
| `UNKNOWN_PROFILE` | rpc、cli_local／posix、windows | 修正參數後 | profile 名稱不存在 | 以 profile 目錄中存在的名稱重試 |
| `WAL_MISSING` | rpc／posix、windows | 視情況 | WAL 曾寫入過（current_seq 大於 0）但目前的 WAL 檔不存在 | 執行 serialwrap doctor 檢查 WAL 目錄與狀態 |

passthrough：last_error_passthrough — session.attach 在 command-capable 且 state 不是 READY／ATTACHING、也不是 FLASHING／RECOVERING 時回 error_code=last_error；session.recover 的 REPROBE 分支未恢復時以 ok:true 回 error_code=last_error 或 SESSION_NOT_READY（值見下方 last_error 表）
<!-- END: generated-fact marker="error-codes" -->

### Session last_error

<!-- BEGIN: generated-fact marker="session-last-errors" -->
| last_error | 終態 | 自動重探 | 意義 | Agent 處置 |
|---|---|---|---|---|
| `ATTACH_FAILED:*` | 非終態 | 不自動重探 | attach 過程中 bridge 啟動或 probe 拋出例外，session 回到 DETACHED；實際值為 ATTACH_FAILED:<例外類別名> | 確認 UART 裝置可開啟（權限與是否被其他程式占用）後以 session attach 重試；反覆發生時查看 daemon 日誌 |
| `BCM_PROMPT_TIMEOUT` | 非終態 | ATTACHED／DETACHED 自動重探 | platform 為 bcm 時等待 BCM CLI 提示（>）逾時 | 檢查 profile 的 prompt_regex 與 timeout_s（bcm 建議 15 秒以上）後以 session attach 重試；裝置未回應時先排除線材與 USB 問題 |
| `BOOTLOADER_STUCK` | 終態 | 不自動重探 | ATTACHED 狀態下自動重探命中 U-Boot 或 bootloader 提示且重探已耗盡；session 停在 ATTACHED，不再自動重探 | 以 session self-test 取得 classification BOOTLOADER 與 recommended_action recover_interactive，依其指示以 interactive-open --allow-attached 脫困後，再以 session attach 重新進入流程 |
| `BRIDGE_DOWN:*` | 非終態 | 不自動重探 | UART 讀取或 select 失敗，bridge 已斷，session 轉為 DETACHED；實際值為 BRIDGE_DOWN:<reason> | 以 session list 確認裝置在線後執行 session attach 或 session recover；裝置不在線時先排除線材或 USB 問題 |
| `CLEARED` | 非終態 | 不自動重探 | operator 執行 session clear 後，session 解除綁定並以此值記錄 | 裝置在線時會立即清除；否則以 session attach 或 device attach 重新附掛後再操作 |
| `CONSOLE_UNLOCK_BOOTLOADER` | 終態 | 不自動重探 | console_unlock 步驟遇到 CFE 或 bootloader 提示，不送 secret 並停止後續 TX；空行 probe 之前判定者為零 TX，之後判定者該處之前已送出一次空行 probe | 以 interactive-open --allow-attached 手動進入 bootloader 處理，完成後以 session attach 重試 |
| `CONSOLE_UNLOCK_IDLE_TIMEOUT` | 終態 | 不自動重探 | console_unlock 等待 RX 閒置逾時（console 持續有輸出，未能靜默）；從未送出 secret。空行 probe 之前判定者為零 TX，之後判定者該處之前已送出一次空行 probe（已有 TX，仍未送 secret） | 等待 console 靜默後以 session attach 重試；若 console 持續輸出，先排查 RX 洪水（RX_FLOOD） |
| `CONSOLE_UNLOCK_PROMPT_TIMEOUT` | 非終態 | ATTACHED／DETACHED 自動重探 | 送出 console_unlock secret 後等不到 prompt（逾時） | 此值具自動重探資格；若持續出現，以 session self-test 排查 console 狀態 |
| `CREDENTIALS_UNRESOLVED` | 終態 | 不自動重探 | profile 宣告了帳密來源（user_env、pass_env 或 env_file）但解析為空（env_file 缺失、不可讀或缺 key）；屬登入終態，daemon 不對 login prompt 送空帳密，也不自動重探 | 不要反覆 recover 或 attach；告知使用者補齊 profile 宣告的帳密來源後，再手動 session attach 或 session recover；WARNING 與 WAL 警告會列出實際解析的路徑與原因（不含帳密值） |
| `DEVICE_NOT_FOUND` | 非終態 | 不自動重探 | bind 或 attach 時 device_by_id 不在目前裝置清單中 | 以 device list 確認裝置已接上，接上後以 session attach 重試 |
| `DEVICE_REBOUND_REQUIRED` | 非終態 | 不自動重探 | 同一 device_by_id 的裝置 real_path 改變（rebound），session 轉為 DETACHED | 確認新裝置節點正確後以 session attach 重新附掛；若 session 未自動恢復，手動 attach |
| `DEVICE_REMOVED` | 非終態 | 不自動重探 | 裝置自 /dev 消失，session 轉為 DETACHED | 確認裝置重新接上後會自動附掛；若未自動恢復，以 session attach 重試 |
| `DEVICE_REMOVED_DURING_ATTACH` | 非終態 | 不自動重探 | attach 收尾時發現裝置已移除（passthrough 不做 probe，收尾才檢查裝置是否仍在） | 等裝置重新出現後以 session attach 重試 |
| `LOGIN_PROMPT_TIMEOUT` | 非終態 | ATTACHED／DETACHED 自動重探 | 等待 login 提示逾時（未見 login 提示） | 此值具自動重探資格；逾期未恢復時檢查 profile 的 login_regex 與 timeout_s，並以 console attach 觀察開機輸出 |
| `LOGIN_REQUIRED` | 終態 | 不自動重探 | rx tail 命中 login 或 password 提示，但目前流程不自動登入（#174 分流），需要人類 console 介入 | 以 console attach 進入人類 console 完成登入，或補齊 profile 的登入設定後以 session attach 重試 |
| `LOGIN_USER_REQUIRED` | 終態 | 不自動重探 | login 提示出現，但未設定帳號（pass_env 有值以進入登入流程） | 在 profile 設定 user_env 或補齊帳號環境變數後，以 session attach 重試 |
| `PASS_ENV_MISSING` | 終態 | 不自動重探 | login 提示需要密碼，但 pass_env 指向的環境變數為空 | 補齊 profile 的 pass_env 對應環境變數（或 env_file 內的 key）後，以 session attach 重試 |
| `PASS_ENV_REQUIRED` | 終態 | 不自動重探 | profile 設定了 user_env 但缺 pass_env，login 與 password 提示皆出現 | 在 profile 補上 pass_env 設定後，以 session attach 重試 |
| `POST_LOGIN_CMD_TIMEOUT` | 終態 | 不自動重探 | 登入成功後執行 post_login_cmd 逾時 | 檢查 profile 的 post_login_cmd 與 timeout_s（bcm 切換 shell 的情境建議加大）後，以 session attach 重試 |
| `PROMPT_TIMEOUT` | 非終態 | ATTACHED／DETACHED 自動重探 | 命令送出後等不到 prompt，且 CTRL_C／CTRL_D 皆未恢復（或 boot quiet 期間跳過強制按鍵）時，由 _transition_to_attached 轉為 ATTACHED | 此值具自動重探資格；逾期未恢復時以 session recover 或 console attach 檢查 target 狀態 |
| `PROMPT_UNAVAILABLE` | 非終態 | 僅 ATTACHED 自動重探 | attach 後 prompt 不可用（開機窗或 probe 未見 prompt） | attach 撞上 DUT 開機窗時屬暫態，daemon 會等 RX 閒置後自動重探；逾期未恢復時以 console attach 觀察 |
| `PRPL_PROMPT_TIMEOUT` | 非終態 | ATTACHED／DETACHED 自動重探 | platform 為 prpl（其餘未指定者一律歸此）時等待 prompt 逾時 | 此值具自動重探資格；逾期未恢復時檢查 profile 的 prompt_regex 與 timeout_s，並以 session attach 重試 |
| `READY_NONCE_TIMEOUT` | 終態 | 不自動重探 | READY 確認 nonce 逾時（ready_probe 的回聲未出現） | 檢查 profile 的 ready_probe 設定與 target 是否可執行命令，修正後以 session attach 重試 |
| `READY_PROMPT_TIMEOUT` | 非終態 | ATTACHED／DETACHED 自動重探 | ready_probe 之後等不到 prompt（逾時） | 此值具自動重探資格；逾期未恢復時檢查 profile 的 ready_probe 與 prompt_regex，並以 session attach 重試 |
| `REBOOTING` | 終態 | 不自動重探 | human 來源的 reboot 命令已送出，session 停留於 ATTACHED，等待 target 重新開機 | 等待開機完成後以 session self-test 確認狀態；需要立即操作時以 console attach 觀察開機輸出 |
| `REBOUND` | 非終態 | 不自動重探 | session.bind 將 session 改綁到不同 device_by_id，session 轉為 DETACHED | 裝置在線時會立即清除；否則以 session attach 重新附掛 |
| `RECOVERY_ERROR` | 終態 | 不自動重探 | reboot 恢復迴圈中 ensure_ready 拋出例外；worker 不結束，記錄此值並續迴圈 | 等待恢復逾時收尾後，以 session self-test 取得分類；若仍異常，以 console attach 觀察並以 session recover 重試 |
| `RECOVERY_TIMEOUT` | 終態 | 不自動重探 | reboot 恢復逾時收尾；bridge 不存在時轉為 DETACHED，bridge 存在時保留既有的 last_error，無既有值則以此值收尾 | 以 session self-test 確認分類後，以 session recover 手動重試；bridge 不存在時以 session attach 重新附掛 |
| `RELEASED` | 終態 | 不自動重探 | session 經 device release 讓出 UART（燒錄或裝置交接），僅操作者可重新附掛；daemon 重啟後由 state.json 還原為 RELEASED，但 last_error 不寫入 state.json，重啟後為 null | 燒錄或交接完成後，以 device attach 重新附掛（session attach／recover 對 RELEASED 只回 released:true＋recommended_action=device_attach，不會附掛）；在此之前不要嘗試 probe |
| `REPROBE_FAILED:*` | 終態 | 不自動重探 | 自動重探的 probe 拋出例外；session 保持 ATTACHED，且此類錯誤不具自動重探資格。實際值為 REPROBE_FAILED:<例外類別名> | 以 session self-test 排查 bridge 狀態；仍異常時以 session recover 手動重試 |
| `RX_FLOOD` | 非終態 | ATTACHED／DETACHED 自動重探 | console 輸出洪水淹沒 probe（rx_bytes_last_10s 超過閾值）；不是 console 死亡 | 等待 RX 排空，daemon 會於閒置後自動重探並升為 READY；不要反覆 recover 或 attach，以 session list 的 rx_bytes_last_10s 觀察排空進度 |
| `SHELL_PROMPT_TIMEOUT` | 非終態 | ATTACHED／DETACHED 自動重探 | platform 為 shell 時等待 shell 提示（#）逾時 | 此值具自動重探資格；逾期未恢復時檢查 profile 的 prompt_regex 與 timeout_s，並以 session attach 重試 |
| `TRANSPORT_STALL` | 非終態 | ATTACHED／DETACHED 自動重探 | probe 全程零 RX 且 RX 凍結 30 秒以上，疑似 USB 或 usbip read-endpoint stall；serialwrap 的 recover 與 release+attach 無法自復 | 勿先 power-cycle DUT；以 dmesg 佐證後，依 last_error_detail 的 host 層指令做 USB re-enumeration（authorized 0 然後 1 或 usbipd detach 與 attach） |
| `USER_ENV_MISSING` | 終態 | 不自動重探 | user_env 指定的帳號環境變數缺漏（目前不可達） | 目前不會由程式回出；若日後遇到，依 user_env 的設定補齊環境變數後以 session attach 重試 |
<!-- END: generated-fact marker="session-last-errors" -->

## 參考檔案
- `README.md`
- `openspec/specs/session-command-readiness/spec.md`（#51）
- `openspec/specs/session-interactive/spec.md`（#53）
- `openspec/specs/device-handoff/spec.md`（#54）
- `openspec/specs/mcu-flash-broker/spec.md`（#55）
