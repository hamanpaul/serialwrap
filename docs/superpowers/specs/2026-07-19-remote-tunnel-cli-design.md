# serialwrap `remote` 隧道便利 CLI 設計

- 日期：2026-07-19
- 分支：`feature/remote-tunnel-cli`
- 狀態：設計待複審（已納入 2026-07-19 Codex adversarial review 之安全／韌性修訂）

## 1. 背景與 root cause

serialwrap 現行的「Remote Support」文件（`README.md`、`sw_core/assets/skill/SKILL.md`）教的遠端連線模型是：

```
agent/RD 端  ── ssh -L 7777:127.0.0.1:7777 ──►  UART host（FAE）── socat ── serialwrapd.sock
```

即 **由 agent 端主動發起一條 inbound SSH 連進 UART host**（`ssh -L`，local forward），並在 UART host 上以 `socat` 把 AF_UNIX socket 橋成 TCP。

這個模型的隱含前提是「agent 端能主動連進放 UART 的那台機器」。實務上 UART host 常是使用者的**本機**，在 NAT／防火牆後、無對外 sshd，agent 進不來 → `ssh -L` 這一步根本建立不起來，就是觀察到的「無法連線」。

**Root cause = 連線發起方向錯配（directionality mismatch），非 client 程式 bug。** 佐證：

- `sw_core/client.py` 已完整支援 `tcp://host:port`（`_parse_endpoint` / `_rpc_call_once`），client 端不是瓶頸。
- POSIX daemon 只透過 `select_rpc_backend()` 起 AF_UNIX server（`sw_core/rpc_posix.py`），**無原生 TCP／遠端 listener**；`daemon start --endpoint tcp://…` 在 POSIX 一律 `REMOTE_NOT_SUPPORTED`。遠端存取被刻意外包給「外部 bridge + SSH tunnel」。

實際能通的方向是**由 UART host 主動撥出**的反向隧道（`ssh -R`）：本機撥出去，把 daemon socket 推到對端，agent 再打對端 loopback 即被隧道回本機。使用者目前手動這樣做，本設計把它收斂成 serialwrap 原生便利指令。

## 2. 目標與非目標

### 目標

- 新增 `serialwrap remote` 子命令群，用**極簡語法**（比照 `--endpoint` 的 `host:port`）在 runtime 按需拉起／關閉 SSH 反向隧道，讓遠端 agent 取得本機 serialwrap 操作。
- **serialwrapd 不得重啟、不新增 listener**：隧道只是外部 `ssh` 子行程，讀既有 daemon socket，daemon 零改動。
- **不做為預設**：永不自動啟動，只在使用者顯式執行時開啟。
- direct（agent host 可被撥入）與 relay（雙方皆 NAT 後）**同一套指令都支援**。
- **help 與所有面向 agent 的文件同步修正**，務必讓 agent 知道如何使用與連線。
- **信任邊界誠實化**（adversarial review 修訂）：明確界定「誰能操控 DUT」的邊界，並在**不改 daemon**的前提下提供把關手段（遠端 unix socket 檔案權限、loopback bind 不變量、單租戶 relay 要求）。

### 驗收條件（acceptance gate）

以 docker 模擬三種拓樸，**三者皆須成立才算測試通過**（見 §11）：

1. **direct**：agent host 可被 UART host 撥入。
2. **NAT → host**：UART host 在 NAT 後，主動 `-R` 撥出到可達的 relay/agent host。
3. **NAT ← client**：agent／client 亦在 NAT 後，主動 `-L` 從 relay 拉回本機 loopback（與 2 合為雙 NAT relay 全鏈）。

每個拓樸都須同時驗證：
- **預設不啟用**：跑 `serialwrap remote …` 之前，無隧道行程、無 state 檔、遠端 endpoint 不可連。
- **手動啟動後正常**：`serialwrap remote …` 後，agent 端可透過隧道 `session list` / `cmd submit` 並取得正確結果。
- **serialwrapd 不重啟**：daemon pid 全程不變。
- **loopback 不變量與信任邊界**（adversarial review）：轉發出的 listener 只綁 loopback（`ss` 斷言非 `0.0.0.0`／`::`）；第三方攻擊者容器行為符合 §8 界定——tcp loopback 模式下「同機可連＝已知殘留風險」被實測記錄，遠端 unix socket 模式下無檔案權限者不可連。
- **readiness 誠實**：慢速／失敗認證時 `remote` 不得回假 `ok`（回 `starting`／錯誤，見 §4）。

### 非目標（本期不做）

