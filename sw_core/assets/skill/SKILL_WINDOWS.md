<!-- 本檔為 SKILL.md（Linux 版權威 skill）的 Windows 補充指南（#131）。
     刻意不含 frontmatter name，避免 skill loader 與 SKILL.md 撞觸發；
     以 `serialwrap skill --platform windows` 輸出，或隨 setup 物化到 skill 目錄。 -->

# serialwrap Windows 操作指南

> 原生 Windows（非 WSL）上安裝、啟動與日常操作 serialwrap 的完整流程。
> Agent 操作語意（命令仲裁、WAL、session 狀態機）與 Linux 版相同，見 `SKILL.md`；
> 本檔聚焦 Windows 專屬差異：無 PTY → TCP console、無 systemd → on-demand、
> `/dev/serial/by-id` → SERIALCOMM 登錄列舉。

## 1. 安裝

**方式 A：release exe（免 Python）**

1. 從 GitHub release 下載 `serialwrap.exe` 與 `serialwrapd.exe`。
2. 兩個 exe 放同一目錄，並把該目錄加入 PATH（`daemon start` 會自動找同層的
   `serialwrapd.exe`）。

**方式 B：pipx（有 Python ≥ 3.10）**

```powershell
pipx install serialwrap   # pyserial 於 Windows 自動帶入
```

安裝後驗證環境：

```powershell
serialwrap doctor
```

Windows 版 doctor 檢查：`python`／`pyyaml`／`pyserial`／exe 是否在 PATH／
`supervision_mode`／daemon endpoint 是否可連／SERIALCOMM COM 裝置列舉
（藍牙 COM 自動排除）。

## 2. 啟動 daemon

```powershell
serialwrap daemon start
```

- 預設 bind `tcp://127.0.0.1:48700`（loopback RPC；可用環境變數
  `SERIALWRAP_TCP_PORT` 或 `SERIALWRAP_ENDPOINT` 覆寫）。
- 冪等：已有健康 daemon 時回 `already_running`，不會多開。
- daemon 以 detached 模式啟動，關閉目前的終端機視窗不會殺掉它。
- 停止：`serialwrap daemon stop`；狀態：`serialwrap daemon status`。
- 進階（手動前景執行，除錯用）：`serialwrapd.exe --socket tcp://127.0.0.1:48700`。

## 3. 用戶端基本操作

所有 client 子命令**免帶 `--endpoint`**（預設自動連 `tcp://127.0.0.1:48700`）：

```powershell
serialwrap daemon status
serialwrap session list
serialwrap session self-test --selector COM0
serialwrap cmd submit --selector COM0 --cmd "cat /proc/version" --source agent:demo --mode line
```

> 注意 selector 的 `COM0`/`COM1` 是 serialwrap 的**邏輯 session 編號**，
> 不是 Windows 裝置管理員的實體 `COM5`/`COM7`；對應關係看 `session list`
> 的 `attached_real_path`（如 `\\.\COM5`）。

## 4. Profile 設定

profile 與 Linux 版同一套規則，路徑在家目錄的 `.config`：

```
%USERPROFILE%\.config\serialwrap\profiles\*.yaml   ← template 與 targets 定義
%USERPROFILE%\.config\serialwrap\config.yaml       ← supervision_mode / socket_path / windows.exclude_coms
```

覆寫優先序：`daemon start --profile-dir` > `SERIALWRAP_PROFILE_DIR` > `XDG_CONFIG_HOME` > 預設。

YAML 三個頂層區段：

- `defaults`：全域預設（如 `log_dir`、`max_sessions`）。
- `profiles`：template 定義（`platform`、`prompt_regex`、`login_regex`、`ready_probe`、`uart.*` 等）。
- `targets`：**明確綁定** COM → template → 裝置。Windows 上 `device_by_id` 填實體 COM 名：

```yaml
targets:
  - act_no: 1
    com: COM0
    profile: prpl-template
    device_by_id: COM5     # Windows：實體 COM 名（Linux 為 /dev/serial/by-id/...）
```

- **沒有 `targets` 時**全走動態偵測：daemon 對每個非藍牙 COM 自動建 session、以
  template 偵測認 profile（`session list` 的 `profile_source: "detected"`）。此時
  `daemon status` 的 warning `no_profiles_loaded` 是正常的——它指「沒有明確
  targets 綁定」，不代表 template 沒載入。
