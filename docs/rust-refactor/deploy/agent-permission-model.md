# Agent 權限模型與部署規範（§10.3）

本文件定義 `serialwrap` 在生產與測試環境中各執行主體之身分劃分、受保護路徑存取控制原則，以及依規格 §10.3 進行權限阻斷驗證之標準程序。

---

## 1. 身分清單

依最小特權原則，環境中參與 `serialwrap` 運作之身分分為三類：

1. **安裝與更新者（Installer / Maintainer）**：負責軟體安裝、版本升級、回滾與服務註冊。具備對程式目錄之完全控制權限（或於 Windows 具備必要之安裝權限；於 Linux 具備專用安裝帳號或 sudo 權限）。
2. **Daemon 執行身分（Daemon Runtime）**：在受管環境中運行 `serialwrapd` 之使用者身分。依監管模式（`windows-task` / `systemd-user` / `on-demand`）通常以專屬服務帳號或登入使用者帳號執行。
3. **一般任務 Agent（Task Agent / Client）**：透過 CLI 或 RPC 對 target 下達指令、讀取狀態之 AI Agent 或自動化工具。**嚴禁**具備修改 `serialwrap` 二進位檔、設定檔或監管定義之寫入權限。

下表由環境部署負責人填寫並登錄：

| 身分 | 帳號 | 取得方式 | 允許操作 |
|---|---|---|---|
| 安裝／更新者 | （待部署負責人填寫） | 系統管理員指定、CI 部署憑據 | 執行 `setup`、`setup --rollback`、更新 `versions/`、註冊監管工作 |
| Daemon 執行身分 | （待部署負責人填寫） | 排程工作／systemd 設定之執行主體 | 讀取設定檔、開啟 UART / COM 裝置、監聽 RPC 端點 |
| 一般任務 Agent | （待部署負責人填寫） | 容器／沙箱／受限使用者登入 | 執行 `serialwrap` CLI 查詢與任務指令；對受保護資產僅唯讀 |

---

## 2. 受保護路徑

為防止非授權竄改或木馬置換，以下路徑屬於受保護資產，一般任務 Agent 僅允許唯讀與執行（RX）存取，禁止任何寫入、刪除、改名或置換：

### Windows 環境
- `%LOCALAPPDATA%\Programs\serialwrap\versions\`：各版本二進位與資產檔案目錄。
- `%LOCALAPPDATA%\Programs\serialwrap\bin\`：包含 `serialwrap.exe`、`serialwrapd.exe` 與 `current-release.json` 之入口目錄。
- `%LOCALAPPDATA%\Programs\serialwrap\backups\`：更新切換之版本備份目錄。
- `%LOCALAPPDATA%\Programs\serialwrap\supervisor\`：排程工作 XML 定義與監管資產目錄。
- 設定檔與憑據：`%APPDATA%\serialwrap\config.yaml`、profiles 設定目錄與環境檔。
- 上述各目錄之父目錄（`%LOCALAPPDATA%\Programs\serialwrap\` 與其所有上層祖先目錄）。

### Linux / POSIX 環境
- 預設使用者佈局：`${DATA_DIR}/versions/`（如 `~/.local/share/serialwrap/versions/`）、`~/.local/bin/serialwrap`、`~/.local/bin/serialwrapd`。
- 自訂全域佈局（使用 `SERIALWRAP_INSTALL_ROOT`，例如 `/opt/serialwrap`）：`/opt/serialwrap/bin/`、`/opt/serialwrap/versions/`。
- 設定檔與憑據：`~/.config/serialwrap/config.yaml`、profiles 設定檔與 env files。
- 軟體安裝之所有祖先目錄（例如 `/opt/serialwrap` 或 `${DATA_DIR}` 及其上層目錄）。

---

## 3. Windows ACL 設定

在 Windows 上，受保護目錄應由安裝者帳號擁有，並使用 `icacls` 移除一般任務 Agent 之寫入、修改與刪除權限，僅保留讀取與執行：

```powershell
# 移除一般 Agent 身分之寫入 (W)、修改 (M)、刪除 (D) 權限，僅保留讀取與執行 (RX)
icacls "$env:LOCALAPPDATA\Programs\serialwrap" /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "DeployerUser:(OI)(CI)F" "AgentUser:(OI)(CI)RX"
icacls "$env:LOCALAPPDATA\Programs\serialwrap\bin" /grant:r "AgentUser:(OI)(CI)RX"
icacls "$env:LOCALAPPDATA\Programs\serialwrap\versions" /grant:r "AgentUser:(OI)(CI)RX"
```

---

## 4. Linux 權限設定

在 Linux / POSIX 上，受保護檔案與目錄應由安裝者帳號擁有（如 root 或獨立之 maintainer 帳號，若使用 `SERIALWRAP_INSTALL_ROOT=/opt/serialwrap` 則由專用帳號維護），並將模式設為嚴格唯讀（目錄 `0755`、檔案 `0555` 或 `0444`），Agent 執行帳號僅屬於 `other` 或唯讀群組：

```bash
# 若指定 SERIALWRAP_INSTALL_ROOT=/opt/serialwrap：
# 由維護者擁有，移除群組與其他使用者的寫入權限
chown -R deployer:deployer /opt/serialwrap
find /opt/serialwrap -type d -exec chmod 0755 {} +
find /opt/serialwrap -type f -exec chmod 0555 {} +
```

---

## 5. 同身分部署的限制

依據規格 §10.3 之原文要求：若一般任務 Agent 與安裝／更新者屬於同一系統帳號，只能宣稱流程約束，不得宣告已完成權限阻斷。

原因在於：當受保護資產之同 owner 為該執行身分時，**擁有者可自行恢復寫權**（在 POSIX 平台擁有者可無條件呼叫 `chmod` 還原寫入權限；在 Windows 平台對象擁有者隱含 `WRITE_DAC` 特權，可自行修改 DACL 重新賦予寫入權限）。因此，即使部署時暫時將檔案設為唯讀（如 `0555`），驗證工具 `verify_agent_perms.py` 在分析權限鏈與恢復特權時亦會判定其具備恢復能力，回報 `not_blocked`。在此種部署模式下，不得將唯讀檔案視為有效之安全性隔離。

---

## 6. 驗證程序

部署完成後，必須以一般任務 Agent 之實際執行身分執行驗證工具 `scripts/rust/verify_agent_perms.py`。

### 驗證腳本行為與安全原則
- 驗證工具對受保護路徑僅進行唯讀查詢與「開啟後立即關閉」探測，絕不改寫真實目標檔案，亦不修改其權限或 ACL。
- 寫入與置換探測透過在父目錄與目錄目標內部建立隨機名稱之 `.permprobe-<pid>-<rand>` sentinel 檔案進行，探測完畢立即透過 `finally` 清除。
- 驗證工具檢查檔案系統權限與權杖特權（privileges），但**不檢查** `sudo`、`doas`、docker 群組或 UAC 提權機制；因此取得 `blocked` 為達成權限阻斷之必要條件，但非主機防護之充分條件。

```bash
# 以 Agent 身分執行驗證（若使用自訂安裝路徑須指定 SERIALWRAP_INSTALL_ROOT）
python3 scripts/rust/verify_agent_perms.py \
  --targets /opt/serialwrap/bin/serialwrap /opt/serialwrap/versions \
  --json-out ./target/perm_evidence/perm_evidence_agent_$(date +%Y%m%d_%H%M%S).json