- 不在 daemon／RPC 層做任何改動（純 CLI 便利層）。
- **不在 RPC 層加認證／peer-credential 檢查**（adversarial review finding 1）：完整解法需改 daemon，牴觸「daemon 零改動」。**殘留信任邊界**——凡能連上被轉發 endpoint 的主體即可全權操控 DUT（`command.submit`／`file.push`／`daemon.stop`，RPC request 僅 `id/method/params`、無 token；socket 本機權限為 0660 group）——**以 §8 的手段緩解而非消除**：優先 `-R` 到**遠端 unix socket**（用檔案權限把關，等同把本機 0660 語意延伸到 relay）、明列**單租戶／可信 relay** 要求、loopback bind 不變量。若 relay 為多租戶共享主機且未用遠端 unix socket，此殘留風險存在，文件須明講。
- 不自製 SSH（不引入 paramiko）；auth 全數委由系統 `ssh`。
- 不做 relay 伺服器、不做隧道健康自動修復以外的 orchestration。
- 不改變既有 `--endpoint` 語意；agent 端 direct 情境仍用既有 `--endpoint tcp://…`。
- **native Windows UART host 本期不支援**（R2 finding 4）：lifecycle 全用 POSIX primitive，Windows 無等價；native Windows 執行 `remote` 回 `REMOTE_NOT_SUPPORTED`，defer 至 §13（Windows 現行仍可手動 `ssh -R`）。

## 3. 使用者介面（CLI surface）

沿用熟悉的 `host:port`，方向借用 ssh 同名的 `-R`／`-L`：

```bash
# 你本機（UART host）：反向把本機 daemon 推到對端 —— 日常唯一要記的一行
serialwrap remote user@host:7777            # 預設 = -R（reverse / expose）
serialwrap remote -R user@host:7777         # 顯式 reverse

# agent 端（只有 relay / 雙 NAT 才需要）：把對端 port 拉回本機 loopback
serialwrap remote -L user@host:7777

# 查看 / 關閉
serialwrap remote                            # 裸命令 = 列目前隧道（等同 status）
serialwrap remote status                     # 同上（顯式）
serialwrap remote close 7777                 # 拆除指定 port 的隧道
serialwrap remote close all                  # 拆除全部
```

### positional 與旗標

- positional target = `[user@]host:port`。`host` 可為 `~/.ssh/config` 的 Host alias → 可短到 `別名:port`。
- **方向預設 `-R`**（reverse／expose）；`-R`／`-L` 互斥，`-L` 需顯式。
- 罕用旗標（皆有預設，日常免帶）：
  - `--autossh`：以 `autossh -M 0` 取代 `ssh`，斷線自動重連。
  - `--local N`：`-L` 時本機 loopback port（預設 = target port）。
  - `--socket EP`：`-R` 時要暴露的本機 daemon endpoint（預設 = `_resolve_endpoint` 解析結果）。
  - `--remote-socket PATH`（**硬化模式**，R1 finding 1／R2 finding 1）：`-R` 時遠端改建 **unix socket** 而非 tcp loopback port，用檔案權限把關；**成對地** `-L` 亦以 `--remote-socket PATH` 指定要連的 relay-side unix socket（雙 NAT 全鏈需兩側配對，見 §4.2）。agent 端仍用回傳的 `tcp://127.0.0.1:<local>`。**共享／多租戶 relay 建議必開**；省略則預設 tcp loopback（便利但受 §8 殘留風險）。
  - `--ready-timeout S`：readiness 確認上限（預設 10s）；逾時仍未確認 forward 建立 → 回 `starting`（見 §4）。
  - `--ssh-opt "..."`：透傳額外 ssh 參數（可重複），例如 `-o ServerAliveInterval=30`、`-p 2222`。
- 所有子動作輸出緊湊 JSON（`ensure_ascii=False, separators=(",",":")`，含 `ok`），對齊 CLI 慣例。

## 4. 行為規格

### 4.0 共同：tunnel identity 與 readiness（adversarial review R1 findings 3/5、R2 findings 2/5/6）

- **tunnel identity**（R2 finding 6）= **canonical effective forward spec** 的雜湊，須涵蓋所有會改變「入口位置／目的地」的欄位，**至少**：`role`、`ssh_target`、遠端 bind 類型與位址（tcp `127.0.0.1:<port>` 或 unix `PATH`）、`local`（-L）、`forward_src`／`forward_target`、`via`（ssh／autossh）、以及目的地相關 `--ssh-opt`（`-p`／`ProxyJump` 等）。避免「改 `--remote-socket /a→/b`、換 port／ProxyJump 卻被誤判 already_running、舊入口續暴露」。listen_port（`-R`＝遠端 bind port、`-L`＝本機 local port）僅供顯示與 `close` 選取。
- **readiness 協定（骨架）**：spawn 後**不以「行程還活著」當成功**。附 `-o BatchMode=yes`（禁互動、不卡密碼提示）+ ControlMaster（`-o ControlMaster=auto -o ControlPath=<run>/remote/cm-<id>`），以 `ssh -O check` 輪詢至主連線確認「認證完成且 forward 已建立」（`ExitOnForwardFailure=yes` 保證 forward 失敗即 ssh 退出），或子行程提前死亡，或 `--ready-timeout` 到期：
  - **role 專屬補強（R2 findings 2/5）**：`ssh -O check` 只證明 master 存活＋本機 listener bind，**不證明端到端可用**。故 `active` 的最終判定另加：`-R` → 遠端 bind loopback 驗證（§4.1 step 4）；`-L` → 透過回傳 endpoint 打 serialwrap `health.ping` 成功（§4.2 step 4）。
  - 確認就緒（含 role 補強）→ 寫 `status="active"`，回 `{"ok":true,"status":"active",...}`。
  - 逾時但行程仍在 / role 補強尚未通過 → 寫 `status="starting"`，回 `{"ok":true,"status":"starting",...}`（**非** silent success；agent 應再 `remote status` 或探測 endpoint 確認）。
  - 行程已死 → `TUNNEL_SPAWN_FAILED`（附 stderr 尾），不留 active state。