- 查看生效結果：`serialwrap session list`（`profile`／`profile_source`／
  `attached_real_path`）；查 profile 檔本身直接開 YAML（`type`／`notepad`）。
- 改完 profile 後 `serialwrap daemon stop` ＋ `serialwrap daemon start` 重載。

## 5. 找到 human console 的連線端口

每個 session 有一個 `127.0.0.1:<port>` 的 TCP console listener（Windows 無
PTY 的替代，#84 PORT-2）。port 為隨機配置，從 `session list` 讀：

```powershell
serialwrap session list
# 每個 session 的 "console_endpoint": "127.0.0.1:52085" 即是（#131）
```

`session console-attach --selector COM0` 的回傳亦帶 `endpoint` 與
`protocol: "telnet"`。

## 6. Tera Term 連線（建議走 Telnet）

console listener 講 **Telnet**（server 主動協商 char-mode + 遠端回顯，#131）：

1. File → New connection → 選 **TCP/IP**。
2. Host 填 `127.0.0.1`，TCP port# 填 `console_endpoint` 的 port。
3. **Service 選 Telnet**（不要選 Other）。
4. OK 連線後即為逐字元互動：打字即時透傳、回顯由 DUT 回送，體驗同 ssh。

建議 Terminal 設定（Setup → Terminal）：

- New-line Receive：`CR`（DUT 輸出多為 CRLF，選 CR+LF 會出現雙倍行距時改 CR）。
- New-line Transmit：`CR` 或 `CR+LF` 皆可（telnet 層會把 Enter 正規化為單一 CR）。
- **Local echo：不要勾**（遠端回顯已由 telnet 協商接手）。

## 7. PuTTY／telnet.exe 連線

- PuTTY：Connection type 選 **Telnet**，Host `127.0.0.1`、Port 填 console port。
- Windows 內建 telnet client（選用功能）：`telnet 127.0.0.1 <port>`。

## 8. Raw（Service: Other）備援模式

仍可用 Tera Term「Other」或 PuTTY「Raw」連入，但：

- 連線瞬間會看到少量亂碼（12 bytes 的 telnet 協商 greeting）。
- 送出的 `0xFF` byte 會被當成 telnet IAC 解讀、收到的 `0xFF` 會成對出現
  （IAC 逸出）；純文字互動不受影響。
- client 端行為（行緩衝、無 local echo 抑制、Nagle）不受 server 控制，
  體驗不如 Telnet 模式。**建議一律走 Telnet**。

## 9. Windows 與 Linux 差異對照

| 面向 | Linux | Windows |
|------|-------|---------|
| human console | PTY（minicom 開 `/dev/pts/N`） | TCP loopback listener（Tera Term/PuTTY Telnet 連 `console_endpoint`） |
| RPC 通道 | Unix socket（`$XDG_RUNTIME_DIR`） | `tcp://127.0.0.1:48700`（loopback-only，無認證故拒綁非 loopback） |
| 監管模式 | systemd-user／system／on-demand | 僅 on-demand（無 systemd） |
| 裝置列舉 | `/dev/serial/by-id`（udev） | HKLM `SERIALCOMM` 登錄（藍牙 BTHENUM／`bthmodem` 自動排除；`windows.exclude_coms` 可手動排除） |
| 序列埠後端 | termios | pyserial（`SERIALWRAP_SERIAL_BACKEND` 可覆寫） |
| MCU 燒錄 | `/dev/ttyMCU` PTY-bridge（#55） | `device release` 釋放 COM → 外部燒錄工具獨佔 → `device attach` 收回（#54 語意） |
| 單例防護 | flock + socket probe | msvcrt 檔鎖 + TCP probe |

## 10. Remote Support（native Windows）

native Windows 支援 `serialwrap remote -R/-L`、`connect` 與 `benches`。請先安裝並設定 Win32-OpenSSH，確保 `ssh` 可由 `PATH` 找到，且認證方式可在非互動模式使用。Windows 隧道不使用 OpenSSH ControlMaster；`-R`、`-L`、行程生命週期與 readiness 由 serialwrap 管理。

```powershell
serialwrap remote user@AGENT_OR_RELAY:7777
serialwrap remote -L user@RELAY:7777
serialwrap connect <bench-code>
serialwrap benches
serialwrap remote stop 7777
serialwrap remote stop              # 停止全部隧道
```

