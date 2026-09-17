## ADDED Requirements

### Requirement: 管理端 tunnel issue 腳本

`tools/bench-issue.sh <code>` SHALL 在持有帳號級 `cert.pem` 的管理端建立該代號專屬的 Cloudflare Named Tunnel：`tunnel create`、`route dns <code>.<domain>`、產生 ingress 為 `ssh://localhost:22` 的 config，並打包成 `handoff/<code>/`（含該 tunnel 的 `<UUID>.json` 與 config）。`<domain>` MUST 由環境變數或參數提供，MUST NOT 硬編。腳本 SHALL 同時在管理端 `benches.yaml` 寫入該代號（`local_port` 遞增不衝突）。

#### Scenario: 產出憑證包與 benches 條目
- **WHEN** 設定好 domain 後執行 `tools/bench-issue.sh eit-02`
- **THEN** 建立 tunnel `eit-02` 與 DNS `eit-02.<domain>`，產出 `handoff/eit-02/`（憑證＋config），並在 benches.yaml 新增 `eit-02` 條目且 `local_port` 不與既有代號衝突

#### Scenario: 未提供 domain 即拒絕
- **WHEN** 未提供 domain 就執行腳本
- **THEN** 腳本 MUST 以非零退出並提示需要 domain，MUST NOT 使用任何硬編網域

### Requirement: remote 端一鍵 enroll 腳本

`tools/bench-enroll.sh --bundle <dir>` SHALL 在 remote 端以單一命令完成佈署：安裝 cloudflared（apt，帶 `NEEDRESTART_SUSPEND=1`）、把 bundle 的憑證與 config 放進 `/etc/cloudflared/`、以 `--config` 明確指定 `service install`、設定 sshd 為金鑰限定（既有連線以 reload 不中斷）、將 bundle 內 host 公鑰追加到登入使用者的 `authorized_keys`。腳本 SHALL 執行 CP-1（journal 出現 `Registered tunnel connection`）與 CP-2（本地 self-ssh 成功）並回報結果，任一失敗 MUST 以非零退出。腳本 MUST NOT 修改既有 serialwrap 設定，MUST NOT 重啟 serialwrapd。

#### Scenario: enroll 成功
- **WHEN** 帶合法 bundle 執行 `tools/bench-enroll.sh --bundle handoff/eit-02`
- **THEN** cloudflared 服務 active 且 journal 有 `Registered tunnel connection`、sshd 為金鑰限定、host 公鑰已在 authorized_keys，CP-1/CP-2 皆通過並印出結果

#### Scenario: 檢查點失敗即非零退出
- **WHEN** enroll 過程 cloudflared 未成功註冊（CP-1 失敗）
- **THEN** 腳本 MUST 以非零退出並回報失敗的檢查點，MUST NOT 靜默視為成功

#### Scenario: 不觸碰既有 serialwrap 狀態
- **WHEN** enroll 在已有 serialwrapd 在跑的 remote 上執行
- **THEN** 既有 serialwrap 設定與 daemon MUST 不被修改或重啟
