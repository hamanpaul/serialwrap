# P2-1 MCU 韌體升級（`/dev/ttyMCU`）實機手動程序

> 對應 `tests/acceptance/cases.yaml` 的 `p2-1-mcu-flash-manual`（`suite: manual-procedure`、`layer: manual`、`counted_in: none`）。
> 本程序為**手動、逐步命令**，**不計入** L2 38／L3 37 等任何計數，也不屬 Stage 0 出口的自動化證據；實機執行屬 BLOCKED-OWNER（擁有者實機）。
> 來源：repo `CLAUDE.md`「MCU flash 真機驗證手法（#55 `/dev/ttyMCU`，PR #66 實證）」，改寫為編號步驟。
> 本程序的 `revision` 為本檔的 git blob sha1（`git hash-object realhw/procedures/p2-1-mcu-flash.md`），內容變更即需重新登記。

| 欄位 | 內容 |
|---|---|
| entry | 手動，依下方步驟逐步執行 |
| 平台 | 僅 Linux（POSIX PTY bridge；`/dev/ttyMCU` 為 POSIX-only）。Windows 燒錄不走本程序，改走 #54 device release |
| 破壞性 | 是：燒錄 DUT MCU 韌體，會改寫目標板狀態 |
| owner | 擁有者（實機） |
| evidence | `flash.log`（燒錄工具輸出）、`procedure-checklist.md`（每步勾選、時間與 console 回應摘錄）、`daemon-status-before.json`、`daemon-status-after.json` |
| rollback | 任一步失敗：先停止燒錄工具，確認 DUT 不在 BSL 後，以隔離 socket 執行 `serialwrap --socket "$SERIALWRAP_RUN_DIR/serialwrapd.sock" device attach --selector <COM>`，再以 `session list` 確認 `READY`；仍卡住則停止受測 throwaway daemon，保留全部證據 |

## 前置條件

1. **只在隔離實例執行，不動正式 daemon**。`SERIALWRAP_RUN_DIR`、`SERIALWRAP_STATE_DIR`、`SERIALWRAP_CONFIG_DIR` 三者必須明確設定，且各自不等於預設路徑。正式 daemon 不停止、不重啟、不下任何寫入命令。
2. **by-id 目錄只放 MCU 線（FTDI）的 by-id symlink**，不得包含 DUT console（如 CH340 `ttyUSB0`）。否則動態偵測會抓到人類 minicom 正在使用的 DUT console，造成 two-reader 衝突。
3. **DUT console 由人類 minicom 持有**（不在隔離 by-id 目錄內）。GPIO 指令經該 console 送出，不經隔離 daemon。
4. 所有 CLI 呼叫都帶 `--socket "$SERIALWRAP_RUN_DIR/serialwrapd.sock"`，避免連到預設（正式）endpoint。

## 步驟

以 `SBX` 代表隔離根目錄（例如 `$HOME/tmp-mcu-flash`），`EVD` 代表本次證據目錄（例如 `~/b-log/rust-refactor/p2-1/<YYMMDD-HHMMSS>/`），兩者皆不得位於正式路徑下。

**S1．建立隔離環境（證據：`procedure-checklist.md`）**

```bash
export SBX=$HOME/tmp-mcu-flash
export SERIALWRAP_RUN_DIR=$SBX/run
export SERIALWRAP_STATE_DIR=$SBX/state
export SERIALWRAP_CONFIG_DIR=$SBX/config
export SERIALWRAP_BY_ID_DIR=$SBX/byid
export SERIALWRAP_BY_PATH_DIR=$SBX/bypath
mkdir -p "$SERIALWRAP_RUN_DIR" "$SERIALWRAP_STATE_DIR" "$SERIALWRAP_CONFIG_DIR" "$SERIALWRAP_BY_ID_DIR" "$SERIALWRAP_BY_PATH_DIR" "$EVD"
ln -sfn "$(readlink -f /dev/serial/by-id/<MCU 線 FTDI 的 by-id 名稱>)" "$SERIALWRAP_BY_ID_DIR/<同名>"
ls -l "$SERIALWRAP_BY_ID_DIR"        # 只允許出現 MCU 線；DUT console 不得出現
```

**S2．確認 MCU 線未被持有，啟動受測 throwaway daemon 並記錄基線（證據：`procedure-checklist.md`、`daemon-status-before.json`）**

正式 daemon 仍在運作時，CLI 會偵測到另一個綁在不同 socket 的 `serialwrapd`，預設回 `DAEMON_ALREADY_RUNNING_ELSEWHERE`（exit 2）並拒絕另起。本步驟因此先確認 MCU 線未被持有，再以 `--force-spawn` 於隔離 socket 另起受測 daemon。