### 4.1 `remote -R`（expose；跑在 UART host）

1. 解析 target 為 `(ssh_target, port)`；格式不符 → `INVALID_TARGET`。
2. 決定 **forward_target**（`--socket` 或 `_resolve_endpoint(args)` 決定本機轉發源）：
   - `--remote-socket PATH` → 遠端 unix socket：`-R PATH:<本機轉發源>`（硬化模式）。
   - 否則 → 遠端 loopback tcp：`-R 127.0.0.1:<port>:<本機轉發源>`（**顯式綁 loopback**，見 §5/§8）。
   - 本機轉發源：AF_UNIX 路徑（`ssh -R … :<sock>`，免 socat，需兩端 OpenSSH ≥ 6.7）；`tcp://127.0.0.1:<p>`（daemon endpoint 為 tcp 時，如 `SERIALWRAP_ENDPOINT` 覆寫）→ `127.0.0.1:<p>`。
   - 完全解析不到任何本機 endpoint → `NO_LOCAL_DAEMON`。解析到 unix 路徑但檔案暫不存在 → **僅 warn**（`ssh -R` 要等有連線進來才連 socket，daemon 稍後起即通）。
3. **identity 衝突檢查（持 registry flock，見 §6）**：同 listen_port 已有 state──
   - identity 完全相同且 active/starting → `{"ok":true,"already_running":true,...}` no-op。
   - identity 不同（換 target/forward）→ `TUNNEL_CONFLICT`（要求先 `remote close <port>`）。
   - state 在但行程已死 → 視為可覆寫。
4. 組 argv（§5），spawn detached，跑 readiness（§4.0）。**遠端 bind loopback 驗證（R2 finding 2，fail-closed）**：tcp 模式下 master 建立後，透過同一 ControlMaster 連線在 relay 執行 `ss -ltnH 'sport = :<port>'`（或等價），確認實際 bind：
   - 綁 `127.0.0.1`/`::1` → 續判 `active`。
   - 偵測 wildcard（`0.0.0.0`/`::`，即 relay `GatewayPorts yes` 漂移）或**無法驗證** → **立即拆除隧道 + 回 `REMOTE_BIND_UNVERIFIED`**（絕不留暴露隧道）。
   - `--remote-socket`（unix）模式免此驗證（不受 `GatewayPorts` 影響，天然 fail-closed）。
   依結果寫 state（§6）並回：
   ```json
   {"ok":true,"status":"active","role":"expose","pid":12345,"listen_port":7777,
    "target":"user@host","forward_target":"unix:///run/.../serialwrapd.sock","via":"ssh",
    "remote_hint":"agent 端用 serialwrap --endpoint tcp://127.0.0.1:7777（--remote-socket 時 unix://PATH）"}
   ```

### 4.2 `remote -L`（connect / relay；跑在 agent host）

1. 解析 target 與本機 `--local`（預設 = target port）。
2. identity 衝突檢查同 §4.1 step 3（以 local_port 選取）。
3. 決定 `-L` 遠端目的地（**R2 finding 1**，與 `-R` 硬化模式配對，否則雙 NAT 全鏈連不上）：
   - `--remote-socket PATH` → `-L 127.0.0.1:<local>:PATH`（連 relay 上那個 unix socket；硬化模式的雙 NAT 全鏈唯一走得通的方式）。
   - 否則 → `-L 127.0.0.1:<local>:localhost:<port>`（連 relay 的 tcp loopback port）。
   兩者皆**顯式綁本機 loopback**（見 §8）。
4. spawn，跑 readiness（§4.0）。**`-L` 端到端 readiness（R2 finding 5）**：本機 listener 能 bind、`ssh -O check` 通過，都不代表上游 `-R` 存在；故 `active` 須以回傳 endpoint 打 serialwrap `health.ping` 得有效回應為準：
   - `health.ping` ok → `active`。
   - listener 起但 ping 失敗（上游 `-R` 缺失／已斷／接受即斷）→ `starting`（未達 active）；`--ready-timeout` 內未 ok 維持 `starting`。
   寫 state，回：
   ```json
   {"ok":true,"status":"active","role":"connect","pid":23456,"local_port":7777,
    "target":"user@relay","endpoint":"tcp://127.0.0.1:7777"}
   ```
   `endpoint` 即 agent 接著餵給 `--endpoint` 的值。
