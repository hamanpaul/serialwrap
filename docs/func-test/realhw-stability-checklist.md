# 實機穩定性測試清單（realhw stability checklist，#122）

> 本清單是 `python3 -m realhw` 套件的**權威描述與手動 fallback**。suite 是它的可執行形式；本文件是它的可讀對照與 P2 手動索引。
> 每次重大更新**部署到本機系統後**執行——用已安裝的 `serialwrap` CLI 操作 live daemon 與兩塊真板（部署驗收），**不是**在沙箱裡跑 repo 程式碼（那是開發期驗證，見 P2 前置的 throwaway 隔離法）。
> case id 與 `python3 -m realhw --list` 完全一致（P0×8＋P1×20＋longrun×1，共 29 條）。
> 路徑一律以 `~` / `$HOME` 表示，不寫絕對 home 字面。

## 本機環境基準

| 項目 | 值 |
|---|---|
| 監管模式 | systemd（本機為 systemd-system，run-as-user） |
| live state | `~/.local/state/serialwrap/state.json` |
| live WAL | `~/.local/state/serialwrap/wal/raw.mirror.log`、`raw.wal.ndjson`（systemd 不繼承 shell 的 `SERIALWRAP_WAL_DIR`） |
| b-log（minicom capture／agent log） | `~/b-log/` |
| 報告輸出 | `~/b-log/realhw-reports/<YYMMDD-HHMMSS>/` |
| 兩板 | COM0＝`dut-prpl`（serial `AC01QZT0`、platform prpl）；COM1＝`sta-prpl`（serial `AQ00OAQ7`、platform brcm/BDK） |
| usbipd busid | COM0＝`8-1`、COM1＝`8-2`（**換線會變**，每輪跑前 `usbipd list` 重驗） |
| 組態 | `realhw/config.json`（stdlib-only 契約；機器特定值；timeouts：ready_wait 180s／reboot_wait 300s／human_active_window 60s） |

---

## 前置作業（preflight，任一不過整場拒跑）

suite 於 `realhw/preflight.py` 自動執行以下六項；手動跑時亦逐項確認。**跑本套件期間不得同時跑 `pytest tests/`**（#120 live guard 會把本套件對 live 的操作誤判為污染而 FAIL；suite 偵測到其他 pytest 會拒跑）。

### A. 部署新鮮度

```bash
# 比對本機是否落後 origin/main（VERSION 不每 PR 升版，版本號看不出落後）
git -C <repo> fetch -q origin && git rev-list --count HEAD..origin/main   # 期望 0；>0 僅警告不擋
serialwrap --version                                                       # 記錄進報告
```

落後即在報告記錄 commit 數（警告、不擋）；重大功能驗收前建議先重裝到最新 main。

### B. 環境清潔（無殘留污染）

```bash
# 1) 殘留 throwaway / pytest-iso daemon（coexist E2E 常洩漏，飽和系統造成 live COM 掉字）
pgrep -af 'sw-coexis[t]|sw-pytest-iso'          # 應為空
pkill -f 'serialwrapd.py.*sw-coexist'           # 清理法（有殘留時）

# 2) 其他 pytest 在跑（與本套件互斥）
pgrep -af 'pytes[t]'                            # 排除自身後應為空

# 3) live state.json 污染哨兵（測試洩漏把 /tmp/sw-* binding 寫進 live state）
python3 -c "import json,os;d=json.load(open(os.path.expanduser('~/.local/state/serialwrap/state.json')));print('/tmp/' in json.dumps(d.get('bindings') or {}))"  # 期望 False
```

> `pgrep`／`pkill` 一律用 character-class（`sw-coexis[t]`）或精確 pattern 防 self-match（`pkill -f 'serialwrapd'` 會殺到自己 exit 144）。

### C. 依賴健康＋兩板 READY

```bash
serialwrap doctor                               # checks[].ok 全綠（含 single_daemon）
serialwrap session list                         # 兩板 state==READY，device_by_id 含各板 serial
tmux -V && minicom --version | head -1          # 工具可用
'/mnt/c/Program Files/usbipd-win/usbipd.exe' list   # busid 可列
sudo -n true                                    # NOPASSWD sudo（restart 類 case 需要）
```

