## Context

完整設計見 `docs/superpowers/specs/2026-06-29-daemon-device-identity-hardening-design.md`。本檔聚焦技術決策與風險。

現況：COM 編號在每裝置的並發 attach thread 內以 `_next_dynamic_com()` 取「最低空號」分配（`sw_core/session_manager.py:511-528`，經由 `_spawn_attach`:1416 → `_attach_by_id_dynamic`:1771 → `_session_from_template`:1821），故 startup 多裝置並發 attach 時 COM0 給「最先搶到 lock 的 thread」，restart 對調（#100 根因，Codex CONFIRMED）。`SingletonLock` 為 per-`(lock_path, socket_path)`，跨 socket/監管模式不互斥（#101 根因，Codex CONFIRMED）。

現行基準：COM0=`AC01QZT0`、COM1=`AQ00OAQ7`；by-id 字典序 `AC01…`<`AQ00…`，故 sorted rank 即還原現行對應。

## Goals / Non-Goals

**Goals:**
- COM↔by-id 對應在 restart / 亂序 attach 下確定性穩定。
- daemon 主動暴露多開 / two-reader，免 user 手動 `ps`。
- （on-demand `session renumber` 重排已 defer 至 follow-up，不在本 PR 目標內。）

**Non-Goals:**
- 不做 #101 自動 refuse/kill/退讓（純偵測+回報）。
- 不做 COM map sticky 持久化（確定性來自排序）。
- 不改 runtime hotplug 既有 DETACHED-rebind 行為。
- 不碰 #94、#84。

## Decisions

- **D1：確定性 sorted-by-`device_key` rank，而非 sticky 持久化**。替代方案（持久化 by-id→COM map）需額外 state 與 reconcile，且仍要定義衝突；排序天生 restart-stable 且零持久化。`device_key`=by-id 路徑，同款晶片衝突 fallback by-path。
- **D2：rank 上移到 lock 內、spawn threads 之前**。替代（在 thread 內讓 `_next_dynamic_com` 算 rank）仍受並發 race；唯有在同步點對「整批在線 dynamic 裝置」一次排序配號才確定。兩條 startup 入口（`service.py:464` `update_devices` + `:465` `bootstrap_attach`）都涵蓋。
- **D3：rank 僅作用於 dynamic 自動偵測 session**；explicit `targets`/`bind` COM 為權威、排除 pool 外，避免污染既有持久化綁定。
- **D4：runtime hotplug 沿用現有（使用者拍板 (a)）**；不同 by-id 板繼承空槽，不活體重編 active。on-demand 顯式重排（`session renumber`）已 defer 至 follow-up。
- **D5：`session renumber` defer 至 follow-up（#103）；本 PR 不含**。經 reviewer（superpowers + codex）審查：強制重編 active session 會牽動 attach 時以值捕捉 `session_id` 的 bridge callback、flash state、lease reverse-link 等深層管路，須改以「拆 bridge → 改號 → 重 attach」另案重做，故自本 PR 移除。
- **D6：#101 偵測為 on-demand、module-level helper**（非複用只回 pid 的 `_probe_external_holder` instance method）。doctor 直接用（daemon-less）；daemon status 走 executor offload（`health.status` 在同步 dispatcher，全 /proc 掃描會凍結 event loop）。

## Risks / Trade-offs

- **[renumber 原子 remap 漏改某狀態 → session 參照斷裂]** → 即此風險導致 `session renumber` defer 至 follow-up（bridge callback / flash state / lease reverse-link 須改以拆 bridge → 改號 → 重 attach 重做），本 PR 不含。
- **[跨 uid 讀不到 `/proc/<pid>/fd` → 無法判定 tty 持有者]** → mitigation：明確降級為 `permission`/`unknown` 狀態並寫進輸出契約，不靜默；至少回報「另有 serialwrapd 存在」。
- **[hotplug (a) 與 #100「COM 誠實對應實體板」部分矛盾]** → 已知取捨，使用者明確選 (a)；矯正手段（`session renumber`）defer 至 follow-up，現階段以 restart 重排為暫時手段。
- **[daemon status 掃 /proc 阻塞 event loop]** → mitigation：executor offload 或快取，不在 RPC 同步路徑掃描。

## Migration Plan

- 純行為強化，無資料遷移。新欄位/命令為增量、向後相容。
- rollback：revert PR 即回到現行 index-order 分配（行為退回，無 state 殘留）。

## Open Questions

- `session renumber` 的「拆 bridge → 改號 → 重 attach」具體編排（bridge callback 重綁、flash state / lease reverse-link 處理）於 follow-up 另案定案。
