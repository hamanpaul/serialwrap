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
3. `serialwrap device attach --selector COM0`：收回裝置並重建 primary console。外部仍持有時回 `DEVICE_STILL_HELD`（附 pids）；確認外部真的結束後可加 `--force` 略過檢查。
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
- **長操作自動固定 floor**：`session attach`／`session recover`／`session self-test`／`session console-attach`（recover 升級分支可同步跑數十秒）為 daemon 端同步長操作；未指定全域 `--timeout` 時 CLI 自動採固定 45s 的 floor，一般方法維持 5s。顯式指定 `--timeout` 一律照用。floor 為誠實的寬鬆常數——CLI 無從得知 daemon 端 profile 的 `timeout_s`（bcm 類平台常 15s+、多階段 probe），不隨 recover/self-test 的子命令參數縮放（daemon 端對那些參數本就有 2s cap）；仍逾時時改看下一條的診斷欄位與 `session list`。
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
- `auto_enable_com_on_load: true` 的規則在 daemon 重啟後自動啟用。
- Counter 由 disable/delete/reset 清除；daemon 重啟後 counter 保留（tmpfs 除外）。

## 透過 UART 推送／拉取檔案
```bash
serialwrap file push --selector COM0 --local ./probe.sh --remote /tmp/probe.sh --source agent:diag
serialwrap file pull --selector COM0 --remote /etc/config/wireless --local ./wireless.bak --source agent:diag
```
- `--remote`／`--local` 必填於 push；pull 省略 `--local` 時回傳檔案內容。
- `--chunk-size` 預設 2048（push）。
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

## 參考檔案
- `README.md`
- `openspec/specs/session-command-readiness/spec.md`（#51）
- `openspec/specs/session-interactive/spec.md`（#53）
- `openspec/specs/device-handoff/spec.md`（#54）
- `openspec/specs/mcu-flash-broker/spec.md`（#55）
