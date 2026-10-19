# Stage 0 Spike 1：Windows 虛擬 COM 資格實機程序

> 對應規格 §13 Spike 1（原無任何計畫交付程序，跨計畫審查補入）、OD-01（Console 側）、OD-08（DUT 側改實體 USB-UART 直連）；計畫 T21 交付物與 P9 `S0-SPIKE1-WIN-VCOM`。
> 手動程序；需**管理員權限**，侵入性驅動安裝由擁有者執行（計畫 §2 彙總 #7：com0com 安裝或實體 USB-UART 直連）。

| 欄位 | 內容 |
|---|---|
| entry | 手動：於 OD-05 代表機台以管理員 PowerShell 執行「安裝與量測」步驟 1～6（見下文）；Python 側開埠量測使用 `py -3.12 spike1_open_check.py`（步驟 4 寫入的檢查腳本） |
| revision | `git rev-parse HEAD:realhw/procedures/stage0-spike1-win-vcom.md`（blob sha1）；com0com 安裝檔以 SHA-256 記錄，**不記錄** 驅動私鑰或授權碼 |
| expected | 在 OD-05 機台分別記錄：com0com 版本與數位簽章、Secure Boot／HVCI（Memory integrity）／VBS 狀態、DUT-side pair 與 Console-side pair 建立結果、Python 原生 serial backend 開埠成功（`dwShareMode=0`）且第二次開啟回 `ERROR_ACCESS_DENIED`、DTR／RTS／baud 不跨 pair 外洩 |
| owner | 擁有者（管理員權限與驅動安裝決定權；OD-05 機台擁有者確認） |
| evidence_dir | `~/b-log/rust-refactor/p1/spike1-win-vcom/<YYMMDD-HHMMSS>/`（Windows 端 `%USERPROFILE%\b-log\...` 同步回收） |
| rollback | 量測結束後以管理員權限執行 com0com 設定工具移除本次建立的 pair（`setupc remove <n>`），並卸載本次安裝的驅動；**不得**為了量測而關閉 Secure Boot、HVCI 或 VBS 後才宣稱符合 |

## 判定原則（必讀）

- 本程序的目的是**資格判定**，不是讓測試通過。任何安全功能（Secure Boot、HVCI、VBS）被關閉後取得的結果，一律標示為「不符合」，不得作為 Spike 1 通過證據。
- 失敗時依退路：Console 側改依 **OD-01**；DUT 側改為**實體 USB-UART 直連** Windows 依 **OD-08**。不得以「虛擬 COM 在我的機器可用」取代機台實測。

## 步驟

### 步驟 1：環境狀態紀錄（管理員 PowerShell）

```powershell
Get-CimInstance -ClassName Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber | Format-List
Confirm-SecureBootUEFI                       # True／False（失敗時寫 unknown）
Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\Microsoft\Windows\DeviceGuard |
  Select-Object VirtualizationBasedSecurityStatus, SecurityServicesRunning
```

記錄 `VirtualizationBasedSecurityStatus`（2 = 執行中）與 `SecurityServicesRunning`（含 2 表示 HVCI）。查不到寫 `unknown`。

### 步驟 2：安裝 com0com（擁有者執行）

1. 下載 com0com 安裝檔，記錄 SHA-256：`Get-FileHash <installer> -Algorithm SHA256`。
2. 檢查數位簽章：`Get-AuthenticodeSignature <installer>`，記錄簽署者與狀態。
3. 安裝後記錄版本（`setupc.exe list` 輸出）。若簽章無效或 HVCI 阻擋驅動載入，**記錄為失敗**，轉退路，不得改關安全設定。

### 步驟 3：建立 pair

1. DUT-side pair：`setupc install PortName=COM20 PortName=COM21`（數字依機台現況調整，記錄實際 COM 號）。
2. Console-side pair：另建一組，記錄 COM 號。
3. 記錄建立結果：`setupc list` 輸出與裝置管理員狀態。

### 步驟 4：Python 原生 serial backend 開埠（`dwShareMode=0`）

在 Windows Python 3.12 執行，**經由 `sw_core/serial_port.py::open_serial_port` 工廠**取得 `_PySerialPort`（`SERIALWRAP_SERIAL_BACKEND=pyserial` 或 `backend="pyserial"`）。不得直接用裸 `serial.Serial`：那不會經過產品的開埠路徑，也不會受 `SERIALWRAP_SERIAL_BACKEND` 影響。先把檢查腳本寫入工作目錄（證據目錄內保留此檔）；腳本需以 `sw_core/` 可 import 的方式執行（同 `win-pytest.sh` 的 staging，或在 Windows 端 clone repo 後於 repo 根目錄執行）：

