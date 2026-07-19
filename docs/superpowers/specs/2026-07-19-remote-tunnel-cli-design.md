# serialwrap `remote` 隧道便利 CLI 設計

- 日期：2026-07-19
- 分支：`feature/remote-tunnel-cli`
- 狀態：設計待複審

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

### 驗收條件（acceptance gate）

以 docker 模擬三種拓樸，**三者皆須成立才算測試通過**（見 §11）：

1. **direct**：agent host 可被 UART host 撥入。
2. **NAT → host**：UART host 在 NAT 後，主動 `-R` 撥出到可達的 relay/agent host。
3. **NAT ← client**：agent／client 亦在 NAT 後，主動 `-L` 從 relay 拉回本機 loopback（與 2 合為雙 NAT relay 全鏈）。

每個拓樸都須同時驗證：
- **預設不啟用**：跑 `serialwrap remote …` 之前，無隧道行程、無 state 檔、遠端 endpoint 不可連。
- **手動啟動後正常**：`serialwrap remote …` 後，agent 端可透過隧道 `session list` / `cmd submit` 並取得正確結果。
- **serialwrapd 不重啟**：daemon pid 全程不變。

### 非目標（本期不做）

- 不在 daemon／RPC 層做任何改動（純 CLI 便利層）。
- 不自製 SSH（不引入 paramiko）；auth 全數委由系統 `ssh`。
- 不做 relay 伺服器、不做隧道健康自動修復以外的 orchestration。
- 不改變既有 `--endpoint` 語意；agent 端 direct 情境仍用既有 `--endpoint tcp://…`。

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
  - `--ssh-opt "..."`：透傳額外 ssh 參數（可重複），例如 `-o ServerAliveInterval=30`、`-p 2222`。
- 所有子動作輸出緊湊 JSON（`ensure_ascii=False, separators=(",",":")`，含 `ok`），對齊 CLI 慣例。

## 4. 行為規格

### 4.1 `remote -R`（expose；跑在 UART host）

1. 解析 target 為 `(ssh_target, port)`；格式不符 → `{"ok":false,"error_code":"INVALID_TARGET"}`。
2. 解析要暴露的本機 daemon endpoint（`--socket` 或 `_resolve_endpoint(args)`）：
   - AF_UNIX 路徑 → 遠端轉發目標為該 socket 路徑（`ssh -R <port>:<sock>`，**免 socat**，需對端與本機 OpenSSH ≥ 6.7）。
   - `tcp://127.0.0.1:<p>`（Windows daemon 或顯式覆寫）→ `ssh -R <port>:127.0.0.1:<p>`。
   - 完全解析不到任何本機 endpoint → `NO_LOCAL_DAEMON`（提示先 `serialwrap daemon status`）。若解析到 unix 路徑但檔案暫不存在 → **僅 warn 不擋**：`ssh -R` 要等遠端有連線進來才連該 socket，daemon 稍後起來即通（隧道可先於 daemon 起）。
3. 組 argv（見 §5），spawn detached 子行程，寫 state（見 §6），立即返回：
   ```json
   {"ok":true,"role":"expose","pid":12345,"bind_port":7777,"target":"user@host",
    "daemon_endpoint":"unix:///run/.../serialwrapd.sock","via":"ssh",
    "remote_hint":"agent 端用 serialwrap --endpoint tcp://127.0.0.1:7777"}
   ```
4. idempotency：該 port 已有**存活**的隧道 → 回 `{"ok":true,"already_running":true,...}` no-op；state 在但 pid 已死 → 覆寫重建。

### 4.2 `remote -L`（connect / relay；跑在 agent host）

1. 解析 target 與本機 `--local`（預設 = target port）。
2. 組 `ssh -N -L <local>:localhost:<port> <ssh_target>`，spawn detached，寫 state，回：
   ```json
   {"ok":true,"role":"connect","pid":23456,"local_port":7777,"target":"user@relay",
    "endpoint":"tcp://127.0.0.1:7777"}
   ```
   `endpoint` 即 agent 接著要餵給 `--endpoint` 的值。
3. 本機 `local` port 已被佔 → `PORT_IN_USE`。

