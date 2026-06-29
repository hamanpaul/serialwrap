# daemon/device 身分穩定性 hardening Implementation Plan

> **⚠️ 範圍更新（2026-06-29，PR #104）**：本計畫於 writing-plans 階段寫成、含 `session renumber`（Tasks 3 系列：Task 5/6/7 的 `renumber_dynamic`/RPC/CLI）。實作後經兩位 reviewer（superpowers + codex 對抗）審查，判定強制重編 active session 會牽動 attach 時以值捕捉 `session_id` 的 bridge callback、flash state、lease reverse-link，須改以「拆 bridge → 改號 → 重 attach」另案重做，故 **`session renumber` 已自本 PR 移除、defer 至 follow-up #103**。下方 renumber 相關 task 為歷史紀錄，**本 PR 未實作**；實際交付以 openspec `tasks.md` 與 `design.md` D5 為準。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 COM↔裝置 by-id 對應在 restart/亂序 attach 下確定性穩定、提供 `session renumber` on-demand 重排，並讓 daemon 被動偵測同機多開（two-reader）暴露到 doctor / daemon status。

**Architecture:** #100 把 COM 編號從「並發 attach thread 內取最低空號」改為「startup 在 lock 內依 `device_key`(by-id, by-path fallback) 排序一次配號」；`session renumber` 由 **Service 層編排**（SessionManager 算+套 session/alias/binding/state remap 回傳 `old_sid→new_sid`，Service 再 remap 它持有的 arbiter worker）。#101 新增 module-level on-demand /proc 偵測 helper，doctor 直接用、daemon status 走 executor offload。

**Tech Stack:** Python 3.10+、stdlib（os/glob/`/proc`）、pytest + unittest、既有 `SessionManager`/`SerialwrapService`/`CommandArbiter`/`AliasRegistry`/`DeviceWatcher`/`doctor_cmd`。

**語言：** 所有 code comment / docstring / commit / 文件繁體中文（repo 政策）。

**既有 flaky 排除（自跑測試門）：**
```
--ignore=tests/test_human_agent_coexist.py --ignore=tests/test_multiagent_e2e.py \
--ignore=tests/test_multiagent_stress.py --ignore=tests/test_flash_pump.py \
--ignore=tests/test_flash_service_wiring.py --ignore=tests/test_agent_defer_tx.py
```

**現行實機基準（測試期望錨點）：** COM0=`usb-FTDI_FT232R_USB_UART_AC01QZT0-if00-port0`(/dev/ttyUSB0)、COM1=`usb-FTDI_FT232R_USB_UART_AQ00OAQ7-if00-port0`(/dev/ttyUSB1)。by-id 字典序 `AC01…`<`AQ00…`，故 sorted rank 即還原現行對應。

---

## 檔案結構

| 檔案 | 責任 | 動作 |
|------|------|------|
| `sw_core/session_manager.py` | `device_key` 排序鍵、startup 批次 rank 預配、`renumber_dynamic()`、rank 作用域 | Modify |
| `sw_core/service.py` | startup 兩入口收斂、`session.renumber` RPC 編排（含 arbiter remap）、`daemon status` 多開欄位 | Modify |
| `sw_core/cli.py` | `session renumber` subparser + dispatch | Modify |
| `sw_core/multi_open.py` | **新增** module-level /proc 多開偵測 helper | Create |
| `sw_core/doctor_cmd.py` | `_check_single_daemon` 接進 `run_doctor` | Modify |
| `tests/test_com_rank.py` | startup rank / 作用域 / hotplug(a) | Create |
| `tests/test_session_renumber.py` | renumber 重排 + remap 一致性 | Create |
| `tests/test_multi_open_detect.py` | 偵測 helper + doctor/status surface | Create |
| `CHANGELOG.md` / `README.md` / `docs/serialwrap-spec.md` | 對外契約對齊 | Modify |

---

## Task 1: `device_key` 排序鍵 helper

