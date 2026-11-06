# Windows Telnet console 前端互動驗證（BLOCKED-OWNER #3）

對應 `plans/07-slice22-console.md` Task 2、Task 12 與 BLOCKED-OWNER 總表 #3。驗證 Rust daemon 的 Windows Telnet human console
（`127.0.0.1` ephemeral port，`session list` 的 `console_endpoint`）在真實互動前端下的逐字元行為。本程序需要互動桌面
（`cap_interactive_desktop`），雲端／CI 環境不得產出證據，也不得標 PASS。

## 前置條件

1. 擁有者 Windows host；已安裝 Rust bundle（`serialwrap.exe`／`serialwrapd.exe`），`serialwrap.exe doctor` 無 FAIL。
2. 一條可 READY 的 DUT serial（OD-15：com0com `COM20:COM21`＋`tools/uart_stdio_bridge` Windows pySerial 端點，或實體 USB-UART）。
3. Tera Term 5.x、PuTTY 0.8x、Windows 內建 `telnet.exe`（「Telnet 用戶端」功能）三者皆可啟動。
4. 證據目錄 `EVID=target/win-telnet-frontends/<yymmdd-hhmmss>/` 已建立。

## 逐步驟

1. `serialwrap.exe daemon start`；`serialwrap.exe session list > %EVID%\session-list.json`，記下 COM0 的 `console_endpoint`（`127.0.0.1:<port>`）。
2. Tera Term：新連線 → TCP/IP、Host `127.0.0.1`、Service `Telnet`、Port `<port>`。依序鍵入：
   - `↑`（方向鍵）、`Tab`、`Ctrl-C`；以 `serialwrap.exe wal tail --selector COM0 --json > %EVID%\teraterm-wal.json` 擷取 WAL。
   - 期望：三個按鍵逐字元出現在 WAL TX（`source` 為 `human:<client_id>`，payload 依序為 `1b 5b 41`、`09`、`03`），畫面有遠端回顯。
3. 保持 Tera Term 連線，開 PuTTY（Connection type `Telnet`，同 host／port）作第二視窗：鍵入 `echo L2` 後 Enter。
   - 期望：PuTTY 本地回顯整行；DUT 收到 `echo L2\n`（WAL TX `source` 為 `human:<putty client_id>`、`cmd_id` 非 null）；第二視窗的 `↑` 不直接送出。
4. 關閉 PuTTY；`telnet.exe 127.0.0.1 <port>` 連入，重複步驟 2 的三個按鍵（Tera Term 已關閉時，`telnet.exe` 取得 raw owner）。
5. 任一視窗開啟期間，另開 PowerShell 送 `serialwrap.exe cmd submit --selector COM0 --cmd "echo AGENT" --cmd-timeout 5`，期間於前端鍵入 `X`。
   - 期望：`X` 不落在 agent 命令 payload 中間，命令完成後才送出（BC-02）；前端連線不中斷。
6. `serialwrap.exe session console-list --selector COM0 > %EVID%\console-list.json`；逐一關閉前端，1 s 內 console-list 不再列出。

## 證據檔名清單

- `session-list.json`、`console-list.json`
- `teraterm-wal.json`、`putty-wal.json`、`telnet-exe-wal.json`、`agent-coexist-wal.json`
- `screens/teraterm.png`、`screens/putty.png`、`screens/telnet-exe.png`（含遠端回顯）
- `env.json`（Windows 版本、三個前端版本、bundle 版本、DUT serial 來源）

## PASS 條件

步驟 2～6 的期望全部成立，且證據檔齊全、WAL 的 TX 順序可逐筆對應按鍵；任一證據缺漏判 `INSUFFICIENT`，不得 PASS。

## 失敗時記錄

記下失敗步驟、前端與版本、預期與實際 WAL 片段、daemon stderr 尾段（`%LOCALAPPDATA%\serialwrap\logs`），以 `fail-<步驟>.md`
存入 `EVID` 並回報 issue；不得以 raw 模式（Service=Other／PuTTY Raw）通過取代 Telnet 結果。
