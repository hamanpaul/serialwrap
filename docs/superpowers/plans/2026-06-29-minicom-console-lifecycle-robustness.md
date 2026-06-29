# minicom console lifecycle robustness 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（建議）或 superpowers:executing-plans 逐 task 實作。步驟以 `- [ ]` 追蹤。

**Goal:** 修好 minicom 因 serialwrap 端 console 生命週期問題造成的兩個同源回歸——孤兒/stale console 累積（#76 掉字/卡頓）與 human console 掉回 line-buffer（症狀1：Tab/方向鍵失效）。

**Architecture:** 三條協同修法。Fix2＝關終端 SIGHUP 時可靠 detach（minicom wrapper）＋ daemon 週期主動回收無 reader 的孤兒 console（含死掉的 primary）。Fix3＝human lease 被動拆除加 peer-loss grace（防 flap）＋ lease-backed 週期自癒重授 raw ownership。所有跨層 bridge 狀態存取走**原子鎖內快照**與**原子條件式 grant**（Codex 對抗審查 finding-2），鎖序 `SessionManager._lock ⊃ UARTBridge._state_lock`、`/proc` 掃描一律在鎖外。

**Tech Stack:** Python 3.10+（`from __future__ import annotations`、完整型別標註、`threading.RLock`/`Lock`、`@dataclasses.dataclass`）、bash（minicom_router.sh）、pytest/unittest（既有 PTY 測試慣例）。

**OpenSpec:** `openspec/changes/minicom-console-lifecycle-robustness/`（proposal/design/specs/tasks，已含 Codex 三點修正）。

**前置（已完成的實機重現，餵入本計畫）：**
- 症狀1：`serialwrap session console-list` 顯示 COM0/COM1 `interactive_session_id:null`、兩 console `interactive_owner:false`（line-buffer）；各 `console_count:2`＝1 真 minicom + 1 個 label=`primary` 死 console（pts 僅 daemon slave、無外部 reader、因 primary 永不 reap 卡死）。
- SIGHUP 孤兒：用真 `minicom_router.sh` 走 broker 路徑 + fake-serialwrap 記錄呼叫 + fake-minicom `sleep` + `kill -HUP`：紀錄只見 `session list`、`console-attach`，**無 `console-detach`** → 孤兒（重現 bug）。

---

## File Structure

| 檔案 | 變更 | 責任 |
|---|---|---|
| `sw_core/assets/tools/minicom_router.sh` | 修改（`trap` 兩處） | wrapper：SIGHUP 也跑 cleanup→console-detach |
| `sw_core/constants.py` | 新增常數 | `_HUMAN_PEER_GRACE_S` |
| `sw_core/uart_io.py` | 修改 | `ConsoleClient.internal`、`reap_stale_consoles()`、`snapshot()` 擴充、`try_grant_interactive_if_idle()` |
| `sw_core/session_manager.py` | 修改 | `InteractiveLease.peer_lost_at`、`_refresh_interactive_locked` grace、`reconcile_readiness` 接 reaper + lease-backed 自癒 |
| `tests/test_minicom_router.py` | 新增測試 | SIGHUP detach |
| `tests/test_uart_io.py` | 新增測試 | internal flag、reaper、snapshot、原子 grant |
| `tests/test_interactive_raw.py` | 新增測試 | grace、自癒、TOCTOU、真 PTY attach 授予 |
| `README.md` / `CHANGELOG.md` | 修改 | 契約對齊（R-18）、變更紀錄 |

**測試 fixture 慣例：** 各 task 測試出現的 `_make_bridge` / `_make_session_manager_with_ready_bridge` / `_attach_human_owner` / `bridge_console_peer_returns` 為示意名；實作時**優先沿用 `tests/test_uart_io.py`、`tests/test_interactive_raw.py`、`tests/test_session_bind.py` 既有的 bridge/SessionManager 建構 fixture**，無等價者才新增最小 helper（建真 `UARTBridge`/`SessionManager` + 假 PTY device，比照既有測試）。peer 判定的 monkeypatch 以該檔既有方式為準。

**共用介面契約（後續 task 一致引用）：**
- `ConsoleClient.internal: bool = False`（uart_io）。
- `UARTBridge.reap_stale_consoles(self, *, held_slave_paths: set[str] | None = None) -> list[ConsoleClient]`（uart_io）——回傳已回收的 client（呼叫端關 fd 已於內完成）。`held_slave_paths` 為鎖外預掃的「被外部持有的 slave_path 集合」；None 時自行於鎖外掃。
- `UARTBridge.snapshot()` 新增鍵：`agent_active: bool`、`suspended_owner: str | None`、`flash_mode: bool`、`primary_client_id: str | None`（既有已有 `interactive_owner`/`vtty`/`last_human_input_at`/`vtty_alive`）。
- `UARTBridge.try_grant_interactive_if_idle(self, owner: str) -> bool`（uart_io）——單次 `_state_lock`，僅當 `_interactive_owner is None and _suspended_owner is None and not _agent_active and not _flash_mode` 才設 owner 回 True。
- `_HUMAN_PEER_GRACE_S: float = 3.0`（constants）。
- `InteractiveLease.peer_lost_at: float | None = None`（session_manager）。

---

## Task 1: Fix2 — minicom wrapper SIGHUP detach

**Files:**
- Modify: `sw_core/assets/tools/minicom_router.sh:355,362`
- Test: `tests/test_minicom_router.py`（新增 1 個 method）

