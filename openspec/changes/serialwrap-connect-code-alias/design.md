## Context

`serialwrap remote -L` 已能在 SSH 可達性由外部（Cloudflare Named Tunnel 等）解決後，把 remote daemon 的 unix socket 拉回 host loopback（agent-pull，#193/#197）。目前一切靠手打長參數，且 `remote_tunnel.py` 已具備 spawn／registry／readiness 全套邏輯。本設計在其上加一層「代號別名」，讓 host 端以一個代號完成連線與後續命令定址，remote 端與管理端以 `tools/` 腳本一鍵完成佈署。

約束：#185 要求 serialwrap 核心 provider-neutral（不得出現 `--cloudflare`/`--tailscale` 或 provider SDK）；`serialwrap remote` 強制 `BatchMode=yes` 不可覆寫；設定物件 frozen、執行期狀態 mutable；RPC 路由為平面 if/elif；CLI JSON 緊湊穩定。

## Goals / Non-Goals

**Goals:**
- host 端：`serialwrap connect <code>` 一行取代整串 `remote -L --ssh-opt=…`，並讓後續命令用 `--bench <code>` 定址、免帶 `--endpoint`。
- `benches.yaml` 為 provider-neutral 的純 ssh 別名表，serialwrap 不解讀 provider 語意。
- remote/管理端：`tools/` 腳本把 #197 SOP 的多步佈署收成各一個命令，含檢查點回報。
- 重用 `remote_tunnel.py` 既有 spawn 路徑，`connect` 只是參數展開 + endpoint 記憶的上層。

**Non-Goals:**
- 不做 NAT 穿越、不內建 tunnel 協定、不綁 provider（維持 #185）。
- 不改 `serialwrap remote` 既有 `-L/-R` 旗標語意與 `BatchMode=yes`。
- 不做 Cloudflare API token 自助鑄 tunnel（第二階段另議）。
- 不把 `bench-enroll`/`bench-issue` 做成 `serialwrap` 子命令（provider-specific → 留 `tools/`）。

## Decisions

1. **benches.yaml schema（provider-neutral）**。位置 `~/.config/serialwrap/benches.yaml`（可由 `SERIALWRAP_BENCHES_FILE` 覆寫，供測試）。結構：
   ```yaml
   benches:
     eit-test:
       target: eit@eit-test.hamanpaul.cc   # user@host，username 由 host 端維護
       remote_socket: /tmp/serialwrap/serialwrapd.sock
       local_port: 7777
       ssh_opts:                            # 純字串陣列，serialwrap 不解讀語意
         - "-o"
         - "ProxyCommand=cloudflared access ssh --hostname %h"
         - "-i"
         - "~/.ssh/id_ed25519_serialwrap_bench"
       autossh: true
   ```
   `target` 一律 `user@host` 形式；缺 user 視為錯誤（不猜、不帶當前使用者）。`ssh_opts` 原樣接到 `remote -L --ssh-opt=…`。**schema 驗證明確拒絕任何 cloudflare/tailscale 專屬鍵**，把「provider-neutral」變成可測不變量。

2. **`sw_core/bench_registry.py`（新模組）**。純函式 + 一個 frozen `BenchEntry` dataclass：`load_benches(path)`→`dict[str,BenchEntry]`、`resolve(code)`→`BenchEntry`、`to_remote_argv(entry)`→展開成 `remote -L` 的 argv（重用 `remote_tunnel.build_argv`/spawn 不重造）。endpoint 記憶另存 runtime state（見決策 4），與 benches.yaml（使用者設定，frozen 語意）分離。

3. **`connect` 子命令路徑重用既有 spawn**。`serialwrap connect <code>` = `resolve(code)` → 組 `remote -L` 參數 → 呼叫 `remote_tunnel.open_tunnel(spec)`（既有），成功後寫 endpoint 記憶。`--close` = `remote_tunnel.close(local_port)`。`serialwrap benches` = 列 benches.yaml + 各代號目前 endpoint 記憶狀態。錯誤一律走既有 `TunnelError`→CLI JSON `{ok:false,error_code}` 邊界，例外不穿越 CLI。

4. **endpoint 記憶與 `--bench` 解析**。`connect` 成功後把 `code→tcp://127.0.0.1:<local_port>` 寫進 runtime state（`$XDG_STATE/serialwrap/benches.state.json`，`sort_keys` 緊湊 JSON）。新增全域參數 `--bench <code>`：`_resolve_endpoint` 優先序調整為 `--endpoint` > `--socket` > `--bench <code>`（查 state）> config fallback。`--bench` 查無記憶 endpoint → 明確 error（提示先 `connect`），不靜默 fallback 到本機 daemon。

5. **`tools/bench-issue.sh <code>`（管理端）**。`tunnel create` + `route dns <code>.<domain>` + 產 `/tmp/<code>-config.yml` + 打包 `handoff/<code>/`（`<UUID>.json` + config）+ 在管理端 `benches.yaml` 寫入該代號（port 遞增）。`<domain>` 由環境變數或參數給，不硬編。

6. **`tools/bench-enroll.sh --bundle <dir>`（remote 端）**。從 bundle 讀憑證 + host 公鑰：cloudflared apt 安裝（`NEEDRESTART_SUSPEND=1`）→ `/etc/cloudflared/` 放置 → `--config` service install → sshd 金鑰限定（reload 不斷線）→ 追加 host 公鑰。跑 CP-1（journal `Registered tunnel connection`）與 CP-2（本地 self-ssh）並印結果；任一失敗即非零退出並回報。**不動既有 serialwrap 設定**、不重啟 serialwrapd。

## Risks / Trade-offs

- **benches.yaml 是新的持久化狀態，可能與 profiles/state 混淆**。緩解：schema 明確只放 ssh 別名，與 `profiles/*.yaml`（UART template）、`state.json`（session/binding）職責分離；文件講清楚。
- **`--bench` 免 `--endpoint` 若 endpoint 記憶過時（隧道已死）會連錯或失敗**。緩解：記憶只存 loopback endpoint，命令實際連線失敗會回既有 `SOCKET_ERROR`；`benches` 列表顯示 alive 狀態供人核對；不做背景健康檢查（YAGNI）。
- **`tools/` 腳本 provider-specific，與核心分離但仍需維護**。接受：這是 #185 的刻意取捨——provider 邏輯集中在少數 `tools/` 腳本、核心零污染，換 provider 時只換腳本與 benches.yaml 的 `ssh_opts`，別名層不動。
- **shell 腳本測試成本**。以 shellcheck + 少量 bats/純 pytest 包裝（呼叫 `--help`/dry-run 模式）覆蓋參數解析與檢查點分支；完整佈署仍需實機（歸 #197 類真機驗證，不強塞 pytest）。