**Files:**
- Modify: `sw_core/session_manager.py`（新增 module-level 函式，靠近 imports 後）
- Test: `tests/test_com_rank.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_com_rank.py
from sw_core.session_manager import device_sort_key

def test_device_sort_key_by_id_lexicographic():
    a = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AC01QZT0-if00-port0"
    b = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AQ00OAQ7-if00-port0"
    assert device_sort_key(a, None) < device_sort_key(b, None)

def test_device_sort_key_falls_back_to_by_path_on_collision():
    # 同 by-id（同款晶片）時，用 by-path 區分
    same_by_id = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
    k1 = device_sort_key(same_by_id, "/dev/serial/by-path/pci-0000:00:14.0-usb-0:8.1:1.0-port0")
    k2 = device_sort_key(same_by_id, "/dev/serial/by-path/pci-0000:00:14.0-usb-0:8.2:1.0-port0")
    assert k1 != k2 and k1 < k2
```

- [ ] **Step 2: 跑驗證 RED**

Run: `python3 -m pytest tests/test_com_rank.py -q`
Expected: FAIL（`ImportError: cannot import name 'device_sort_key'`）

- [ ] **Step 3: 實作**

```python
# sw_core/session_manager.py（module-level）
def device_sort_key(by_id: str, by_path: str | None) -> tuple[str, str]:
    """COM rank 排序鍵：以 by-id 路徑字串為主、by-path 為輔（同款晶片 by-id 衝突時區分）。
    回傳 tuple 確保穩定且可比較。"""
    return (by_id or "", by_path or "")
```

- [ ] **Step 4: 跑驗證 GREEN**

Run: `python3 -m pytest tests/test_com_rank.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/session_manager.py tests/test_com_rank.py
git commit -m "feat(session): device_sort_key 排序鍵（by-id 優先 by-path fallback）#100"
```

---

## Task 2: startup 批次 rank 預配（消除並發 race）

**Files:**
- Modify: `sw_core/session_manager.py`（`_next_dynamic_com` 區附近新增 `assign_dynamic_coms_in_order`；`update_devices` / `bootstrap_attach` 入口）
- Test: `tests/test_com_rank.py`

**背景：** 現行 `_next_dynamic_com()`（`session_manager.py:511-518`）在 attach thread 內取「最低空號」，並發完成順序決定 COM0。改為：在 lock 內、spawn attach 前，對「整批要 attach 的 dynamic by-id」依 `device_sort_key` 排序，預先在 `_pending_com` map 配好號；attach 時若該 by-id 已預配則用預配號。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_com_rank.py（續）
def test_startup_assigns_com_by_sorted_order_regardless_of_attach_order(make_manager):
    # make_manager fixture：建立帶 prpl-template、兩個 fake by-id 的 SessionManager（見 conftest）
    mgr = make_manager()
    a = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AC01QZT0-if00-port0"
    b = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AQ00OAQ7-if00-port0"
    # 故意以「反序」呈現裝置（b 先 a 後）
    mgr.prepare_dynamic_rank([b, a])     # 新 API：批次預配
    assert mgr.com_for_by_id(a) == "COM0"
    assert mgr.com_for_by_id(b) == "COM1"
```

> conftest fixture `make_manager`：複用 `tests/test_session_bind.py` 既有建構方式（無 device IO，僅 template + 假 device map）。實作 Task 時對齊既有 helper 命名；若不存在則於 `tests/conftest.py` 新增最小 fixture。

- [ ] **Step 2: 跑驗證 RED**

Run: `python3 -m pytest tests/test_com_rank.py -k startup -q`
Expected: FAIL（`AttributeError: prepare_dynamic_rank`）

- [ ] **Step 3: 實作預配**

```python
# sw_core/session_manager.py
# ctor 內新增： self._pending_com: dict[str, str] = {}   # by_id -> 預配 COM

def prepare_dynamic_rank(self, by_ids: list[str]) -> None:
    """startup：對整批在線 dynamic by-id 依 device_sort_key 排序，預配 COM 號（lock 內）。
    explicit/bound/RELEASED 的 by-id 不進預配（rank 作用域）。"""
    with self._lock:
        used = {s.profile.com for s in self._sessions.values()}
        # 排序：用 watcher 提供的 (by_id, by_path)；by_path 經 _devices 取得
        ordered = sorted(
            (b for b in by_ids
             if b not in self._released_by_ids
             and not self._is_explicit_or_bound(b)),
            key=lambda b: device_sort_key(b, self._by_path_for(b)),
        )
        idx = 0
        for b in ordered:
            while f"COM{idx}" in used:
                idx += 1
            self._pending_com[b] = f"COM{idx}"
            used.add(f"COM{idx}")
            idx += 1