`remote stop <port|all>` 是 `remote close` 的別名；省略參數時視為 `all`。
依 OD-12，隧道讓通過 SSH 驗證的使用者取得 DUT 完整控制權。僅用於單人工作站與可信 relay；不提供共享主機上的同使用者隔離。

如需手動管理，等價的 SSH 反向隧道為：

```powershell
ssh -N -R 7777:127.0.0.1:48700 user@AGENT_OR_RELAY
```

（Windows daemon 為 TCP loopback `48700`。）agent 端照舊 `serialwrap --endpoint tcp://127.0.0.1:7777`。

## 11. MCU 燒錄（device release / attach）

Windows 燒錄工具直接獨佔開 `COMx`，serialwrap 只需讓出 handle：

```powershell
serialwrap device release --selector COM0 --source agent:flash --reason "flash MCU"
# → 外部工具燒錄（此時 serialwrap 不持有該 COM）
serialwrap device attach --selector COM0
```

### COM 獨占與設定（BC-04、SC-09）

- **COM 獨占（BC-04）**：daemon 持有某 COM 時，其他程式（TeraTerm 直連 `COMx`、燒錄工具）**不可再直接開該 COM**——Windows 回 `ERROR_ACCESS_DENIED`，serialwrap 對應為 `DEVICE_STILL_HELD`（`pids: []`、`holder_unknown: true`，原因不明時不可視為可收回）。人類 console 走第 5～8 節的 serialwrap console 端口；外部工具獨佔先 `device release`，用完再 `device attach`。
- **設定探索順序（SC-09）**：`SERIALWRAP_CONFIG` → `%USERPROFILE%\.config\serialwrap\config.yaml` → 存在時的 `%ProgramData%\serialwrap\config.yaml`（系統檔唯讀）；寫入落在 `SERIALWRAP_CONFIG`（有設定時）或使用者設定檔。
- **socket 權限基線**：POSIX Unix socket 維持 `0660`，新建目錄權限依 process umask 決定；Windows 走 loopback TCP（`tcp://127.0.0.1:48700`），不套用此權限模型。

## 12. 疑難排解

- 任何指令連不上 → `serialwrap doctor` 看 `daemon_endpoint`；未在跑則
  `serialwrap daemon start`。
- `session list` 沒有裝置 → doctor 的 `devices` 檢查（USB-serial 是否出現在
  裝置管理員；藍牙 COM 會被自動排除是正常的）。
- 換 port：設 `SERIALWRAP_TCP_PORT` 後重啟 daemon；client 端同樣設該環境變數
  或帶 `--endpoint tcp://127.0.0.1:<port>`。
- config.yaml 若殘留 WSL 的 unix `socket_path`，CLI 會自動改連 tcp 並在
  stderr 提示（唯讀不改寫；daemon start 會把正確 endpoint 寫回 config）。

