---
status: accepted
work_item: issue-203-serialwrap-connect-code-alias
target_branch: feature/203-serialwrap-connect-code-alias
issue: 203
---

# #203 serialwrap connect 代號別名層實作計畫

**Goal:** 收斂 Cloudflare 遠端連線的操作摩擦到「一個代號」的體感——host 端 `serialwrap connect <code>` 一行取代整串 `remote -L --ssh-opt=… ProxyCommand=… -i …`，並讓後續命令用 `--bench <code>` 定址免帶 `--endpoint`；remote/管理端以 `tools/` 腳本一鍵佈署。**不改安全模型（仍是 SSH 金鑰）、不綁 provider（維持 #185）。**

**Architecture:** 別名層是 `serialwrap remote -L` 的上層包裝，最終仍呼叫 `sw_core/remote_tunnel.py` 既有 spawn 路徑，不重造隧道邏輯。新增 `sw_core/bench_registry.py`（載入 `benches.yaml`、provider-neutral schema 驗證、代號→remote argv 展開）與 endpoint 記憶（runtime state）。cloudflared 相關佈署腳本放 `tools/`（provider-specific），刻意不進 `sw_core` CLI。對外契約完整見 OpenSpec change `serialwrap-connect-code-alias`（proposal/design/specs）。

## Decisions（本計畫已裁決，實作不得再開放）

1. `benches.yaml` 位置 `~/.config/serialwrap/benches.yaml`，可由 `SERIALWRAP_BENCHES_FILE` 覆寫（供測試）。schema 只含 `target`（`user@host`）、`remote_socket`、`local_port`、`ssh_opts`（純字串陣列）、`autossh`（bool）。
2. **`target` 一律 `user@host`**；缺 `user@` 視為錯誤，**不猜、不補當前使用者**（SSH username 由 host 端維護）。
3. schema 驗證**明確拒絕** cloudflare/tailscale 等 provider 專屬鍵，把 provider-neutral 變成可測不變量（守 #185）；`ssh_opts` 內字串（含 ProxyCommand）serialwrap **不解讀語意**、原樣透傳。
4. endpoint 記憶存 `$XDG_STATE/serialwrap/benches.state.json`（緊湊 `sort_keys` JSON、非機密，不含憑證/密碼），與 `benches.yaml`（使用者設定）分離。
5. `--bench` 端點解析優先序：`--endpoint` > `--socket` > `--bench`（查 state）> config fallback。`--bench` 查無記憶 endpoint → **明確錯誤**（提示先 connect），**不靜默 fallback 到本機 daemon**。
6. `connect` 重用 `remote_tunnel.open_tunnel`/`close`，錯誤走既有 `TunnelError`→CLI JSON `{ok:false,error_code}` 邊界，例外不穿越 CLI。
7. `bench-enroll`/`bench-issue` **不做成 `serialwrap` 子命令**（provider-specific → 留 `tools/`）；shell 腳本以 shellcheck 乾淨＋pytest 包裝 `--help`/dry-run 覆蓋參數解析與檢查點分支，完整佈署歸實機（不強塞 pytest）。
8. 不做 Cloudflare API token 自助鑄 tunnel（第二階段另案）；不改 `serialwrap remote` 既有 `-L/-R` 旗標語意與 `BatchMode=yes`。

## Task 1: bench_registry 模組（RED first）

**Files:** `sw_core/bench_registry.py`、`tests/test_bench_registry.py`

