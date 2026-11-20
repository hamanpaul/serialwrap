# 跨 reboot 實板 console 情境（BLOCKED-OWNER #6）

對應 `plans/07-slice22-console.md` Task 4、Task 7 與 BLOCKED-OWNER 總表 #6：真實 U-Boot autoboot 倒數中的 `boot_interrupt`
救援，以及實板重開期間 human 鍵入不回放（BC-02／SC-31）。L1 與 L1.5 以 FakeTarget 覆蓋同一邏輯，本程序只補實板時序證據。

## 前置條件

1. 一塊有 U-Boot autoboot 倒數（`Hit any key to stop autoboot`）且允許 reboot 的實板；擁有者授權本次 reboot。
2. Rust bundle 已部署；profile 的 `bootloader_prompts` 含該板 U-Boot prompt（例如 `^=> $`）。
3. human console（Linux：minicom 開 `session.vtty`；Windows：Tera Term 連 `console_endpoint`）已連上。
4. 證據目錄 `EVID=target/cross-reboot-console/<yymmdd-hhmmss>/`。

## 逐步驟

1. 對照組（不 reboot）：agent `cmd submit --cmd "sleep 3"` 期間，human 鍵入 `K`；命令完成後確認 DUT 收到 `K`（回放成立）。
2. agent 送 `cmd submit --cmd reboot`；DUT 開始重開後、autoboot 倒數出現前，human 鍵入 `R`。
3. 倒數出現時立即 `session interactive-open --selector COM0 --owner agent --allow-attached`。
   - 期望：回 `recovery_mode:true`、`boot_interrupt:true`；以 `interactive-send --data " "` 中斷 autoboot，畫面停在 U-Boot prompt。
4. `interactive-send --data "reset\n"`（U-Boot 唯讀護欄：只用 `reset` 離開），等 session 回 READY。
5. 擷取 `wal tail --selector COM0 --json > $EVID/wal.json`，確認 `R` 從未出現在任何 TX（含 reboot 後）。

## 證據檔名清單

- `wal.json`、`session-states.ndjson`（每 0.5 s 一筆 `session list` 的 state）
- `interactive-open.json`、`control-replay.json`（步驟 1）
- `console-capture.log`（human console 畫面全程擷取）
- `env.json`（板型、U-Boot 版本、bundle 版本、profile 片段）

## PASS 條件

步驟 1 對照組回放成立；步驟 3 的回應欄位相符；步驟 5 的 WAL 無含 `R` 的 TX；session 最終 READY。對照組不成立時判
`INSUFFICIENT`（無法區分「沒回放」與「沒擷取到」），不得 PASS。

## 失敗時記錄

記下失敗步驟、WAL 片段、session state 序列與 daemon stderr，存 `fail-<步驟>.md`；若板子停在 U-Boot，先以 `reset` 還原再離場。
