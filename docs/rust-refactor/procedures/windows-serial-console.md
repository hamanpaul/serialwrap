# Windows Console-side com0com Serial console 驗證（BLOCKED-OWNER #2）

對應 `plans/07-slice22-console.md` Task 8、Task 12、BLOCKED-OWNER 總表 #2 與 SC-29（OD-01 選配）。驗證 `windows.serial_console`
指向的 com0com pair 一端由 Rust daemon 以 Serial console 開啟、另一端由 Tera Term Serial 逐字元互動。需要擁有者 Windows host；
雲端／CI 不得產出證據，也不得標 PASS。

## 前置條件

1. com0com 已由擁有者預先安裝並建立 `COM20:COM21`（DUT）與 `COM22:COM23`（console）兩組 pair。
   本程序不安裝驅動、不改 Windows 安全設定；若配對不存在，維持 `BLOCKED-OWNER`。
2. 設定 `SW_TEST_COM_PAIR=COM20:COM21` 與 `SW_TEST_CONSOLE_PAIR=COM22:COM23`。
3. Rust bundle 已安裝，`serialwrap.exe doctor` 無 FAIL。console mapping 設在隔離的 `config.yaml`：

   ```yaml
   windows:
     serial_console:
       COM20:
         console_port: COM22
         peer_port: COM23
   ```
4. 證據目錄 `EVID=target/win-serial-console/<yymmdd-hhmmss>/`。

## 逐步驟

1. 執行 `scripts/rust/win-test.sh --package sw-daemon --features test-fakes --caps windows_com --test windows_console_native`，並設定
   `SW_TEST_CONSOLE_PAIR=COM22:COM23`；報告存為 `%EVID%\windows_console_native.json`。
   `cap_windows_com__serial_console_end_to_end` 必須實際執行，不得為 ignored。
2. `serialwrap.exe daemon start`；`serialwrap.exe session list > %EVID%\session-list.json`；`serialwrap.exe device list > %EVID%\device-list.json`
   （期望：`device list` 不含 `COM22`／`COM23`，DUT `COM20` 維持可用）。
3. Tera Term：Serial、Port `COM23`、115200 8N1。依序鍵入 `↑`、`Tab`、`Ctrl-C`；以 `serialwrap.exe wal tail --selector COM0 --json > %EVID%\serial-wal.json` 擷取。
   - 期望：WAL TX 依序 `1b 5b 41`、`09`、`03`，`source` 為 `human:<client_id>`；畫面有遠端回顯。
4. 於 Tera Term 切換 DTR／RTS 與 baud（9600），再於 DUT 側確認 `COM20` 的 modem line 與 baud 不變（`%EVID%\control-lines.txt`）。
5. 關閉 Tera Term；1 s 內 `session console-list --selector COM0` 不再列出該 serial console（`%EVID%\console-list.json`）。

## 證據檔名清單

- `windows_console_native.json`、`session-list.json`、`device-list.json`、`serial-wal.json`、`control-lines.txt`、`console-list.json`
- `screens/teraterm-serial.png`、`env.json`（Windows 版本、com0com 版本、HVCI／Secure Boot 狀態、pair 名稱）

## PASS 條件

步驟 1 的 `cap_windows_com__serial_console_end_to_end` passed，步驟 2～5 期望全部成立且證據齊全；任一缺漏判 `INSUFFICIENT`。

## 失敗時記錄

記下失敗步驟、com0com 與 Tera Term 版本、預期與實際 WAL／控制線狀態、daemon stderr 尾段（`%LOCALAPPDATA%\serialwrap\logs`），
以 `fail-<步驟>.md` 存入 `EVID` 並回報 issue。