- [ ] 1.1 先寫失敗測試：合法 `benches.yaml` 載入回 frozen `BenchEntry`、`ssh_opts` 原樣保留；provider 專屬鍵（`cloudflare:`/`tailscale:`）被拒並回明確 schema 錯誤；`target` 缺 `user@` 報錯不補當前使用者；`SERIALWRAP_BENCHES_FILE` 覆寫生效。確認 RED＝模組/函式未實作的正當理由。
- [ ] 1.2 實作 `sw_core/bench_registry.py`：frozen `BenchEntry` dataclass、`load_benches(path)`→`dict[str,BenchEntry]`、`resolve(code)`→`BenchEntry`，schema 驗證（拒 provider 專屬鍵、強制 `user@host`）。轉綠。
- [ ] 1.3 `to_remote_argv(entry)`：展開成等價 `remote -L` 的 argv（`--remote-socket`、逐項 `--ssh-opt`、`--autossh`、`local_port`），重用 `remote_tunnel` 既有 build/spawn，不重造。加對應測試。

## Task 2: connect / benches / --bench（host 端 CLI）

**Files:** `sw_core/cli.py`、`tests/test_bench_connect_cli.py`

- [ ] 2.1 先寫失敗測試：`connect <code>` 展開參數等價於手打 `remote -L`（mock spawn 驗 argv）；未知代號回 `{ok:false,error_code}` 不丟例外；`--close` 拆隧道並清 endpoint 記憶。
- [ ] 2.2 實作 `serialwrap connect <code>` / `serialwrap connect <code> --close` subparser，呼叫 `bench_registry` + 既有 `remote_tunnel.open_tunnel`/`close`；成功寫 endpoint 記憶（緊湊 `sort_keys` JSON）。轉綠。
- [ ] 2.3 先寫失敗測試：`--bench <code>` 端點解析優先序（決策 5）；未 connect 的代號回明確錯誤、不 fallback 本機 daemon。
- [ ] 2.4 實作全域 `--bench` 參數與 `_resolve_endpoint` 優先序調整。轉綠。
- [ ] 2.5 實作 `serialwrap benches`（list 代號 + endpoint 記憶／隧道 alive 狀態）＋測試。

## Task 3: tools 佈署腳本（provider-specific）

**Files:** `tools/bench-issue.sh`、`tools/bench-enroll.sh`、`tests/test_bench_tools_cli.py`

- [ ] 3.1 `tools/bench-issue.sh <code>`：`tunnel create`/`route dns <code>.<domain>`/產 config/打包 `handoff/<code>/`/寫管理端 `benches.yaml`（port 遞增）；`<domain>` 由 env/參數，未提供即非零退出。shellcheck 乾淨。
- [ ] 3.2 `tools/bench-enroll.sh --bundle <dir>`：cloudflared 安裝（`NEEDRESTART_SUSPEND=1`）→ `/etc/cloudflared/` 放置 → `--config` service install → sshd 金鑰限定（reload 不斷線）→ 追加 host 公鑰 → 跑 CP-1（journal `Registered tunnel connection`）/CP-2（本地 self-ssh）印結果；任一失敗非零退出；**不動既有 serialwrap 設定、不重啟 serialwrapd**。shellcheck 乾淨。
- [ ] 3.3 pytest 包裝：以 `--help`/dry-run 模式覆蓋兩腳本的參數解析與檢查點分支（不需實機）。

## Task 4: 文件與 changelog

**Files:** `README.md`、`docs/**`、`sw_core/assets/skill/SKILL.md`、`changelog.d/203-serialwrap-connect-code-alias.md`

- [ ] 4.1 README（中英雙語）新增 connect 別名層與 enroll 流程；`benches.yaml` schema 文件化（R-16/R-18）。
- [ ] 4.2 `docs/**` 對齊；`sw_core/assets/skill/SKILL.md` 補精簡版。
- [ ] 4.3 新增 `changelog.d/203-serialwrap-connect-code-alias.md` fragment（type `feat`、issue 203）。

## Task 5: 驗證閘

**Files:**（無新檔，執行閘門）

- [ ] 5.1 `python3 -m pytest -q tests/` 全綠、無新失敗。
- [ ] 5.2 `python3 -m policy_check --repo .`（帶 PR 參數複現 CI）通過。
- [ ] 5.3 `openspec validate serialwrap-connect-code-alias` 通過；archive 於實作＋review 通過後。

## Open Questions

- none
