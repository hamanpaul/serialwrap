---
work_item: issue-203-serialwrap-connect-code-alias
---

## Why

#197 已證明 Cloudflare Named Tunnel 能讓「一台待測機一個永久代號、遠端 agent 拿代號就接上該機所有 UART」（2026-09-17 雙機 CP-1~CP-4 通過）。但每次連線要手打一長串 `remote -L --ssh-opt=… ProxyCommand=… -i …`，且之後每條命令都要帶 `--endpoint`，離 AnyDesk「一個 ID 就連」還很遠。本變更收斂 host 端與 remote 端的操作摩擦，**不改安全模型（仍是 SSH 金鑰）、不綁 provider（維持 #185）**。

身分模型已與 owner 定案：每台 remote 一條自己的 tunnel／憑證／hostname（代號）；host 端一把金鑰授權多台；UART 完整身分 = 代號 + COM；**SSH username 由 host 端以 `user@host` 形式維護**，別名層原樣傳給 ssh，不猜使用者。

## What Changes

- **新增 host 端 `serialwrap connect <code>` 代號別名層**：從 `~/.config/serialwrap/benches.yaml` 讀該代號的 ssh 目標與選項，展開成現行 `remote -L` 全參數開隧道；開成功後把 loopback endpoint 記住，後續 `serialwrap --bench <code> <subcommand>` 免帶 `--endpoint`。
- **`benches.yaml` 只存 provider-neutral 的 ssh 資訊**（`target: user@host`、`remote_socket`、`local_port`、`ssh_opts` 字串陣列），**不含任何 cloudflare/tailscale 專屬欄位** → 守 #185。ProxyCommand 若有，以純字串放進 `ssh_opts`，serialwrap 不解讀其語意。
- **新增 `serialwrap benches`（list）與 `serialwrap connect <code> --close`**。
- **新增 remote 端 enroll 與管理端 issue 的 `tools/` 腳本**（`tools/bench-enroll.sh`、`tools/bench-issue.sh`）：因涉及 cloudflared（provider-specific），依 #185 刻意放 `tools/` 而非 `sw_core` CLI；一個命令做完 cloudflared 安裝／服務／sshd 金鑰限定／host 公鑰寫入，並印檢查點。
- **不改** `serialwrap remote` 既有 `-L/-R` 契約與強制 `BatchMode=yes`；`connect` 是它的上層別名，最終仍呼叫同一條 spawn 路徑。

## Capabilities

### New Capabilities
- `remote-code-alias`: host 端代號別名層——`benches.yaml`（provider-neutral schema）、`connect`/`benches`/`--close` 子命令、開隧道後 endpoint 記憶與 `--bench <code>` 解析、代號↔ssh 目標／選項的原樣透傳。
- `bench-enrollment-tools`: `tools/` 下的 remote 端 enroll 與管理端 tunnel issue 腳本（provider-specific，刻意不進 `sw_core`），一鍵完成 cloudflared＋sshd 金鑰限定＋憑證放置＋檢查點回報。

### Modified Capabilities
<!-- 無既有 spec 的 requirement 變更；本變更為兩個新 capability。`serialwrap remote` 的 -L spawn 契約不變。 -->

## Impact

- **程式**：`sw_core/cli.py`（`connect`/`benches` subparser、`--bench` 全域參數解析、endpoint 記憶）、`sw_core/remote_tunnel.py`（既有 spawn 邏輯重用、代號展開 helper）、新增 `sw_core/bench_registry.py`（benches.yaml 載入／驗證／endpoint state）、`tests/`（別名展開、endpoint 記憶、provider-neutral schema 驗證的 unit tests）。
- **新增檔案**：`tools/bench-enroll.sh`、`tools/bench-issue.sh`（provider-specific，含 shellcheck 乾淨）。
- **對外契約**：README（中英雙語）與 `docs/**` 新增 connect 別名層與 enroll 流程（R-16/R-18）；`benches.yaml` schema 文件化。
- **持久化**：新增 `~/.config/serialwrap/benches.yaml`（使用者維護）與 runtime endpoint 記憶（state，非機密）；不含憑證、不含密碼。
- **不影響**：WAL 格式、event engine、flash/MCU 路徑、`serialwrap remote` 既有旗標語意、#185 provider-neutral 約束（本變更刻意維持）。
