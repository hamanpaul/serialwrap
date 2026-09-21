---
type: feat
issue: 203
scope: remote
---
新增 bench 佈署腳本（provider-specific，刻意放 `tools/` 不進 `sw_core`，維持 #185 provider-neutral）：

- `tools/bench-issue.sh <code>`：管理端一鍵發代號——`tunnel create`／`route dns`／產 cloudflared config／打包 `handoff/<code>/` 憑證包／寫入 `benches.yaml`。domain 由 `--domain` 或 `BENCH_ISSUE_DOMAIN`／`SERIALWRAP_BENCH_DOMAIN` 提供，未給即非零退出。
- `tools/bench-enroll.sh --bundle <dir>`：bench 端一鍵入列——安裝 cloudflared（帶 `NEEDRESTART_SUSPEND=1`）→ 佈署 `/etc/cloudflared/` → 以 `--config` 做 service install → 加 sshd 金鑰限定 drop-in 並 `reload`（不 restart）→ 追加 controller 公鑰 → 跑 CP-1/CP-2 檢查點。任一步失敗即非零退出，且不改動既有 serialwrap 設定。
- 兩支皆支援 `--help` 與 dry-run，shellcheck 乾淨；`tests/test_bench_tools_cli.py` 以 pytest 包裝覆蓋參數解析與檢查點分支，不需實機。