- [ ] **Step 1: 寫 failing 測試**（複製既有 `test_broker_console_detaches_after_minicom_nonzero_exit` 的 stub 慣例，改送 SIGHUP）

```python
    def test_broker_console_detaches_on_sighup(self) -> None:
        with self._temporary_directory() as td:
            root = Path(td)
            fake_minicom = root / "fake-minicom.sh"
            fake_serialwrap = root / "fake-serialwrap.sh"
            serialwrap_log = root / "serialwrap.log"

            # fake minicom 睡著，模擬 human 連線中（讓 router 仍在跑時可被 SIGHUP）
            self._write_executable(
                fake_minicom,
                "#!/usr/bin/env bash\nsleep 30\n",
            )
            self._write_executable(
                fake_serialwrap,
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_SERIALWRAP_LOG\"\n"
                "if [[ \"${1:-}\" == '--socket' ]]; then shift 2; fi\n"
                "if [[ \"${1:-}\" == 'session' && \"${2:-}\" == 'list' ]]; then\n"
                "  echo '{\"ok\":true,\"sessions\":[{\"com\":\"COM0\",\"alias\":\"default\",\"session_id\":\"s0\",\"state\":\"READY\"}]}'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"${1:-}\" == 'session' && \"${2:-}\" == 'console-attach' ]]; then\n"
                "  echo '{\"ok\":true,\"client_id\":\"client-1\",\"vtty\":\"/dev/null\"}'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"${1:-}\" == 'session' && \"${2:-}\" == 'console-detach' ]]; then echo '{\"ok\":true}'; exit 0; fi\n"
                "echo '{\"ok\":false}'\nexit 1\n",
            )

            env = os.environ.copy()
            env["SERIALWRAP_BIN"] = str(fake_serialwrap)
            env["SERIALWRAP_SOCKET"] = str(root / "serialwrapd.sock")
            env["SERIALWRAP_AUTO_START_DAEMON"] = "0"
            env["FAKE_SERIALWRAP_LOG"] = str(serialwrap_log)
            env["MINICOM_BIN"] = str(fake_minicom)
            env["MINICOM_AUTO_CAPTURE"] = "0"
            env["MINICOM_DEFAULT_COLOR"] = ""
            env["MINICOM_CAPTURE_MODE"] = "off"

            # 自成 process group，送 SIGHUP 給 router 本身
            proc = subprocess.Popen(
                ["bash", str(ROUTER), "COM0"],
                cwd=str(REPO_ROOT), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                # 等 console-attach 出現（最多 ~5s）
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if serialwrap_log.exists() and "session console-attach" in serialwrap_log.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.05)
                proc.send_signal(signal.SIGHUP)
                proc.wait(timeout=5)
            finally:
                if proc.poll() is None:
                    proc.kill()

            calls = serialwrap_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("session console-attach" in c for c in calls))
            self.assertTrue(
                any("session console-detach" in c for c in calls),
                msg="SIGHUP 應觸發 console-detach（trap 需含 HUP），否則留孤兒",
            )
```

  並於檔頭確認 `import signal`、`import time` 已存在（若無則補）。

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `python3 -m pytest -q tests/test_minicom_router.py::TestMinicomRouter::test_broker_console_detaches_on_sighup`
Expected: FAIL（斷言 console-detach 未出現——現況 `trap` 無 HUP，bash 收 SIGHUP 直接死、不跑 EXIT trap）。

- [ ] **Step 3: 改 trap 加 HUP**

`sw_core/assets/tools/minicom_router.sh` 第 355 行：
```bash
  trap cleanup EXIT INT TERM HUP
```
第 362 行（清 trap 處同步）：
```bash
  trap - EXIT INT TERM HUP
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `python3 -m pytest -q tests/test_minicom_router.py`
Expected: PASS（含新測試；既有 broker-detach/capture 測試續綠）。

- [ ] **Step 5: Commit**

```bash
git add sw_core/assets/tools/minicom_router.sh tests/test_minicom_router.py
git commit -m "fix(minicom): SIGHUP 也觸發 console-detach，避免關終端留孤兒 console（#76）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Fix2 — ConsoleClient.internal 旗標 + 哨兵 primary

**Files:**
- Modify: `sw_core/uart_io.py`（`ConsoleClient` dataclass ~34-48；`_create_console_client` ~192-208；`start()` 哨兵建立 ~228）
- Test: `tests/test_uart_io.py`

說明：start() 在無 preserved console 時建一個 broker 內部哨兵 primary（作 `snapshot.vtty` 錨點，無外部 reader）。此哨兵須標記 `internal=True` 永不被 reaper 回收；經 `attach_console`/TCP accept 建立的真實 console 為 `internal=False`。

- [ ] **Step 1: 寫 failing 測試**