> doctor 的 `checks[]` 名稱鍵為 **`check`**（非 `name`），判讀時勿取錯鍵。

### D. 破壞性動作預告

suite 於選中含 destructive case 時，跑前印出本輪破壞性動作清單（板卡 reboot／daemon restart／usbipd 插拔）與報告目錄。手動跑破壞性 case 時，**一條一條跑、確認板子還原 READY 再跑下一條**。

### 通用還原

任一 case 失敗把板子弄到非 READY，續跑前先恢復：

```bash
serialwrap session clear --selector COM0 && serialwrap session attach --selector COM0
serialwrap device attach --selector COM1 --force
sudo -n systemctl restart serialwrap            # 最後手段（會重偵測所有 COM）
```

---

## P0 煙霧（tier=p0，~15 分鐘，全非破壞性）

`python3 -m realhw --tier p0`

| case id | 驗什麼 | 手動等效命令 | 判定 | 坑 |
|---|---|---|---|---|
| `p0-doctor` | daemon 與環境健康 | `serialwrap doctor`；`serialwrap session list` | doctor `checks[].ok` 全綠；兩板 `state==READY`；`device_by_id` 含各板 serial（COM0/AC01QZT0、COM1/AQ00OAQ7） | 名稱鍵是 `check`；同款晶片 by-id 相同須靠 serial 區辨 |
| `p0-cmd-async` | agent 命令端到端 | `serialwrap cmd submit --selector COM0 --cmd 'echo P0_$RANDOM' --cmd-timeout 12`→隔一拍`serialwrap cmd status --cmd-id <id>` | status `done`、stdout 含 marker（雙板逐板序列化） | submit 後立刻讀 status 有 line race，要隔拍輪詢；雙板 back-to-back 會撞 foreground busy |
| `p0-console-raw` | minicom 連線＋raw ownership | tmux 起 `serialwrap-minicom COM0`；`serialwrap session console-list --selector COM0`；send-keys 半個 `ec`+`Tab`；capture-pane | 至少一個 `interactive_owner:true`；Tab 補完出現 `echo` | 方向鍵/Tab 失效＝掉回 line-buffer（orphan lease #76/#99）；minicom 顯示 Offline（DCD 未拉）不影響輸入 |
| `p0-clear-reattach` | session clear 自動恢復 | `serialwrap session clear --selector COM0`；輪詢 `session list` | `ready_wait_s`（180s）內回 READY | clear 沿用舊 profile，不會改偵測 |
| `p0-selftest` | self-test 基本判讀 | `serialwrap session self-test --selector COM0` | `probe_ok=true`、`classification==OK` | 全譜情境見 P2 self-test 表 |
| `p0-blog-clean` | b-log 純淨度（ANSI 回歸） | tmux 開關一次 `serialwrap-minicom COM0`→檢查新 `~/b-log/mini_COM0_*.log` | 無 `Script started` 標頭、ESC(0x1b) 計數＝0 | 回歸根因＝script transcript 模式（6df17a5）；預設應為 minicom 原生 `-C`（PR#98）；繞過用 `MINICOM_CAPTURE_MODE=minicom` |
| `p0-wal-live` | WAL 活性與位置 | 戳 `echo X`→檢查 `~/.local/state/serialwrap/wal/raw.mirror.log` mtime＋grep marker | mtime 跳動、tail 含 marker | live WAL 一律在 XDG state（systemd 不繼承 shell env）；勿讀 stale `~/b-log/raw.*` 或測試洩漏的 `/tmp/sw-wal-*` |
| `p0-multiopen` | 無多開（two-reader） | `serialwrap daemon status` | `multi_open` 假、`foreign_holders` 無「自身 pid 以外」的 pid | `foreign_holders` 健康的單一 daemon 一定含**自身 pid**（非空）；真正 two-reader＝`multi_open` 為真或出現他 pid |

---

## P1 核心穩定性（tier=p1，20 條）

`python3 -m realhw --tier p1`（含破壞性；逐條驗收用 `--only <id>`，排除用 `--skip <id,...>`）
「⚡」＝destructive（reboot／restart／usbipd 插拔）。