def com_for_by_id(self, by_id: str) -> str | None:
    with self._lock:
        for s in self._sessions.values():
            if s.profile.device_by_id == by_id:
                return s.profile.com
        return self._pending_com.get(by_id)
```

並修改 `_session_from_template`（`:520`）：建立 dynamic session 時，**優先用 `self._pending_com.pop(device_by_id, None)`，無則 fallback `_next_dynamic_com()`**：

```python
com = self._pending_com.pop(device_by_id, None) or self._next_dynamic_com()
```

新增輔助（rank 作用域）：

```python
def _is_explicit_or_bound(self, by_id: str) -> bool:
    """explicit YAML target 或 binding override 綁定的裝置不進 dynamic rank pool。"""
    if by_id in self._binding_overrides.values():
        return True
    return any(t.device_by_id == by_id for t in getattr(self, "_explicit_targets", []))

def _by_path_for(self, by_id: str) -> str | None:
    dev = self._devices.get(by_id)
    return getattr(dev, "by_path", None) if dev else None
```

> 註：`_devices` 內容為 `DeviceInfo(by_id, real_path)`，目前無 by_path 欄位。若 by-path fallback 需要，Task 2 同步在 `DeviceWatcher._scan` 的 `DeviceInfo` 補 `by_path`（由 scan_dir 判定來源）。若本批裝置 by-id 皆唯一（FTDI 情境），`_by_path_for` 回 None 不影響排序。

- [ ] **Step 4: 跑驗證 GREEN**

Run: `python3 -m pytest tests/test_com_rank.py -k startup -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/session_manager.py tests/test_com_rank.py tests/conftest.py
git commit -m "feat(session): startup 依 by-id 排序批次預配 COM，消除並發 attach race #100"
```

---

## Task 3: 收斂兩條 startup 入口走預配

**Files:**
- Modify: `sw_core/service.py`（`start()`:457-466）

- [ ] **Step 1: 寫失敗測試（整合層）**

```python
# tests/test_com_rank.py（續）— 以 service 啟動兩裝置（fake watcher）驗證最終 session.list COM 對應
def test_service_start_orders_coms_by_by_id(make_service_two_fake_ftdi):
    svc = make_service_two_fake_ftdi(order=["AQ00OAQ7", "AC01QZT0"])  # 反序呈現
    svc.start()
    sessions = {s["device_by_id"]: s["com"] for s in svc._sessions.list_sessions()}
    assert sessions[".../AC01QZT0-if00-port0"] == "COM0"
    assert sessions[".../AQ00OAQ7-if00-port0"] == "COM1"
```

> fixture：以既有 PTY 假 target 或純 fake DeviceWatcher 建 service。若整合 fixture 成本高，可標 `@pytest.mark.realhw` 並改由 Task 6 實機涵蓋；單元層以 Task 2 的 SessionManager 級測試為主。

- [ ] **Step 2: 跑驗證 RED**

Run: `python3 -m pytest tests/test_com_rank.py -k service_start -q`
Expected: FAIL（COM 對調或 race）

- [ ] **Step 3: 在 start() 注入預配**

```python
# sw_core/service.py start()
self._watcher.poll_once()
present = list(self._watcher.devices.keys())   # by_id 列表
self._sessions.prepare_dynamic_rank(present)   # ← 新增：spawn attach 前先排序配號
self._sessions.update_devices(self._watcher.devices)
self._sessions.bootstrap_attach()
```

- [ ] **Step 4: 跑驗證 GREEN**

Run: `python3 -m pytest tests/test_com_rank.py -q`
Expected: PASS（含 1.x/2.x）

- [ ] **Step 5: Commit**

```bash
git add sw_core/service.py tests/test_com_rank.py
git commit -m "feat(service): start() 於 attach 前批次預配 COM rank，兩入口收斂 #100"
```

---

## Task 4: rank 作用域 + hotplug(a) 回歸測試

**Files:**
- Test: `tests/test_com_rank.py`
- Modify（若需）：`sw_core/session_manager.py`

- [ ] **Step 1: 寫測試**

```python
def test_explicit_target_com_not_overwritten_by_rank(make_manager_with_explicit_target):
    mgr = make_manager_with_explicit_target(com="COM5", by_id=".../AC01QZT0-if00-port0")
    mgr.prepare_dynamic_rank([".../AC01QZT0-if00-port0", ".../AQ00OAQ7-if00-port0"])
    # explicit COM5 不被 rank 覆寫；另一片走 dynamic rank
    assert mgr.com_for_by_id(".../AC01QZT0-if00-port0") == "COM5"

