# Stage 0 Windows remote 三拓樸實機程序

> 對應規格 §9.1 BC-05、SC-21（`remote stop`）、SC-27（`connect`／`benches`）、OD-12 信任範圍；計畫 T21 交付物「Windows remote 三拓樸」。
> 手動程序；實跑屬 BLOCKED-OWNER（計畫 §2 彙總 #17：relay 主機、金鑰、Windows 工作站）。

| 欄位 | 內容 |
|---|---|
| entry | 拓樸 1：`serialwrap remote <user@host>:<port>`（-R expose），relay 端以 `serialwrap --endpoint tcp://127.0.0.1:<port> daemon status` 驗證；拓樸 2：`serialwrap remote <user@host>:<port> --remote-socket <relay-socket-path>`，relay 端以 `serialwrap --socket <relay-socket-path> daemon status` 驗證；拓樸 3：`serialwrap connect <bench>`、`serialwrap benches`、`serialwrap connect <bench> --close`（bench 定義於 `benches.yaml`） |
| revision | `git rev-parse HEAD:realhw/procedures/stage0-windows-remote-topologies.md`（blob sha1）；被測 CLI 以 `serialwrap --version` 與 `sw_core` tree_sha256 記錄 |
| expected | 三拓樸各自完成 open → status → stop → 重啟後 registry 清理；`serialwrap remote stop` 後不留殘留 ssh 轉送行程與 registry 項目；`serialwrap remote`（status）中本次項目為 `active`（`starting` 表示未確認，不得判 PASS）；信任範圍依 OD-12 記錄 ssh 驗證者 |
| owner | 擁有者實機（Windows 工作站與 relay 主機；Win32-OpenSSH 金鑰由擁有者管理，**不得**把金鑰內容寫入 evidence） |
| evidence_dir | `~/b-log/rust-refactor/p1/remote-topologies/<拓樸代號>/<YYMMDD-HHMMSS>/` |
| rollback | 每個拓樸結束後執行 `serialwrap remote stop all`，再以 `serialwrap remote`（無參數＝status）確認無殘留；仍殘留時於 Windows 工作站以工作管理員結束 `ssh.exe` 轉送行程，並刪除 registry 內本次建立的項目（僅本次） |

## 信任範圍（OD-12，必讀）

- Windows 本機 RPC 預設 `tcp://127.0.0.1:48700`，信任範圍＝**整機本機使用者**。本程序**不得**宣稱「同使用者隔離」。
- `serialwrap remote` 經 ssh 暴露 RPC 時，信任範圍延伸至**通過該 ssh 驗證者**（金鑰持有者與 relay 管理員）。每次結果的證據必須記錄實際 ssh 驗證者，以角色代稱（如「relay 管理員」「工作站使用者」），不寫帳號或金鑰指紋原文。

## 隔離

- 三拓樸一律以隔離實例執行：`SERIALWRAP_STATE_DIR`、`SERIALWRAP_RUN_DIR`、`SERIALWRAP_CONFIG_DIR` 三者皆明確設定且不等於預設值（SC-19），並以 `SERIALWRAP_TEST_DEVICE_ALLOWLIST` 明列測試裝置（by-id 路徑）。
- **所有 CLI 呼叫都必須帶 endpoint 參數**（`--endpoint` 或 `--socket`）。未帶時 CLI 連到本機預設 endpoint，會得到無效證據，且可能觸及正式 daemon。
- 不得觸碰正式 daemon、`%USERPROFILE%\.config\serialwrap` 與正式裝置。

## 拓樸 1：expose tcp（本機 RPC 經 ssh -R 暴露）

1. 於 Windows 工作站確認隔離 daemon 已啟動，RPC 為預設 `tcp://127.0.0.1:48700`：

   ```powershell
   serialwrap --endpoint tcp://127.0.0.1:48700 daemon status
   ```

2. 建立 -R 隧道（預設即 expose，對端為 relay 的 `user@host:port`）：

   ```powershell
   serialwrap remote <user@host>:<port>
   ```

3. 驗證：
   - 工作站端 `serialwrap remote`（無參數＝status）列出本次隧道，狀態為 `active`；
   - relay 端由授權金鑰連入後，以轉送到 relay 的 port 驗證可達：

     ```bash
     serialwrap --endpoint tcp://127.0.0.1:<port> daemon status
     ```

     回應必須來自工作站的隔離 daemon（比對 `daemon status` 回報的 state 目錄與證據中記錄的隔離路徑）。