### P1 console 對抗（7 條，coexist T 系列實機化；tmux+minicom）

| case id | 驗什麼 | 手動等效命令 | 判定 | 坑 |
|---|---|---|---|---|
| `p1-con-fanout`（T6） | RX fan-out | minicom 掛著→`serialwrap cmd submit --selector COM0 --cmd 'echo T6_*'` 依序 3 次→capture-pane | pane 依序全見 3 個 marker | 缺 marker 先查 console 沒掉回 line-buffer、無洩漏 daemon 掉字 |
| `p1-con-defer`（T7） | suspend/deferred/resume | send-keys `echo HUMAN_HALF`（不按 Enter）→`cmd submit --cmd 'echo T7_AGENT'`→補 `Enter` | agent cmd `done` 且耗時 ≤15s；補 Enter 後 pane 見 `HUMAN_HALF` 自成一行 | deferred flush 後 human 輸入不應與 agent byte 交錯 |
| `p1-con-busy` | human_active gating | send-keys 造 human_active→`serialwrap session interactive-open --selector COM0 --owner agent:realhw --timeout 10` | 回 `error_code==SESSION_INTERACTIVE_BUSY`（active 窗內不奪權） | human_active 窗＝60s（`HUMAN_ACTIVE_WINDOW_S`），剛敲鍵即在窗內 |
| `p1-con-softpreempt` | 閒置降級 | minicom 起後**不輸入**、等 `human_active_window_s+5`（65s）→`interactive-open` 同上→`interactive-close --interactive-id <id>` | open `ok` 且 `soft_preempted:true`；close 後 `console-list` 原 owner 恢復 | 閒置降級只降級**不中斷** human console |
| `p1-con-liveness` | 死亡偵測 | `pgrep -x minicom` 取本套件新起 PID（before/after 差集）→`kill -9 <pid>`→輪詢 `session self-test` | ≤60s 內 `human_attached` 轉 false | 勿 `pkill -f`（self-match exit 144）；孤兒只來自 SIGKILL/crash；只殺自己新起的 minicom |
| `p1-con-orphan`（#76） | 孤兒回收＋自癒 | 承 liveness 造孤兒→過 grace（~4s）→直接重開 `serialwrap-minicom COM0`→Tab 補完 | 不需 daemon restart 即拿回 raw ownership | grace 3s 內短暫 flap 不掉 line-buffer |
| `p1-con-second` | 第二 console line-buffer | 第一個 minicom 起→第二個 tmux 再起 `serialwrap-minicom COM0`→`console-list` | 恰一個 `interactive_owner:true`；`consoles`≥2（第二個走 line-buffer） | 第二 console 走 line-buffer 是契約，非 bug |

### P1 命令執行（3 條）

| case id | 驗什麼 | 手動等效命令 | 判定 | 坑 |
|---|---|---|---|---|
| `p1-cmd-modes` | 三模式＋錯誤碼 | line：`cmd submit --cmd 'echo MODE_LINE'`；background：`cmd submit --mode background --cmd 'for i in 1 2 3; do echo BG_$i; sleep 1; done'`→`cmd result-tail --cmd-id <id> --from-chunk N`；interactive：`session interactive-open --owner agent:realhw --timeout 20`→`interactive-send --data 'echo IA_OK' --encoding plain`＋`--data enter --encoding key`→`interactive-status`→`interactive-close`；錯誤面：`cmd submit --selector COM9 ...` | 三模式各自輸出正確；`interactive-status.screen` 含 `IA_OK`；不存在 selector 回 `SESSION_NOT_FOUND` | `interactive-send` 有效 encoding＝`plain`/`base64`/`key`（**無 `text`**）；background 收齊用 `result-tail` 的 `chunks`（list），非 `status.stdout` |
| `p1-cmd-serial` | 多來源序列化 | 5 條並發 thread 各 `cmd submit --selector COM0 --source agent:rhwN --cmd 'echo A{n}_R{r}_MARK'` 三輪→`wal export --from-seq <start>` | 每筆 `done` 且 stdout 無 cross-talk；各 source WAL TX 計數＝提交數 | arbiter per-session PriorityQueue 序列化並發 submit；`SESSION_QUEUE_FULL` 退避重試（非 foreground busy）；cross-talk＝某 source stdout 混入他 source marker |
| `p1-cmd-file` | 檔案傳輸＋RPC 不凍結 | 256KB 隨機檔 `file push --selector COM0 --local <f> --remote /tmp/rhw.bin`→背景每 0.5s `daemon status` 量延遲→`file pull ... --local <out>`→md5 比對→板上 `rm /tmp/rhw.bin` | round-trip md5 一致；探針最大延遲 <3s | **無 `health ping` 子命令**：以 `daemon status` 當輕量 RPC 探針（#52 歷史病灶 file.* 期間阻塞 19.8s） |

