# Device Release / Handoff 設計（#54 detach 卡住）

- 日期：2026-06-15
- 對應 issue：[#54](https://github.com/hamanpaul/serialwrap/issues/54)（FAIL gate 將以 `Closes #54` 關閉）
- 上層目標：[#55](https://github.com/hamanpaul/serialwrap/issues/55)（serialwrap session 原生支援 MCU fw upgrade）—— 本設計為其第一步基礎，但本文件**不**實作 #55。
- 狀態：設計（brainstorming 產出），待 user 審閱 → openspec-propose → writing-plans。

## 1. 背景與問題

`serialwrapd` 只要某 session（console / passthrough）attach 著，就會在 `UARTBridge.start()`
（`sw_core/uart_io.py:174`）以 `os.open(device_path, O_RDWR|O_NOCTTY|O_NONBLOCK)` 開啟真實
UART 並由 `_loop()`（`uart_io.py:444`）持續 `os.read`。**沒有 `TIOCEXCL`、沒有 advisory lock**，
所以它是一個「持續讀取的第二 reader」。

當外部工具（如 `ocp-mcu-upgrade -d /dev/ttyUSB1`）要對同一 raw device 做 SBL 二進位協定
的獨佔燒錄時，daemon 與 flasher 形成「同一 tty 兩個 reader」競爭，MCU 回應被拆走，flasher
間歇 timeout（`Timed out waiting for data header`）。釋放裝置後同一份 fw 即可完整燒錄成功。

### 為何現況放不掉裝置（「detach 卡住」本體）

- 完整 `daemon stop`（process 結束）會由 kernel 回收 FD → 裝置確實釋放，但這會殺掉**所有**
  COM port / session，co-work 下代價過高。
- `session clear`（`sw_core/session_manager.py:516` `clear_session`）被設計成 detach 後**立即
  re-attach**：只要 `device_by_id` 還在 device 表（`by_id in self._devices`），就 `_spawn_attach`
  把 FD 搶回去（`session_manager.py:528-529`）。
- 目前唯一可行的 workaround 是把 session bind 到一個假 by-id（`__detached_for_flash__`），靠
  「假裝置不在 device 表」繞過 re-attach —— clumsy、易錯。

## 2. 目標 / 非目標

**目標**
- 提供一條 surgical、可恢復的「把單一 device 交給外部工具、燒完手動收回」路徑。
- daemon 持續運作、其他 COM port 不受影響。
- 釋放狀態「黏得住」：不被 `clear_session` re-attach、`bootstrap_attach`、device-watcher
  reconcile、recovery 的 force re-attach 搶回；且**跨 daemon 重啟**仍保持釋放。
- 避免製造反向故障：被忘記 / crash 後的 release 不可變成「裝置永久不可用」。

**非目標（本次不做）**
- #55 的原生 fw upgrade（serialwrap 內建 / 整合 flasher 協定）。
- 自動偵測還原（auto-resume / auto-reclaim）—— 明確拒絕，理由見 §7。
- TTL lease —— 本次不做（保留為未來 opt-in）。
- 對 serialwrap 自身 attach 加 `TIOCEXCL`（與本問題無關，且會擋外部工具）。

## 3. 關鍵決策（brainstorming 結論）

| 決策 | 選擇 | 理由 |
|------|------|------|
| Resume 模型 | **明確兩步**（`device release` → 外部燒錄 → `device attach`） | 最穩、競態最少，serialwrap 在被叫之前完全不動 |
| Release 期間 console | **clean slate**（全部拆掉，attach 時重建 primary console） | 最接近「裝置完整交出去」語意；避免 stale minicom |
| 黏住機制 | **Approach A**：session 上 first-class 持久化 `released` 旗標 | 意圖明確、集中、可測；最容易讓 #55 疊上去 |
| 防 release 卡住 | **Baseline + 唯讀 idle 標註**（無 TTL、無自動搶回） | 讓「被忘記的 release」可被看見、可安全收回，但不撞燒錄 race |

## 4. 狀態模型

`SessionRuntime`（`sw_core/session_manager.py:144`）新增：
- 新狀態字串 `"RELEASED"`（與 `DETACHED`/`ATTACHING`/`ATTACHED`/`READY` 並列）。
- 欄位：`released_by: str | None`、`released_at: str | None`、`released_reason: str | None`。

語意：`RELEASED` = serialwrap 已關閉 raw FD、清空 console（clean slate）、且**刻意不再自動
attach**，直到明確 `device attach`。狀態轉移：

```
READY/ATTACHED --device.release--> RELEASED
RELEASED       --device.attach--->  ATTACHING -> ATTACHED/READY（重建 primary console）
RELEASED       --(USB 重插/重啟/clear/recover)--> RELEASED（一律略過自動 attach）
```

## 5. 核心修正：單一 choke-point guard

所有自動 attach 路徑都匯流到 `_spawn_attach(by_id)`（`session_manager.py:653`）：
- `update_devices`（`session_manager.py:645`，USB realpath 變動 / 新增）
- `bootstrap_attach`（`session_manager.py:651`，daemon 重啟）
- `clear_session`（`session_manager.py:529`，clear 後 re-attach）
- recovery 的 `FORCE_CLEAR_REATTACH`（`session_manager.py:2151` 經 `clear_session`）

做法：
- 維護衍生集合 `self._released_by_ids: set[str]`（source of truth 仍是 session 欄位；此 set
  僅供熱路徑 O(1) 查詢）。
- 在 `_spawn_attach(by_id)` **最前面**加 guard：

  ```python
  with self._lock:
      if by_id in self._released_by_ids:
          return  # 已 released，任何自動 attach 一律略過
      if by_id in self._attach_inflight:
          return
      self._attach_inflight.add(by_id)
  ```

一處 guard 涵蓋全部 re-attach 路徑 —— 把目前 placeholder workaround 在 hack 的事正規化。

## 6. 持久化（跨 daemon 重啟）

現況 `_save_state` / `_load_state`（`session_manager.py:313` / `:294`）只存 `aliases` + `bindings`。
擴充加入 `released` map：

```json
"released": {
  "<session_id>": {"by_id": "...", "released_by": "...", "released_at": "...", "reason": "..."}
}
```

- `_save_state`：dump 時帶上 `released`（由現有 session 欄位彙整）。
- `_load_state`：把這些 by_id 灌回 `self._released_by_ids`，並還原對應 session 的
  `state=RELEASED` + provenance 欄位。**必須在 `bootstrap_attach`（`service.py:232`）之前完成**，
  確保重啟後燒錄中的裝置不被 bootstrap 搶回。

## 7. 唯讀 idle 標註（safety net）

- 新 helper `_probe_external_holder(real_path) -> {"pids": list[int], "holder": str|None}`：
  讀 `/proc/*/fd`（或 `lsof -t <path>`），**唯讀、不開 tty、不碰 I/O，對燒錄零干擾**。
- `self_test`（`session_manager.py:1925`）當 `state == RELEASED` 時附帶：
  - `released_by` / `released_at` / `reason`
  - `external_holder`（pid 清單或 `none`）
  - `reclaimable`（= 無外部持有者 → `true`）
  - `recommended_action`：有持有者 → `wait_external_flash`；無 → `device_attach`
- `to_public_dict`（`session_manager.py:206`）/ `session list` 補上 `released_by` / `released_at`，
  RELEASED 一眼可辨（不必跑 self-test）。

**為何只標註、不自動搶回**：`ocp-mcu-upgrade` 這類 flasher 在 erase→program 間會**多次
open/close** raw device。任何「短暫沒人持有」的空檔若自動 reclaim，就會在燒錄中途插進去弄壞
binary —— 正是當初否決「自動偵測還原」的 race。故 idle 偵測只用來標註，收回仍是明確動作。

## 8. CLI / RPC 介面

沿用 #54 命名，掛在既有 `device` subcommand（目前只有 `list`，`sw_core/cli.py:273`）：

```
serialwrap device release --selector COM0 [--source agent:x] [--reason "flash CC2674"]
serialwrap device attach  --selector COM0 [--force]
```

RPC 新增（`sw_core/service.py` rpc dispatch，緊接 `device.list`，`service.py:284`）：
- `device.release` `{selector, source?, reason?}` → clean-slate 關 FD、設 RELEASED、persist；
  **不** `_spawn_attach`。
- `device.attach` `{selector, force?}` → 安全 guard 通過後清 released、persist、`_spawn_attach`
  收回（重建 primary console）。

`source` 預設 `"cli"`；`reason` 選填（純 provenance）。

### clean-slate detach（release 路徑）

現有 `_detach_session_locked`（`session_manager.py:479`）走 `bridge.stop(preserve_consoles=True)`
並 stash 既有 console（為「detach 後還原」用，**不符 clean slate**）。新增參數：

```python
def _detach_session_locked(self, session, *, reason, drop_consoles=False):
    ...
    if session.bridge is not None:
        preserved = session.bridge.stop(preserve_consoles=not drop_consoles)
        session.bridge = None
    if drop_consoles:
        preserved = None
        session.retained_consoles = None   # 不 stash，人類 minicom 的 pts 被關
```

`device release` 以 `drop_consoles=True` 呼叫；`device attach` 走一般 attach → 重建乾淨 primary
console。

### attach 安全 guard

`device attach` 預設**安全**：先 `_probe_external_holder`，若外部仍持有 → 回 `DEVICE_STILL_HELD`
（附 pid），拒絕收回，避免又變回兩個 reader 搶 byte。`--force` 可明知故犯時略過。

## 9. 邊界與錯誤

| 情境 | 行為 |
|------|------|
| selector 不存在 | `SESSION_NOT_FOUND` |
| release 已 RELEASED / DETACHED 的 session | 冪等：回 `ok` + `already_released` |
| release 時有 foreground 命令 / 人類 interactive lease | clean slate **強拆**，但 response 回報 `aborted_cmd` / `closed_consoles=N`，保持透明 |
| attach 一個 by-id 不在 device 表的 session | `DEVICE_NOT_PRESENT` |
| attach 時外部仍持有且未 `--force` | `DEVICE_STILL_HELD`（附 pid 清單） |
| `device list` | 對 released 裝置標 `released=true` |

## 10. 測試策略

### Unit / 整合（`tests/`，沿用既有 pytest 風格）
1. release 後 `_spawn_attach(by_id)` 被略過（直接呼叫驗證 guard）。
2. release → `update_devices`（模擬 USB 重插）**不**會搶回。
3. release → 模擬 daemon 重啟（`_load_state`）→ `bootstrap_attach` 略過該 by_id。
4. release → `attach` → 回到 READY/ATTACHED、有新 primary console。
5. attach 安全 guard：external_holder 存在 → `DEVICE_STILL_HELD`；`--force` 可過。
6. `self_test` 在 RELEASED 下回 `reclaimable` / `recommended_action` 正確。
7. clean slate：release 後 `retained_consoles` 被清、原 console 不還原。
8. release 冪等、`SESSION_NOT_FOUND` / `DEVICE_NOT_PRESENT` 等錯誤碼。
9. 既有測試不得新壞（CLAUDE.md 標註的 pre-existing 失敗除外）。

### 對抗測試（adversarial，PRE-PR）
- release 期間連續 USB 拔插、daemon restart、並發 `clear` + `recover` + `attach`，確認 guard
  不被任一路徑繞過、`_released_by_ids` 與持久化不漂移。
- `_probe_external_holder` 對「flasher open/close 多次」的取樣不得導致誤收回（因為不自動收回，
  驗證標註在 holder 出現/消失間正確翻轉）。

### 實機測試（real-machine，PRE-PR HARD GATE）
真實 FTDI + CC2674：
1. serialwrap attach 該 FTDI（COM0）。
2. `serialwrap device release --selector COM0`。
3. 外部 `ocp-mcu-upgrade -d /dev/ttyUSBx -b 115200 -t 8 -e -s -i fw.bin` 燒錄成功
   （`Return error code : 0x0`）。
4. `serialwrap device attach --selector COM0` 收回，console / command 能力恢復。
- 需 user 確認硬體就緒、測試件可安全反覆燒錄、並給 go。**未通過不得上 PR。**

## 11. 風險與緩解

| 風險 | 緩解 |
|------|------|
| 反向故障：release 被忘記 → 裝置長期不可用 | RELEASED 大聲可見（provenance）+ 唯讀 idle 標註讓「可安全收回」自動浮現；一條 `device attach` 收回 |
| 自動收回撞燒錄 open/close race | 設計上**不**自動收回，只標註 |
| `_released_by_ids` 與 session 欄位不同步 | session 欄位為 source of truth，set 僅衍生；release/attach/load 三處同時更新並有測試覆蓋 |
| 持久化 released 在裝置已永久移除後殘留 | `update_devices` 的 `DEVICE_REMOVED` 與 idle 標註可辨識；attach 對不存在裝置回 `DEVICE_NOT_PRESENT` |
| clean slate 強拆人類 minicom 造成困惑 | response 透明回報拆除內容；屬刻意選擇（co-work 下燒錄前本就該清場）|

## 12. 與既有設計的關係

- 與 `suspend_interactive` / `resume_interactive`（`uart_io.py:598`）無關：那只掛起 human
  ownership、不關 FD，無法用於 raw handoff。
- 與 #51（passthrough 停在 ATTACHED）、#52（傳檔拖慢 console）、#53（孤兒 minicom）正交，
  本設計不處理那些，但 clean-slate release 對 #53 的孤兒 console 有附帶清理效果。