### 4.3 direct 情境（agent 端免 connect）

UART host 的 `-R` 已直接落在 agent host 時，agent 端**不需任何新指令**，照舊：

```bash
serialwrap --endpoint tcp://127.0.0.1:7777 session list
```

`remote -L` 僅在 relay／雙 NAT（需要在 agent 端再撥一段 `-L` 到 relay）時使用。文件（§10）必須清楚標示這條岔路，避免 agent 誤用。

### 4.4 `remote status`（含裸 `remote`）

掃 state registry，對每筆探測 pid 存活；死的 prune 掉。輸出：

```json
{"ok":true,"tunnels":[
  {"role":"expose","pid":12345,"alive":true,"bind_port":7777,"target":"user@host","via":"ssh","started":"..."},
  {"role":"connect","pid":23456,"alive":true,"local_port":7778,"target":"user@relay","endpoint":"tcp://127.0.0.1:7778"}
]}
```

### 4.5 `remote close <port|all>`

對指定 port（或全部）的隧道 pid 送 `SIGTERM`，移除其 state 檔。找不到該 port → `{"ok":true,"closed":[]}`（冪等，不視為錯誤）。

## 5. 隧道 argv 組裝

- 基底：`["ssh","-N","-T", *ssh_opts, direction_args, ssh_target]`
  - `-N`（不開 shell）、`-T`（不配 tty）。
  - 預設附健全性 `-o`：`ExitOnForwardFailure=yes`（forward 建不起來即讓 ssh 失敗，配合 §4.1 step ‑spawn 檢查回 `TUNNEL_SPAWN_FAILED`）、`ServerAliveInterval=30`、`ServerAliveCountMax=3`（可被 `--ssh-opt` 覆寫）。
- `-R`：`["-R", f"{port}:{forward_target}"]`，`forward_target` = unix socket 路徑 或 `127.0.0.1:{tcpport}`。
- `-L`：`["-L", f"{local}:localhost:{port}"]`。
- `--autossh`：改用 `["autossh","-M","0", ...其後同 ssh...]`。
- **安全**：`ssh -R` 預設只 bind 遠端 loopback（不設 `GatewayPorts`），維持「絕不對外」。
- **Windows UART host**：無 AF_UNIX，daemon 為 TCP loopback，`-R` 轉發目標自動為 `127.0.0.1:<tcpport>`；需系統有 `ssh`（Win10+ OpenSSH client）。此為次要平台，文件標注即可。

## 6. 背景行程與 state registry

- spawn 方式：`subprocess.Popen(..., start_new_session=True)`（setsid，脫離父行程與終端），stdout/stderr 導到 state 目錄下的 log 檔（供 `TUNNEL_SPAWN_FAILED` 撈 stderr 尾）。
- state 目錄：`$RUN_DIR/remote/`（`RUN_DIR` 預設 XDG runtime，與 socket／lock 同層；reboot 清掉剛好隧道也一併消失，語意一致）。
- 每條隧道一檔 `$RUN_DIR/remote/<bind_or_local_port>.json`：
  ```json
  {"role":"expose|connect","pid":12345,"target":"user@host","bind_port":7777,
   "local_port":null,"daemon_endpoint":"unix:///…","via":"ssh|autossh",
   "argv":["ssh","-N",...],"started_mono":123456.7,"started_wall":"2026-07-19T..."}
  ```
  以 `sort_keys=True` 寫出（對齊 state.json 慣例）。
- 存活判定：`os.kill(pid, 0)`；`autossh` 情境 pid 為 autossh 本身（其子 ssh 由 autossh 管）。
- idempotency／prune 規則見 §4.1、§4.4。
- **與 serialwrapd 生命週期完全解耦**：daemon 死了隧道還在（連過去會 EOF）；daemon 重啟不需重開隧道（AF_UNIX 路徑不變）。

## 7. daemon socket 解析（-R）

重用 `sw_core/cli.py::_resolve_endpoint(args)`（既有優先序：`--endpoint` > `--socket` > config.yaml > 平台預設）取得本機 daemon endpoint，再依 transport（unix／tcp）決定 `-R` 的 forward target。不新增解析邏輯、不改寫 config（維持 CLI 對 config.yaml 唯讀）。

