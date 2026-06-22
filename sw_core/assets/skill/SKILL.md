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
9. 需要完整證據時：`serialwrap log tail-raw --selector COM0`／`serialwrap wal export`；只要查目前 WAL seq 用 `serialwrap wal current-seq`。

## command_capable 與 READY 判定（#51）
- 一個 session 是否「可下命令」由其 profile 的 `ready_probe` 是否非空決定（`command_capable = bool(profile.ready_probe.strip())`），**與底層是 OS shell 或 bootloader 無關**——只要 `prompt_regex` 對得上、`ready_probe` 能 round-trip 即可進 `READY`（含 U-Boot 之類 bootloader command profile，如 `uboot-template`）。
- **非 command-capable**（無 `ready_probe`，如 `others-template`、未設 `ready_probe` 的 passthrough）：session 維持 `ATTACHED`、**不**轉 `READY`；此時 `cmd submit` 回 `PROFILE_NOT_COMMAND_CAPABLE`（附 hint：要下命令請設定 `ready_probe` 或改用具 prompt 的 profile）。
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

## FAQ：開機窗連不到 / broker not ready（#69）
- 先跑 `serialwrap session self-test --selector COM0` 與 `serialwrap session list`，看 `classification`、`state`、`last_error`、`reprobe_attempts`、`reprobe_exhausted`。
- `ATTACHED_NOT_READY` + `last_error=PROMPT_UNAVAILABLE` / `PROMPT_TIMEOUT`：通常是 attach 撞 DUT 開機窗、prompt 尚未出現；daemon 會等 RX 閒置後自動重探，成功會回 `READY`。
- `BRIDGE_DOWN` + `DETACHED` + `*_PROMPT_TIMEOUT`：若 device 還在位，daemon 會重新走 attach/probe 路徑。
- `minicom_router.sh` 會提示「DUT 可能仍在開機、serialwrap 正在自動重探」；需要阻塞等 READY 時可設 `MINICOM_WAIT_READY=1`。
- 若 `reprobe_exhausted=true` 或等待過久仍未 READY，再手動 `serialwrap session recover --selector COM0`（必要時 `--force`）。

## Remote Support 用法（ssh-tunnel）
當 Agent 不在 target 所在機器上，而要從遠端 debug UART 時，走 **remote endpoint** 模式。
- daemon 仍跑在 **target 所在主機**；Agent 端只透過 `--endpoint tcp://host:port` 連到遠端 daemon。
- 正式環境優先使用 **ssh-tunnel**。

target 端（暴露 daemon socket，務必 bind 127.0.0.1）：
```bash
socat TCP-LISTEN:7777,bind=127.0.0.1,reuseaddr,fork \
      UNIX-CONNECT:/tmp/serialwrap/serialwrapd.sock &
```
RD / Agent 端：
```bash
ssh -N -L 127.0.0.1:7777:127.0.0.1:7777 remote_user@target_host
serialwrap --endpoint tcp://127.0.0.1:7777 session list
```
注意事項：
- `serialwrap daemon start` 不支援 `--endpoint`。
- `file push` / `file pull` 的 `--local` 路徑是 **daemon 所在 host/container** 的路徑，不是 Agent 本機路徑。
- 正式環境 `socat` 必須 `bind=127.0.0.1`，不可直接暴露到外網。
- 隔離煙測可跑 `./tools/docker/remote_smoke.sh`，不代表 production 暴露方式。

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
serialwrap file push --selector COM0 --local ./fw.bin --remote /tmp/fw.bin --source agent:diag
serialwrap file pull --selector COM0 --remote /etc/config/wireless --local ./wireless.bak --source agent:diag
```
- `--remote`／`--local` 必填於 push；pull 省略 `--local` 時回傳檔案內容。
- `--chunk-size` 預設 2048（push）。
- remote 模式下 `--local` 指 daemon 所在 host 的路徑。

## 安全規則
- 禁止 Agent 直接寫 `/dev/ttyUSB*` 或 `/dev/ttyACM*`。
- 禁止繞過 broker 自行開多個 serial writer。
- 外部工具需要獨佔 raw device 時，用 `device release` / `device attach` 正式交接（或走 `/dev/ttyMCU` 燒錄端點），**不要**直接 kill daemon 或用 placeholder bind 繞過。
- 長流命令（`logread -f`、`tcpdump`、kernel debug）一律用 `--mode background` 或設足夠長的 `--cmd-timeout`，避免阻塞共享通道。
- 每筆自動化命令必填 `--source`，不可省略，確保追蹤性。
- 卡住時先 `serialwrap session self-test`，再決定是否 `serialwrap session recover`（可加 `--force`）。recover 成功恢復 prompt 時回 `ok: true`（附 `error_code: PROMPT_TIMEOUT_RECOVERED`, `partial: true`），表示 session 可繼續使用。

## 短命令原則（Best Practice）
- **避免 heredoc**：heredoc 經 UART 傳輸時容易遺失字元或打亂 prompt，改用 `echo ... > file` 分步寫入。
- **單行盡量短**：每條命令控制在 2 KB 以內；> 4 KB 會 warning、> 16 KB 會被 reject（`CMD_TOO_LONG`）。命令不得含 `\n` 換行字元，否則回 `CMD_CONTAINS_NEWLINE`。
- **避免 base64 inline**：不要將整個檔案 base64 編碼塞進 `cmd submit`，改用 `serialwrap file push`。
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
serialwrap log tail-raw --selector COM0 --from-seq 0 --limit 200
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
