---
work_item: issue-203-serialwrap-connect-code-alias
---

## 1. bench_registry 模組（RED first）

- [x] 1.1 寫失敗測試 `tests/test_bench_registry.py`：合法 benches.yaml 載入回 frozen entry、`ssh_opts` 原樣保留；provider 專屬鍵（`cloudflare:`/`tailscale:`）被拒；`target` 缺 user 報錯不補當前使用者。確認 RED 為「模組不存在／函式未實作」的正當理由。
- [x] 1.2 實作 `sw_core/bench_registry.py`：frozen `BenchEntry` dataclass、`load_benches(path)`、`resolve(code)`，schema 驗證（拒 provider 專屬鍵、強制 `user@host`），`SERIALWRAP_BENCHES_FILE` 覆寫。轉綠。
- [ ] 1.3 `to_remote_argv(entry)`：展開成等價 `remote -L` 的 argv，重用 `remote_tunnel` 既有 spawn，不重造 build。加對應測試。

## 2. connect / benches / --bench（host 端 CLI）

- [x] 2.1 寫失敗測試：`connect <code>` 展開參數等價於手打 `remote -L`（以 mock spawn 驗 argv）；未知代號回 `{ok:false,error_code}` 不丟例外；`--close` 拆隧道並清 endpoint 記憶。
- [x] 2.2 實作 `serialwrap connect <code>` / `--close` subparser（`sw_core/cli.py`），呼叫 `bench_registry` + 既有 `remote_tunnel.open_tunnel`/`close`；成功寫 endpoint 記憶（`$XDG_STATE/serialwrap/benches.state.json`，緊湊 `sort_keys` JSON）。轉綠。
- [x] 2.3 寫失敗測試：`--bench <code>` 端點解析優先序（`--endpoint` > `--socket` > `--bench` > config）；未 connect 的代號回明確錯誤、不 fallback 本機 daemon。
- [ ] 2.4 實作全域 `--bench` 參數與 `_resolve_endpoint` 優先序調整。轉綠。
- [ ] 2.5 實作 `serialwrap benches`（list 代號 + endpoint 記憶／alive 狀態）＋測試。

## 3. tools 佈署腳本（provider-specific）

- [ ] 3.1 `tools/bench-issue.sh <code>`：`tunnel create`/`route dns`/產 config/打包 `handoff/<code>/`/寫 benches.yaml；domain 由 env/參數、未提供即非零退出。shellcheck 乾淨。
- [ ] 3.2 `tools/bench-enroll.sh --bundle <dir>`：cloudflared 安裝（`NEEDRESTART_SUSPEND=1`）→ `/etc/cloudflared/` 放置 → `--config` service install → sshd 金鑰限定（reload）→ 追加 host 公鑰 → 跑 CP-1/CP-2 印結果；失敗非零退出；不動既有 serialwrap 設定。shellcheck 乾淨。
- [ ] 3.3 加輕量測試：以 `--help`/dry-run 模式覆蓋參數解析與檢查點分支（pytest 包裝呼叫，不需實機）。

## 4. 文件與對外契約

- [ ] 4.1 README（中英雙語）新增 connect 別名層與 enroll 流程；`benches.yaml` schema 文件化（R-16/R-18）。
- [ ] 4.2 `docs/**` 對齊；`sw_core/assets/skill/SKILL.md` 補精簡版。
- [x] 4.3 新增 `changelog.d/203-serialwrap-connect-code-alias.md` fragment（type `feat`，issue 203）。

## 5. 收斂

- [x] 5.1 `python3 -m pytest -q tests/` 全綠、無新失敗。
- [x] 5.2 `python3 -m policy_check --repo .`（帶 PR 參數複現 CI）通過。
- [x] 5.3 openspec 驗證 `openspec validate --change serialwrap-connect-code-alias`（若適用）；archive 於實作＋review 通過後。