### P1 WAL（2 條）

| case id | 驗什麼 | 手動等效命令 | 判定 | 坑／還原 |
|---|---|---|---|---|
| `p1-wal-reset`（T1/T2/T3） | reset 契約＋console 不斷線 | `session console-attach --selector COM0 --label realhw` 取 `vtty`/`client_id`→tmux `cat <vtty>`→`wal current-seq`（0 則先 `echo SEED`）→`wal reset`→`wal current-seq`→`cmd submit 'echo T1_ALIVE'`→`console-list`／pane | reset 後 `current-seq==0` 後重新遞增；原 console client 仍在；pane 見 `T1_ALIVE`；`current-seq` 與 live WAL 檔尾 seq 相等 | 還原：`session console-detach --selector COM0 --client-id <cid>` |
| `p1-wal-fullrun`（T8） | orchestrator 模擬 | console-attach＋`cat vtty`→`wal reset`→3 輪 `cmd submit 'echo CASE_{i}_RESULT'`（每輪 `current-seq` 嚴格遞增）→`wal export --from-seq 0`→pane | 每輪 seq 嚴格遞增；export 有記錄；console 存活見全部 marker | 歷史 flaky（t8 假 PTY ~50%）在實機版應穩；async line race——等待要足；還原：console-detach |

### P1 重啟恢復（4 條，全 ⚡）

| case id | 驗什麼 | 手動等效命令 | 判定 | 坑／還原 |
|---|---|---|---|---|
| `p1-rst-daemon` ⚡ | restart 不變式（#100/#95） | 記兩板 `device_by_id`/`profile`＋`systemctl show -p MainPID serialwrap`→`log-start`/等 3s/`log-stop` 驗兩板 0 byte→`sudo -n systemctl restart serialwrap`→等兩板 READY | MainPID 變更；兩板 `device_by_id`/`profile` 逐板不變 | 板不安靜（byte_count>0）→**SKIP**（避免打斷 log）；MainPID 未變＝restart 未生效（NOPASSWD sudo？）；by-id 對調＝#100 rank 退化；profile 漂移＝#95。**還原＝無**（restart 即狀態） |
| `p1-rst-reboot` ⚡ | RECOVERING 自動恢復 | console-attach＋`cat vtty`→記 `wal current-seq`→`cmd submit 'reboot' --cmd-timeout 10`（status 可能 timeout，容忍）→輪詢見 `RECOVERING`/`DETACHED`/`ATTACHING`→等 READY | `reboot_wait_s`（300s）內自動回 READY；console client 跨 reboot 存活；WAL `end_seq>start_seq` | prplOS reboot 立刻回 prompt、1-3s 後才真斷；還原：console-detach＋clear+attach 等 READY |
| `p1-rst-bootwindow`（#69/#94） ⚡ | 開機窗自動重探 | `cmd submit 'reboot'`→等 8s 開機窗→`session clear --selector COM0`＋`session attach --selector COM0`（非致命 error_code 或 ok 皆記）→輪詢 `reprobe_attempts`／狀態 | 降級斷言：無人工介入下最終自動 READY 即 PASS；`reprobe_attempts` 記進 evidence | live profile timeout_s=10s 可能卡不住板 boot 窗——採降級斷言不視為 FAIL；還原：clear+attach 等 READY |
| `p1-rst-recover` ⚡ | recover TIMEOUT 複檢 | `session recover --selector COM0`（TIMEOUT/ok:false 都接受）→立刻 `session self-test --selector COM0` | self-test `probe_ok=true`＋`classification==OK`＝實已成功 | recover 回 TIMEOUT 常其實已成功（契約行為，非失敗）；`bridge_generation` 記進 evidence |

