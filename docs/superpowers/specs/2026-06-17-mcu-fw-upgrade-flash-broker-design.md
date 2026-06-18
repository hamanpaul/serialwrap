# MCU 韌體升級 flash-broker 設計（#55 session 原生支援 fw upgrade）

- 日期：2026-06-17
- 對應 issue：[#55](https://github.com/hamanpaul/serialwrap/issues/55)（serialwrap session 原生支援 MCU 韌體升級）
- 底層基礎：[#54](https://github.com/hamanpaul/serialwrap/issues/54)（device release/attach，已交付）—— 本設計**不**重做 handoff，而是補上 #54 當時列為「Desired」但未做的「pause bridge + byte-transparent 端點」那一塊。
- 狀態：設計（brainstorming 產出），待 user 審閱 → openspec-propose → writing-plans。

## 1. 背景與問題

目前要對掛在 serialwrap 下的 UART 做 MCU 韌體升級，必須把 raw device 透過 `device release`
（#54）整個交給外部 flasher（`ocp-mcu-upgrade`）。這已可運作，但使用者要的更進一步：

> 在 **serialwrap daemon 持續 maintain tty** 的前提下，讓 `ocp-mcu-upgrade` 能直接「走通」，
> 不必把整個 device 釋放出去、也不必記住底層是哪個 `/dev/ttyUSBx`。

### 1.1 為什麼現有的「raw / passthrough」不能直接拿來燒

程式碼層級確認（`sw_core/uart_io.py`）：

- 底層 termios 確實是 raw（`_configure_serial` `uart_io.py:118`：iflag/oflag/lflag 全清 0），
  device→console 方向也是 raw 1:1（`_handle_serial_rx` `:329` 把 device bytes 原樣
  `_write_console_best_effort` 寫進每個 console PTY，PTY slave 亦為 raw）。
- **但有三道牆擋住 `ocp-mcu-upgrade`**：
  1. **daemon 永遠在讀 real device**：`_loop()`（`:431`/`:468`）把 `serial_fd` 永遠放進
     `select` read set，有 bridge 就持續 `os.read` + `_handle_serial_rx`。若 flasher 也開同一個
     real device → two-reader race（#54 根因，MCU 的 SBL 回應被拆走 → flasher timeout）。
     **沒有任何 mode 能在 bridge 活著時讓 daemon 停讀。**
  2. **console→device（TX）只有 human owner 才 raw**：`_handle_console_rx`（`:398`）只有
     `human:<id>` owner 走 `send_bytes(data)` 原樣送（`:408`）；其餘（agent / 一般 console）
     走 `_consume_console_input`（`:367`）做行處理（`0x08/0x7F` 當退格、`0x0A/0x0D` 當斷行、
     組 line 才送）。SBL binary frame 含這些 byte → TX 被汙染。
  3. **passthrough profile 不改 byte 行為**：`others-template` / `uboot-template` 的
     `platform: passthrough` 只改 login/prompt FSM，bridge 的行處理照舊。

  （`tools/minicom-raw.sh` 的「raw」是直接 `minicom -D /dev/ttyUSBx` **繞過 serialwrap**，
  非「daemon maintain tty 下跑通」，且 daemon 若仍 attached 一樣撞牆 #1。）

### 1.2 真正的痛點：哪一條 tty 才是 MCU 線？

`/dev/ttyUSBx` 列舉順序不固定（重插 / 重開機會 ttyUSB0↔1 對調），且**每次重燒用的轉接板
不見得一樣**（by-id 也會變）。實測本機 `device list` + `session list`：

| 裝置 | by-id | session | profile | 角色 |
|---|---|---|---|---|
| `/dev/ttyUSB0` | `usb-1a86_USB_Serial…`（CH340）| COM1（op3-template+2）| shell, command_capable | DUT console |
| `/dev/ttyUSB1` | `usb-FTDI_FT232R…AQ00OAQ7`（FTDI）| COM0（others-template+1）| passthrough | MCU SBL UART |

注意 COM 編號（依 session 建立順序給）與 ttyUSB 編號錯開：MCU 線是 COM0，console 是 COM1。
所以「用固定名字綁死」會失效——**身分會變，唯一不變的是行為**：MCU 線是「BSL invoke 後會講
SBL 的那條」，DUT 線是「一個在跑的 Linux console」。本設計用行為（sync-probe）認線。

## 2. 目標 / 非目標

**目標**
- 在 daemon 持續運作、其他 COM 不受影響下，提供一個 byte-transparent 端點 `/dev/ttyMCU`，
  讓 `ocp-mcu-upgrade -d <…/dev/ttyMCU> …` 直接走通。
- 不靠不穩定的 `/dev/ttyUSBx` / by-id：用**非破壞性 sync-probe** 自動認出 MCU 線。
- 支援多家 MCU：可擴充的 pattern registry（預設 TI CC2674/CC2652）。
- 全程保留 RAW WAL 證據（這正是相對「跳出去燒」的最大價值）。
- 結束後自動恢復 console；daemon 全程是 real device 唯一 reader（結構上免 two-reader race）。

**非目標（本次不做）**
- 把 SBL / `ocp-mcu-upgrade` 的完整 flash 協定塞進 daemon（只持有最小 sync 握手知識）。
- BSL-invoke（GPIO reset）的編排：v1 仍走既有 DUT console cmd，由操作者/agent 負責（見 §7 未來）。
- 透過 PTY 路由「破壞性 flasher bytes 去 probe」的做法（明確拒絕，理由見 §6.A/§3）。
- 自動把 device 釋放給外部（那是 #54 的 `device release`，與本設計正交）。

## 3. 關鍵決策（brainstorming 結論）

| 決策 | 選擇 | 理由 |
|------|------|------|
| 燒錄通道 | **B 案：serialwrap 出 byte-transparent PTY 端點 `/dev/ttyMCU`** | daemon 維持 real device 唯一 reader、無 race、RAW evidence 全程留；flasher 開的是 serialwrap 端點，不碰 ttyUSBx |
| 認線方式 | **非破壞性 sync-probe**（行為判別，非身分判別）| 轉接板/ttyUSB 會變，sync 行為不變；不依賴會漂的 by-id |
| 協定知識 | **最小且可擴充**：per-family pattern registry（預設 TI `55 55`→`00 CC`）| 換家只加一筆設定，不把整個 flash 協定搬進來 |
| 候選收斂 | **排除 `command_capable` console（DUT）** | 從源頭避免燒到 console 線 |
| 多候選都 ACK | **不自動挑，回 `FLASH_AMBIGUOUS`** | 寧可停手要求明指，也不亂燒 |
| 指到 console | **預設擋下 + 警告，需 `--force`** | 誤燒防護 |
| flash 期間 console | **轉唯讀快照**（看得到進度、不准注入）；**RAW WAL 全程保留** | 不污染 binary，但可稽核 |
| no-MCU 偵測不到 | **不回合成錯誤，正常 timeout**（+ 週期 re-probe）| 沿用 flasher 自身 retry/timeout，體驗一致；遲到的 BSL 仍能中途 latch |
| 端點命名 | 固定 symlink `…/dev/ttyMCU`（底層 pts 換不影響）| 使用者只記一個名字 |

## 4. 架構與元件

新增 **flash-broker 子系統**，掛在現有 daemon 內，動到面最小：

| 元件 | 職責 | 落點 |
|---|---|---|
| MCU pattern registry | 載入 per-family sync/ack 設定（預設 TI CC26xx），供 probe 與 `cat` 列表用 | 新檔 `sw_core/mcu_patterns.py` + `profiles/` 設定 |
| ttyMCU endpoint | 建立/管理 `…/dev/ttyMCU`（常駐 PTY + 固定 symlink），分流「讀→列表 / flash→bridge」 | 新檔 `sw_core/flash_endpoint.py` |
| sync-probe 偵測器 | 排除 console、逐候選逐 pattern 送非破壞 sync、認線並定 family | 同上 |
| raw bridge（flash mode）| 命中後 ttyMCU ⇿ 目標 session real device 1:1 raw 雙向轉送，TX 跳過 `_consume_console_input`；daemon 維持唯一 reader | 擴充 `uart_io.py`（加 raw/flash 旗標） |
| flash 狀態 + 仲裁 | 目標 session 進 `FLASHING`：擋其他 agent `cmd submit`、其他 COM 不受影響、結束自動恢復 console | 擴充 `session_manager.py`（沿用 release/attach 骨架） |
| CLI/RPC | `mcu patterns` / `mcu status`；flash 端點 begin/end（或自動）；device list 反查 | `cli.py` / `service.py` / `rpc.py` |

**核心不變式**：真 flasher 只會碰到「已被 sync ACK 確認」的那條線；daemon 全程是 real device
唯一 reader（無 two-reader race）；RAW WAL 全程留證。

## 5. 資料流與生命週期

### 5.1 端點存在方式
- daemon 啟動時建一組 PTY，master 自持，slave symlink 成固定名 `…/dev/ttyMCU`。
- 路徑現實面：寫進 `/dev/` 需 root，serialwrapd 多以使用者身分跑 → **預設端點
  `${RUN_DIR}/dev/ttyMCU`（如 `/tmp/serialwrap/dev/ttyMCU`）**；要真正 `/dev/ttyMCU` 則附
  udev/root symlink 說明。symlink 名穩定，底層 pts 換不影響指令。

### 5.2 開啟分流（依「有沒有送出 bytes」，非猜意圖）
- **flasher（會先送 sync bytes）**：serialwrap 跑自己的 probe；**沒命中就保持沉默**，flasher
  走它原本的 retry/timeout。沉默期間 serialwrap **週期性 re-probe**，BSL invoke 稍晚也能在
  flasher 重試窗內中途 latch 開 bridge。命中則轉 byte-transparent bridge。
- **`cat`（只讀、不送 bytes）**：serialwrap 回支援家族清單 + 候選 tty/分類文字 + EOF。
  （canonical 查詢另給 `serialwrap mcu patterns` / `mcu status`，`cat` 為便捷鏡像。）

### 5.3 端到端流程（標出責任歸屬）
```
[DUT console — 既有 serialwrap console session]
  1. 跑 GPIO BSL-invoke（unbind serial / GPIO13,14 in / GPIO31,54 reset）把 MCU 帶進 BSL
     ★ v1 不歸 flash-broker 管，走既有 console cmd

[host — flash-broker]
  2. ocp-mcu-upgrade -d <…/dev/ttyMCU> -b 115200 -t 8 -e -s -i fw.bin
  3. open(ttyMCU) → 偵測：候選=attached sessions 排除 command_capable console
                     → 逐候選逐 pattern 送非破壞 sync
  4. 某條回 ACK → 鎖定該 session：state→FLASHING，記 family
  5. ttyMCU.master ⇿ 該 session real fd 1:1 raw 雙向轉送
       TX：ttyMCU→device 原樣（跳過行處理）；RX：device→ttyMCU 原樣
       daemon 仍唯一 reader → 無 race；RAW WAL 持續記 TX/RX（cmd_id 標 flash）
  6. flasher 關閉 ttyMCU（或 holder 清空 / 逾時）→ 結束
  7. 自動恢復：FLASHING→ATTACHING→（_spawn_attach 重建 console）→READY；ttyMCU 回待命
```

### 5.4 狀態機
```
READY/ATTACHED ──open+ACK──▶ FLASHING ──close / holder空 / timeout──▶ ATTACHING ─▶ READY
                                  │  期間：該 session cmd submit → FLASHING_BUSY
                                  └  其他 COM 完全不受影響；daemon 不死
```
恢復沿用既有 `_spawn_attach`，失敗停 `ATTACHING` + 明確 `last_error`（與現有 attach 一致）。

## 6. 錯誤處理、邊角、安全

**A. 誤燒防護（最高優先）**
- detection 排除 `command_capable` console。
- `--selector/--by-id` 明指 command_capable session → 擋下 + 警告，需 `--force`。
- 多候選都 ACK → `FLASH_AMBIGUOUS`，列出所有命中（by-id/real_path/family），要求明指。

**B. probe 與 flasher 自身 sync**
- serialwrap 的 sync 只**觀察 ACK、不吃協定狀態**；命中後對該 session `clear_rx_buffer` 清掉
  probe 殘響再開 bridge，讓 flasher 的 connect/sync 重新握手（TI ROM 可重複 sync）。
- 沒命中的候選只收到 2 bytes `55 55`（無害）。
- ⚠️ double-sync 與殘響清理時序 → **真機待驗（§8 gate）**。

**C. baud / 線參數**
- 把 ttyMCU slave 的 termios（baud/framing）**鏡射到 real device**；鏡射不到時 fallback 用
  命中 pattern 的 registry baud（CC2674 預設 115200）。避免 PTY 與實體線 baud 不一致。

**D. 結束 / timeout / 自動恢復**
- 結束觸發任一：flasher 關閉 ttyMCU slave（hangup）、閒置 timeout、顯式 `flash-end`。
- 結束後 `_probe_external_holder` 確認沒人持有 → `_spawn_attach` 重建 console。

**E. 並發 / 重入**
- 單一 ttyMCU 端點；已 FLASHING 時再 open → `FLASH_IN_PROGRESS`（不排隊、明確擋）。
- FLASHING 期間 daemon 不 release real fd（維持唯一 reader）→ 結構上不可能 two-reader race。
- 有人繞過 serialwrap 直接開 real `/dev/ttyUSBx` 屬手動繞道，超出範圍；`mcu status` 以 holder
  probe 顯示「該線被外部持有」提示。

**F. registry 安全不變式**
- 每筆 probe **必須是非破壞性 sync**（只握手，不 erase/write）；probe bytes 限「已審核」清單，
  新增 pattern 需明確標記 reviewed。

**G. 偵測不到 MCU**
- **不回合成錯誤**：保持沉默，由 flasher 自身 retry/timeout 處理（+ 週期 re-probe）。

## 7. 與 #54 的關係、未來

- #54 `device release/attach`：把整個 device 釋放給外部、手動收回。**正交**於本設計。
- 本設計：daemon 不放手，出一個 byte-transparent 端點 + 自動認線。對應 #54「Desired」段
  當時未做的「pause bridge / advisory-lock」精神，但以「serialwrap 自持 + PTY bridge」實現。
- 未來（非本次）：把 BSL-invoke（GPIO reset）也編成一個 recipe / session 動作，串成一鍵
  end-to-end；以及 by-path 區分「兩顆無序號同款轉接器」的 edge case。

## 8. 測試策略

### 8.1 Unit（純邏輯、無硬體，進 `tests/`）
- registry 載入/解析、非破壞不變式（破壞性序列被拒）。
- detection 候選計算：正確排除 `command_capable` console；`--force` 覆寫。
- ambiguous 判定：多候選 ACK → `FLASH_AMBIGUOUS`，不自動挑。
- ttyMCU 分流：read-only→列表；有 write→flash 路徑；no-MCU→沉默（不合成錯誤）。
- 狀態機：`READY→FLASHING→ATTACHING→READY`；FLASHING 期間 `cmd submit`→`FLASHING_BUSY`。

### 8.2 整合（假 PTY/loopback：一條會回 `55 55→00 CC` 的假 MCU + 一條 console）
- 端到端：open ttyMCU → 認到假 MCU → raw 雙向 byte-perfect（含 `0x08/0x0A/0x7F` 等會被行處理
  吃掉的 byte，驗證**沒被汙染**）。
- 其他 COM 在 FLASHING 期間不受影響；結束自動恢復 console。
- baud termios 鏡射：slave 改 baud → real device 跟著改。

### 8.3 真機 gate（**強制，必做，不得 deferred**）
在 OCTOPUS / CC2674 rig 上以下列**驗證過的流程**完成一次實燒：

```bash
# ── DUT console（serialwrap console session）── 讓 MCU 進 BSL ──
echo 1fbf0300.serial > /sys/bus/platform/drivers/of_serial/unbind
grep -E "pin 1[34] " /sys/kernel/debug/pinctrl/1fbf0200.pinctrl/pinmux-pins
test -d /sys/class/gpio/gpio13 || echo 13 > /sys/class/gpio/export
echo in > /sys/class/gpio/gpio13/direction
test -d /sys/class/gpio/gpio14 || echo 14 > /sys/class/gpio/export
echo in > /sys/class/gpio/gpio14/direction
cat /sys/class/gpio/gpio13/direction /sys/class/gpio/gpio13/value
cat /sys/class/gpio/gpio14/direction /sys/class/gpio/gpio14/value
echo 31 > /sys/class/gpio/export 2>/dev/null; echo 54 > /sys/class/gpio/export 2>/dev/null
echo high > /sys/class/gpio/gpio31/direction
echo high > /sys/class/gpio/gpio54/direction
echo 0 > /sys/class/gpio/gpio31/value; echo 0 > /sys/class/gpio/gpio54/value; sleep 1
echo 1 > /sys/class/gpio/gpio54/value; sleep 1
echo 1 > /sys/class/gpio/gpio31/value; sleep 1

# ── host ── 改走 serialwrap 端點（取代原本的 -d /dev/ttyUSB1）──
ocp-mcu-upgrade -d <…/dev/ttyMCU> -b 115200 -t 8 -e -s -i /mnt/d/tftp/OCTOPUS_MCU_R0B_16bit.bin
```

**驗收條件**
- serialwrap 自動 sync-probe 認到 MCU 線（COM0/FTDI，非 console）→ 開 bridge。
- flasher 印 `Return error code : 0x0`（完整 erase + program + verify）。
- `led-test.sh -v` 版本回讀正確（對齊 `docs/arcadyan/versions.json`）。
- double-sync 不干擾：serialwrap probe 後 flasher 自身 connect 仍成功。
- 過程其他 COM 不受影響；daemon 不死；結束後該 session 自動恢復 console（READY）。
- RAW WAL 含本次 flash 的 TX/RX 證據。

### 8.4 回歸
- 不得引入**新的**測試失敗（既有 flaky：`test_five_agents_three_rounds_no_conflict`、
  `t8_full_run_simulation`、`test_t1_wal_reset_preserves_console` 不計）。
- `python3 -m pytest -q tests/` 與 `python3 -m policy_check --repo .` 通過。