```python
    def test_start_primary_is_internal_sentinel(self) -> None:
        bridge = _make_bridge()  # 既有 helper；若無，依檔內慣例以假 device 建 UARTBridge
        bridge.start()
        try:
            with bridge._state_lock:
                primary = bridge._clients[bridge._primary_client_id]
            self.assertTrue(primary.internal, "start() 建的哨兵 primary 應 internal=True")
        finally:
            bridge.stop()

    def test_attach_console_client_is_not_internal(self) -> None:
        bridge = _make_bridge()
        bridge.start()
        try:
            info = bridge.attach_console(label="minicom-test")
            with bridge._state_lock:
                client = bridge._clients[info["client_id"]]
            self.assertFalse(client.internal, "attach_console 建的真實 console 應 internal=False")
        finally:
            bridge.stop()
```

  （若檔內已有建 bridge 的 helper/ fixture，沿用之；本步驟只新增斷言 `internal` 欄位語意。）

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `python3 -m pytest -q tests/test_uart_io.py::TestUartIo::test_start_primary_is_internal_sentinel tests/test_uart_io.py::TestUartIo::test_attach_console_client_is_not_internal`
Expected: FAIL（`AttributeError: 'ConsoleClient' object has no attribute 'internal'`）。

- [ ] **Step 3: 加 internal 欄位 + 哨兵標記**

`ConsoleClient` dataclass 末尾（`sock: Any = None` 之後）新增：
```python
    # broker 內部哨兵 primary（start() 建、無外部 reader、作 snapshot.vtty 錨點）為 True，永不被 reaper 回收；
    # 經 attach_console / TCP accept 建立的真實 console 為 False。
    internal: bool = False
```
`start()` 建哨兵 primary 處（目前 `primary = self._create_console_client("primary")`）改為標記 internal。最小作法：在該行後加 `primary.internal = True`（`ConsoleClient` 為可變 dataclass，可直接設）。確保只有這條哨兵被設 True；`_create_console_client` 預設 `internal=False` 不變，故 `attach_console` 路徑自動為 False。

- [ ] **Step 4: 跑測試確認 PASS**

Run: `python3 -m pytest -q tests/test_uart_io.py`
Expected: PASS（含兩新測試；既有 console 測試續綠）。

- [ ] **Step 5: Commit**

```bash
git add sw_core/uart_io.py tests/test_uart_io.py
git commit -m "feat(uart_io): ConsoleClient.internal 標記哨兵 primary（reaper 永不回收）（#76）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Fix2 — UARTBridge.reap_stale_consoles()

**Files:**
- Modify: `sw_core/uart_io.py`（新增 method；參考既有 `_prune_stale_consoles_locked` :442-458、`_client_has_external_peer_locked` :398-428、`_drop_console_client` :291、`_close_console_client`、`_STALE_CONSOLE_GRACE_S` :23）
- Test: `tests/test_uart_io.py`

設計鐵則（Codex finding-2 + #78 防回歸）：
- **lock-split**：鎖內快照候選 →（若未給）鎖外掃一次 `/proc` 建 `held_slave_paths` → 回鎖 pop → 鎖外 close fd（沿用既有 pop-in-lock/close-out-of-lock，防 #83 use-after-close）。
- **硬性跳過**：`_interactive_owner` 與 `_suspended_owner` 衍生的 client_id、`internal=True` 哨兵。
- 被 reap 的 client 一律 `_deferred_buffers.pop(cid, None)`；若剛好是 `_primary_client_id` 則比照 `_drop_console_client` 重指。
- 只回收 `internal=False` 且過 `_STALE_CONSOLE_GRACE_S`（attached_at 起算）且 `slave_path not in held_slave_paths` 者。

- [ ] **Step 1: 寫 failing 測試**

```python
    def test_reap_drops_orphan_non_primary(self) -> None:
        bridge = _make_bridge(); bridge.start()
        try:
            info = bridge.attach_console(label="orphan")          # 真實 console，無外部 reader
            cid = info["client_id"]
            with bridge._state_lock:
                bridge._clients[cid].attached_at -= 10.0          # 強制過 grace
            reaped = bridge.reap_stale_consoles(held_slave_paths=set())  # 模擬「無人持有任何 slave」
            self.assertIn(cid, [c.client_id for c in reaped])
            with bridge._state_lock:
                self.assertNotIn(cid, bridge._clients)
        finally:
            bridge.stop()

    def test_reap_never_touches_internal_sentinel(self) -> None:
        bridge = _make_bridge(); bridge.start()
        try:
            pid = bridge._primary_client_id
            with bridge._state_lock:
                bridge._clients[pid].attached_at -= 10.0
            bridge.reap_stale_consoles(held_slave_paths=set())
            with bridge._state_lock:
                self.assertIn(pid, bridge._clients, "internal 哨兵 primary 不得被回收")
        finally:
            bridge.stop()

    def test_reap_skips_owner_and_suspended_owner(self) -> None:
        bridge = _make_bridge(); bridge.start()
        try:
            info = bridge.attach_console(label="owner"); cid = info["client_id"]
            bridge.set_interactive_owner(f"human:{cid}")
            with bridge._state_lock:
                bridge._clients[cid].attached_at -= 10.0
            bridge.reap_stale_consoles(held_slave_paths=set())
            with bridge._state_lock:
                self.assertIn(cid, bridge._clients, "當前 owner 不得被 reaper 回收")
            # suspended owner（agent 命令中）
            bridge.suspend_interactive()
            bridge.reap_stale_consoles(held_slave_paths=set())
            with bridge._state_lock:
                self.assertIn(cid, bridge._clients, "suspended owner 不得被 reaper 回收")
                self.assertEqual(bridge._suspended_owner, f"human:{cid}")
            bridge.resume_interactive()
        finally:
            bridge.stop()
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `python3 -m pytest -q tests/test_uart_io.py -k reap`
Expected: FAIL（`AttributeError: ... has no attribute 'reap_stale_consoles'`）。