```powershell
$env:SERIALWRAP_SERIAL_BACKEND = "pyserial"
@'
from sw_core.config import UartProfile
from sw_core.serial_port import open_serial_port

profile = UartProfile(baud=115200)
p = open_serial_port("COM20", profile, backend="pyserial")
p.open()
p.configure(profile)
print("first_open_ok")
try:
    q = open_serial_port("COM20", profile, backend="pyserial")
    q.open()
    print("second_open_UNEXPECTED_OK")
except OSError as e:
    print("second_open_error:", type(e).__name__, e)
finally:
    p.close()
'@ | Set-Content -Encoding utf8 spike1_open_check.py
py -3.12 spike1_open_check.py
```

判定：第一次開啟成功；第二次開啟必須失敗，錯誤訊息含 `Access is denied`／`ERROR_ACCESS_DENIED`（winerror 5）。若第二次成功，表示獨占語意不成立，結果為**不符合**。

### 步驟 5：DTR／RTS／baud 不跨 pair 外洩

pyserial 只提供**本端**的 `dtr`／`rts`（寫入的輸出狀態），讀不到對端的 DTR／RTS 輸出值；因此判定以**對端輸入線**為準（`dsr`、`cts` 屬性，反映同一 pair 另一端送來的控制線），且 baud 以回環傳輸驗證。

1. 以 `open_serial_port` 開啟 Console-side pair 的 `COM30`（同步驟 4 的 `backend="pyserial"`），設定 `dtr=True`、`rts=True`，並以 `baudrate=9600` 配置。
2. 讀取**同一 Console-side pair** 的對端 `COM31` 的 `dsr`、`cts`：兩者必須為 `True`（證明同 pair 內控制線確實傳遞，量測有效）。
3. 讀取 **DUT-side pair** 的 `COM20`、`COM21` 的 `dsr`、`cts`：兩者都必須為 `False`（控制線未跨 pair 外洩）。任一為 `True` 即**不符合**。
4. baud：以 `open_serial_port` 開啟 DUT-side pair（`COM20`、`COM21`，baud 115200），在 `COM20`→`COM21` 傳送一段含全部 256 個位元組值的測試封包並讀回，封包完整一致；於 Console-side 將 baud 改為 9600 後重複一次，DUT-side 傳輸仍完整一致（baud 不跨 pair 外洩）。封包不一致即**不符合**。

### 步驟 6：收尾

```powershell
setupc remove 1          # 依步驟 3 記錄之 pair 編號逐一移除
```

並記錄移除後 `setupc list` 為空，以及驅動卸載結果。

## 證據

evidence_dir 內至少包含：步驟 1 的環境輸出、步驟 2 的 SHA-256 與簽章輸出、步驟 3 的 `setupc list`、步驟 4 的命令輸出（first_open_ok 與 second_open_error 行）、步驟 5 的控制線讀值、步驟 6 的收尾輸出。每項標註執行時間與執行者角色。

## 判定與退路

| 結果 | 判定 | 退路 |
|---|---|---|
| 全部步驟成立且安全功能維持開啟 | 符合（Spike 1 通過，S0-SPIKE1-WIN-VCOM 可關閉） | 無 |
| 簽章無效、HVCI 阻擋驅動載入 | 不符合（驅動路徑不可用） | Console 側改依 OD-01；DUT 側改依 OD-08 實體 USB-UART 直連 |
| 第二次開埠成功（獨占不成立） | 不符合 | DUT 側改依 OD-08；Console 側依 OD-01，並回報 BC-04 Windows 路徑證據不足 |
| 控制線或 baud 跨 pair 外洩 | 不符合 | DUT 側改依 OD-08，不得以虛擬 pair 宣稱隔離 |
| 無管理員權限或無法安裝驅動 | 證據不足（INSUFFICIENT_EVIDENCE） | 交由擁有者取得權限後重跑；不得以單元測試替代 |

## 注意

- 本程序不修改 serialwrap 產品碼。若步驟 4 發現 `_PySerialPort` 獨占語意異常，回報計畫 T10 owner（BC-04 Windows `ACCESS_DENIED` 映射）。
- com0com 為第三方驅動，安裝前須取得擁有者同意（計畫 §2 彙總 #1、#7）。