5. 本機 `local` port 已被佔（非本工具的隧道）→ `PORT_IN_USE`。

### 4.3 direct 情境（agent 端免 connect）

UART host 的 `-R` 已直接落在 agent host 時，agent 端**不需任何新指令**，照舊：

```bash
serialwrap --endpoint tcp://127.0.0.1:7777 session list
```

`remote -L` 僅在 relay／雙 NAT 時使用。文件（§10）須清楚標示這條岔路，避免 agent 誤用。

### 4.4 `remote status`（含裸 `remote`）

掃 state registry，對每筆以 **pid + 行程啟動時刻**（§6，防 PID reuse）驗證存活，死的 prune 掉；`starting` 狀態順手重探是否已 `active`。輸出：

```json
{"ok":true,"tunnels":[
  {"status":"active","role":"expose","pid":12345,"alive":true,"listen_port":7777,"target":"user@host","via":"ssh"},
  {"status":"starting","role":"connect","pid":23456,"alive":true,"local_port":7778,"target":"user@relay","endpoint":"tcp://127.0.0.1:7778"}
]}
```

### 4.5 `remote close <port|all>`

對選中的隧道（持 registry flock）：
1. 以 **pid + 啟動時刻** 驗證確為本工具 spawn 的該行程（不符 → 略過該 pid，不誤殺被 reuse 的 PID）。
2. 對其 **process group** 送 `SIGTERM`（`os.killpg`），bounded 等待退出（如 5s），未退再 `SIGKILL`。
3. 確認退出後才移除 state 檔與 ControlPath；**驗證／等待失敗 → 保留 `status="error"` state**（不靜默刪，讓孤兒隧道仍能被 `status`／再次 `close` 看到、處理）。

找不到該 port → `{"ok":true,"closed":[]}`（冪等）。

## 5. 隧道 argv 組裝

- 基底：`["ssh","-N","-T", *default_opts, *ssh_opts, direction_args, ssh_target]`，spawn 時 `stdin=DEVNULL`（配合 `BatchMode=yes`，禁互動認證卡死——finding 3）。
- 預設 `-o`（可被 `--ssh-opt` 覆寫）：
  - `BatchMode=yes`：禁密碼／passphrase 互動提示；認證只走金鑰／agent，失敗即退出而非卡住。
  - `ExitOnForwardFailure=yes`：forward 建不起即 ssh 退出，供 readiness 判死（配合 §4.0）。
  - `ControlMaster=auto`、`ControlPath=<run>/remote/cm-<id>`、`ControlPersist=no`：供 `ssh -O check` readiness 探測與精準 teardown。
  - `ServerAliveInterval=30`、`ServerAliveCountMax=3`。
- `-R`（expose）：
  - `--remote-socket PATH` → `["-R", f"{PATH}:{forward_src}"]`（遠端 unix socket，硬化）。
  - 否則 → `["-R", f"127.0.0.1:{port}:{forward_src}"]`（**顯式遠端 loopback bind**）。
  - `forward_src` = 本機 unix socket 路徑 或 `127.0.0.1:{tcpport}`。
  - **GatewayPorts 前提**（finding 2）：遠端 bind 是否真的只綁 loopback，最終受 **relay sshd 的 `GatewayPorts`** 決定——`GatewayPorts yes` 會強制 remote forward 綁 wildcard，client 無法覆寫。故本設計要求「relay `GatewayPorts no`（預設）」為前提，於 docker gate 以 `ss` 斷言實測（§11），文件明講此前提。
- `-L`（connect）：`--remote-socket PATH` → `["-L", f"127.0.0.1:{local}:{PATH}"]`（連 relay unix socket）；否則 → `["-L", f"127.0.0.1:{local}:localhost:{port}"]`。皆**顯式本機 loopback bind**（不受 client `GatewayPorts` 影響——R1 finding 2）。
- `--autossh`：改用 `["autossh","-M","0", ...其後同 ssh...]`（pid 為 autossh；readiness／close 對 autossh **process group** 操作，其子 ssh 由 autossh 管）。
- **Windows UART host：本期不支援（R2 finding 4，defer §13）**——lifecycle 全用 POSIX primitive（`start_new_session`／`flock`／`os.killpg`／`/proc` start-ticks），native Windows 無等價（detach 需 `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP`、無 flock/killpg//proc）。故 `remote` **僅 POSIX（Linux/WSL 生產路徑）**；native Windows 執行 `remote` → 回 `REMOTE_NOT_SUPPORTED`（明確拒絕，不半實作）。Windows 現行仍可手動 `ssh -R`。

## 6. 背景行程與 state registry