- [ ] **Step 3: 實作 reap_stale_consoles**

於 `UARTBridge` 新增（緊鄰 `_prune_stale_consoles_locked`）：
```python
    def reap_stale_consoles(self, *, held_slave_paths: set[str] | None = None) -> list[ConsoleClient]:
        """週期主動回收無外部 reader 的孤兒 console（含死掉的非-internal primary）。

        lock-split：鎖內快照候選 → 鎖外掃 /proc（若 held_slave_paths 未給）→ 回鎖 pop → 鎖外 close fd。
        硬性跳過當前 _interactive_owner / _suspended_owner / internal 哨兵，避免破壞 #78 suspend 簿記
        與 Fix3 的 peer-loss grace（owner 拆除全權交 session-layer）。
        """
        now = time.time()
        with self._state_lock:
            owner_cid = self._interactive_owner.split(":", 1)[1] if (self._interactive_owner or "").startswith("human:") else None
            susp_cid = self._suspended_owner.split(":", 1)[1] if (self._suspended_owner or "").startswith("human:") else None
            protected = {cid for cid in (owner_cid, susp_cid) if cid}
            candidates = [
                (c.client_id, c.slave_path)
                for c in self._clients.values()
                if not c.internal
                and c.client_id not in protected
                and (now - c.attached_at) >= _STALE_CONSOLE_GRACE_S
            ]
        if not candidates:
            return []
        if held_slave_paths is None:
            held_slave_paths = self._scan_held_slave_paths()  # 鎖外 /proc 掃描
        reaped: list[ConsoleClient] = []
        with self._state_lock:
            for cid, slave_path in candidates:
                client = self._clients.get(cid)
                if client is None or client.internal:
                    continue
                # 回鎖後重查保護集合（期間可能變成 owner/suspended）
                cur_owner = self._interactive_owner.split(":", 1)[1] if (self._interactive_owner or "").startswith("human:") else None
                cur_susp = self._suspended_owner.split(":", 1)[1] if (self._suspended_owner or "").startswith("human:") else None
                if cid in {x for x in (cur_owner, cur_susp) if x}:
                    continue
                if slave_path in held_slave_paths:
                    continue
                removed = self._clients.pop(cid, None)
                if removed is None:
                    continue
                self._deferred_buffers.pop(cid, None)
                if self._primary_client_id == cid:
                    nxt = next(iter(self._clients.values()), None)
                    self._primary_client_id = nxt.client_id if nxt is not None else None
                reaped.append(removed)
        for client in reaped:
            self._close_console_client(client)  # 鎖外關 fd
        return reaped
```
並新增鎖外 `/proc` 掃描 helper（抽自 `_client_has_external_peer_locked` 的掃描邏輯，但回傳「所有被外部 process 持有的 slave_path 集合」、排除自身 pid）：
```python
    def _scan_held_slave_paths(self) -> set[str]:
        held: set[str] = set()
        self_pid = os.getpid()
        try:
            pids = os.listdir("/proc")
        except OSError:
            # procfs 不可用：保守起見回傳所有現存 client 的 slave_path（視為仍被持有，不誤剪）
            with self._state_lock:
                return {c.slave_path for c in self._clients.values()}
        for pid_text in pids:
            if not pid_text.isdigit() or int(pid_text) == self_pid:
                continue
            fd_dir = os.path.join("/proc", pid_text, "fd")
            try:
                fd_names = os.listdir(fd_dir)
            except OSError:
                continue
            for fd_name in fd_names:
                try:
                    target = os.readlink(os.path.join(fd_dir, fd_name))
                except OSError:
                    continue
                held.add(target)
        return held
```
注意：TCP（`sock is not None`）console 不靠 /proc（其斷線由 console loop `recv b""` drop）；`reap_stale_consoles` 的候選只針對 PTY（POSIX），TCP client 的 `slave_path` 為 `host:port` 不會出現在 /proc readlink，故若要支援 Windows 應在候選過濾排除 `sock is not None`（本變更 POSIX-only，可加 `and c.sock is None` 於候選條件以明確界定）。

- [ ] **Step 4: 跑測試確認 PASS**

Run: `python3 -m pytest -q tests/test_uart_io.py -k reap`
Expected: PASS（三個 reap 測試）。再跑整檔 `python3 -m pytest -q tests/test_uart_io.py` 確認既有 console/prune 測試續綠。

- [ ] **Step 5: Commit**

```bash
git add sw_core/uart_io.py tests/test_uart_io.py
git commit -m "feat(uart_io): reap_stale_consoles 主動回收孤兒 console（lock-split、跳過 owner/suspended、primary 可回收）（#76）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Fix2 — 把 reaper 接進 reconcile_readiness tick

**Files:**
- Modify: `sw_core/session_manager.py`（`reconcile_readiness` :792-816；遍歷 sessions 處）
- Test: `tests/test_interactive_raw.py`（或 session 測試檔，依現有 SessionManager 測試所在）

設計：tick 末尾、**鎖外**對每個 `bridge is not None` 的 session 呼 `bridge.reap_stale_consoles()`。多 session 共用單次 `/proc` 掃描（在 SessionManager 層掃一次 `held = <掃 /proc>`，傳給每個 bridge），避免 N 次掃描；節流以 `self._last_console_reap_at`（monotonic）每 `_HUMAN_PEER_GRACE_S` 掃一次即可。**不得**在持 `self._lock` 時掃 /proc。

- [ ] **Step 1: 寫 failing 測試**（以真 UARTBridge + SessionManager；斷言孤兒於 tick 後被回收）

```python
    def test_reconcile_reaps_orphan_console(self) -> None:
        mgr = _make_session_manager_with_ready_bridge()   # 依現有測試慣例建一個含真 bridge 的 READY session
        session = next(iter(mgr._sessions.values()))
        bridge = session.bridge
        info = bridge.attach_console(label="orphan")
        with bridge._state_lock:
            bridge._clients[info["client_id"]].attached_at -= 10.0
        mgr._last_console_reap_at = 0.0                    # 解除節流
        mgr.reconcile_readiness()
        with bridge._state_lock:
            self.assertNotIn(info["client_id"], bridge._clients)