def test_hotplug_different_by_id_inherits_detached_slot(make_manager):
    mgr = make_manager()
    # 既有 COM0 的 session 轉 DETACHED，插入不同 by-id → 沿用既有 _attach_by_id rebind
    ...  # 斷言新 by-id 取得原 COM0（維持現有行為，非預配新號）
```

- [ ] **Step 2: 跑驗證**（RED→實作→GREEN，視 `_is_explicit_or_bound` 是否已涵蓋）

Run: `python3 -m pytest tests/test_com_rank.py -q`

- [ ] **Step 3: Commit**

```bash
git add tests/test_com_rank.py sw_core/session_manager.py
git commit -m "test(session): rank 作用域 + hotplug(a) 槽繼承回歸 #100"
```

---

## Task 5: `renumber_dynamic()`（SessionManager 端 remap）

**Files:**
- Modify: `sw_core/session_manager.py`
- Test: `tests/test_session_renumber.py`

**語意：** 依 sorted by-id 重算所有 dynamic session 的 COM；對 COM 有變的 session，在單一 lock 區間原子 remap：`_sessions` key（sid=`profile:COM`）、`profile.com`/`session_id`、`_aliases`（`set_for_session`）、`_binding_overrides` key、`state.json`。回傳 `dict[old_sid, new_sid]` 供 Service 層 remap arbiter。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_session_renumber.py
def test_renumber_snaps_to_sorted_and_remaps_alias_binding(make_manager_two_dynamic):
    mgr = make_manager_two_dynamic(coms={"COM0": ".../AQ00OAQ7-if00-port0",
                                          "COM1": ".../AC01QZT0-if00-port0"})  # 亂序
    mapping = mgr.renumber_dynamic()
    # AC01 應排到 COM0
    assert mgr.com_for_by_id(".../AC01QZT0-if00-port0") == "COM0"
    assert mgr.com_for_by_id(".../AQ00OAQ7-if00-port0") == "COM1"
    # session_id remap 回傳完整
    assert mapping["prpl-template:COM1"] == "prpl-template:COM0"
    # alias / binding 對應到新 sid
    assert mgr._aliases.for_session("prpl-template:COM0") is not None
```

- [ ] **Step 2: 跑驗證 RED**

Run: `python3 -m pytest tests/test_session_renumber.py -q`
Expected: FAIL（`AttributeError: renumber_dynamic`）

- [ ] **Step 3: 實作**

```python
# sw_core/session_manager.py
def renumber_dynamic(self) -> dict[str, str]:
    """依 sorted by-id 重排所有 dynamic session 的 COM。無條件強制（含 active）。
    回傳 {old_session_id: new_session_id}；Service 層據此 remap arbiter worker。"""
    with self._lock:
        dynamic = [s for s in self._sessions.values()
                   if not self._is_explicit_or_bound(s.profile.device_by_id)
                   and s.profile.device_by_id]
        ordered = sorted(dynamic,
                         key=lambda s: device_sort_key(s.profile.device_by_id,
                                                        self._by_path_for(s.profile.device_by_id)))
        # 先算目標 COM（避開 explicit/bound 佔用的號）
        reserved = {s.profile.com for s in self._sessions.values()
                    if self._is_explicit_or_bound(s.profile.device_by_id)}
        targets: dict[str, str] = {}
        idx = 0
        for s in ordered:
            while f"COM{idx}" in reserved:
                idx += 1
            targets[s.session_id] = f"COM{idx}"
            idx += 1
        mapping: dict[str, str] = {}
        # 兩階段：先全部移出（避免 key 撞），再以新 sid 寫回
        moved = []
        for s in ordered:
            old_sid = s.session_id
            new_com = targets[old_sid]
            if new_com == s.profile.com:
                continue
            self._sessions.pop(old_sid, None)
            new_profile = dataclasses.replace(s.profile, com=new_com)
            new_sid = f"{new_profile.profile_name}:{new_com}"
            s.profile = new_profile
            s.session_id = new_sid
            moved.append((old_sid, new_sid, s))
            mapping[old_sid] = new_sid
        for old_sid, new_sid, s in moved:
            self._sessions[new_sid] = s
            # alias remap
            alias = self._aliases.for_session(old_sid)
            if alias:
                self._aliases.set_for_session(new_sid, alias)
            # binding override remap
            if old_sid in self._binding_overrides:
                self._binding_overrides[new_sid] = self._binding_overrides.pop(old_sid)
        if mapping:
            self._save_state()
        return mapping
```