- spawn：`subprocess.Popen(..., start_new_session=True, stdin=DEVNULL, stdout=log, stderr=log)`（setsid：脫離父行程與終端，並使子行程自成 process group／pgid，供 §4.5 `killpg`）。log 檔在 state 目錄下（供 `TUNNEL_SPAWN_FAILED` 撈 stderr 尾）。
- state 目錄：`$RUN_DIR/remote/`（`RUN_DIR` 預設 XDG runtime，與 socket／lock 同層；reboot 清掉剛好隧道也一併消失）。
- **registry 鎖（R1 finding 5）**：`$RUN_DIR/remote/.registry.lock` 的 flock，包住每次 `check → spawn → 立即寫 durable state → readiness → atomic replace`（`os.replace`）與 `close` 的 read-verify-remove，序列化並行 `remote` 操作，消除「兩個並行 start 同時通過檢查」競態。沿用 `sw_core/lock_posix.py` 既有 flock 慣例。
- **crash-consistent 寫入順序（R2 finding 3）**：`Popen` 一返回即取 `pid`／`pgid`／`control_path`，**先** atomic 寫 `status="spawning"` 的 durable state，**才**跑 readiness；readiness 結果再 replace 成 `active`／`starting`／清除。如此 CLI 於 spawn 與 readiness 間被 SIGKILL／例外，ssh 雖脫離父行程續活，仍留有可被 `status`／`close` 找到的 state（指向 pgid）。**殘留窗**僅 `Popen` 返回到首次寫檔的次毫秒級；另以 **orphan scan** 兜底：`status`／`close` 掃 `$RUN_DIR/remote/cm-*` control socket 與對應 ssh 行程，活著卻無 state → 標為 orphan 列出（可 `close`）。
- 每條隧道一檔 `$RUN_DIR/remote/<listen_port>.json`：
  ```json
  {"identity":"<hash(canonical effective forward spec：role,target,remote_bind,local,forward_src,via,dest-ssh-opts)>",
   "status":"spawning|active|starting|error",
   "role":"expose|connect","pid":12345,"pgid":12345,"pid_start_ticks":8837201,
   "target":"user@host","listen_port":7777,"remote_bind":"127.0.0.1:7777|unix:/relay/x.sock",
   "forward_target":"unix:///…","via":"ssh|autossh","control_path":"…/cm-<id>",
   "argv":["ssh","-N",...],"started_mono":123456.7,"started_wall":"2026-07-19T..."}
  ```
  以 `sort_keys=True` 寫出（對齊 state.json 慣例）。
- **存活與身分驗證（R1 finding 4）**：`os.kill(pid,0)` 只判 pid 存在，無法分辨 **PID reuse**；故另存 `pid_start_ticks`（Linux `/proc/<pid>/stat` 第 22 欄），驗活時比對一致——不符即「該 pid 已非我方隧道」→ 不誤殺、prune 舊 state。`autossh` 情境 `pid`／`pgid` 為 autossh leader；status／close 驗證並操作**整個 pgid**（涵蓋其子 ssh），非只父 pid。非 Linux POSIX 無 `/proc` → 本期不支援（§5 Windows 排除；其餘非 Linux POSIX 退化 pid-only、best-effort、文件標注）。
- idempotency／conflict／close 規則見 §4.1、§4.4、§4.5。
- **與 serialwrapd 生命週期完全解耦**：daemon 死了隧道還在（連過去 EOF）；daemon 重啟不需重開隧道（AF_UNIX 路徑不變）。

## 7. daemon socket 解析（-R）

重用 `sw_core/cli.py::_resolve_endpoint(args)`（既有優先序：`--endpoint` > `--socket` > config.yaml > 平台預設）取得本機 daemon endpoint，再依 transport（unix／tcp）決定 `-R` 的 forward target。不新增解析邏輯、不改寫 config（維持 CLI 對 config.yaml 唯讀）。

## 8. 安全（adversarial review R1+R2 findings 1/2）

**信任邊界（trust boundary）**——SSH 只認證「誰建立隧道」，**不認證之後連入被轉發 endpoint 的 client**。RPC request 僅 `id/method/params`、無 token，daemon 不做 peer 認證（本期不改，見 §2 非目標）。故：

- **本機（UART host）**：daemon socket 為 0660＋group，受檔案權限把關。
- **relay／agent host 的轉發 endpoint**：
  - tcp loopback（預設）→ **同機任何本機使用者／程序皆可連**並全權操控 DUT（`command.submit`／`file.push`／`daemon.stop`）＝**殘留風險**。**要求 relay／agent host 為單租戶／可信主機**。
  - `--remote-socket PATH`（硬化）→ 遠端 unix socket 受檔案權限把關，等同把 0660 語意延伸到 relay，**多租戶／共享 relay 建議必開**。