```

  （若無現成 `_make_session_manager_with_ready_bridge`，依 `tests/test_session_bind.py` / 既有 fixture 建最小 SessionManager + 真 bridge；本測試重點是 tick 呼到 reaper。）

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `python3 -m pytest -q tests/test_interactive_raw.py::...::test_reconcile_reaps_orphan_console`
Expected: FAIL（tick 尚未呼 reaper，孤兒仍在）。

- [ ] **Step 3: 接線 reconcile_readiness**

於 `SessionManager.__init__` 加 `self._last_console_reap_at: float = 0.0`。於 `reconcile_readiness` 尾端（`for action, ... in jobs:` 迴圈之後、方法 return 前）加：
```python
        # 週期回收孤兒 console（#76）：節流 + 單次共享 /proc 掃描，鎖外執行。
        if now - self._last_console_reap_at >= _HUMAN_PEER_GRACE_S:
            self._last_console_reap_at = now
            with self._lock:
                bridges = [s.bridge for s in self._sessions.values() if s.bridge is not None]
            held: set[str] | None = None
            if bridges:
                held = bridges[0]._scan_held_slave_paths()  # 任一 bridge 的掃描即全域 /proc，共用
            for bridge in bridges:
                try:
                    bridge.reap_stale_consoles(held_slave_paths=held)
                except Exception:  # noqa: BLE001 — 單一 bridge 回收失敗不得中斷 tick
                    pass
```
（`_scan_held_slave_paths` 為 bridge 的鎖外 method，掃的是全系統 /proc，與哪個 bridge 呼叫無關，故共用其一即可。）匯入 `_HUMAN_PEER_GRACE_S`（見 Task 5 在 constants 新增；本 task 若先於 Task 5，先於 constants 加該常數）。

- [ ] **Step 4: 跑測試確認 PASS**

Run: `python3 -m pytest -q tests/test_interactive_raw.py tests/test_session_bind.py`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add sw_core/session_manager.py tests/test_interactive_raw.py
git commit -m "feat(session): reconcile tick 週期回收孤兒 console（節流+共享 /proc 掃描、鎖外）（#76）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Fix3 — human lease peer-loss grace

**Files:**
- Modify: `sw_core/constants.py:~71`（新常數）；`sw_core/session_manager.py`（`InteractiveLease` ~140 加欄位；`_refresh_interactive_locked` :1945-1976 human 分支）
- Test: `tests/test_interactive_raw.py`

設計：`_refresh_interactive_locked` 對 human lease 不再「peer False 即拆」。peer 在→清 `peer_lost_at`；peer 不在→若 `peer_lost_at is None` 設為 `now` 並**回傳 lease（暫不拆）**；僅 `now - peer_lost_at > _HUMAN_PEER_GRACE_S` 才 detach+close。grace 只套用於被動拆當前 owner。

- [ ] **Step 1: 寫 failing 測試**

```python
    def test_peer_flap_within_grace_keeps_lease(self):
        mgr, session, bridge, cid = _attach_human_owner()   # 建一個 human lease owner（真 bridge）
        # 模擬 peer 瞬時消失（console_has_external_peer 回 False）
        bridge_console_peer_returns(bridge, cid, False)
        lease, _ = mgr._refresh_interactive_locked(session)
        self.assertIsNotNone(lease, "grace 窗內首次 peer-loss 不應拆 lease")
        self.assertIsNotNone(lease.peer_lost_at)
        # peer 回復
        bridge_console_peer_returns(bridge, cid, True)
        lease2, _ = mgr._refresh_interactive_locked(session)
        self.assertIsNotNone(lease2)
        self.assertIsNone(lease2.peer_lost_at, "peer 回復應清 peer_lost_at")

    def test_peer_gone_past_grace_tears_down(self):
        mgr, session, bridge, cid = _attach_human_owner()
        bridge_console_peer_returns(bridge, cid, False)
        lease, _ = mgr._refresh_interactive_locked(session)        # 首次：記 peer_lost_at
        lease.peer_lost_at -= (_HUMAN_PEER_GRACE_S + 1.0)          # 強制超過 grace
        result, _ = mgr._refresh_interactive_locked(session)
        self.assertIsNone(result, "超過 grace 應拆 lease")
        self.assertIsNone(session.interactive_session_id)