### P1 裝置交接（2 條，全 ⚡，#54；對象＝COM1）

| case id | 驗什麼 | 手動等效命令 | 判定 | 坑／還原 |
|---|---|---|---|---|
| `p1-ho-cycle` ⚡ | release→外部佔用→收回 | 記 COM1 `attached_real_path`→`device release --selector COM1 --source agent:realhw --reason 'realhw p1-ho-cycle'`→tmux `minicom -D <real_path> -b 115200`（外部持有者）→`daemon status`（`foreign_holders` 供參）→`device attach --selector COM1`（回 `DEVICE_STILL_HELD`）→kill 外部 minicom→`device attach --selector COM1` | release 後 COM1 `RELEASED`；外部持有時 attach 回 `DEVICE_STILL_HELD`；kill 後收回 READY；COM0（prpl）全程不受擾 | `foreign_holders` 只掃 serialwrapd fd，外部 minicom 不列入（僅供參考）；`DEVICE_STILL_HELD` 靠 `_probe_external_holder` 掃 `/proc`；還原：kill 外部 minicom＋`device attach --selector COM1 --force` 等 READY |
| `p1-ho-persist` ⚡ | RELEASED 跨 restart | `device release --selector COM1 ...`→`sudo -n systemctl restart serialwrap`→等 COM0 READY→驗 COM1 仍 `RELEASED`→`device attach --selector COM1` | restart 後 COM1 保持 `RELEASED`（不被搶回）、COM0 照常 READY；attach 後 COM1 回 READY | released map 自 `state.json` 還原；還原：`device attach --selector COM1 --force` |

### P1 usbipd 插拔（2 條，全 ⚡；busid 換線會變）

| case id | 驗什麼 | 手動等效命令 | 判定 | 坑／還原 |
|---|---|---|---|---|
| `p1-hp-cycle` ⚡ | 同板拔插回原槽 | `usbipd list`（驗 COM1 busid 存在）→`usbipd detach -b <COM1 busid>`→輪詢 COM1 離開 READY→`usbipd attach -w -b <busid>` | ≤30s COM1 轉非 READY、COM0 不受擾；回插後 COM1 自動回原 COM READY、`device_by_id` 含 serial | busid 不在 `usbipd list`＝換線→**SKIP**（非 FAIL）；熱插沿用 DETACHED-rebind（同 by-id 回空槽 #100）；還原：缺席 busid 補 `attach`＋兩板等 READY |
| `p1-hp-reorder` ⚡ | 反序插拔＋restart COM 不對調（#100） | `usbipd list`（驗兩板 busid）→`detach` 兩板→**反序** `attach`（COM1 busid 先）→兩板回原 COM→`sudo -n systemctl restart serialwrap` | 反序回插後兩板各回原 COM（by-id 認板、非列舉序）；restart 後 startup rank 下仍 COM0=AC01QZT0/COM1=AQ00OAQ7 | real_path 可能翻轉（記 evidence）、by-id 不變；還原：兩 busid 補 `attach`＋restart 後等兩板 READY |

---

## 長跑（tier=longrun，無人看護）

```bash
python3 -m realhw --tier longrun --duration 48h      # <N>h/<N>m/<N>s；預設（省略時）32h
# 短跑全鏈路驗證：
python3 -m realhw --tier longrun --duration 15m
```

長跑**不含在 `--tier p1` 內**，必須顯式 `--tier longrun`（使用者情境：放假/下班前放下去跑）。單一 case `lr-mixed`：

