# Design：daemon/device 身分穩定性 hardening（#100 + #101）

> 制定日期：2026-06-29 ｜ 對應 issue：#100、#101 ｜ 語言：繁體中文（repo 政策）

> **⚠️ 範圍更新（PR #104）**：本設計含 `session renumber`（§2.4），但實作後經 reviewer 審查判定強制重編 active session 是深水區（bridge callback 以值捕捉 `session_id`、flash/lease 狀態），已 **defer 至 follow-up #103**，本 PR 不含。§2.4 為歷史紀錄。

## 1. 背景與範圍

本設計合併兩個同源 issue（皆出自 2026-06-29 同一次實機 restart / two-reader 事件）：

- **#100**：`serialwrapd` restart 後，COM 名與實體板的對應被重新指派（對調）。
- **#101**：同機可同時跑多個 `serialwrapd`（不同 socket / 監管模式）造成 two-reader，daemon 互不偵測、靜默掉字。

兩者各為一個獨立 capability，合在一個 change 交付。

**明確不做（範圍外）**：

- #94（長跑 attach 失敗）：root cause 未明，需先 systematic-debugging，另案。
- #84（Windows/macOS platform port）：program 級架構工程，最低優先，另案。
- #101 **不**自動 refuse / kill / 退讓（純偵測+回報）。
- #100 **不**做 sticky 持久化（確定性來自排序本身）。

## 2. Capability A — #100 COM 編號確定性綁定 by-id

### 2.1 根因（已驗證，Codex 對抗審查 CONFIRMED）

COM 編號**不是**在裝置列舉時決定的——`device_watcher._scan()` 已 `sorted(os.listdir())`（`sw_core/device_watcher.py:49`）、`poll_once` 的 added_keys 亦 sorted。真正的分配發生在**每個裝置各自的並發 attach thread 內**：

```
_spawn_attach(by_id)          # session_manager.py:1416，per-device daemon thread
  → _attach_by_id_dynamic     # :1771（thread 內）
    → _session_from_template   # :1821 → :520
      → _next_dynamic_com()    # :511-518，取「目前最低空 COM 號」
```

startup 多裝置並發 attach 時，**誰先搶到 `self._lock` 誰拿 COM0** → 分配順序 = thread 完成順序（受 probe/IO timing 影響）→ restart 後對調。實機 `AC01QZT0`/`AQ00OAQ7` 對調即此。

### 2.2 現行基準（測試期望錨點）

| COM | by-id | real_path |
|-----|-------|-----------|
| COM0 | `usb-FTDI_FT232R_USB_UART_AC01QZT0-if00-port0` | /dev/ttyUSB0 |
| COM1 | `usb-FTDI_FT232R_USB_UART_AQ00OAQ7-if00-port0` | /dev/ttyUSB1 |

by-id 字典序 `AC01…` < `AQ00…`（`C` < `Q`）→ sorted 規則得 **COM0=AC01QZT0、COM1=AQ00OAQ7**，與現行一致。故「sorted by-id」即「以現行 COM0/COM1 為主」。

### 2.3 修法：三條腿

**腿 1 — startup 確定性 rank**
在 `self._lock` 內、**spawn attach threads 之前**，對「當下在線的 dynamic 裝置集合」依 `device_key` 排序，一次配好 COM rank（`COM0, COM1, …`），再 spawn 並發 attach。兩條 startup 入口都要涵蓋：

- `SerialwrapService.start()` → `self._sessions.update_devices(...)`（`sw_core/service.py:464`）
- `SerialwrapService.start()` → `self._sessions.bootstrap_attach()`（`sw_core/service.py:465`）

`device_key` 排序鍵 = by-id 路徑字串；同款晶片 by-id 衝突時 fallback by-path（沿用 `DeviceWatcher` 既有「by-id 優先、同 real_path 去重」語意）。

**腿 2 — rank 作用域（避免污染既有持久化狀態）**
rank 只管 **dynamic 自動偵測 session**。下列為權威、排除在 rank pool 外：

- explicit YAML `targets` 指定的 COM。
- `session.bind` / `_binding_overrides` 指定的綁定。
- `#95` profile_pins（pin 的是 profile，不影響 COM rank，但 RELEASED by_id 不自動 attach、不進 pool）。

優先順序：explicit/bound COM > dynamic rank。

**腿 3 — runtime hotplug = (a)（沿用現有，使用者拍板）**
不同 by-id 的板**繼承空出的 DETACHED/RELEASED 槽**——`_attach_by_id()` 既有的 DETACHED-rebind（`sw_core/session_manager.py:1573-1583`，依 `act_no` 排序把舊 DETACHED session 重綁到新 by-id）**不改**。同 by-id 重接總是拿回自己原槽。active session 的 COM 名在 daemon 存活期間不變。

### 2.4 新增 `session renumber`（on-demand 重排，無條件強制）

使用者要的「renew」目前不存在（已查遍 CLI 子命令與 RPC 方法，最接近的 `session clear` 是單一 session 且走 race 路徑，非確定性重排）。新增：

- **語意**：把所有 **dynamic** session 的 COM 依 sorted by-id 重排，**不檢查 busy、無條件強制執行**（使用者明確選擇）。
- **連帶實作（最高風險點，plan 階段須原子處理）**：重編改 `session_id`（`profile:COM`），須在單一 lock 區間內同步 remap：
  - `_sessions` dict 的 key（sid → 新 sid）
  - `_aliases`（alias → sid 對應）
  - `_binding_overrides`（sid → by_id）
  - `CommandArbiter` 每 session worker thread 的 sid 對應
  - in-flight 命令 / `cmd_id` 的 session 參照
  - console reader / interactive lease 的 session 參照
  - `state.json` 持久化
