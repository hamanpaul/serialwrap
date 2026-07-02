# Design: test-state-isolation-120

> 完整設計（含探索證據、四路稽核、adversarial review 迭代）見
> `docs/superpowers/specs/2026-07-02-issue-120-test-state-isolation-design.md`（spec v2）。
> 本檔為 OpenSpec 摘要版，聚焦技術決策。

## Context

- `sw_core/constants.py` 的路徑常數 import-time 凍結，消費模組以 `from .constants import X` 綁定副本，`WalWriter.__init__` 預設參數 def-time 再凍結一層——共三層凍結；測試內 `setenv` 一律太遲。
- `SessionManager.__init__` 尾段無條件 `_save_state()`：光是建構就寫 `STATE_PATH`。8 檔 33 個建構點完全未隔離（vector 1）。
- `cli.py:289` 以 `args.socket != SOCKET_PATH` 判斷明確性；測試傳的 `--socket` 恰等於自身 env 推導的預設值 → 誤判 → fallback 讀 live config → RPC 全打到 live daemon（vector 2；fake binding／live WAL reset／真板 TX／`daemon stop` route 到 live systemd 的真兇）。
- `tests/` 無 conftest.py，隔離全靠各檔自律（19 檔對、8 檔漏）。

## Goals / Non-Goals

**Goals:**
- 有 production daemon 的機器上跑完整 pytest suite，live state.json／WAL／config.yaml／daemon 零接觸；復發時 gate 立刻紅（本機與 CI 皆真陽性）。
- `python3 -m unittest discover` 跑法下 state.json 維度同樣安全（per-file 隔離）。

**Non-Goals:**
- constants 全面惰性化（三層凍結使其單獨無效、改動面最大）。
- 跨行程 spy 級的零接觸「證明」——以 outcome guard（前後快照）取代。
- unittest runner 的雜項維度完整隔離——events 維度已由 `tests/state_iso.py` 涵蓋（#121 F3），其餘（LOG_DIR 等）仍 pytest-only（文件載明）。
- `setup_cmd._state_path_for` 對 systemd-system 的既有 path mismatch（另案）。

## Decisions

1. **注入而非惰性化**：`SessionManager(state_path=None)` 建構注入，fallback 讀模組層全域（非 def-time default）→ 既有 19 檔 `setattr` 測試零破壞；`SerialwrapService` 透傳；daemon 不改。`WalWriter` default 改 None-sentinel 建構時解析，消除 def-time 凍結層。
   - 替代案（PEP 562 lazy constants）被否決：三層凍結需同時改掉全部 from-import 與函式簽章，reload 型測試全要改寫。
2. **CLI `--socket` sentinel**：`default=None`，「有傳即明確」。保留 a64c465 原意（未指定 → 讀 config 連 systemd-system socket）；語意變更僅「明確傳入恰等於預設值」一處——即 bug 本身。`_run_daemon_start` on-demand 路徑以 `args.socket or SOCKET_PATH` 等價替換。
3. **conftest 三層防線**（pytest-only，時序關鍵——top-level 早於任何測試模組 import）：
   - L1 強制 env 隔離：先算 live 快照再硬覆寫 `SERIALWRAP_{STATE,RUN,WAL,CONFIG,LOG,EVENTS,EVENTS_RUNTIME}_DIR`＋`BY_ID/BY_PATH`（空目錄防抓真板）到 per-run tmp。
   - L2 function-scoped autouse `setattr(session_manager, "STATE_PATH", tmp)`：消除順序耦合。
   - L3 live guard（`tests/liveguard.py` 純函式＋sessionstart/finish hook）：
     - Guard 1 state.json：**任何 byte 變更／從無到有 → FAIL**（strict 預設）；`SERIALWRAP_LIVE_GATE=warn` 逃生閥下，結構性破壞（`released`/`bindings`/`aliases` key 消失、污染特徵）**仍 FAIL**。
     - Guard 2 live WAL：檔案消失 → FAIL（結構級，不受 warn 閥管）；size 縮小 → FAIL（strict；warn 下降 WARN——rotation 誤報情境）；append 為 live daemon 常態不 FAIL；外層 shell 原有 `SERIALWRAP_WAL_DIR` 路徑任何變更（含同 size 內容改寫）→ 無條件 FAIL。
     - Guard 3 live config.yaml：任何 byte 變更 → 無條件 FAIL（沒有合法變更情境）。
     - Guard 4 live daemon（多層探測，#121 F1：system systemd → user systemd → 唯讀 RPC on-demand；全不可達才 SKIP）：pre 有 systemd unit 時 unit 轉 inactive／MainPID 變更 → FAIL（結構級，不受 warn 閥管；on-demand 無 unit 不比此項）；唯讀 RPC 快照比對任一 session 的 `last_tx_at` 前進／`bridge_generation` 變更／`state` 變更／session 消失 → FAIL（strict；warn 下降 WARN——開發者測試期間對真板操作情境）。
   - live path 公式一律用 XDG（刻意忽略 `SERIALWRAP_*` 隔離變數），與 daemon 實際行為一致；不沿用 `setup_cmd._state_path_for`。
4. **雙 runner 防線刻意冗餘**：conftest（pytest 下防未來漏網）＋8 檔 per-file `setattr` STATE_PATH/WAL_DIR（unittest 與單檔直跑防線）。
5. **subprocess 測試縱深防禦**：coexist／e2e env 補 `SERIALWRAP_CONFIG_DIR`；e2e 覆寫 `SERIALWRAP_WAL_DIR`；coexist setUp 改 `addCleanup`（`_wait_ready` 失敗不再洩漏 daemon）。

## Risks / Trade-offs

- [Guard strict 誤報：測試期間對真板操作／hotplug] → `SERIALWRAP_LIVE_GATE=warn` 逃生閥（降級範圍：Guard 1 非結構變更、Guard 2 size 縮小、Guard 4 tx/gen/state/session 消失）；結構級不受閥管仍 FAIL（Guard 1 結構破壞——含 #121 F2 升格的既有 binding 值改寫、Guard 2 消失、Guard 3 與 shell-wal 全部、Guard 4 inactive/PID）。寧誤報不放過（adversarial review F1 決議）。**warn 為明知風險的 opt-in：daemon TX/state 變更僅示警，開發者需自行確認。**
- [Guard 2 誤報：64MB WAL rotation 恰逢測試期間] → 極罕見，載明；逃生閥同上（warn 下縮小降 WARN）。
- [gate 先行、隔離未合 → CI 立即紅] → gate 與隔離同一 PR（真陽性，非 false-fail）。
- [conftest env 覆寫 shadow 掉 runtime-lazy 測試的 XDG monkeypatch] → 已實測發現 `test_setup_materialize.py` 2 例，補 `delenv`；實作時全 suite 驗證掃同型案例。
- [`unittest discover` 雜項維度仍不隔離] → CLAUDE.md 測試政策註記 pytest 為準。

## Migration Plan

單 PR 交付（gate＋隔離不可拆）；production 變更向後相容（default fallback 不變），無部署遷移。合入後本機 `git pull`＋重裝非必要（測試側為主）；驗收＝在本機（有 production daemon）跑完整 suite 四 guard 全綠。