- **負載**：4 個 agent worker（輪流對兩板，line／background／interactive 混合、每動作唯一 marker，同板以 per-board lock 序列化避免 FOREGROUND_BUSY 噪音）＋1 個模擬 human（tmux `serialwrap-minicom COM0`，每 2-5 分鐘敲一行 `HUMAN_TICK`，minicom 死了記事件後重開）＋1 個 snapshot thread（每 `snapshot_interval_s`＝300s 記狀態/RSS/pid）。
- **無人看護**：case 級異常記事件後續跑；**重大事件**（daemon pid==0／兩板同時非 READY 持續 >15min）→ 記事件、停止負載、**保留現場（不自動重啟 daemon）**。`Ctrl-C`（SIGINT）提前收斂＝記 sigint 事件、停負載，仍產出分析報告。

**報告落 `~/b-log/realhw-reports/<YYMMDD-HHMMSS>/lr-mixed/`**，三個檔：

| 檔案 | 內容 |
|---|---|
| `snapshots.ndjson` | 每 5 分鐘一筆 `{t, sessions:{com:state}, rss_kb, pid}`（RSS 讀 `/proc/<MainPID>/status` VmRSS） |
| `events.ndjson` | 逐動作 append+flush：`{t, source, kind, ...}`（kind＝submit/done/error/busy/tick/major/sigint…；即時 flush，無人環境 log 完整優先） |
| `longrun-analysis.md` | 事後 `analyze()` 收斂：per-source 命令計數（submit/done/error）、`stuck_attached`（連續非 READY 區段起訖與時長）、`pid_changes`／`daemon_death_at`、RSS 首尾趨勢、事件時間線（只列值得注意者）、與歷史基線對照（32h／31 segments／submitted 49,899；主要退化模式＝卡 ATTACHED 未回 READY） |

**FAIL 判準**：重大事件、`daemon_death_at` 非空、單板連續非 READY ≥15min、或命令錯誤率 >50%（submit≥10）。否則 PASS。

---

## P2 手動程序（排除於自動化；完整索引與程序）

以下情境需特定環境或天然互動，未納入 suite，改以本節手動程序驗證。

### P2-1 MCU flash `/dev/ttyMCU` 完整程序（#55，POSIX-only）

serialwrap 原生 MCU 韌體升級端點，daemon 維持 tty 唯一 reader、sync-probe 自動認線、FLASHING 仲裁。

**隔離跑法（不動 prod / 人類 minicom）**：prod daemon 不停；用獨立 socket/state/run 的 **throwaway daemon** 跑待測程式碼（`SERIALWRAP_RUN_DIR` / `SERIALWRAP_STATE_DIR` / `SERIALWRAP_BY_ID_DIR` 等 env）。**關鍵**：`SERIALWRAP_BY_ID_DIR` 指向只放「MCU 線（FTDI）by-id symlink」的 **FTDI-only sandbox** 目錄，否則動態偵測會抓到被人類 minicom 佔住的 DUT console（ttyUSB0）造成 two-reader 衝突。

**進 BSL**：在 DUT console（如 CH340/ttyUSB0）下 GPIO BSL-invoke（unbind `1fbf0300.serial`、GPIO13/14 設 in、GPIO31/54 reset）。**長指令會在 UART console 被截斷 → 必須逐行短指令送**（`tmux send-keys -l` 每行 +Enter +sleep，**勿用 `;` 串長行**）。

**燒錄**：

```bash
ocp-mcu-upgrade -d <RUN_DIR>/dev/ttyMCU -b 115200 -t 8 -e -s -i <fw.bin>   # 期望 Return error code : 0x0
```

燒後 session 自動恢復 `ATTACHED`、daemon 不死、其他 COM 不受影響。清單查詢只走 `serialwrap mcu patterns` / `serialwrap mcu status`（**不經此 PTY 端點**）。

**三個只有實機才現形的坑（皆已修，回歸重點）**：

1. **idle 汙染**：端點未 bridge 時一律沉默、不主動寫任何 bytes（曾於 idle 寫支援清單→被 flasher 讀成假回應、汙染 SBL sync）。
2. **double-sync 吃 ACK**：認線 probe 必須用 flasher 自身的 sync bytes 並把 MCU 的 ACK 回放給 flasher；另注入獨立 sync 會吃掉 MCU 的 ACK，flasher 隨後自己的 sync 永遠收不到回應。
3. **PTY 無 EOF 卡 FLASHING**：daemon 同持 PTY master+slave（避免閒置 master 一直 EOF 空轉）→ flasher 關端點時 master 無 EOF；需以 holder-probe（`_probe_external_holder` 掃 pts）偵測 flasher 斷線才能結束 pump、離開 `FLASHING` 自動恢復。