**loopback bind 不變量**：
- `-L` 顯式 `127.0.0.1:` bind，不受 client `GatewayPorts` 影響。
- `-R` 顯式 `127.0.0.1:` bind，但**最終受 relay sshd `GatewayPorts` 決定**——`GatewayPorts yes` 會強制綁 wildcard 對外、client 無法覆寫，且 forward 仍成功（`ExitOnForwardFailure`／`ssh -O check` 皆過），**光靠前提不夠**。**fail-closed（R2 finding 2）**：tcp 模式 active 前必以 master 連線在 relay `ss` 實測遠端 bind，非 loopback／無法驗證即**拆除＋`REMOTE_BIND_UNVERIFIED`**（§4.1 step 4）。硬化 `--remote-socket`（unix）不受 `GatewayPorts` 影響、天然 fail-closed，為多租戶安全預設；docker gate 另含 `GatewayPorts yes` 必須失敗案例（§11）。

**其他**：
- `BatchMode=yes` 禁互動認證，避免弱認證與卡死。
- auth（金鑰／known_hosts／agent forwarding）全交 ssh，serialwrap 不碰金鑰。
- `remote` 輸出與 `--help`／SKILL 文件標警語：**只透過 ssh-tunnel、單租戶 relay 或 `--remote-socket`，不可對網路直接開放**。

## 9. 錯誤處理（回 error_code，不讓例外穿越）

| 情境 | error_code |
|---|---|
| target 非 `[user@]host:port` | `INVALID_TARGET` |
| 無 `ssh`（或 `--autossh` 但無 `autossh`）於 PATH | `SSH_NOT_FOUND` |
| readiness 期間子行程死亡（認證／forward 失敗） | `TUNNEL_SPAWN_FAILED`（附 stderr 尾） |
| `-R` tcp 模式遠端 bind 非 loopback（`GatewayPorts` 漂移）或無法驗證 | `REMOTE_BIND_UNVERIFIED`（已拆除隧道，R2 finding 2） |
| 同 listen_port 已有**不同 identity** 的隧道 | `TUNNEL_CONFLICT` |
| `-R` 完全解析不到任何本機 endpoint | `NO_LOCAL_DAEMON` |
| `-L` 本機 port 已被佔（非本工具隧道） | `PORT_IN_USE` |
| `-R` 與 `-L` 同時指定 | `INVALID_ARGS` |
| native Windows 執行 `remote`（本期不支援，R2 finding 4） | `REMOTE_NOT_SUPPORTED` |

- **`status` 非 error**：`spawning`／`active`／`starting`／`error` 為隧道生命週期狀態，隨 `{"ok":true,...}` 回傳（`spawning`＝已 Popen、durable state 已寫、readiness 未跑完；`starting`＝forward／端到端 readiness 尚未確認，逾時走此；`error`＝`close` 驗證／等待失敗保留的孤兒 state，見 §4.5）。

## 10. 改動表面（顯式同步多表面）

### Code

- `sw_core/cli.py`：新增 `remote` subparser（positional target、`-R`/`-L`/`--autossh`/`--local`/`--socket`/`--remote-socket`/`--ready-timeout`/`--ssh-opt`、`status`/`close` 子動作）+ 平面 if/elif 分派。
- 新模組 `sw_core/remote_tunnel.py`：target 解析、canonical identity 計算、argv 組裝、spawn（注入式 spawner）、**readiness**（注入式 `ssh -O check` ＋ `-R` 遠端 bind `ss` 驗證 ＋ `-L` `health.ping`）、durable-intent 寫入 ＋ flock registry（state／identity 衝突／atomic replace／orphan scan）、status prune（pid+start-ticks、pgid）、robust close（verify→`killpg` 整組→wait→error-state）、native Windows 守衛回 `REMOTE_NOT_SUPPORTED`。純函式為主，副作用（Popen／flock／檔案／probe）集中且可注入。

### Docs / help / skill（本次硬需求：讓 agent 會用、會連）

- `sw_core/assets/skill/SKILL.md`：**改寫「Remote Support 用法（ssh-tunnel）」（現 line 92-112）**。現內容教舊的 `ssh -L`（inbound）正是誤導源頭，改為：
  - UART host：`serialwrap remote user@host:7777`（`-R` 預設）。
  - agent 端連線 runbook：direct → `serialwrap --endpoint tcp://127.0.0.1:7777 …`；relay → 先 `serialwrap remote -L user@relay:7777` 再用回傳 endpoint。
  - 明確標示 direct vs relay 岔路、`remote status`/`close`、`starting` 狀態需再確認、以及信任邊界警語（**單租戶／可信 relay**，共享 relay 用 `--remote-socket` 硬化）。