> 實作註：確認 `AliasRegistry` 有 `for_session(sid)` 讀法；若無，於 `alias_registry.py` 補最小讀取（回 alias 或 None）。`SessionRuntime.session_id` 需為可寫欄位（mutable dataclass，已是）。

- [ ] **Step 4: 跑驗證 GREEN**

Run: `python3 -m pytest tests/test_session_renumber.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/session_manager.py sw_core/alias_registry.py tests/test_session_renumber.py
git commit -m "feat(session): renumber_dynamic 依 by-id 重排並原子 remap alias/binding/state #100"
```

---

## Task 6: `session.renumber` RPC（Service 編排 + arbiter remap）

**Files:**
- Modify: `sw_core/service.py`（`rpc()` 平面 dispatcher 加分支）
- Test: `tests/test_session_renumber.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_service_renumber_remaps_arbiter_workers(make_service_two_dynamic):
    svc = make_service_two_dynamic(coms={"COM0": "...AQ00OAQ7...", "COM1": "...AC01QZT0..."})
    res = svc.rpc("session.renumber", {})
    assert res["ok"] is True
    # 舊 sid 的 arbiter worker 已 unregister、新 sid 已 register
    assert "prpl-template:COM0" in svc._arbiter._queues       # 新（AC01→COM0）
    assert svc._arbiter._queues.keys() == {"prpl-template:COM0", "prpl-template:COM1"}
```

- [ ] **Step 2: 跑驗證 RED**

Run: `python3 -m pytest tests/test_session_renumber.py -k arbiter -q`
Expected: FAIL（unknown method 或 arbiter 未 remap）

- [ ] **Step 3: 實作 RPC 分支**

```python
# sw_core/service.py rpc()
if method == "session.renumber":
    mapping = self._sessions.renumber_dynamic()
    # arbiter worker 以 session_id 索引、由 Service 持有 → 在此 remap
    for old_sid, new_sid in mapping.items():
        self._arbiter.unregister_session(old_sid)   # 強制：丟棄該 session in-flight 命令
        self._arbiter.register_session(new_sid)
    return {"ok": True, "renumbered": mapping}
```

- [ ] **Step 4: 跑驗證 GREEN**

Run: `python3 -m pytest tests/test_session_renumber.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/service.py tests/test_session_renumber.py
git commit -m "feat(service): session.renumber RPC 編排 SM remap + arbiter worker re-register #100"
```

---

## Task 7: CLI `session renumber`

**Files:**
- Modify: `sw_core/cli.py`（`sess_sub` 區，約 :473-535；dispatch 區）

- [ ] **Step 1: 寫測試**

```python
# tests/test_session_renumber.py
def test_cli_session_renumber_parses_and_calls(monkeypatch, capsys):
    # 對齊既有 CLI 測試風格：parse args → 呼叫 client.rpc('session.renumber')
    ...
```

- [ ] **Step 2: 跑驗證 RED**

Run: `python3 -m pytest tests/test_session_renumber.py -k cli -q`

- [ ] **Step 3: 實作 subparser + dispatch**

```python
# sw_core/cli.py（sess_sub 區）
sess_sub.add_parser("renumber", help="依 by-id 排序重排所有 dynamic session 的 COM（強制，含 active）")
# dispatch 區（對齊既有 session_cmd 分派）
elif args.session_cmd == "renumber":
    _emit(client.rpc("session.renumber", {}))
```