> **平台範圍**：`/dev/ttyMCU` PTY-bridge 為 POSIX-only。Windows 韌體升級工具直接獨佔開啟 `COMx` 自行燒錄，serialwrap 只需 `device release` 該 COM（燒完 reclaim），對應 #54 語意（非 #55）。

### P2-2 U-Boot / bootloader recovery lease（#44/#114/#130）

在 bootloader 停板做救援（燒壞 fw 後救回）。

- **進 bootloader**：`serialwrap session interactive-open --selector COMx --allow-attached`——`ATTACHED` 且 RX tail 命中 bootloader prompt（如 `=> `）**或** boot banner（`Hit any key to stop autoboot` 倒數行／`U-Boot` 版本行，#114）時授予 recovery lease（回應帶 `recovery_mode=True`，倒數窗命中另帶 `boot_interrupt:true`）。
- **攔 autoboot**：開 lease→送 `reboot`→以 ~0.3s 間隔連打 `interactive-send`（space）約 30 秒攔「Hit any key to stop autoboot」窗→停在 `=> `→逐字驅動 U-Boot。（#130 boot quiet window 只 gate `source=system` 的自動 probe，**不擋 interactive lease 鍵擊**。）
- **驗證與還原**：`session self-test`（期望 `OK`/`probe_ok=True`/`READY`）→`cmd submit --cmd 'printenv' --mode line`；還原送 `boot` 回正常 OS→`device attach`／restart 讓 detection 重綁原 profile。

**坑**：多個 `passthrough` template 會搶 auto-detect 通用 fallback（通用 fallback 須限定非 command-capable passthrough）；實機 U-Boot prompt 可能大寫 `U-Boot> `，`prompt_regex` 用 `(?mi)` 大小寫不敏感。

### P2-3 self-test classification 全譜情境表

`serialwrap session self-test --selector COMx` 的 `classification` 依即時探測分類。手動製造各情境驗判讀正確（值取自 `sw_core/session_manager.py`）：

| 情境（如何製造） | 期望 classification | probe_ok |
|---|---|---|
| 健康 READY 板 | `OK` | true |
| 未登入（板停在 `login:`） | `LOGIN_REQUIRED` | false |
| reboot 進行中 | `REBOOTING` | false |
| 停在 bootloader（`=> `） | `BOOTLOADER` | false |
| autoboot 倒數安靜窗 | `AUTOBOOT_QUIET` | false |
| `platform=passthrough`（停 ATTACHED） | `PASSTHROUGH` | — |
| human console 正互動中 | `HUMAN_INTERACTIVE_ACTIVE` | — |
| `device release` 後 | `RELEASED` | — |
| recover 進行中 | `SESSION_RECOVERING` | — |
| 拔線／裝置消失 | `DEVICE_MISSING` | false |
| by-id 換板需重綁 | `DEVICE_REBOUND_REQUIRED` | false |
| bridge 掛掉／vtty stale | `BRIDGE_DOWN` / `VTTY_STALE` | false |
| 板無回應（無 prompt） | `TARGET_UNRESPONSIVE` | false |
| ATTACHED 但未確認可執行 prompt | `ATTACHED_NOT_READY` | false |

> `recover` 回 TIMEOUT 時以 `self-test` 複檢為準（`probe_ok=true`＋`OK`＝實已成功）。

### P2-4 安裝／監管模式轉換

```bash
./install.sh                          # pipx 隔離 venv + serialwrap setup（有 systemd→systemd-user，無→on-demand 降級）
./install.sh --system --with-sudo     # 本機系統安裝（systemd-system）
serialwrap doctor                     # 驗 python/pyyaml/PATH/dialout/systemd/監管模式/裝置
```