## 8. 安全

- auth 全數委由 `ssh`（金鑰／known_hosts／agent forwarding 皆 ssh 自理），serialwrap 不碰金鑰。
- `-R` 只 bind 遠端 loopback；不提供 `GatewayPorts`／`0.0.0.0` 對外選項。
- 隧道等於讓對端全權操控 DUT（`command.submit`／`file.push`／`daemon.stop`）→ `remote` 輸出與 `--help`／SKILL 文件明確標警語：**只透過 ssh-tunnel 使用、不可對網路直接開放**。

## 9. 錯誤處理（回 error_code，不讓例外穿越）

| 情境 | error_code |
|---|---|
| target 非 `[user@]host:port` | `INVALID_TARGET` |
| 無 `ssh`（或 `--autossh` 但無 `autossh`）於 PATH | `SSH_NOT_FOUND` |
| spawn 後 ~0.5s 子行程即死 | `TUNNEL_SPAWN_FAILED`（附 stderr 尾） |
| `-R` 完全解析不到任何本機 endpoint | `NO_LOCAL_DAEMON` |
| `-L` 本機 port 已被佔 | `PORT_IN_USE` |
| `-R` 與 `-L` 同時指定 | `INVALID_ARGS` |

## 10. 改動表面（顯式同步多表面）

### Code

- `sw_core/cli.py`：新增 `remote` subparser（positional target、`-R`/`-L`/`--autossh`/`--local`/`--socket`/`--ssh-opt`、`status`/`close` 子動作）+ 平面 if/elif 分派。
- 新模組 `sw_core/remote_tunnel.py`：target 解析、argv 組裝、spawn（注入式 spawner 便於測試）、state 讀寫、status prune、close。以純函式為主，副作用（Popen／檔案）集中且可注入。

### Docs / help / skill（本次硬需求：讓 agent 會用、會連）

- `sw_core/assets/skill/SKILL.md`：**改寫「Remote Support 用法（ssh-tunnel）」（現 line 92-112）**。現內容教舊的 `ssh -L`（inbound）正是誤導源頭，改為：
  - UART host：`serialwrap remote user@host:7777`（`-R` 預設）。
  - agent 端連線 runbook：direct → `serialwrap --endpoint tcp://127.0.0.1:7777 …`；relay → 先 `serialwrap remote -L user@relay:7777` 再用回傳 endpoint。
  - 明確標示 direct vs relay 岔路與 `remote status`/`close`、安全警語。
- `sw_core/assets/skill/SKILL_WINDOWS.md`：新增精簡 remote 段（Windows daemon 為 TCP loopback，`-R` 轉發 `127.0.0.1:48700`；需 OpenSSH client）。
- `README.md`：**改寫「Remote Support（ssh-tunnel 遠端連線）」（現 line 1632+）**，以 `serialwrap remote` 為主軸、保留 `ssh -R`/`-L` 手動等價、涵蓋 direct/relay。依語言政策 **README 中英雙語並存**，英文段與繁中段一致更新。
- `README.md` cli-help markers（R-16）：新增 `serialwrap-remote-help` marker 區塊；因 `remote` 進入子命令列表，**一併再生 `serialwrap-help` 區塊**。
- `.paul-project.yml`：`cli:` 新增 `{command:"./serialwrap remote", help_args:["--help"], reflected_in:"README.md", marker:"serialwrap-remote-help"}`。
- `changelog.d/remote-tunnel-cli.md`：per-PR fragment（policy v1.0.10；無對應 issue，以 slug 命名）。

## 11. 測試策略

### 11.1 單元（`tests/test_remote_tunnel.py`）