- **同步面**：RPC `session.renumber`（`sw_core/service.py` 平面 dispatcher 加分支）+ CLI `session renumber`（`sw_core/cli.py` subparser）+ README/help/spec。

## 3. Capability B — #101 two-reader 純偵測+回報

### 3.1 根因（已驗證 CONFIRMED）

`SingletonLock`（`sw_core/daemon_lock.py:9-53`）是 per-`(lock_path, socket_path)` 的 `flock` + socket liveness 探測。不同 socket / 不同 RUN_DIR / systemd-system vs systemd-user 用不同 lock_path/socket → singleton lock 完全擋不到第二個 daemon。

### 3.2 修法：純被動、on-demand（不自動動手、無週期掃描）

**獨立偵測 helper（module-level，非 SessionManager method）**
新增一個獨立函式（不複用 `_probe_external_holder` 的 instance method 形態，因它只回 pid、且為 SessionManager 綁定）：

- 掃 `/proc/*/cmdline` 找其他 `serialwrapd` 程序（不限同 socket / 同監管模式）。
- best-effort 讀 `/proc/<pid>/fd` 找哪個程序持有目標 tty。
- 回**結構化結果**，並在跨 uid 讀不到 fd symlink 時**明確降級**為 `permission` / `unknown` 狀態（此時只能確認「另有 serialwrapd 存在」，無法判定持有哪條 tty）。此降級資訊本身即為輸出契約的一部分，不可靜默。

**兩個 surface（issue 明列）**

- `serialwrap doctor`：新增 daemon-less 的 `_check_single_daemon`（`sw_core/doctor_cmd.py`，回 `{check, ok, detail, fix}`，與既有 `_check_supervision_mode` 同形）。doctor 為獨立程序、不碰 socket，掃 /proc 找 serialwrapd 可行。
- `serialwrap daemon status`：回應加 `multi_open` / `foreign_holders` 欄位。因 `health.status` 在同步 RPC dispatcher 內直接呼叫（`sw_core/service.py:524-527`，會凍結單執行緒 asyncio event loop），偵測掃描須走 **executor offload** 或快取，不可在 event loop 同步掃全 /proc。

## 4. 測試策略（實機驗證為驗收權威）

> 使用者要求：test case 一律以**實機驗證**收尾；plug/unplug 用 WSL `usbipd`（`usbipd.exe attach -w -b 8-1|8-2` / `usbipd.exe detach -b 8-1|8-2`）。

### 4.1 自動化測試（pytest，TDD RED first；CI 與政策門檻）

- **A-rank**：模擬亂序 / 並發 attach 一組裝置 → 斷言 COM 依 sorted `device_key`（含 by-path fallback）；斷言與 attach 完成順序無關。
- **A-renumber**：建立亂序 COM 後呼叫 `session.renumber` → 斷言 COM 重排到 sorted 序；斷言 session_id / alias / binding remap 一致、in-flight 參照不破。
- **A-hotplug(a)**：DETACHED 空槽 + 不同 by-id 插入 → 斷言新板繼承空槽（維持現有行為）。
- **B-detect**：fake `/proc` 佈兩個 serialwrapd / 一個外部 tty holder → 斷言 doctor `_check_single_daemon` 與 status 欄位；單 daemon 時 `ok=True`；無 fd 權限時降級欄位正確。
- 既有 flaky 排除（自跑門禁用正確檔名 `--ignore=tests/test_human_agent_coexist.py` 等 PTY-heavy 群）。

### 4.2 實機驗證協定（throwaway daemon，不動 prod COM0/COM1）

沿用 repo 既有實機驗證手法（獨立 `SERIALWRAP_RUN_DIR`/`_STATE_DIR` 的 throwaway daemon 跑 worktree 新碼）：

1. **restart-rank 確定性**：throwaway daemon 起 → 斷言 COM0=AC01QZT0、COM1=AQ00OAQ7；重啟數次皆同。
2. **usbipd 亂序 attach**：先 `detach` 兩板，再以**相反順序** attach（`usbipd.exe attach -w -b 8-2` 先、`8-1` 後）→ 斷言 COM 仍依 by-id 排序、不隨 attach 順序變。
3. **renumber**：人為製造亂序後 `serialwrap session renumber` → 斷言 snap 回 sorted。
4. **hotplug(a)**：`detach 8-1`（COM0→DETACHED）→ 斷言；再 `attach 8-1` 同板 → 拿回 COM0。
5. **#101 偵測**：刻意起第二個 serialwrapd（不同 socket）→ 斷言 `doctor` 與 `daemon status` 報出 multi_open / holder。

## 5. docs / 政策同步矩陣

| 面 | 動作 |
|----|------|
| `CHANGELOG.md` | `[Unreleased]` 記 #100 / #101 |
| `README.md` | doctor / daemon status JSON 契約段 + `session renumber` 用法 |
| `docs/serialwrap-spec.md` | 對齊 RPC / CLI 契約（R-18） |
| CLI help | `session renumber` help 字串 |
| tests | restart-rank、renumber、#101 偵測、doctor/status |
| `VERSION` | 非 release chore，不動（或上 `policy-exempt:version`） |

## 6. 交付物路徑

- 本 design：`docs/superpowers/specs/2026-06-29-daemon-device-identity-hardening-design.md`
- openspec change：`openspec/changes/daemon-device-identity-hardening/`（proposal → design/specs → tasks）
- 實作計畫：`docs/superpowers/plans/2026-06-29-daemon-device-identity-hardening.md`