- `sw_core/assets/skill/SKILL_WINDOWS.md`：新增精簡 remote 段——native Windows **`serialwrap remote` 本期不支援**（回 `REMOTE_NOT_SUPPORTED`，R2 finding 4）；需遠端存取時**手動** `ssh -R 7777:127.0.0.1:48700 user@host`（Windows daemon 為 TCP loopback），agent 端照舊 `--endpoint tcp://127.0.0.1:7777`。
- `README.md`：**改寫「Remote Support（ssh-tunnel 遠端連線）」（現 line 1632+）**，以 `serialwrap remote` 為主軸、保留 `ssh -R`/`-L` 手動等價、涵蓋 direct/relay。依語言政策 **README 中英雙語並存**，英文段與繁中段一致更新。
- `README.md` cli-help markers（R-16）：新增 `serialwrap-remote-help` marker 區塊；因 `remote` 進入子命令列表，**一併再生 `serialwrap-help` 區塊**。
- `.paul-project.yml`：`cli:` 新增 `{command:"./serialwrap remote", help_args:["--help"], reflected_in:"README.md", marker:"serialwrap-remote-help"}`。
- `changelog.d/remote-tunnel-cli.md`：per-PR fragment（policy v1.0.10；無對應 issue，以 slug 命名）。

## 11. 測試策略

### 11.1 單元（`tests/test_remote_tunnel.py`）

- target 解析（`user@host:port`／`alias:port`／缺 port／壞格式）。
- argv 組裝：`-R` unix-socket／tcp／`--remote-socket` 三種 forward 形式、`-R` 顯式 `127.0.0.1:` bind、`-L` 顯式 `127.0.0.1:` bind、`--autossh`、`--ssh-opt` 透傳、預設 `-o`（`BatchMode`／`ExitOnForwardFailure`／`ControlMaster`／`ControlPath`）附加。
- identity／衝突（R1 finding 5／R2 finding 6）：identity 涵蓋 role/target/remote_bind/local/forward_src/via/dest-ssh-opt；同 identity→`already_running`；任一欄不同（含 `--remote-socket /a→/b`、`ssh→autossh`、換 `-p`／`ProxyJump`）→`TUNNEL_CONFLICT`；死 state→覆寫。
- readiness 狀態機（R1 finding 3／R2 findings 2/5；注入式 `ssh -O check`／`ss`／`health.ping` probe）：master+forward 確認且 role 補強通過→`active`；`-R` 遠端 bind 驗到 wildcard→`REMOTE_BIND_UNVERIFIED`+拆除；`-L` 上游缺失致 `health.ping` 失敗→`starting`；逾時行程仍活→`starting`；行程即死→`TUNNEL_SPAWN_FAILED`。
- crash-consistency（R2 finding 3）：spawn 後、readiness 前殺掉 CLI → durable `spawning` state 仍在且 `close` 能循 pgid 拆除；autossh leader 死亡→status 循 pgid 驗整組。
- `close`（R1 finding 4）：pid+start-ticks 驗證（**PID reuse**：start-ticks 不符→不殺、prune）、`killpg` 整組→wait→移檔；驗證／等待失敗→保留 `error` state。
- flock 並行（R1 finding 5）：barrier 式兩並行 start 同 port → 只成一條、另一條 `TUNNEL_CONFLICT`／`already_running`；並行 start/close 不留孤兒。
- 平台：模擬 native Windows（`os.name` monkeypatch）執行 `remote`→`REMOTE_NOT_SUPPORTED`。
- error_code：`INVALID_TARGET`／`SSH_NOT_FOUND`（PATH monkeypatch）／`INVALID_ARGS`／`NO_LOCAL_DAEMON`／`PORT_IN_USE`／`TUNNEL_CONFLICT`／`REMOTE_BIND_UNVERIFIED`／`REMOTE_NOT_SUPPORTED`。
- 生命週期以**注入式 spawner + 假長命行程**（`sleep`）＋**注入式 readiness probe**（`ssh -O check`／`ss`／`health.ping`）驗證，不真的起 ssh。

### 11.2 docker 拓樸驗收（acceptance gate；`tools/docker/remote_tunnel_test.sh`）

以真 sshd + 真 `serialwrap remote` 跑三種拓樸，**全過才算通過**。以 docker network 隔離模擬 NAT（不共網段＝互不可達，只能經 relay）。

**測試映像**：延伸現有 `serialwrap:remote-smoke`，加 `openssh-server`／`openssh-client`／`autossh`／`iproute2`（`ss`）+ 預燒 keypair（passwordless），內含 fake target + `serialwrapd`（AF_UNIX socket）。relay/agent sshd 明設 **`GatewayPorts no`**。另備 `attacker` 角色：同 relay 網段的獨立容器（無金鑰），及 relay 容器內第二個非特權使用者。

**拓樸 1 — direct**
- `net_direct`：`uart`（serialwrapd）、`agent`（跑 sshd）同網段。
- `uart`：`serialwrap remote -R tester@agent:7777`（`-R` 為預設，亦即裸 `serialwrap remote tester@agent:7777`）。
- `agent`：`serialwrap --endpoint tcp://127.0.0.1:7777 session list` / `cmd submit`。