- `supervision_mode` 為單一事實來源（`~/.config/serialwrap/config.yaml`）；systemd 模式用 `serialwrap service start|stop|restart` 管理生命週期（`daemon start`/`daemon stop` 在 systemd 模式自動 route 到 `service`）。
- setup `transitioned:false` 不自動重啟；系統安裝後 restart 需 `sudo systemctl restart serialwrap`。
- 轉換驗證：切換模式後 `serialwrap doctor` 的 `supervision_mode`／`single_daemon` 應一致、無殘留第二 daemon（`daemon status` 的 `multi_open`/`foreign_holders`）。
- 舊版偵測：`detect_legacy_install` 對 `~/.paul_tools` 只給退役指引、不刪除。

### P2-5 Windows loopback console（#84 PORT-2）

Windows 無 PTY，human console 改走 `127.0.0.1` TCP listener（預設 RPC `tcp://127.0.0.1:48700`），由 TeraTerm/PuTTY 連入，每條連線即一個 socket-backed console，對端斷線以 socket EOF 偵測。手動驗：daemon start（Windows 直接可用、detached、冪等）→TeraTerm 連 loopback port→`serialwrap session console-list` 見該 console→斷線後 count 回落。

---

## 坑速查表

| 坑 | 症狀 | 對策 |
|---|---|---|
| `pkill -f serialwrapd` self-match | 命令自己 exit 144 | 用 character-class（`serialwrapd.py.*sw-coexis[t]`）或精確 pattern |
| 洩漏 throwaway daemon 飽和系統 | live COM 掉字／卡頓 | `pgrep -af 'sw-coexis[t]'` 查、`pkill -f 'serialwrapd.py.*sw-coexist'` 清、stop user service |
| 測試未隔離 `SERIALWRAP_STATE_DIR` | fake binding/alias 寫進 live `state.json`（持久化、survive restart） | preflight state 污染檢查；清 live state |
| 讀錯 WAL 檔 | 讀到 stale `~/b-log/raw.*` 或 `/tmp/sw-wal-*` | live WAL 一律 `~/.local/state/serialwrap/wal/`（systemd 不繼承 shell env） |
| submit 後立刻讀 status | line race，讀到空/半行 | 隔一拍再輪詢 `cmd status`（suite 的 `submit_and_wait` 已隔拍） |
| 雙板 back-to-back submit | 撞 foreground busy | 逐板序列化 |
| minicom Tab/方向鍵失效 | 掉回 line-buffer（`[A` 症狀） | 多半 orphan lease 佔授予閘（#76/#99）；重開或等孤兒回收 |
| minicom b-log 夾 ANSI | `mini_*.log` 含 transcript 標頭/ESC | 預設應 minicom 原生 `-C`（PR#98）；繞過 `MINICOM_CAPTURE_MODE=minicom` |
| restart 後 COM↔板對調 | by-id 對調 | #100 startup rank；`p1-rst-daemon`／`p1-hp-reorder` 守 |
| profile 偵測漂移 | 吵板 fallback passthrough/others | 趁安靜（capture 0 byte）restart 重偵測 |
| recover 回 TIMEOUT | 誤判失敗 | 常其實已成功，用 `self-test` 複檢 |
| WAL/CLI 時區 | WAL=UTC、`ls`=本機+8 | 對時比對時換算 |
| COM 物理板會換線 | busid/real_path 變 | 每輪 `usbipd list` 重驗，靠 serial（by-id）認板 |
| daemon RPC 單執行緒阻塞 | 長 handler 凍結全 daemon（#52，19.8s） | `p1-cmd-file` 以 `daemon status` 探針量延遲守 |
| 與 pytest 併跑 | live guard（#120）把本套件操作誤判 FAIL | preflight 拒跑；跑本套件期間不跑 `pytest tests/` |

---

## 參考

- 設計與 case 目錄：`docs/superpowers/specs/2026-07-02-realhw-stability-suite-design.md`
- 實作計畫（逐 case 規格）：`docs/superpowers/plans/2026-07-02-realhw-stability-suite.md`
- CLI 契約與狀態機：`README.md`、`docs/serialwrap-spec.md`
- 政策（測試/policy/分支/commit）：`CLAUDE.md`