```

  （`_attach_human_owner` / `bridge_console_peer_returns` 為測試 helper：前者建 READY session + console-attach 取得 human lease；後者 monkeypatch `bridge.console_has_external_peer` 對該 cid 的回傳。依現有 `test_interactive_raw.py` 既有 helper 慣例實作；若已有等價 fixture 則沿用。）

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `python3 -m pytest -q tests/test_interactive_raw.py -k "peer_flap or peer_gone"`
Expected: FAIL（現況首次 peer False 即 detach → `test_peer_flap_within_grace_keeps_lease` 失敗）。

- [ ] **Step 3: 加常數 + 欄位 + grace 邏輯**

`sw_core/constants.py`（`HUMAN_ACTIVE_WINDOW_S` 附近）新增：
```python
# human console lease 被動拆除的 peer-loss grace：peer 瞬時 flap 不立即拆，持續超過此秒數才拆。
_HUMAN_PEER_GRACE_S: float = 3.0
```
`InteractiveLease` dataclass 加可變欄位（`suspended_human` 之後）：
```python
    # human lease：首次觀測到 console peer 消失的 monotonic 時間；peer 回復清為 None。供 peer-loss grace 判定。
    peer_lost_at: float | None = None
```
`_refresh_interactive_locked` 的 human 分支（現為 `if not session.bridge.console_has_external_peer(client_id): detach+close`）改為：
```python
        if lease.owner.startswith("human:"):
            client_id = lease.owner.split(":", 1)[1]
            if not session.bridge.console_has_external_peer(client_id):
                if lease.peer_lost_at is None:
                    lease.peer_lost_at = time.monotonic()
                    return lease, post           # grace 窗內：暫不拆
                if time.monotonic() - lease.peer_lost_at <= _HUMAN_PEER_GRACE_S:
                    return lease, post           # 仍在 grace 窗
                session.bridge.detach_console(client_id)
                session.vtty_path = session.bridge.vtty_path
                _, post = self._close_interactive_locked(session, interactive_id=lease_id)
                return None, post
            lease.peer_lost_at = None            # peer 在：清 grace 計時
            snapshot = session.bridge.snapshot()
            if snapshot.get("interactive_owner") != lease.owner:
                _, post = self._close_interactive_locked(session, interactive_id=lease_id)
                return None, post
```
匯入 `_HUMAN_PEER_GRACE_S`（from `.constants import _HUMAN_PEER_GRACE_S`，比照既有 `HUMAN_ACTIVE_WINDOW_S` 匯入）。

- [ ] **Step 4: 跑測試確認 PASS**

Run: `python3 -m pytest -q tests/test_interactive_raw.py tests/test_session_bind.py`
Expected: PASS。並確認既有依賴「peer False 即拆」語意的測試（若有）已對齊或在 grace 後仍成立。

- [ ] **Step 5: Commit**

```bash
git add sw_core/constants.py sw_core/session_manager.py tests/test_interactive_raw.py
git commit -m "fix(session): human lease 被動拆除加 peer-loss grace，防瞬時 flap 誤拆（症狀1 觸發B）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Fix3 — bridge 原子快照擴充 + 原子條件式 grant（Codex finding-2）

**Files:**
- Modify: `sw_core/uart_io.py`（`snapshot()` ~955；新增 `try_grant_interactive_if_idle`）
- Test: `tests/test_uart_io.py`

- [ ] **Step 1: 寫 failing 測試**

```python
    def test_snapshot_exposes_decision_fields(self):
        bridge = _make_bridge(); bridge.start()
        try:
            snap = bridge.snapshot()
            for key in ("agent_active", "suspended_owner", "flash_mode", "primary_client_id"):
                self.assertIn(key, snap)
        finally:
            bridge.stop()

    def test_try_grant_if_idle_succeeds_when_idle(self):
        bridge = _make_bridge(); bridge.start()
        try:
            info = bridge.attach_console(label="c1"); cid = info["client_id"]
            self.assertTrue(bridge.try_grant_interactive_if_idle(f"human:{cid}"))
            self.assertEqual(bridge.snapshot()["interactive_owner"], f"human:{cid}")
        finally:
            bridge.stop()

    def test_try_grant_if_idle_fails_when_owner_set(self):
        bridge = _make_bridge(); bridge.start()
        try:
            info = bridge.attach_console(label="c1"); cid = info["client_id"]
            bridge.set_interactive_owner(f"human:{cid}")
            self.assertFalse(bridge.try_grant_interactive_if_idle("human:other"))
            self.assertEqual(bridge.snapshot()["interactive_owner"], f"human:{cid}")
        finally:
            bridge.stop()

    def test_try_grant_if_idle_fails_when_agent_active(self):
        bridge = _make_bridge(); bridge.start()
        try:
            info = bridge.attach_console(label="c1"); cid = info["client_id"]
            bridge.set_interactive_owner(f"human:{cid}")
            bridge.suspend_interactive()                  # _agent_active=True, _suspended_owner=human:cid, owner=None
            self.assertFalse(bridge.try_grant_interactive_if_idle("human:new"),
                             "agent 進行中（suspended）不得授予")
            bridge.resume_interactive()
        finally:
            bridge.stop()
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `python3 -m pytest -q tests/test_uart_io.py -k "snapshot_exposes or try_grant"`
Expected: FAIL（snapshot 缺鍵 / 無 `try_grant_interactive_if_idle`）。

- [ ] **Step 3: 擴充 snapshot + 新增原子 grant**

`snapshot()` 的 `with self._state_lock:` 區塊內補讀，並於回傳 dict 加鍵：
```python
            agent_active = self._agent_active
            suspended_owner = self._suspended_owner
            flash_mode = self._flash_mode
            primary_client_id = self._primary_client_id
        # ... 回傳 dict 增加：
            "agent_active": agent_active,
            "suspended_owner": suspended_owner,
            "flash_mode": flash_mode,
            "primary_client_id": primary_client_id,