4. 停止：工作站執行 `serialwrap remote stop <port>`，再由 relay 端確認 `tcp://127.0.0.1:<port>` 已不可達。
5. 重啟 daemon 後以 `serialwrap remote` 檢查 registry：不得殘留本次項目。

判定：open／status（`active`）／stop 皆 ok；停止後 relay 端不可達；重啟後 registry 無本次項目。

## 拓樸 2：expose remote_socket（relay 端 Unix domain socket）

1. 於 relay 主機確認 Win32-OpenSSH 版本與 `sshd_config` 的 `StreamLocalBindUnlink` 設定（由擁有者確認）。
2. 於 Windows 工作站建立硬化的 socket 型 -R：

   ```powershell
   serialwrap remote <user@host>:<port> --remote-socket <relay-socket-path>
   ```

3. 工作站 `serialwrap remote`（status）確認本次項目為 `active`。由 relay 主機以授權金鑰連入，對該 socket 執行：

   ```bash
   serialwrap --socket <relay-socket-path> daemon status
   ```

   回應必須可達且來自工作站的隔離 daemon。
4. `serialwrap remote stop <port>` 後確認 relay 端 socket 檔已移除；若殘留，以擁有者權限手動刪除本次建立的 socket 檔。
5. 重啟後以 `serialwrap remote` 檢查 registry 清理。

判定：遠端可達 → 停止後不可達；relay socket 檔已清理；信任範圍記錄 relay 管理員。

## 拓樸 3：connect（工作站以 bench 代號連入 relay）

`connect <code>` 的 `<code>` 是**工作站本機 `benches.yaml` 的 bench 代號**（可重複使用的別名），不是 relay 發出的一次性碼；本拓樸不存在「一次性碼」概念，也不驗證碼重複使用的拒絕行為。

1. 於工作站建立隔離的 `benches.yaml`（路徑由 `SERIALWRAP_BENCHES_FILE` 指定，或放在 `$SERIALWRAP_CONFIG_DIR\benches.yaml`；內容不含金鑰，僅含 bench 定義）。每個 bench 欄位為 `target`（relay 的 `user@host`）、`remote_socket`（relay 端 socket 路徑）、`local_port`（工作站 loopback port）、`ssh_opts`（字串陣列）、`autossh`（布林）：

   ```yaml
   benches:
     relay-lab:
       target: <user@relay-host>
       remote_socket: <relay-socket-path>
       local_port: 7781
       ssh_opts: []
       autossh: false
   ```

2. 執行：

   ```powershell
   serialwrap connect relay-lab
   serialwrap benches
   ```

3. 判定：
   - `serialwrap benches` 列出 `relay-lab`，且其 local_port 與 `benches.yaml` 一致；
   - `serialwrap connect relay-lab` 成功，`serialwrap remote`（status）中本次項目為 `active`；
   - 工作站以 `serialwrap --endpoint tcp://127.0.0.1:7781 daemon status` 經 connect 隧道取得 relay 端 daemon 回應（或 relay 端對應 endpoint 的回應），證明隧道可用。
4. 拆除：`serialwrap connect relay-lab --close`，再以 `serialwrap remote`（status）確認本端轉送已清除、registry 無殘留。
5. 重啟後再次以 `serialwrap remote` 檢查 registry 清理。

判定：connect 與 benches 皆 ok、隧道可達、`--close` 後無殘留、重啟後 registry 無本次項目。

## 判定與證據

- PASS：三拓樸判定皆成立，且 evidence 含 open／status／stop／重啟後 registry 四組輸出與信任範圍記錄。
- FAIL：任一拓樸判定不成立；保留輸出與 registry 快照，回報計畫 T14 owner。
- INSUFFICIENT_EVIDENCE：relay 或工作站不可達、金鑰未就緒、status 僅為 `starting`；記錄原因，不得以此宣稱 BC-05 通過。

## 退路

- Windows 原生 remote 不可用時，BC-05 維持「證據不足」，不得以 `remote` 單元測試替代實機結果。
- relay 不可用時，拓樸 2、3 不得以本機 loopback 模擬宣稱通過。