- target 解析（`user@host:port`／`alias:port`／缺 port／壞格式）。
- argv 組裝：`-R` unix-socket 與 tcp 兩種 forward target、`-L`、`--autossh`、`--ssh-opt` 透傳、預設 `-o` 附加。
- state 讀寫、`status` prune 死 pid、`close` 送訊號與移檔、idempotency（同 port 活著→no-op、死→覆寫）。
- error_code 路徑：`INVALID_TARGET`／`SSH_NOT_FOUND`（PATH monkeypatch）／`INVALID_ARGS`／`NO_LOCAL_DAEMON`／`PORT_IN_USE`。
- 生命週期以**注入式 spawner + 假長命行程**（如 `sleep`）驗證，不真的起 ssh。

### 11.2 docker 拓樸驗收（acceptance gate；`tools/docker/remote_tunnel_test.sh`）

以真 sshd + 真 `serialwrap remote` 跑三種拓樸，**全過才算通過**。以 docker network 隔離模擬 NAT（不共網段＝互不可達，只能經 relay）。

**測試映像**：延伸現有 `serialwrap:remote-smoke`，加 `openssh-server`／`openssh-client`／`autossh` + 預燒 keypair（passwordless），內含 fake target + `serialwrapd`（AF_UNIX socket）。

**拓樸 1 — direct**
- `net_direct`：`uart`（serialwrapd）、`agent`（跑 sshd）同網段。
- `uart`：`serialwrap remote -R tester@agent:7777`（`-R` 為預設，亦即裸 `serialwrap remote tester@agent:7777`）。
- `agent`：`serialwrap --endpoint tcp://127.0.0.1:7777 session list` / `cmd submit`。

**拓樸 2 — NAT → host**
- `net_a`：`uart`（NAT，僅與 `relay` 同網段）、`relay`（可達，跑 sshd，agent CLI 在此）。
- `uart`：`serialwrap remote -R tester@relay:7777` → agent CLI 於 `relay` 用 `--endpoint tcp://127.0.0.1:7777`。

**拓樸 3 — NAT ← client（雙 NAT relay 全鏈）**
- `net_a`：`uart`＋`relay`；`net_b`：`agent`＋`relay`。`uart` 與 `agent` **無共網段**（互不可達）。
- `uart`：`serialwrap remote -R tester@relay:7777`（把 daemon 推上 relay loopback:7777）。
- `agent`：`serialwrap remote -L tester@relay:7777`（把 relay:7777 拉回本機 loopback）→ 再用回傳 `endpoint` 下命令。

**每個拓樸的斷言（缺一不可）**
1. **預設不啟用**（跑 `remote` 前）：`serialwrap remote status` 回空 `tunnels`；`$RUN_DIR/remote/` 無 state 檔；無 serialwrap-spawn 的 `ssh`/`autossh` 行程；遠端 `--endpoint` 連線失敗（`SOCKET_ERROR`）。
2. **手動啟動後正常**：`remote -R`/`-L` 後 agent 端 `session list` 有 READY session、`cmd submit`→`cmd status` 得 `done` 且輸出正確。
3. **serialwrapd 不重啟**：擷取 daemon pid，全程（expose/connect/close 前後）不變。
4. **teardown 乾淨**：`serialwrap remote close all` 後隧道行程消失、state 檔清空、`--endpoint` 復歸不可連。

**執行與 CI**：docker 可用時列為必跑驗收；docker 不可用的環境明確 SKIP 並印原因（不靜默略過）。單元測試（11.1）恆入 CI。

### 11.3 政策

`python3 -m pytest -q tests/` 無新失敗；`python3 -m policy_check --repo .` 通過（R-09 fragment、R-16 help、R-18 docs 對齊）。

## 12. 相依與相容性

- 執行期需系統 `ssh`（`--autossh` 時另需 `autossh`）；`-R port:unix-socket` 需 OpenSSH ≥ 6.7（2014，現行發行版皆滿足）。
- 純 CLI 便利層，向後相容：不改 `--endpoint`／daemon／RPC；既有手動 `ssh -R`/`-L` + `--endpoint` 流程照舊可用。

## 13. 未來（defer）

- `remote` 隧道健康的主動探測／自動重連（超出 `--autossh` 的部分）。
- 多 UART host → 單 agent host 的 endpoint 名錄／自動選號。
- Windows daemon 端 `device release`／remote 編排（對齊 #84 尚未完成的 Windows daemon 面）。