```
新增原子條件式 grant：
```python
    def try_grant_interactive_if_idle(self, owner: str) -> bool:
        """原子 check-and-set：僅當 bridge 完全 idle 時授予 interactive ownership。

        單次 _state_lock critical section，消除「讀到陳舊 idle 快照→期間 agent suspend/flash→仍誤授」的 TOCTOU。
        """
        with self._state_lock:
            if (
                self._interactive_owner is None
                and self._suspended_owner is None
                and not self._agent_active
                and not self._flash_mode
            ):
                self._interactive_owner = owner
                return True
            return False
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `python3 -m pytest -q tests/test_uart_io.py`
Expected: PASS（含四新測試；既有 snapshot 取用者不受影響——只增鍵不改既有鍵）。

- [ ] **Step 5: Commit**

```bash
git add sw_core/uart_io.py tests/test_uart_io.py
git commit -m "feat(uart_io): snapshot 擴充決策欄位 + try_grant_interactive_if_idle 原子 grant（Codex finding-2）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Fix3 — lease-backed 週期自癒（用 Task 6 原子 primitive）

**Files:**
- Modify: `sw_core/session_manager.py`（`reconcile_readiness` 尾端，reaper 之後；`_open_interactive_locked` 既有 :1864）
- Test: `tests/test_interactive_raw.py`

設計：tick 中對每個 bridge：取 `snap = bridge.snapshot()`；若 `snap["interactive_owner"] is None and snap["suspended_owner"] is None and not snap["agent_active"] and not snap["flash_mode"]` 且 session state∈{READY,ATTACHED} 且 `session.interactive_session_id` 解析為 None 且有「活 primary console」（`snap["primary_client_id"]` 且 `console_has_external_peer(primary_cid)` 為 True，**鎖外**判定）→ 呼 `bridge.try_grant_interactive_if_idle(f"human:{primary_cid}")`；成功才在 `self._lock` 內建對應 lease（`_open_interactive_locked` 變體：lease 已由 grant 設 bridge owner，這裡只補 session-layer lease 記錄，不重設 owner）。grant 失敗則本 tick 不開 lease。**不裸讀 bridge 私有欄位、不在持 self._lock 時掃 /proc。**

- [ ] **Step 1: 寫 failing 測試（含 TOCTOU）**

```python
    def test_self_heal_regrants_after_owner_loss(self):
        mgr, session, bridge, cid = _attach_human_owner()
        # 模擬 owner 掉失但 console 仍活：清 bridge owner + session lease
        bridge.set_interactive_owner(None)
        session.interactive_session_id = None
        mgr._last_console_reap_at = 0.0
        mgr.reconcile_readiness()
        self.assertEqual(bridge.snapshot()["interactive_owner"], f"human:{cid}")
        self.assertIsNotNone(session.interactive_session_id)

    def test_self_heal_skips_during_agent_active(self):
        mgr, session, bridge, cid = _attach_human_owner()
        bridge.set_interactive_owner(None)
        session.interactive_session_id = None
        bridge.suspend_interactive()                       # agent 進行中
        mgr._last_console_reap_at = 0.0
        mgr.reconcile_readiness()
        self.assertIsNone(bridge.snapshot()["interactive_owner"],
                          "agent 進行中不得自癒奪權")
        bridge.resume_interactive()

    def test_self_heal_grant_fails_on_toctou(self):
        # 快照判 idle 後、grant 前 agent 介入 → try_grant_interactive_if_idle 回 False、不開 lease
        mgr, session, bridge, cid = _attach_human_owner()
        bridge.set_interactive_owner(None); session.interactive_session_id = None
        orig = bridge.try_grant_interactive_if_idle
        def racing_grant(owner):
            bridge.set_interactive_owner(f"human:{cid}")   # 模擬期間 owner 被搶
            return orig(owner)
        bridge.try_grant_interactive_if_idle = racing_grant
        mgr._last_console_reap_at = 0.0
        mgr.reconcile_readiness()
        # 不得開出與既有 owner 衝突的新 lease
        self.assertIsNone(session.interactive_session_id)
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `python3 -m pytest -q tests/test_interactive_raw.py -k self_heal`
Expected: FAIL（tick 尚無自癒）。

- [ ] **Step 3: 接線自癒**