- [ ] **Step 4: 跑驗證 GREEN**

Run: `python3 -m pytest tests/test_session_renumber.py -q`

- [ ] **Step 5: Commit**

```bash
git add sw_core/cli.py tests/test_session_renumber.py
git commit -m "feat(cli): session renumber 子命令 #100"
```

---

## Task 8: 多開偵測 helper（module-level）

**Files:**
- Create: `sw_core/multi_open.py`
- Test: `tests/test_multi_open_detect.py`

- [ ] **Step 1: 寫失敗測試（fake /proc）**

```python
# tests/test_multi_open_detect.py
from sw_core.multi_open import detect_multi_open

def test_detect_two_serialwrapd(tmp_path):
    proc = tmp_path / "proc"
    _make_fake_proc(proc, {
        "2114": {"cmdline": "python\0serialwrapd.py\0--socket\0/run/serialwrap/serialwrapd.sock\0",
                 "fd": {"5": "/dev/ttyUSB0"}},
        "9001": {"cmdline": "python\0serialwrapd.py\0", "fd": {}},
    })
    res = detect_multi_open(proc_root=str(proc), tty_paths=["/dev/ttyUSB0"])
    assert res["multi_open"] is True
    assert {h["pid"] for h in res["daemons"]} == {2114, 9001}
    assert res["holders"]["/dev/ttyUSB0"] == 2114

def test_single_daemon_ok(tmp_path):
    proc = tmp_path / "proc"
    _make_fake_proc(proc, {"2114": {"cmdline": "python\0serialwrapd.py\0", "fd": {}}})
    res = detect_multi_open(proc_root=str(proc), tty_paths=[])
    assert res["multi_open"] is False

def test_permission_denied_degrades(tmp_path, monkeypatch):
    # 模擬讀 fd 失敗 → status 降級為 'permission'，仍標 multi_open
    ...
    assert res["holders_status"] == "permission"
```

- [ ] **Step 2: 跑驗證 RED**

Run: `python3 -m pytest tests/test_multi_open_detect.py -q`
Expected: FAIL（module 不存在）

- [ ] **Step 3: 實作 helper**

```python
# sw_core/multi_open.py
"""同機多開（two-reader）被動偵測：掃 /proc 找其他 serialwrapd 與 tty 持有者。
純偵測+回報，不終止任何 daemon。on-demand，無背景掃描。"""
from __future__ import annotations
import os

def _iter_pids(proc_root: str):
    for name in os.listdir(proc_root):
        if name.isdigit():
            yield int(name)

def _is_serialwrapd(proc_root: str, pid: int) -> bool:
    try:
        with open(f"{proc_root}/{pid}/cmdline", "rb") as fh:
            parts = fh.read().split(b"\0")
        return any(b"serialwrapd" in p for p in parts)
    except OSError:
        return False

def detect_multi_open(proc_root: str = "/proc", tty_paths: list[str] | None = None) -> dict:
    """回傳 {multi_open, daemons:[{pid}], holders:{tty:pid}, holders_status}。
    holders_status: 'ok' | 'permission'（無權限讀 fd） | 'unknown'。"""
    tty_paths = tty_paths or []
    daemons = []
    try:
        pids = list(_iter_pids(proc_root))
    except OSError:
        return {"multi_open": False, "daemons": [], "holders": {}, "holders_status": "unknown"}
    for pid in pids:
        if _is_serialwrapd(proc_root, pid):
            daemons.append({"pid": pid})
    holders: dict[str, int] = {}
    status = "ok"
    for pid_info in daemons:
        pid = pid_info["pid"]
        fd_dir = f"{proc_root}/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                if target in tty_paths:
                    holders[target] = pid
        except PermissionError:
            status = "permission"
        except OSError:
            if status == "ok":
                status = "unknown"
    return {
        "multi_open": len(daemons) > 1,
        "daemons": sorted(daemons, key=lambda d: d["pid"]),
        "holders": holders,
        "holders_status": status,
    }
```

- [ ] **Step 4: 跑驗證 GREEN**