```bash
# 1) 另起前的前置確認：MCU 線（FTDI）不得出現在任何 serialwrapd 或 tty 持有者清單中（摘錄記入 checklist）
serialwrap daemon status | tee -a "$EVD/procedure-checklist.md"
#    檢視 multi_open_detail／foreign_holders：MCU 線不得被列為任何持有者；若被列出，停止，不得繼續
# 2) 隔離設定與 by-id 目錄確認無誤後，另起受測 daemon（--force-spawn 僅用於此隔離實例）
serialwrap --socket "$SERIALWRAP_RUN_DIR/serialwrapd.sock" daemon start --force-spawn
# 3) 啟動成功才繼續：上一行必須回 ok；失敗（非 ok）即停止，不得進入 S3
serialwrap --socket "$SERIALWRAP_RUN_DIR/serialwrapd.sock" daemon status  > "$EVD/daemon-status-before.json"
serialwrap --socket "$SERIALWRAP_RUN_DIR/serialwrapd.sock" session list   >> "$EVD/daemon-status-before.json"
```

記錄受測 daemon 的 PID，以及每個 session 的 COM 名稱與 state（燒錄前的「其他 COM 狀態」基準）。

**S3．確認 MCU 端點未被寫入（證據：`procedure-checklist.md`）**

以 `mcu patterns` 與 `mcu status` 查詢端點狀態。這兩個查詢都不經 `/dev/ttyMCU` PTY，因此不會對端點寫入任何位元組。燒錄前端點必須保持沉默。

```bash
serialwrap --socket "$SERIALWRAP_RUN_DIR/serialwrapd.sock" mcu patterns
serialwrap --socket "$SERIALWRAP_RUN_DIR/serialwrapd.sock" mcu status
```

**S4．DUT 進入 BSL（證據：`procedure-checklist.md`）**

於人類 minicom（DUT console，CH340／`ttyUSB0`）**逐行**送出下列短指令。不得以 `;` 串接長行，因為長指令會在 UART console 被截斷。若使用 tmux，每行以 `tmux send-keys -l` 送出，行尾加 `Enter` 並短暫 sleep。每行的 console 回應摘錄記入 checklist。

1. 解除 `1fbf0300.serial` 的 driver 綁定（unbind）。
2. 將 GPIO13、GPIO14 設為 input。
3. 依序拉動 GPIO31 與 GPIO54（兩者作為 reset 腳位），使 DUT 重新開機並取樣 BSL 腳位。

每一行送出後，於 checklist 記錄指令、時間與 console 回應。任一行無回應時停止，不得略過。

**S5．燒錄（證據：`flash.log`）**

```bash
ocp-mcu-upgrade -d "$SERIALWRAP_RUN_DIR/dev/ttyMCU" -b 115200 -t 8 -e -s -i <fw.bin> 2>&1 | tee "$EVD/flash.log"
```

期望輸出為 `Return error code : 0x0`。

**S6．燒後恢復（證據：`daemon-status-after.json`）**

燒錄工具結束後，等待受測 session 自動恢復 `ATTACHED`。daemon 不得死亡。

```bash
serialwrap --socket "$SERIALWRAP_RUN_DIR/serialwrapd.sock" daemon status  > "$EVD/daemon-status-after.json"
serialwrap --socket "$SERIALWRAP_RUN_DIR/serialwrapd.sock" session list   >> "$EVD/daemon-status-after.json"
```

比對 S2 與 S6 的 daemon PID 與其他 COM 的 state，記入 checklist。

**S7．收尾（證據：`procedure-checklist.md`）**

1. 停止受測 throwaway daemon（`serialwrap --socket ... daemon stop`），確認其 PID 已消失。
2. 人類 minicom 以 `Ctrl-A X` 或等效方式退出，確認 DUT 已離開 BSL 並以正常韌體開機。
3. 刪除 `SBX` 內的 symlink 與暫存目錄；`EVD` 保留。

## 三個只有實機才會出現的陷阱

1. **端點未 bridge 時一律沉默，不主動寫任何位元組。** 曾發生於 idle 時寫入支援清單，被 flasher 讀成假回應，汙染 SBL sync。S3 的查詢只走 `mcu patterns`／`mcu status`，不經 PTY。
2. **認線 probe 必須使用 flasher 自身的 sync bytes，並把 MCU 的 ACK 回放給 flasher。** 若另注入獨立 sync，會吃掉 MCU 的 ACK（double-sync），flasher 之後自己的 sync 將永遠收不到回應。不得在 S5 之前手動送 sync。
3. **daemon 同時持有 PTY master 與 slave，flasher 關閉端點時 master 不會收到 EOF。** 離開 `FLASHING` 需以 holder-probe（`_probe_external_holder` 掃 pts）偵測 flasher 斷線。燒後若 session 未恢復 `ATTACHED`，先依 holder-probe 結果判斷，不得直接 kill 正式 daemon。

## PASS 條件

以下四項**全部**成立才判 PASS；任一不成立判 FAIL 或 INSUFFICIENT，保留全部證據：

1. `flash.log` 含 `Return error code : 0x0`。
2. `daemon-status-after.json` 中受測 session 的 state 為 `ATTACHED`。
3. `daemon-status-before.json` 與 `daemon-status-after.json` 中的 daemon PID 相同（燒錄期間 daemon 不死）。
4. 其他 COM 的 state 在 before 與 after 之間一致（燒錄不影響其他 COM）。

缺任一證據檔即判 INSUFFICIENT（不得以 PASS 結案）。本程序的結果不回填任何自動化計數。