```

### 證據保存格式規範
驗證產出之 JSON 報告應保存於持久性證據目錄（如 `target/perm_evidence/` 或部署 CI 產物），命名格式為 `perm_evidence_<identity>_<timestamp>.json`。報告包含以下欄位：
- `identity`：執行檢驗之 Agent 身分（POSIX 為使用者名稱，Windows 為 `DOMAIN\User`）。
- `platform`：作業系統平台識別。
- `results`：各目標路徑之當前操作探測清單（`target`、`op`、`allowed`、`error`）。
- `restore_checks`：路徑鏈之恢復寫權能力檢查清單（`target`、`path`、`kind`、`possible`、`detail`）。
- `verdict`：整體判定結論（`"blocked"` / `"not_blocked"` / `"unknown"`）。
- `blocked`：布林判定結果（`true` / `false` / `null`）。

### 判定標準與報告要求
1. **阻斷成功（blocked，exit 0）**：當且僅當所有此刻寫入／置換操作（write, rename, delete, replace, parent_dir_write）均被拒絕（`allowed: false`），**且**所有恢復能力檢查（`restore_checks`）均確認無法恢復（`possible: false`）時，輸出 `verdict: "blocked"`，`blocked: true`。
2. **阻斷未達成（not_blocked，exit 3）**：任一操作被允許，或任一恢復檢查判定可能恢復寫權（例如受保護路徑或任一祖先目錄由 Agent 身分擁有、或 Agent 身分可改其 DACL 時，即使當下寫入／改名被拒也判 `not_blocked`）。
3. **證據不足（unknown，exit 4）**：若環境資訊不足（例如 capability 無法讀取、查詢 API 失敗或祖先目錄無讀取權限無法確認安全狀態），輸出 `verdict: "unknown"`。

**重要合規宣告**：`verdict` 為 `not_blocked`（exit 3）或 `unknown`（exit 4）的報告不得作為已完成權限阻斷的證據。只有取得 `verdict: "blocked"` 且 exit code 0 之報告方可納入驗收證據保存。

---

## 7. 信任範圍

依據架構決策 OD-12，`serialwrap` 在本機之信任範圍界定如下：

- **本機信任邊界（OD-12）**：Windows 上 JSON-RPC（`127.0.0.1:48700`）與 Telnet console 之信任範圍明定為**整機本機使用者皆可信**。在多人共用主機上不得宣稱同使用者隔離。
- **多人共用機器限制**：多人共用機器（含 `windows-service` bench）在完成多使用者認證／授權（RPC token＋ACL，且 Telnet console 一併納管）前不得部署；僅允許部署於單人使用之工作站（`windows-task`）。
- **遠端隧道信任邊界**：當使用 `serialwrap remote` 經由 SSH 連線對外暴露或連入時，信任範圍延伸至通過該 SSH 認證之使用者；凡通過 SSH 認證者即獲得對應 UART 資源之控制權。
- **單人工作站原則**：在完成多使用者認證前，多人共用環境不得宣稱同使用者隔離，亦不得在缺乏完整 RPC Token 與 Telnet Console 存取控制下直接部署。