**拓樸 2 — NAT → host**
- `net_a`：`uart`（NAT，僅與 `relay` 同網段）、`relay`（可達，跑 sshd，agent CLI 在此）。
- `uart`：`serialwrap remote -R tester@relay:7777` → agent CLI 於 `relay` 用 `--endpoint tcp://127.0.0.1:7777`。

**拓樸 3 — NAT ← client（雙 NAT relay 全鏈）**
- `net_a`：`uart`＋`relay`；`net_b`：`agent`＋`relay`。`uart` 與 `agent` **無共網段**（互不可達）。
- tcp 模式：`uart` `remote -R tester@relay:7777`、`agent` `remote -L tester@relay:7777` → 用回傳 `endpoint` 下命令。
- **硬化模式（R2 finding 1，須同樣端到端走得通）**：`uart` `remote -R tester@relay:7777 --remote-socket /run/relay-sw.sock`、`agent` `remote -L tester@relay:7777 --remote-socket /run/relay-sw.sock` → agent 端 endpoint 仍 `tcp://127.0.0.1:7777`，可下命令。

**每個拓樸的斷言（缺一不可）**
1. **預設不啟用**（跑 `remote` 前）：`serialwrap remote status` 回空 `tunnels`；`$RUN_DIR/remote/` 無 state 檔；無 serialwrap-spawn 的 `ssh`/`autossh` 行程；遠端 `--endpoint` 連線失敗（`SOCKET_ERROR`）。
2. **手動啟動後正常**：`remote -R`/`-L` 後 agent 端 `session list` 有 READY session、`cmd submit`→`cmd status` 得 `done` 且輸出正確。
3. **serialwrapd 不重啟**：擷取 daemon pid，全程（expose/connect/close 前後）不變。
4. **teardown 乾淨**：`serialwrap remote close all` 後隧道行程消失、state 檔清空、`--endpoint` 復歸不可連。
5. **loopback 不變量**（finding 2）：relay/agent 上 `ss -ltn` 斷言 forward listener 綁 `127.0.0.1`（非 `0.0.0.0`／`::`）；獨立 `attacker` 容器連 `relay:<port>` **必失敗**（證明非對外）。
6. **信任邊界實測**（finding 1）：relay 內第二個本機使用者連 `127.0.0.1:<port>`——tcp loopback 模式**可連**（記錄為已知殘留風險）；`--remote-socket` 模式無檔案權限者**被拒**（證明硬化有效）。
7. **readiness 誠實**（R1 finding 3/4）：錯 key／未知 host key／`BatchMode` 擋互動時 `remote` 回 `starting`／錯誤而非假 `ok`；真 sshd 下 PID reuse 不誤殺、並行 start/close 不留孤兒。
8. **GatewayPorts fail-closed**（R2 finding 2）：另備一台 relay sshd 設 `GatewayPorts yes`，`-R` tcp 模式必回 `REMOTE_BIND_UNVERIFIED` 且**無殘留隧道**（state/行程/listener 皆已拆）；同情境 `--remote-socket` 模式仍成功（不受影響）。
9. **`-L` 端到端誠實**（R2 finding 5）：relay sshd 正常但**無對應 `-R`**（上游缺 port／接受即斷）時，`remote -L` 回 `starting`（非 `active`），不誤報成功。

**執行與 CI**：docker 可用時列為必跑驗收；docker 不可用的環境明確 SKIP 並印原因（不靜默略過）。單元測試（11.1）恆入 CI。

### 11.3 政策

`python3 -m pytest -q tests/` 無新失敗；`python3 -m policy_check --repo .` 通過（R-09 fragment、R-16 help、R-18 docs 對齊）。

## 12. 相依與相容性

- 執行期需系統 `ssh`（`--autossh` 時另需 `autossh`）；`-R port:unix-socket`／`--remote-socket` 需 OpenSSH ≥ 6.7（2014，現行發行版皆滿足）；readiness 用的 `ControlMaster`／`ssh -O check` 亦為 OpenSSH 標配。
- PID-reuse 防護的 `pid_start_ticks` 依賴 Linux `/proc`（生產路徑 Linux/WSL）；非 Linux POSIX 退化 pid-only（best-effort，文件標注）。
- docker gate 另需 `openssh-server`／`iproute2`（`ss`），僅在測試映像內。
- 純 CLI 便利層，向後相容：不改 `--endpoint`／daemon／RPC；既有手動 `ssh -R`/`-L` + `--endpoint` 流程照舊可用。

## 13. 未來（defer）

- `remote` 隧道健康的主動探測／自動重連（超出 `--autossh` 的部分）。
- 多 UART host → 單 agent host 的 endpoint 名錄／自動選號。
- **native Windows `remote` expose lifecycle**（R2 finding 4）：Windows registry lock、process-tree／job-object teardown、creation-time identity、可用 readiness 與 native Windows 驗收；隨 #84 Windows daemon 面一併補。
- Windows daemon 端 `device release`／remote 編排（對齊 #84 尚未完成的 Windows daemon 面）。