於 `reconcile_readiness` reaper 區塊之後新增（沿用同一 `bridges` 與節流；`held` 已於 reaper 算出可重用於 peer 判定）：
```python
            for bridge in bridges:
                snap = bridge.snapshot()
                if snap["interactive_owner"] is not None or snap["suspended_owner"] is not None:
                    continue
                if snap["agent_active"] or snap["flash_mode"]:
                    continue
                primary_cid = snap["primary_client_id"]
                if not primary_cid:
                    continue
                # 活 primary 判定（鎖外）：primary 的 slave_path 是否被外部持有
                if not bridge.console_has_external_peer(primary_cid):
                    continue
                with self._lock:
                    session = self._session_for_bridge_locked(bridge)
                    if session is None or session.state not in {"READY", "ATTACHED"}:
                        continue
                    if session.interactive_session_id is not None:
                        continue
                    if bridge.try_grant_interactive_if_idle(f"human:{primary_cid}"):
                        # 補 session-layer lease 記錄（owner 已由原子 grant 設好，勿重設）
                        self._record_self_heal_lease_locked(session, f"human:{primary_cid}")
```
新增兩個小 helper：
```python
    def _session_for_bridge_locked(self, bridge: UARTBridge) -> SessionRuntime | None:
        for s in self._sessions.values():
            if s.bridge is bridge:
                return s
        return None

    def _record_self_heal_lease_locked(self, session: SessionRuntime, owner: str) -> None:
        # 比照 _open_interactive_locked 但不呼 set_interactive_owner（已由 try_grant 原子設定）
        interactive_id = uuid.uuid4().hex
        lease = InteractiveLease(
            interactive_id=interactive_id,
            session_id=session.session_id,
            owner=owner,
            created_at=now_iso(),
            timeout_s=max(session.profile.hard_timeout_s, _ATTACHED_CONSOLE_LEASE_TIMEOUT_S),
        )
        self._interactive[interactive_id] = lease
        session.interactive_session_id = interactive_id
```
（`_ATTACHED_CONSOLE_LEASE_TIMEOUT_S` 既有常數 :40；`now_iso`/`uuid`/`InteractiveLease` 既有匯入。）

- [ ] **Step 4: 跑測試確認 PASS**

Run: `python3 -m pytest -q tests/test_interactive_raw.py tests/test_suspend_resume_reentrant.py`
Expected: PASS（含三自癒測試；#78 suspend/resume 全綠不回歸）。

- [ ] **Step 5: Commit**

```bash
git add sw_core/session_manager.py tests/test_interactive_raw.py
git commit -m "feat(session): lease-backed 週期自癒重授 raw ownership（原子 grant、不裸讀 bridge、防 TOCTOU）（症狀1 觸發C）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: docs/CHANGELOG + 整合驗證 + 真機回歸

**Files:**
- Modify: `README.md`（console 生命週期/raw ownership 契約段落，R-18 對齊）、`CHANGELOG.md`（`[Unreleased]`）
- 驗證：openspec / pytest / policy_check / 真機

- [ ] **Step 1: CHANGELOG [Unreleased] 新增條目**（繁中，描述三條修法 + 對應 #76/症狀1 + Codex finding-2 原子化）。

- [ ] **Step 2: README 對齊**：確認「console-attach 自動授予 raw ownership」「bridge rebuild 保留 console」等段落與新行為一致；補述「daemon 週期回收孤兒 console」「owner 掉失自癒」「SIGHUP 自動 detach」。

- [ ] **Step 3: openspec archive 準備**：`openspec validate minicom-console-lifecycle-robustness --strict` 通過（spec 與實作一致）。

- [ ] **Step 4: 全套件測試**

Run: `python3 -m pytest -q tests/`
Expected: 通過，**唯一**可容忍 `tests/test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict`（CLAUDE.md 載明 pre-existing）。任何其他失敗（含 PTY/coexist）須先在 **base commit（main 17710d4）以相同指令重現確切 test id + 附證據**才可判 pre-existing，否則視為本變更回歸須修正（Codex finding-3）。

- [ ] **Step 5: policy check（複現 CI PR 規則）**

Run: `python3 -m policy_check --repo . --pr-title "<繁中 conventional title>" --pr-body "<body, 含 Closes #76>" --pr-base-ref main --pr-head-ref feature/minicom-console-lifecycle-robustness`
Expected: 通過。本 PR **closes #76**（孤兒/掉字）→ PR body 用 `Closes #76`（closing-keyword 形式，R-17 不需豁免；症狀1 無對應 issue，照常不引用）。

- [ ] **Step 6: 真機回歸（COM0/COM1）**：`serialwrap session console-list` 連入 client `interactive_owner:true`；不乾淨關 minicom（kill 終端）後孤兒於下一 tick 被回收（`console_count` 回落）；agent 命令期間 human 不被奪權；Tab/方向鍵在 minicom 即時生效。

- [ ] **Step 7: Commit + push + 開 PR**

```bash
git add README.md CHANGELOG.md
git commit -m "docs(minicom): console 生命週期/raw ownership 契約對齊 + CHANGELOG（#76）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git push -u origin feature/minicom-console-lifecycle-robustness
# gh pr create --base main --title "..." --body-file ...（body 含 Closes #76 + Policy Checklist）
```

---

## 風險與防回歸重點（實作時務必守）

- **#78（suspend 簿記）**：reaper 硬性跳過 `_suspended_owner`/`_interactive_owner`；自癒只走原子 `try_grant_interactive_if_idle`（agent_active/suspended 時必失敗）。`tests/test_suspend_resume_reentrant.py` 全綠為門檻。
- **#83（use-after-close）**：reaper 維持 pop-in-lock / close-out-of-lock。
- **#81（deferred 污染）**：絕不在 bridge 層做 lease-less 自癒；ownership 永遠由 lease 背書。
- **鎖序**：`SessionManager._lock ⊃ UARTBridge._state_lock`；`/proc` 掃描只在鎖外；tick 不在持 `self._lock` 時阻塞 I/O。
- **#76 不被 script wrapper 加重**：PR-A 已把預設改回 `-C`（少一層 script 孤兒），與本變更協同。