Run: `python3 -m pytest tests/test_multi_open_detect.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/multi_open.py tests/test_multi_open_detect.py
git commit -m "feat(multi_open): 同機多開被動偵測 helper（/proc 掃描 + 降級回報）#101"
```

---

## Task 9: doctor `_check_single_daemon`

**Files:**
- Modify: `sw_core/doctor_cmd.py`
- Test: `tests/test_multi_open_detect.py`

- [ ] **Step 1: 寫測試**

```python
def test_doctor_single_daemon_check(tmp_path):
    from sw_core.doctor_cmd import _check_single_daemon
    proc = tmp_path / "proc"
    _make_fake_proc(proc, {"1": {"cmdline":"serialwrapd.py\0","fd":{}}, "2": {"cmdline":"serialwrapd.py\0","fd":{}}})
    r = _check_single_daemon(proc_root=str(proc))
    assert r["check"] == "single_daemon" and r["ok"] is False and r["detail"]
```

- [ ] **Step 2: 跑驗證 RED** → `python3 -m pytest tests/test_multi_open_detect.py -k doctor -q`

- [ ] **Step 3: 實作**

```python
# sw_core/doctor_cmd.py
from .multi_open import detect_multi_open

def _check_single_daemon(proc_root: str = "/proc") -> dict:
    res = detect_multi_open(proc_root=proc_root)
    n = len(res["daemons"])
    ok = not res["multi_open"]
    detail = f"{n} 個 serialwrapd 在跑" + ("（偵測到多開）" if res["multi_open"] else "")
    fix = "停掉多餘 daemon（serialwrap service stop / 檢查 systemd-user 與 system 是否同時在跑）" if not ok else ""
    return {"check": "single_daemon", "ok": ok, "detail": detail, "fix": fix}
```

接進 `run_doctor()`（`doctor_cmd.py:158` 的 checks list 加 `_check_single_daemon()`）。

- [ ] **Step 4: 跑驗證 GREEN** → `python3 -m pytest tests/test_multi_open_detect.py -q`

- [ ] **Step 5: Commit**

```bash
git add sw_core/doctor_cmd.py tests/test_multi_open_detect.py
git commit -m "feat(doctor): _check_single_daemon 多開健檢 #101"
```

---

## Task 10: `daemon status` 多開欄位（executor offload）

**Files:**
- Modify: `sw_core/service.py`（`health()` 或 `daemon status` 對應路徑；`rpc` `health.status`）
- Test: `tests/test_multi_open_detect.py`

**背景：** `health.status` 在同步 dispatcher（`service.py:524-527`）。掃描走 offload：用既有 executor 機制（參考 PR#73 file.* offload 模式）或在背景 thread 預算快取，避免凍結 event loop。

- [ ] **Step 1: 寫測試**

```python
def test_daemon_status_includes_multi_open(make_service_one_daemon, monkeypatch):
    svc = make_service_one_daemon()
    monkeypatch.setattr("sw_core.service.detect_multi_open",
                        lambda **k: {"multi_open": False, "daemons":[{"pid":1}], "holders":{}, "holders_status":"ok"})
    st = svc.health()
    assert "multi_open" in st and st["multi_open"] is False
```

- [ ] **Step 2: 跑驗證 RED** → `python3 -m pytest tests/test_multi_open_detect.py -k daemon_status -q`

- [ ] **Step 3: 實作**

在 `health()` 回傳 dict 加入：

```python
# sw_core/service.py（health()）
from .multi_open import detect_multi_open
tty_paths = [s.get("attached_real_path") for s in sessions if s.get("attached_real_path")]
mo = detect_multi_open(tty_paths=[t for t in tty_paths if t])
status["multi_open"] = mo["multi_open"]
status["foreign_holders"] = mo["holders"]
status["multi_open_detail"] = {"daemons": mo["daemons"], "holders_status": mo["holders_status"]}
```

> offload：若 `health.status` 經量測在多裝置/大量 pid 下耗時超過數十 ms，改為背景 thread 週期更新 `self._multi_open_cache` 並於 `health()` 讀快取。先以直接呼叫實作 + 測試，offload 視 Task 6 實機量測決定（記憶：RPC 單執行緒 asyncio，長阻塞 handler 凍結全 daemon）。

