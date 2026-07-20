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

## 10. Remote Support（native Windows：本期不支援 serialwrap remote）

native Windows 執行 `serialwrap remote` 回 `REMOTE_NOT_SUPPORTED`。需遠端存取時**手動**建反向隧道：

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

## 12. 疑難排解

- 任何指令連不上 → `serialwrap doctor` 看 `daemon_endpoint`；未在跑則
  `serialwrap daemon start`。
- `session list` 沒有裝置 → doctor 的 `devices` 檢查（USB-serial 是否出現在
  裝置管理員；藍牙 COM 會被自動排除是正常的）。
- 換 port：設 `SERIALWRAP_TCP_PORT` 後重啟 daemon；client 端同樣設該環境變數
  或帶 `--endpoint tcp://127.0.0.1:<port>`。
- config.yaml 若殘留 WSL 的 unix `socket_path`，CLI 會自動改連 tcp 並在
  stderr 提示（唯讀不改寫；daemon start 會把正確 endpoint 寫回 config）。
