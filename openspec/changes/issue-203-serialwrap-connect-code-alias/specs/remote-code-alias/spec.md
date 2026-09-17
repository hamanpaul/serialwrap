## ADDED Requirements

### Requirement: 代號別名表為 provider-neutral

`benches.yaml`（預設 `~/.config/serialwrap/benches.yaml`，可由 `SERIALWRAP_BENCHES_FILE` 覆寫）SHALL 只儲存 provider-neutral 的 ssh 別名資訊：每個代號含 `target`（`user@host`）、`remote_socket`、`local_port`、`ssh_opts`（純字串陣列）、`autossh`（bool）。載入器 MUST 拒絕任何 cloudflare/tailscale 等 provider 專屬鍵，並 MUST NOT 解讀 `ssh_opts` 內字串的語意（含 ProxyCommand）。

#### Scenario: 合法 benches.yaml 載入
- **WHEN** benches.yaml 定義代號 `eit-test` 的 `target: eit@eit-test.hamanpaul.cc`、`remote_socket`、`local_port: 7777`、`ssh_opts` 含 ProxyCommand 字串
- **THEN** 載入器回傳該代號的 frozen entry，`ssh_opts` 原樣保留、未被解析

#### Scenario: 出現 provider 專屬鍵即拒絕
- **WHEN** benches.yaml 某代號含 `cloudflare:` 或 `tailscale:` 之類 provider 專屬鍵
- **THEN** 載入器 MUST 回傳明確的 schema 錯誤，指出違反 provider-neutral 約束，且不載入該代號

#### Scenario: target 缺少 user 即錯誤
- **WHEN** 某代號的 `target` 只有 host 而無 `user@`
- **THEN** 解析 MUST 回傳錯誤要求 `user@host` 形式，且 MUST NOT 以當前使用者補上

### Requirement: connect 代號展開重用既有 remote spawn

`serialwrap connect <code>` SHALL 從 benches.yaml 解析該代號，展開成等價於現行 `serialwrap remote -L` 的參數（`--remote-socket`、`--ssh-opt` 逐項、`--autossh`、`local_port`），並呼叫既有隧道 spawn 路徑建立隧道。成功時 SHALL 回傳緊湊 JSON `{ok:true,...}`；失敗 SHALL 走既有 TunnelError→`{ok:false,error_code}` 邊界，例外不得穿越 CLI。

#### Scenario: connect 已知代號
- **WHEN** 對已定義的代號執行 `serialwrap connect eit-test`
- **THEN** 建立與手打 `remote -L --autossh --remote-socket … --ssh-opt=… <target>:<local_port>` 等價的隧道，回 `status: active`

#### Scenario: connect 未知代號
- **WHEN** 對 benches.yaml 未定義的代號執行 connect
- **THEN** 回 `{ok:false, error_code: ...}` 指出代號不存在，不建立隧道、不丟例外

#### Scenario: connect --close 拆除
- **WHEN** 對已連線代號執行 `serialwrap connect eit-test --close`
- **THEN** 拆除該代號對應 `local_port` 的隧道並清除其 endpoint 記憶

### Requirement: 連線後以 --bench 代號定址免帶 endpoint

`connect` 成功後 SHALL 把 `代號 → tcp://127.0.0.1:<local_port>` 寫入 runtime state（緊湊、`sort_keys` JSON、非機密）。新增全域參數 `--bench <code>`：endpoint 解析優先序 SHALL 為 `--endpoint` > `--socket` > `--bench`（查 state）> config fallback。`--bench` 指向查無記憶 endpoint 的代號時 MUST 回明確錯誤（提示先 connect），MUST NOT 靜默 fallback 到本機 daemon。

#### Scenario: connect 後用代號下命令
- **WHEN** `serialwrap connect eit-test` 成功後執行 `serialwrap --bench eit-test session list`
- **THEN** 命令定址到該代號記住的 loopback endpoint，回傳該 remote 的 session 清單，全程未帶 `--endpoint`

#### Scenario: 未連線的代號被拒
- **WHEN** 對尚未 connect（無 endpoint 記憶）的代號執行 `serialwrap --bench eit-02 session list`
- **THEN** 回明確錯誤要求先 `connect`，MUST NOT 連到本機 daemon

#### Scenario: benches 列表顯示狀態
- **WHEN** 執行 `serialwrap benches`
- **THEN** 列出 benches.yaml 各代號與其目前 endpoint 記憶／隧道 alive 狀態