- [ ] **Step 4: 跑驗證 GREEN** → `python3 -m pytest tests/test_multi_open_detect.py -q`

- [ ] **Step 5: Commit**

```bash
git add sw_core/service.py tests/test_multi_open_detect.py
git commit -m "feat(service): daemon status 加多開/外部持有者欄位 #101"
```

---

## Task 11: docs / 政策同步

**Files:** `CHANGELOG.md`、`README.md`、`docs/serialwrap-spec.md`

- [ ] **Step 1: CHANGELOG `[Unreleased]`** 記 #100（COM 確定性 rank + `session renumber`）、#101（多開偵測 doctor/status）
- [ ] **Step 2: README** 補 `session renumber` 用法、`daemon status` 新欄位、doctor `single_daemon` 檢查項
- [ ] **Step 3: `docs/serialwrap-spec.md`** RPC `session.renumber` 與 status 欄位契約（R-18）
- [ ] **Step 4: 工具標記殘留檢查**：`grep -rnE '</content>|</invoke>|</parameter>' CHANGELOG.md README.md docs/serialwrap-spec.md`（記憶 write-tool-marker-contamination）
- [ ] **Step 5: Commit**

```bash
git add CHANGELOG.md README.md docs/serialwrap-spec.md
git commit -m "docs: 對齊 session renumber / 多開偵測契約（#100 #101，R-18）"
```

---

## Task 12: 全量測試 + policy gate

- [ ] **Step 1:** `python3 -m pytest -q tests/ <既有 flaky --ignore 群>` → 無新失敗
- [ ] **Step 2:** `python3 -m policy_check --repo .` → 通過（本地假綠，PR 規則由 CI / 帶 --pr-* 複現）
- [ ] **Step 3:** openspec tasks.md 勾選對應項

---

## Task 13: 實機驗證（throwaway daemon + usbipd，不動 prod COM0/COM1）

> 沿用 repo 實機驗證手法（獨立 `SERIALWRAP_RUN_DIR`/`_STATE_DIR`/`_BY_ID_DIR` 的 throwaway daemon 跑 worktree 新碼）。usbipd 從 WSL：`usbipd.exe attach -w -b 8-1|8-2` / `usbipd.exe detach -b 8-1|8-2`（先定位 usbipd.exe 完整路徑、確認 8-1/8-2 ↔ 哪片板）。

- [ ] **6.1** throwaway daemon 起新碼 → `session list` 斷言 COM0=AC01QZT0、COM1=AQ00OAQ7；重啟數次皆同
- [ ] **6.2** `detach 8-1 8-2` → 反序 `attach -w 8-2` 先、`8-1` 後 → COM 仍依 by-id 排序（不隨 attach 順序）
- [ ] **6.3** 人為亂序後 `serialwrap session renumber` → snap 回 sorted；alias/console 不斷
- [ ] **6.4** `detach 8-1`（COM0→DETACHED）→ `attach 8-1` 同板 → 拿回 COM0
- [ ] **6.5** 刻意起第二個 serialwrapd（不同 socket）→ `doctor` 與 `daemon status` 報多開；事後清掉
- [ ] 將實機結果回填 PR 描述

---

## Self-Review（plan 對 spec）

- **Spec coverage：** com-identity-binding 的 4 個 requirement → Task 2/3(rank)、Task 4(作用域/hotplug)、Task 5/6/7(renumber)；daemon-multi-open-detection 的 3 個 requirement → Task 8(偵測+降級)、Task 9(doctor)、Task 10(status)。✓
- **Placeholder：** fixture 細節以「對齊既有 test helper」標註並給 fallback，非 TBD。整合層 fixture 成本高者明確下放 Task 13 實機涵蓋。
- **型別一致：** `device_sort_key`、`prepare_dynamic_rank`、`com_for_by_id`、`renumber_dynamic`、`detect_multi_open`、`_check_single_daemon` 跨 task 命名一致。
- **已知風險：** renumber 跨 Service/SM/arbiter 邊界（Task 5+6 分層）；by-path fallback 需 `DeviceInfo` 補欄位（Task 2 註明，FTDI 情境可暫回 None）。