## 13. 錯誤碼與 last_error 處置目錄（自動產生；勿手改）

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
| `CHECKSUM_VERIFY_FAILED` | rpc／posix、windows | 視情況 | 目標端 md5sum 無回應或無法擷取校驗值 | 確認目標端具備 md5sum 工具後重試 |
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
| `DAEMON_EXITED` | cli_local／posix、windows | 視情況 | spawn 後 daemon 行程在就緒前即退出 | 查看 daemon 的 stderr 並排除原因後再執行 daemon start |
| `DAEMON_NOT_READY` | cli_local／posix、windows | 視情況 | spawn 後等待時間內 health.ping 未成功 | 以 serialwrap daemon status 確認是否已就緒；未就緒時檢查 daemon 行程，勿盲目重複 spawn |
| `DEVICE_ALREADY_BOUND` | rpc／posix、windows | 修正參數後 | 指定的 by_id 已綁定到其他 session | 以 session list 確認該裝置目前所屬 session，改用其他 by_id 或先解除既有綁定 |
| `DEVICE_NOT_BOUND` | rpc／posix、windows | 修正參數後 | session 未設定 device_by_id，無法 attach | 先以 session.bind 綁定裝置後再 attach |
| `DEVICE_NOT_FOUND` | rpc／posix、windows | 視情況 | 指定的裝置或 selector 解析不到對應裝置 | 以 device list 確認裝置在線且 by_id 正確，裝置出現後再 attach |
| `DEVICE_NOT_PRESENT` | rpc／posix、windows | 視情況 | by_id 不在目前的裝置清單中（未插入或尚未列舉） | 確認裝置已插入且 device list 可見後重試 |
| `DEVICE_STILL_HELD` | rpc／posix、windows | 視情況 | UART 獨占開啟或 holder 預檢確認裝置仍被占用 | 等待已識別的外部持有者釋放後重試；force 只略過開檔前預檢，不會繞過實際開檔或鎖後 holder 掃描 |
| `DRAIN_STALLED` | rpc／posix、windows | 否 | device.release 的 drain 超過 3000 ms 仍無成功 handle 關閉回條，或收到 handle_closed=false 失敗回條（後者可立即回應） | 不要重開或讓外部工具開啟該裝置；依呼叫端等待上限輪詢 session get_state，只有 state 為 RELEASED 才交接；上限到期仍為 RECOVERING 時停止輪詢並交由操作員檢查 daemon／handle；再次 device release 不會加速完成 |
| `ECHO_ACK_UNSUPPORTED` | rpc／posix、windows | 修正參數後 | ack_mode 為 echo，但 bridge 不支援 echo 節流送出 | 改用其他 ack_mode 重試 |
| `EMPTY_RESPONSE` | cli_client／posix、windows | 視情況 | 連線已關閉但未收到任何一行回應 | 僅白名單唯讀方法可由 CLI 重試；其他方法先以 health.ping 確認 daemon 狀態 |
| `ENV_FILE_SOURCE_FAILED` | cli_local／posix、windows | 修正參數後 | env 檔以 bash -lc source 執行失敗（非 0 退出），或 Windows 讀取檔案時發生 OS 錯誤 | 確認 env_file 路徑與內容後重試 |
| `EXCEPTION` | rpc_frame／posix、windows | 否 | handler 拋出未預期例外，framing 層以 message 回傳（回應無 version） | 視為結果不明：先以 command.get 或 session get_state 確認狀態，不可假設未執行而直接重送 |
| `FLASHING_BUSY` | rpc、rec、cli_local／posix、windows | 是 | session 處於 FLASHING 或 flash 臨界區，命令或操作被拒 | 等待燒錄結束（session 回到 ATTACHED 或 READY）後再重試 |
| `FLASH_TARGET_IS_CONSOLE` | rpc／posix、windows | 修正參數後 | 燒錄目標是可下命令的 console 裝置，且未指定 force，拒絕執行 | 改選非 console 的燒錄目標；此碼目前 production 不可達，僅作為契約保留 |
| `FLUSHED_BY_RECOVERY` | rec／posix、windows | 是 | 命令尚未執行即因 recovery 或 detach 被清空（未送出，可在 READY 後重送） | 確認 session 已回到 READY 後才可重送 |
| `FLUSHED_BY_SHUTDOWN` | rpc、rec／posix、windows | 是 | daemon 停止時，尚未執行的排隊命令被清空（未送出，可重送）；原子交接凍結後的有副作用 RPC 回 reason:"handoff_frozen" | daemon 重新啟動並確認 session 為 READY 後才可重送 |
| `FORCE_RECOVER_TIMEOUT` | rpc／posix、windows | 視情況 | 強制 recover（clear 後重新 attach）在 10 次、每次 1 秒的窗口內未達 READY | 以 session get_state 確認目前狀態後，再決定重試 recover 或處理裝置 |
| `INTERACTIVE_EXPIRED` | rpc／posix、windows | 否 | 互動 lease 已逾時失效 | 重新 interactive open 取得新的 lease 後再送出 |
| `INTERACTIVE_NOT_FOUND` | rpc／posix、windows | 否 | interactive_id 不存在，或 lease 狀態已不是 active | 重新 interactive open 取得 interactive_id |
| `INTERNAL_ERROR` | cli_local／posix、windows | 否 | CLI 本地流程發生未預期例外 | 回報例外訊息；勿假設操作已失敗而重送寫入型命令 |
| `INVALID_ARGS` | rpc、cli_local／posix、windows | 修正參數後 | 參數缺漏或型別／值不合法（RPC 分派或 CLI 參數驗證） | 依 CLI 或 RPC 說明修正參數後重送 |
| `INVALID_BENCH_CONFIG` | cli_local／posix、windows | 修正參數後 | benches.yaml 驗證失敗 | 修正 benches.yaml 的欄位後重試 |
| `INVALID_BENCH_STATE` | cli_local／posix、windows | 修正參數後 | benches.state.json 結構錯誤 | 檢查 benches.state.json 內容，結構損壞時需人工修復後重試 |
| `INVALID_COMPLETION_REGEX` | rpc、rec、cli_local／posix、windows | 修正參數後 | completion_regex 驗證失敗（空字串、超過 1024 bytes、無法編譯，或可匹配空字串） | 修正 completion_regex 後重送 |
| `INVALID_ENDPOINT` | cli_client、cli_local／posix、windows | 修正參數後 | endpoint 格式錯誤（tcp 缺 host 或 port、unix 路徑非絕對、未知 scheme） | 改用 tcp://host:port 或絕對 unix socket 路徑格式後重試 |
| `INVALID_JSON` | rpc_frame／posix、windows | 修正參數後 | 該行不是合法 JSON，連線保持並續讀下一行（回應無 id 與 version，且為非緊湊分隔符） | 修正請求的 JSON 格式後重送 |
| `INVALID_METHOD` | rpc_frame／posix、windows | 修正參數後 | 請求的 method 欄位不是字串（回應保留 id，無 version） | 以字串形式的 method 重送 |
| `INVALID_REQUEST` | rpc_frame／posix、windows | 修正參數後 | 請求 JSON 不是 object（回應無 id 與 version，且為非緊湊分隔符） | 以 JSON object 格式重送 |
| `INVALID_RESPONSE` | cli_client／posix、windows | 否 | daemon 回應不是合法 JSON，或解析後不是 object | 勿直接重送寫入型命令；先以 health.ping 與 session get_state 確認 daemon 與 session 狀態 |
| `INVALID_RULE_SCHEMA` | rpc／posix、windows | 修正參數後 | 事件規則的 schema 驗證失敗 | 依 schema 修正 rule 欄位後重送 event.rule_set |
| `INVALID_TARGET` | cli_local／posix、windows | 修正參數後 | remote target 不是 [user@]host:port 格式，或 port 超出範圍 | 修正 target 格式後重試 |
| `LOCAL_FILE_NOT_FOUND` | rpc／posix、windows | 修正參數後 | daemon 端看不到 local_path（以 daemon 身分判斷檔案是否存在） | 確認路徑存在且 daemon 具讀取權限後重試 |
| `LOG_OPEN_FAILED` | rpc／posix、windows | 視情況 | session log 檔案開啟失敗（OS 錯誤） | 檢查 log 目錄權限與磁碟空間後重試 |
| `METHOD_NOT_FOUND` | rpc／posix、windows | 否 | method 不存在 | 改用 serialwrap 支援的 method 名稱重送 |
| `MOVE_FAILED` | rpc／posix、windows | 視情況 | mv 之後未見 sentinel，無法確認檔案搬移結果 | 先確認目標檔與暫存檔狀態，再決定是否重試 |
| `MOVE_TIMEOUT` | rpc／posix、windows | 視情況 | mv 之後 prompt 逾時，搬移結果不明 | 先確認目標檔與暫存檔狀態，再決定是否重試 |
| `NEEDS_SUDO` | cli_local／posix | 修正參數後 | systemd-system 的 start、stop 或 restart 未帶 --with-sudo | 加上 --with-sudo 後重試（提權動作需人工確認） |
| `NOT_READY` | rpc／posix、windows | 視情況 | session 為 command-capable 且狀態非 READY／ATTACHING，又無 last_error 可回報；SC-34 起 session.attach 改回 SESSION_NOT_READY | 此碼目前 production 不可達，僅作為契約保留；session.attach／session.recover 失敗時請讀取頂層 last_error 並依該值的處置指引操作 |
| `NO_ACTIVE_CAPTURE` | rpc／posix、windows | 否 | session 沒有進行中的 log capture | 確認是否已執行 log start；若無進行中的 capture 則無需 stop |
| `NO_SYSTEMD` | cli_local／posix、windows | 修正參數後 | on-demand 或未知的監管模式下呼叫 service 管理動作 | 依 supervision_mode 改用正確的 daemon 管理方式（on-demand 使用 daemon start 與 daemon stop） |
| `PROFILE_IS_EXPLICIT` | rpc／posix、windows | 修正參數後 | session 的 profile 來源為 yaml-target，不可 pin 或 unpin | 改為修改 YAML target 設定，而非使用 pin 或 unpin |
| `PROFILE_NOT_COMMAND_CAPABLE` | rpc／posix、windows | 否 | session 為 ATTACHED 且非 passthrough 的 profile 不支援命令（command_capable 為 false），或 file.push／file.pull 的目標為 passthrough（任何狀態） | 命令使用 command-capable profile；passthrough 檔案傳輸改用互動 console |
| `PROMPT_TIMEOUT` | rpc、rec／posix、windows | 否 | 命令逾時且未達完成條件；一般 profile 可能以 CTRL-C／CTRL-D 救援，passthrough 維持 READY 且不送救援按鍵 | 命令結果不明：先以 session self-test 與 session get_state 確認狀態，再由操作者決定是否重送 |
| `PROMPT_TIMEOUT_FORCE_RECOVERED` | rpc／posix、windows | 否 | 命令逾時後，經 force clear 與重新 attach 成功（ok:true 回應，非失敗） | 命令已執行，勿重送；以 session get_state 確認當前狀態 |
| `PROMPT_TIMEOUT_RECOVERED` | rpc、rec／posix、windows | 否 | 命令逾時後 CTRL-C 或 CTRL-D 救回 prompt（ok:true 回應）；命令紀錄不寫入此碼，僅 capture 保留 | 命令已中止且不會自動重送；依 stdout 判斷結果，必要時由操作者決定是否重送 |
| `PULL_PARSE_FAILED` | rpc／posix、windows | 視情況 | 擷取輸出中找不到 sentinel 區段，且無 encoder 失敗 sentinel | 確認目標端 encoder 輸出正常後重試 |
| `READY_UNCONFIRMED` | rpc、rec／posix、windows | 否 | READY 再確認逾 300 秒或已達 5 次仍未確認，拒絕 agent 命令 | 勿重送；執行 serialwrap session self-test 取分類，再依分類處理 |
| `REMOTE_BIND_UNVERIFIED` | cli_local／posix、windows | 視情況 | expose tcp 模式下 relay 的 ss 驗證失敗，或綁定位址非 loopback | 確認 relay 僅綁定 loopback 位址後重試 |
| `REMOTE_NOT_SUPPORTED` | cli_local／posix、windows | 修正參數後 | daemon start 不支援遠端 endpoint；Windows 僅接受 loopback endpoint | 在本機啟動 daemon；Windows 若使用 TCP endpoint，請指定 loopback 位址 |
| `RULE_NOT_FOUND` | rpc／posix、windows | 否 | 指定的 rule_id 不存在 | 以 event rule list 確認 rule_id 後重試 |
| `SAMPLE_READ_FAILED` | cli_local／posix、windows | 修正參數後 | profile test 讀取樣本檔失敗 | 確認樣本檔路徑與讀取權限後重試 |
| `SEND_FAILED` | rec／posix、windows | 否 | send 回呼拋出例外，無法確定命令是否已送出（結果不明） | 勿自動重送；先以 session self-test 與 command.get 確認狀態，再由操作者決定 |
| `SESSION_BUSY` | rpc、rec／posix、windows | 是 | 同一 session 的前景操作仍在進行中 | 等待前景操作完成後重試 |
| `SESSION_INTERACTIVE_BUSY` | rpc、rec／posix、windows | 是 | 已有不可搶占的 interactive lease，或 SC-13 到期收尾的 ownership restore 尚未完成；相關操作被拒 | 等待 interactive lease 結束或到期收尾完成後重試 |
| `SESSION_NOT_FOUND` | rpc／posix、windows | 修正參數後 | selector 未對應任何 session | 以 session list 確認 selector 正確後重送 |
| `SESSION_NOT_READY` | rpc、rec／posix、windows | 視情況 | session 狀態不符（非 READY，或 bridge 不存在）；session.attach／session.recover 失敗時另附 last_error、last_error_detail；passthrough ATTACHED 時附 passthrough_ready_in_s | session.attach／session.recover 失敗時先讀 last_error、last_error_detail 並依 Session last_error 表處置；終態值勿反覆重試，其他暫時狀況可等待 READY 後再重試 |
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
| `TIMEOUT` | cli_client、cli_local／posix、windows | 視情況 | 連線、傳送或接收逾時，或 daemon 管理操作等待舊行程結束逾時；白名單唯讀方法可重試，其他方法不可直接重送 | 非白名單方法不得直接重送；先以 health.ping 與回應中的 daemon_busy 判斷 daemon 狀態 |
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
