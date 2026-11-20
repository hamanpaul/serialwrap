# Stage 2 0.5.0 正式版安裝說明

本文件適用於 Stage 2 `0.5.0` 正式版。下載後先依平台核對 `SHA256SUMS`，雜湊不符時不要解壓或安裝。

## Linux：安裝或由既有 bundle 升級

1. 取得 `serialwrap-0.5.0-linux-x86_64.tar.gz`、`SHA256SUMS` 與本說明，並在同一目錄執行：

   ```bash
   sha256sum -c --ignore-missing SHA256SUMS
   ```

   必須看到 Linux bundle 的 `OK`；雜湊不符時停止安裝。

2. `install.sh` 不在 bundle 資產內；請從正式版 tag 取得同版安裝器，再由 bundle 安裝或升級。以下以 systemd-user 為例：

   ```bash
   TAG=v0.5.0
   curl -fsSL "https://raw.githubusercontent.com/hamanpaul/serialwrap/${TAG}/install.sh" -o ./install.sh
   chmod +x ./install.sh
   ./install.sh --bundle ./serialwrap-0.5.0-linux-x86_64.tar.gz -- --user
   ```

   沒有 systemd 時可改用 `--on-demand`；systemd-system 請依既有管理流程使用 `--system --with-sudo`。`install.sh` 會執行 bundle 的 `serialwrap setup`，不需要先手動停止 daemon。完成後檢查 `serialwrap --version`、`serialwrap doctor` 與 `serialwrap daemon status`（systemd 模式亦可用 `serialwrap service status`）。

3. 本版沒有 `serialwrap update --from` 子命令；從下載的 bundle 更新請使用上述 `install.sh --bundle` 流程。直接解開 bundle 時，也可執行該版本目錄中的 `versions/0.5.0/bin/serialwrap setup --user`（依實際監管模式調整）。

## Windows：安裝或升級

1. 下載 `serialwrap-0.5.0-windows-x86_64.zip`、`SHA256SUMS` 與本說明。PowerShell 中核對 zip 雜湊：

   ```powershell
   $name = 'serialwrap-0.5.0-windows-x86_64.zip'
   $row = Get-Content .\SHA256SUMS | Where-Object { $_ -match "  $([regex]::Escape($name))$" }
   if (@($row).Count -ne 1) { throw 'SHA256SUMS 缺少唯一的 Windows bundle 列' }
   $expected = $row.Substring(0, 64).ToLowerInvariant()
   $actual = (Get-FileHash -Algorithm SHA256 $name).Hash.ToLowerInvariant()
   if ($actual -ne $expected) { throw 'Windows bundle SHA-256 不符；停止安裝' }
   ```

2. 解壓縮後，使用 bundle 版本目錄內的 CLI 執行 setup，並明確選擇監管模式：

   ```powershell
   Expand-Archive .\serialwrap-0.5.0-windows-x86_64.zip .\serialwrap-0.5.0
   & .\serialwrap-0.5.0\serialwrap-0.5.0-windows-x86_64\versions\0.5.0\bin\serialwrap.exe setup --windows-task
   ```

   若使用 on-demand，將 `--windows-task` 改為 `--on-demand`。安裝或升級後，以安裝根目錄的 `bin\serialwrap.exe --version`、`doctor` 及 `daemon status` 確認版本與 daemon 狀態。Windows bundle 未簽署；請以 SHA-256 核對來源，不要因 SmartScreen 警告而略過雜湊驗證。

## 回滾至 0.4.1

- 若 0.4.1 是安裝前的上一個 bundle，並以本版執行過升級，使用目前安裝的 CLI 執行完整組合回滾：
  - Linux：`serialwrap setup --rollback`
  - Windows PowerShell：`& "$env:LOCALAPPDATA\Programs\serialwrap\bin\serialwrap.exe" setup --rollback`
- 回滾後以 `--version`、`doctor` 與 `daemon status` 確認回到 0.4.1 且 CLI／daemon 配對正常。若 0.5.0 是新機首次安裝、沒有上一個 bundle，`setup --rollback` 沒有可回復的 0.4.1；Linux 可用 `./install.sh --version 0.4.1 -- --user` 安裝（將 `--user` 換成原監管模式）；Windows 請取得官方 0.4.1 zip，再執行其版本目錄中的 `serialwrap.exe setup --windows-task`（依原監管模式調整）。
- 不要手動刪除安裝目錄或直接覆蓋 daemon 執行檔；完整 bundle 回滾會一起切換 CLI、daemon、資產與監管設定。
