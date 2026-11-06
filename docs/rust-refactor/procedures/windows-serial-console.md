# Windows Console-side com0com Serial console 驗證（BLOCKED-OWNER #2）

對應 `plans/07-slice22-console.md` Task 8、Task 12、BLOCKED-OWNER 總表 #2 與 SC-29（OD-01 選配）。驗證 `windows.serial_console`
指向的 com0com pair 一端由 Rust daemon 以 Serial console 開啟、另一端由 Tera Term Serial 逐字元互動。需要擁有者 Windows host 與
管理員一次性建立第二組 pair；雲端／CI 不得產出證據，也不得標 PASS。

## 前置條件

1. com0com 驅動已安裝（Spike 1 基本資格通過；HVCI 執行中、Secure Boot 關閉已記錄）。
2. 以管理員建立第二組 pair（例 `COM30:COM31`），設定 `SW_TEST_CONSOLE_PAIR=COM30:COM31`；DUT 側另有 OD-08 的 `COM20:COM21`。
3. `config.yaml` 設 `windows.serial_console: COM30`；Rust bundle 已安裝，`serialwrap.exe doctor` 無 FAIL。
4. 證據目錄 `EVID=target/win-serial-console/<yymmdd-hhmmss>/`。

## 逐步驟

1. 執行 `scripts/rust/win-test.sh --package sw-pal --features test-fakes --caps windows_com --test console_conformance`，報告存為
   `%EVID%\console_conformance.json`（`cap_windows_com__console_com0com_*` 必須實際執行，不得為 ignored）。
2. `serialwrap.exe daemon start`；`serialwrap.exe session list > %EVID%\session-list.json`；`serialwrap.exe device list > %EVID%\device-list.json`
   （期望：`device list` 不含 `COM30`／`COM31`）。
3. Tera Term：Serial、Port `COM31`、115200 8N1。依序鍵入 `↑`、`Tab`、`Ctrl-C`；以 `serialwrap.exe wal tail --selector COM0 --json > %EVID%\serial-wal.json` 擷取。
   - 期望：WAL TX 依序 `1b 5b 41`、`09`、`03`，`source` 為 `human:<client_id>`；畫面有遠端回顯。
4. 於 Tera Term 切換 DTR／RTS 與 baud（9600），再於 DUT 側確認 `COM20` 的 modem line 與 baud 不變（`%EVID%\control-lines.txt`）。
5. 關閉 Tera Term；1 s 內 `session console-list --selector COM0` 不再列出該 serial console（`%EVID%\console-list.json`）。

## 證據檔名清單

- `console_conformance.json`、`session-list.json`、`device-list.json`、`serial-wal.json`、`control-lines.txt`、`console-list.json`
- `screens/teraterm-serial.png`、`env.json`（Windows 版本、com0com 版本、HVCI／Secure Boot 狀態、pair 名稱）

## PASS 條件

步驟 1 的 `cap_windows_com__console_com0com_*` 全數 passed，步驟 2～5 期望全部成立且證據齊全；任一缺漏判 `INSUFFICIENT`。

## 失敗時記錄

記下失敗步驟、com0com 與 Tera Term 版本、預期與實際 WAL／控制線狀態、daemon stderr 尾段（`%LOCALAPPDATA%\serialwrap\logs`），
以 `fail-<步驟>.md` 存入 `EVID` 並回報 issue。
