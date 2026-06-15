# Device Release / Handoff 實作計畫（#54）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 serialwrap 能把單一 session 綁定的 UART 裝置乾淨交給外部 flasher（`device release`）、燒完手動收回（`device attach`），且 released 狀態不被自身 re-attach 邏輯搶回、跨 daemon 重啟保留。

**Architecture:** session 新增持久化 `RELEASED` 狀態；所有自動 attach 匯流點 `_spawn_attach` 加 released guard；`clear_session` 對 released 早退；`release_device`/`attach_device` 兩個方法經 RPC `device.release`/`device.attach` 與 CLI 暴露；唯讀 `/proc` 偵測外部持有者作為 self_test 標註與 attach 安全 guard。

**Tech Stack:** Python 3.12、stdlib（threading/os/json）、pytest（unittest.TestCase 風格）、現有 `sw_core` 模組。

---

## Commit 規範（每個 commit step 都套用）

- Conventional Commits，subject 繁中。
- 結尾兩個 trailer：
  ```
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  ```
- 範例（之後各 step 以此為準，只換 subject/body）：
  ```bash
  git commit -F - <<'EOF'
  <type>(device-handoff): <繁中 subject>

  <body>

  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  EOF
  ```

## 測試慣例（沿用 `tests/test_session_bind.py`）

- `unittest.TestCase`；`setUp` 將 `sm_mod.STATE_PATH` monkeypatch 到 tmp。
- `SessionManager(profiles, WalWriter(wal_dir=...), on_ready=..., on_detached=...)`。
- fake device：`with mgr._lock: mgr._devices = {by_id: DeviceInfo(by_id=..., real_path=...)}`。
- 跑單檔：`python3 -m pytest -q tests/test_device_handoff.py`。
- 全套（CLAUDE.md）：`python3 -m pytest -q tests/`（既有 pre-existing 失敗 `test_multiagent_e2e.py::...::test_five_agents_three_rounds_no_conflict` 除外，不得新增失敗）。

## File Structure

- `sw_core/session_manager.py`（修改）：`SessionRuntime` 新欄位 + `RELEASED`；`__init__` 加 `_released_by_ids`/`_loaded_released`；`_spawn_attach` guard；`clear_session` 早退；`_detach_session_locked(drop_consoles=...)`；`release_device`/`attach_device`/`_probe_external_holder`；`_save_state`/`_load_state`；`self_test` RELEASED 分支；`to_public_dict` 補欄位。
- `sw_core/service.py`（修改）：rpc dispatch 加 `device.release`/`device.attach`。
- `sw_core/cli.py`（修改）：`device` subcommand 加 `release`/`attach` + dispatch。
- `tests/test_device_handoff.py`（新建）：unit + 對抗測試。
- `README.md` / `CHANGELOG.md`（修改）：用法與變更紀錄。

---

## Task 1: SessionRuntime 新欄位 + RELEASED 狀態 + 集合初始化

**Files:**
- Modify: `sw_core/session_manager.py`（`SessionRuntime` ~144-175；`__init__` ~275）
- Test: `tests/test_device_handoff.py`

- [ ] **Step 1: 寫 failing test**

新建 `tests/test_device_handoff.py`：

```python
import os
import tempfile
import unittest
from pathlib import Path

from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(lambda: setattr(sm_mod, "STATE_PATH", self._old_state_path))

    def _make_profile(self, name: str, com: str, alias: str, by_id: str) -> SessionProfile:
        return SessionProfile(
            profile_name=name, com=com, act_no=1, alias=alias,
            device_by_id=by_id, platform="prpl", uart=UartProfile(),
        )

    def _mgr(self, profiles):
        return SessionManager(
            profiles, WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _s: None, on_detached=lambda _s: None,
        )


class TestReleasedStateFields(_Base):
    def test_session_has_released_fields_and_set(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        session = mgr.get_session("COM0")
        assert session is not None
        self.assertIsNone(session.released_by)
        self.assertIsNone(session.released_at)
        self.assertIsNone(session.released_reason)
        self.assertEqual(mgr._released_by_ids, set())
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestReleasedStateFields -x`
Expected: FAIL（`AttributeError: 'SessionRuntime' object has no attribute 'released_by'` 或 `_released_by_ids`）

- [ ] **Step 3: 實作**

在 `SessionRuntime` dataclass（緊接 `last_probe_at` 等欄位之後、`_stashed_human_lease` 之前）新增：

```python
    released_by: str | None = None
    released_at: str | None = None
    released_reason: str | None = None
```

在 `SessionManager.__init__`，於 `self._attach_inflight: set[str] = set()`（~275）之後、`self._load_state()`（~282）之前新增：

```python
        self._released_by_ids: set[str] = set()
        self._loaded_released: dict[str, dict[str, str | None]] = {}
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestReleasedStateFields -x`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add sw_core/session_manager.py tests/test_device_handoff.py
git commit -F - <<'EOF'
feat(device-handoff): SessionRuntime 新增 released 欄位與 _released_by_ids

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 2: `_spawn_attach` released guard（自動路徑）

**Files:**
- Modify: `sw_core/session_manager.py`（`_spawn_attach` ~653）
- Test: `tests/test_device_handoff.py`

- [ ] **Step 1: 寫 failing test**

```python
import time
import unittest.mock as mock


class TestSpawnAttachGuard(_Base):
    def test_spawn_attach_skips_released_by_id(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        with mgr._lock:
            mgr._released_by_ids.add(by_id)
        with mock.patch.object(mgr, "_attach_by_id") as attach_by_id:
            mgr._spawn_attach(by_id)
            time.sleep(0.1)
        attach_by_id.assert_not_called()
        self.assertNotIn(by_id, mgr._attach_inflight)

    def test_spawn_attach_runs_for_non_released_by_id(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        with mock.patch.object(mgr, "_attach_by_id") as attach_by_id:
            mgr._spawn_attach(by_id)
            time.sleep(0.1)
        attach_by_id.assert_called_once_with(by_id)
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestSpawnAttachGuard -x`
Expected: FAIL（`test_spawn_attach_skips_released_by_id`：`_attach_by_id` 被呼叫）

- [ ] **Step 3: 實作**

`_spawn_attach` 開頭（`with self._lock:` 內、`if by_id in self._attach_inflight:` 之前）插入：

```python
    def _spawn_attach(self, by_id: str) -> None:
        with self._lock:
            if by_id in self._released_by_ids:
                return
            if by_id in self._attach_inflight:
                return
            self._attach_inflight.add(by_id)
        # ...（其餘不變）
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestSpawnAttachGuard -x`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add sw_core/session_manager.py tests/test_device_handoff.py
git commit -F - <<'EOF'
feat(device-handoff): _spawn_attach 對 released by-id 早退（涵蓋 update_devices/bootstrap）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 3: `clear_session` released 早退（手動 + recovery 路徑）

**Files:**
- Modify: `sw_core/session_manager.py`（`clear_session` ~516）
- Test: `tests/test_device_handoff.py`

- [ ] **Step 1: 寫 failing test**

```python
class TestClearSessionReleasedGuard(_Base):
    def test_clear_on_released_session_is_noop(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"
        with mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.clear_session("COM0")
        self.assertTrue(resp["ok"])
        self.assertTrue(resp.get("released"))
        self.assertEqual(session.state, "RELEASED")
        spawn_attach.assert_not_called()
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestClearSessionReleasedGuard -x`
Expected: FAIL（state 變 ATTACHING / `released` 不存在 / spawn_attach 被呼叫）

- [ ] **Step 3: 實作**

`clear_session` 內，取得 session、`if session is None` return 之後，`_detach_session_locked` 之前插入：

```python
            if session.state == "RELEASED" or session.profile.device_by_id in self._released_by_ids:
                return {"ok": True, "released": True, "session": session.to_public_dict()}
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestClearSessionReleasedGuard -x`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add sw_core/session_manager.py tests/test_device_handoff.py
git commit -F - <<'EOF'
feat(device-handoff): clear_session 對 released session 早退，保留 RELEASED

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 4: `_detach_session_locked` 新增 `drop_consoles`（clean slate）

**Files:**
- Modify: `sw_core/session_manager.py`（`_detach_session_locked` ~479）
- Test: `tests/test_device_handoff.py`

- [ ] **Step 1: 寫 failing test**

```python
class TestDropConsolesDetach(_Base):
    def test_detach_drop_consoles_closes_and_does_not_stash(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        bridge.stop.return_value = None
        session.bridge = bridge
        session.state = "READY"
        with mgr._lock:
            mgr._detach_session_locked(session, reason="RELEASED", drop_consoles=True)
        bridge.stop.assert_called_once_with(preserve_consoles=False)
        self.assertIsNone(session.retained_consoles)
        self.assertIsNone(session.bridge)

    def test_detach_default_preserves_consoles(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        bridge.stop.return_value = None
        session.bridge = bridge
        session.state = "READY"
        with mgr._lock:
            mgr._detach_session_locked(session, reason="X")
        bridge.stop.assert_called_once_with(preserve_consoles=True)
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestDropConsolesDetach -x`
Expected: FAIL（`_detach_session_locked` 無 `drop_consoles` 參數 → `TypeError`）

- [ ] **Step 3: 實作**

把 `_detach_session_locked` 簽名與 bridge 停止段改為：

```python
    def _detach_session_locked(self, session: SessionRuntime, *, reason: str, drop_consoles: bool = False) -> None:
        preserved = session.retained_consoles
        retained_human_owner = session.retained_human_owner
        retained_human_timeout_s = session.retained_human_timeout_s
        if session.interactive_session_id is not None:
            lease = self._interactive.get(session.interactive_session_id)
            if lease is not None and lease.owner.startswith("human:"):
                retained_human_owner = lease.owner
                retained_human_timeout_s = lease.timeout_s
        if session.bridge is not None:
            preserved = session.bridge.stop(preserve_consoles=not drop_consoles)
            session.bridge = None
        if drop_consoles:
            preserved = None
            retained_human_owner = None
            retained_human_timeout_s = None
            session.retained_consoles = None
        self._store_retained_consoles_locked(
            session,
            preserved,
            human_owner=retained_human_owner,
            human_timeout_s=retained_human_timeout_s,
        )
        # ...（其餘不變）
```

> 註：保留既有預設行為（`drop_consoles=False` → `preserve_consoles=True`），只在 release 路徑傳 True。

- [ ] **Step 4: 跑測試確認 pass**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestDropConsolesDetach -x`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add sw_core/session_manager.py tests/test_device_handoff.py
git commit -F - <<'EOF'
feat(device-handoff): _detach_session_locked 新增 drop_consoles（clean slate 不 stash console）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 5: `release_device`

**Files:**
- Modify: `sw_core/session_manager.py`（在 `clear_session` 附近新增方法）
- Test: `tests/test_device_handoff.py`

- [ ] **Step 1: 寫 failing test**

```python
class TestReleaseDevice(_Base):
    def test_release_clean_slate_and_provenance(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        assert session is not None
        bridge = mock.MagicMock()
        bridge.stop.return_value = None
        bridge.list_consoles.return_value = [{"client_id": "c1"}]
        session.bridge = bridge
        session.state = "READY"
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}

        resp = mgr.release_device("COM0", source="agent:flash", reason="flash CC2674")

        self.assertTrue(resp["ok"])
        self.assertEqual(session.state, "RELEASED")
        self.assertEqual(session.released_by, "agent:flash")
        self.assertIsNotNone(session.released_at)
        self.assertEqual(session.released_reason, "flash CC2674")
        self.assertIn(by_id, mgr._released_by_ids)
        self.assertIsNone(session.retained_consoles)
        bridge.stop.assert_called_once_with(preserve_consoles=False)

    def test_release_idempotent(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            session.state = "RELEASED"
            mgr._released_by_ids.add(by_id)
        resp = mgr.release_device("COM0")
        self.assertTrue(resp["ok"])
        self.assertTrue(resp.get("already_released"))

    def test_release_session_not_found(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        resp = mgr.release_device("COM9")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "SESSION_NOT_FOUND")
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestReleaseDevice -x`
Expected: FAIL（`AttributeError: ... has no attribute 'release_device'`）

- [ ] **Step 3: 實作**

新增方法（放在 `clear_session` 之後）：

```python
    def release_device(self, selector: str, *, source: str = "cli", reason: str | None = None) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session.state == "RELEASED":
                return {"ok": True, "already_released": True, "session": session.to_public_dict()}
            by_id = session.profile.device_by_id
            closed_consoles = len(session.bridge.list_consoles()) if session.bridge is not None else 0
            aborted_cmd = session.foreground_busy
            self._detach_session_locked(session, reason="RELEASED", drop_consoles=True)
            session.state = "RELEASED"
            session.released_by = source
            session.released_at = now_iso()
            session.released_reason = reason
            if by_id:
                self._released_by_ids.add(by_id)
            public = session.to_public_dict()
        self._save_state()
        return {"ok": True, "session": public, "closed_consoles": closed_consoles, "aborted_cmd": aborted_cmd}
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestReleaseDevice -x`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add sw_core/session_manager.py tests/test_device_handoff.py
git commit -F - <<'EOF'
feat(device-handoff): 新增 release_device（clean slate + RELEASED + provenance + 冪等）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 6: `_probe_external_holder`（唯讀 /proc 偵測）

**Files:**
- Modify: `sw_core/session_manager.py`（新增 helper；確認檔案頂部已 `import os`）
- Test: `tests/test_device_handoff.py`

- [ ] **Step 1: 寫 failing test**

```python
class TestProbeExternalHolder(_Base):
    def test_probe_detects_holder_in_fake_proc(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/ttyUSB9", proc / "1234" / "fd" / "5")
        res = mgr._probe_external_holder("/dev/ttyUSB9", _proc_root=str(proc))
        self.assertEqual(res["pids"], [1234])
        self.assertEqual(res["holder"], 1234)

    def test_probe_no_holder(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/ttyUSB0", proc / "1234" / "fd" / "5")
        res = mgr._probe_external_holder("/dev/ttyUSB9", _proc_root=str(proc))
        self.assertEqual(res["pids"], [])
        self.assertIsNone(res["holder"])
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestProbeExternalHolder -x`
Expected: FAIL（無 `_probe_external_holder`）

- [ ] **Step 3: 實作**

```python
    def _probe_external_holder(self, real_path: str, *, _proc_root: str = "/proc") -> dict[str, Any]:
        """唯讀偵測 real_path 是否被其他 process 持有；讀 _proc_root/*/fd，不開 tty、不做 I/O。"""
        my_pid = os.getpid()
        try:
            target = os.path.realpath(real_path)
        except OSError:
            target = real_path
        holders: set[int] = set()
        try:
            entries = os.listdir(_proc_root)
        except OSError:
            return {"pids": [], "holder": None}
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == my_pid:
                continue
            fd_dir = os.path.join(_proc_root, entry, "fd")
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    link = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if link == target or link == real_path:
                    holders.add(pid)
                    break
        ordered = sorted(holders)
        return {"pids": ordered, "holder": (ordered[0] if ordered else None)}
```

> 確認 `sw_core/session_manager.py` 頂部已 `import os`（既有，`_load_state` 用到）。

- [ ] **Step 4: 跑測試確認 pass**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestProbeExternalHolder -x`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add sw_core/session_manager.py tests/test_device_handoff.py
git commit -F - <<'EOF'
feat(device-handoff): 新增 _probe_external_holder 唯讀偵測外部持有者

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 7: `attach_device`（收回 + 安全 guard）

**Files:**
- Modify: `sw_core/session_manager.py`（新增方法）
- Test: `tests/test_device_handoff.py`

- [ ] **Step 1: 寫 failing test**

```python
class TestAttachDevice(_Base):
    def _released_mgr(self, by_id="/dev/serial/by-id/orig", real="/dev/ttyUSB0"):
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path=real)}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"
            session.released_by = "agent:flash"
            session.released_at = "now"
        return mgr, session, by_id

    def test_attach_reclaims_when_free(self) -> None:
        mgr, session, by_id = self._released_mgr()
        with mock.patch.object(mgr, "_probe_external_holder", return_value={"pids": [], "holder": None}), \
             mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.attach_device("COM0")
        self.assertTrue(resp["ok"])
        self.assertNotIn(by_id, mgr._released_by_ids)
        self.assertIsNone(session.released_by)
        self.assertEqual(session.state, "ATTACHING")
        spawn_attach.assert_called_once_with(by_id)

    def test_attach_refuses_when_externally_held(self) -> None:
        mgr, session, by_id = self._released_mgr()
        with mock.patch.object(mgr, "_probe_external_holder", return_value={"pids": [4321], "holder": 4321}), \
             mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.attach_device("COM0")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "DEVICE_STILL_HELD")
        self.assertEqual(resp["pids"], [4321])
        self.assertEqual(session.state, "RELEASED")
        spawn_attach.assert_not_called()

    def test_attach_force_bypasses_holder_check(self) -> None:
        mgr, session, by_id = self._released_mgr()
        with mock.patch.object(mgr, "_probe_external_holder", return_value={"pids": [4321], "holder": 4321}) as probe, \
             mock.patch.object(mgr, "_spawn_attach") as spawn_attach:
            resp = mgr.attach_device("COM0", force=True)
        self.assertTrue(resp["ok"])
        probe.assert_not_called()
        spawn_attach.assert_called_once_with(by_id)

    def test_attach_device_not_present(self) -> None:
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")])
        session = mgr.get_session("COM0")
        with mgr._lock:
            session.state = "RELEASED"
            mgr._released_by_ids.add("/dev/serial/by-id/orig")
        resp = mgr.attach_device("COM0")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "DEVICE_NOT_PRESENT")
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestAttachDevice -x`
Expected: FAIL（無 `attach_device`）

- [ ] **Step 3: 實作**

```python
    def attach_device(self, selector: str, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            by_id = session.profile.device_by_id
            if not by_id or by_id not in self._devices:
                return {"ok": False, "error_code": "DEVICE_NOT_PRESENT", "selector": selector, "device_by_id": by_id}
            real_path = self._devices[by_id].real_path
        if not force:
            holder = self._probe_external_holder(real_path)
            if holder["pids"]:
                return {"ok": False, "error_code": "DEVICE_STILL_HELD", "pids": holder["pids"], "selector": selector}
        with self._lock:
            self._released_by_ids.discard(by_id)
            session.released_by = None
            session.released_at = None
            session.released_reason = None
            session.state = "ATTACHING"
            session.last_error = None
            public = session.to_public_dict()
        self._save_state()
        self._spawn_attach(by_id)
        return {"ok": True, "session": public}
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestAttachDevice -x`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add sw_core/session_manager.py tests/test_device_handoff.py
git commit -F - <<'EOF'
feat(device-handoff): 新增 attach_device（清 released + 安全 guard + 重新 attach）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 8: 持久化（跨 daemon 重啟）

**Files:**
- Modify: `sw_core/session_manager.py`（`_save_state` ~313、`_load_state` ~294、`__init__` profile loop 之後）
- Test: `tests/test_device_handoff.py`

- [ ] **Step 1: 寫 failing test**

```python
class TestReleasedPersistence(_Base):
    def test_released_survives_restart_and_bootstrap_skips(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        profiles = [self._make_profile("p", "COM0", "lab+1", by_id)]
        mgr = self._mgr(profiles)
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock()
        bridge.stop.return_value = None
        bridge.list_consoles.return_value = []
        session.bridge = bridge
        session.state = "READY"
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        mgr.release_device("COM0", source="agent:flash", reason="flash")

        # 模擬 daemon 重啟：同一 STATE_PATH、同 profiles 重建 SessionManager
        mgr2 = self._mgr(profiles)
        s2 = mgr2.get_session("COM0")
        assert s2 is not None
        self.assertEqual(s2.state, "RELEASED")
        self.assertEqual(s2.released_by, "agent:flash")
        self.assertIn(by_id, mgr2._released_by_ids)

        with mgr2._lock:
            mgr2._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        with mock.patch.object(mgr2, "_attach_by_id") as attach_by_id:
            mgr2.bootstrap_attach()
            time.sleep(0.1)
        attach_by_id.assert_not_called()
        self.assertEqual(s2.state, "RELEASED")
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestReleasedPersistence -x`
Expected: FAIL（重啟後 state 非 RELEASED / `by_id` 不在 `_released_by_ids`）

- [ ] **Step 3: 實作**

(a) `_save_state` 內，組 dump dict 時加入 `released`：

```python
    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        released: dict[str, dict[str, str | None]] = {}
        for sid, s in self._sessions.items():
            if s.state == "RELEASED":
                released[sid] = {
                    "by_id": s.profile.device_by_id,
                    "released_by": s.released_by,
                    "released_at": s.released_at,
                    "reason": s.released_reason,
                }
        with open(STATE_PATH, "w", encoding="utf-8") as fp:
            json.dump(
                {"aliases": self._aliases.dump(), "bindings": dict(self._binding_overrides), "released": released},
                fp, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            fp.write("\n")
```

(b) `_load_state` 內，解析 bindings 之後加入：

```python
        released = obj.get("released") if isinstance(obj, dict) else None
        if isinstance(released, dict):
            loaded: dict[str, dict[str, str | None]] = {}
            for sid, meta in released.items():
                if not isinstance(sid, str) or not isinstance(meta, dict):
                    continue
                by_id = meta.get("by_id")
                loaded[sid] = {
                    "by_id": by_id,
                    "released_by": meta.get("released_by"),
                    "released_at": meta.get("released_at"),
                    "reason": meta.get("reason"),
                }
                if isinstance(by_id, str) and by_id:
                    self._released_by_ids.add(by_id)
            self._loaded_released = loaded
```

(c) `__init__`，在 profile 建立迴圈（`self._aliases.set_for_session(...)`）之後、`self._save_state()`（~292）之前加入：

```python
        for sid, meta in self._loaded_released.items():
            s = self._sessions.get(sid)
            if s is not None:
                s.state = "RELEASED"
                s.released_by = meta.get("released_by")
                s.released_at = meta.get("released_at")
                s.released_reason = meta.get("reason")
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestReleasedPersistence -x`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add sw_core/session_manager.py tests/test_device_handoff.py
git commit -F - <<'EOF'
feat(device-handoff): released 狀態持久化並於 bootstrap 前還原（跨 daemon 重啟）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 9: `self_test` RELEASED 分支 + `to_public_dict` 欄位

**Files:**
- Modify: `sw_core/session_manager.py`（`self_test` ~1938、`to_public_dict` ~216）
- Test: `tests/test_device_handoff.py`

- [ ] **Step 1: 寫 failing test**

```python
class TestSelfTestReleased(_Base):
    def _released(self, holder_pids):
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
            session.state = "RELEASED"
            session.released_by = "agent:flash"
            session.released_at = "now"
            session.released_reason = "flash CC2674"
        mgr._probe_external_holder = mock.MagicMock(
            return_value={"pids": holder_pids, "holder": (holder_pids[0] if holder_pids else None)}
        )
        return mgr

    def test_self_test_released_with_holder(self) -> None:
        resp = self._released([4321]).self_test("COM0")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["classification"], "RELEASED")
        self.assertEqual(resp["external_holder"], [4321])
        self.assertFalse(resp["reclaimable"])
        self.assertEqual(resp["recommended_action"], "wait_external_flash")
        self.assertEqual(resp["released_by"], "agent:flash")

    def test_self_test_released_reclaimable(self) -> None:
        resp = self._released([]).self_test("COM0")
        self.assertEqual(resp["classification"], "RELEASED")
        self.assertEqual(resp["external_holder"], "none")
        self.assertTrue(resp["reclaimable"])
        self.assertEqual(resp["recommended_action"], "device_attach")

    def test_public_dict_has_released_fields(self) -> None:
        mgr = self._released([])
        pub = mgr.get_session("COM0").to_public_dict()
        self.assertEqual(pub["released_by"], "agent:flash")
        self.assertEqual(pub["released_at"], "now")
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestSelfTestReleased -x`
Expected: FAIL（classification 非 RELEASED / `released_by` 不在 public dict）

- [ ] **Step 3: 實作**

(a) `self_test` 內，`if session is None:` return 之後（~1938，`lease = self._interactive.get(...)` 之前）插入：

```python
            if session.state == "RELEASED":
                device = self._devices.get(session.profile.device_by_id)
                real_path = device.real_path if device is not None else None
                holder = self._probe_external_holder(real_path) if real_path else {"pids": [], "holder": None}
                reclaimable = not holder["pids"]
                return {
                    "ok": True,
                    "classification": "RELEASED",
                    "session": session.to_public_dict(),
                    "released_by": session.released_by,
                    "released_at": session.released_at,
                    "reason": session.released_reason,
                    "external_holder": holder["pids"] if holder["pids"] else "none",
                    "reclaimable": reclaimable,
                    "recommended_action": "device_attach" if reclaimable else "wait_external_flash",
                    **self._lease_context(None),
                }
```

(b) `to_public_dict` 的 return dict 內補兩欄（任一既有欄位之後）：

```python
            "released_by": self.released_by,
            "released_at": self.released_at,
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestSelfTestReleased -x`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add sw_core/session_manager.py tests/test_device_handoff.py
git commit -F - <<'EOF'
feat(device-handoff): self_test 回報 RELEASED 狀態與 reclaimable，public dict 補 released 欄位

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 10: RPC `device.release` / `device.attach`

**Files:**
- Modify: `sw_core/service.py`（`rpc` dispatch，`device.list` ~284 之後）
- Test: `tests/test_device_handoff.py`

- [ ] **Step 1: 寫 failing test**

```python
class TestDeviceRpc(_Base):
    def _service(self):
        from sw_core.service import SerialwrapService
        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
        return SerialwrapService(profiles)

    def test_rpc_release_and_attach_dispatch(self) -> None:
        svc = self._service()
        svc._sessions.release_device = mock.MagicMock(return_value={"ok": True})
        svc._sessions.attach_device = mock.MagicMock(return_value={"ok": True})

        r1 = svc.rpc("device.release", {"selector": "COM0", "source": "agent:x", "reason": "flash"})
        self.assertTrue(r1["ok"])
        svc._sessions.release_device.assert_called_once_with("COM0", source="agent:x", reason="flash")

        r2 = svc.rpc("device.attach", {"selector": "COM0", "force": True})
        self.assertTrue(r2["ok"])
        svc._sessions.attach_device.assert_called_once_with("COM0", force=True)

    def test_rpc_release_requires_selector(self) -> None:
        svc = self._service()
        resp = svc.rpc("device.release", {})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "INVALID_ARGS")
```

> 若 `SerialwrapService(profiles)` 建構參數不符，參考 `sw_core/service.py` `__init__` 與既有 `tests/test_event_rpc.py` 的建構方式調整 `_service()`。

- [ ] **Step 2: 跑測試確認 fail**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestDeviceRpc -x`
Expected: FAIL（`device.release` 未處理 → 落到 method-not-found 預設）

- [ ] **Step 3: 實作**

`service.py` `rpc` 內 `device.list`（~285）之後插入：

```python
        if method == "device.release":
            selector = str(params.get("selector") or "")
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            source = str(params.get("source") or "cli")
            reason = params.get("reason")
            return self._sessions.release_device(selector, source=source, reason=reason)

        if method == "device.attach":
            selector = str(params.get("selector") or "")
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.attach_device(selector, force=bool(params.get("force")))
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestDeviceRpc -x`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add sw_core/service.py tests/test_device_handoff.py
git commit -F - <<'EOF'
feat(device-handoff): RPC 新增 device.release / device.attach

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 11: CLI `device release` / `device attach`

**Files:**
- Modify: `sw_core/cli.py`（subparser ~273、dispatch ~465）
- Test: `tests/test_device_handoff.py`

- [ ] **Step 1: 寫 failing test（argparse 解析）**

```python
class TestDeviceCli(_Base):
    def test_cli_parses_release_and_attach(self) -> None:
        from sw_core.cli import build_parser
        parser = build_parser()
        a = parser.parse_args(["device", "release", "--selector", "COM0", "--source", "agent:x", "--reason", "flash"])
        self.assertEqual(a.device_cmd, "release")
        self.assertEqual(a.selector, "COM0")
        self.assertEqual(a.source, "agent:x")
        self.assertEqual(a.reason, "flash")
        b = parser.parse_args(["device", "attach", "--selector", "COM0", "--force"])
        self.assertEqual(b.device_cmd, "attach")
        self.assertTrue(b.force)
        c = parser.parse_args(["device", "list"])
        self.assertEqual(c.device_cmd, "list")
```

> 確認 CLI 的 parser 建構函式名稱（`build_parser` 或類似）；若不同，依 `sw_core/cli.py` 實際名稱調整 import。

- [ ] **Step 2: 跑測試確認 fail**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestDeviceCli -x`
Expected: FAIL（`device` 無 `release`/`attach` subcommand）

- [ ] **Step 3: 實作**

(a) 把 `sub.add_parser("device")...add_parser("list")`（~273）改寫為：

```python
    p_device = sub.add_parser("device")
    device_sub = p_device.add_subparsers(dest="device_cmd", required=True)
    device_sub.add_parser("list")
    p_drel = device_sub.add_parser("release")
    p_drel.add_argument("--selector", required=True)
    p_drel.add_argument("--source", default="cli")
    p_drel.add_argument("--reason", default=None)
    p_datt = device_sub.add_parser("attach")
    p_datt.add_argument("--selector", required=True)
    p_datt.add_argument("--force", action="store_true")
```

(b) dispatch（`if args.cmd == "device" and args.device_cmd == "list":` ~465）改為：

```python
    if args.cmd == "device":
        if args.device_cmd == "list":
            return _run_rpc(args, "device.list", {})
        if args.device_cmd == "release":
            return _run_rpc(args, "device.release", {"selector": args.selector, "source": args.source, "reason": args.reason})
        if args.device_cmd == "attach":
            return _run_rpc(args, "device.attach", {"selector": args.selector, "force": args.force})
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestDeviceCli -x`
Expected: PASS

- [ ] **Step 5: commit**

```bash
git add sw_core/cli.py tests/test_device_handoff.py
git commit -F - <<'EOF'
feat(device-handoff): CLI 新增 device release / device attach

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 12: 對抗測試（adversarial）

**Files:**
- Test: `tests/test_device_handoff.py`（新增 `TestAdversarial`）

- [ ] **Step 1: 寫對抗測試**

```python
class TestAdversarial(_Base):
    def test_update_devices_does_not_steal_released(self) -> None:
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock(); bridge.stop.return_value = None; bridge.list_consoles.return_value = []
        session.bridge = bridge; session.state = "READY"
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        mgr.release_device("COM0")
        with mock.patch.object(mgr, "_attach_by_id") as attach_by_id:
            # USB realpath 變動（重插）
            mgr.update_devices({by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB3")})
            time.sleep(0.1)
        attach_by_id.assert_not_called()
        self.assertEqual(session.state, "RELEASED")

    def test_concurrent_clear_and_attach_keep_invariant(self) -> None:
        import threading
        by_id = "/dev/serial/by-id/orig"
        mgr = self._mgr([self._make_profile("p", "COM0", "lab+1", by_id)])
        session = mgr.get_session("COM0")
        bridge = mock.MagicMock(); bridge.stop.return_value = None; bridge.list_consoles.return_value = []
        session.bridge = bridge; session.state = "READY"
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyUSB0")}
        mgr.release_device("COM0")
        with mock.patch.object(mgr, "_spawn_attach"):
            threads = [threading.Thread(target=mgr.clear_session, args=("COM0",)) for _ in range(8)]
            for t in threads: t.start()
            for t in threads: t.join()
        # released 不變、集合不漂移
        self.assertEqual(session.state, "RELEASED")
        self.assertIn(by_id, mgr._released_by_ids)
```

- [ ] **Step 2: 跑測試**

Run: `python3 -m pytest -q tests/test_device_handoff.py::TestAdversarial -x`
Expected: PASS（若 fail 表示 guard 有缺口，回前面對應 task 修正）

- [ ] **Step 3: commit**

```bash
git add tests/test_device_handoff.py
git commit -F - <<'EOF'
test(device-handoff): 對抗測試（USB 重插/並發 clear 不破壞 released invariant）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 13: 文件、CHANGELOG、全套測試 + policy_check

**Files:**
- Modify: `README.md`、`CHANGELOG.md`

- [ ] **Step 1: README 補用法**

在 README 適當 Usage 段落新增 device handoff 流程：

```markdown
### MCU 韌體升級：device handoff

serialwrap 持有 UART 時，外部 flasher（如 `ocp-mcu-upgrade`）無法獨佔 raw device。
先把裝置交出去、燒完再收回：

​```bash
serialwrap device release --selector COM0 --source agent:flash --reason "flash CC2674"
# serialwrap 關閉該 UART、清空 console，且不會自動搶回
ocp-mcu-upgrade -d /dev/ttyUSB1 -b 115200 -t 8 -e -s -i fw.bin
serialwrap device attach --selector COM0   # 收回；外部仍持有時回 DEVICE_STILL_HELD，--force 可強制
​```

`serialwrap session self-test --selector COM0` 在 RELEASED 下會回 `external_holder` /
`reclaimable` / `recommended_action`（`wait_external_flash` 或 `device_attach`）。
```

- [ ] **Step 2: CHANGELOG 補實作條目**

在 `[Unreleased]` `### Added` 新增：

```markdown
- `device release` / `device attach`（RPC `device.release` / `device.attach`）：把單一 session 綁定的 UART 乾淨交給外部 flasher 並可手動收回（#54）；新增 `RELEASED` 狀態、`_spawn_attach` released guard、跨 daemon 重啟持久化、`self_test` 的 `external_holder`/`reclaimable` 標註與 `device attach` 安全 guard（`DEVICE_STILL_HELD`，`--force` 可略過）
```

- [ ] **Step 3: 全套測試**

Run: `python3 -m pytest -q tests/`
Expected: 全綠，僅 `test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict`（pre-existing）可失敗；不得有其他新失敗。

- [ ] **Step 4: policy check**

Run: `python3 -m policy_check --repo .`
Expected: PASS（如未安裝，先依 CLAUDE.md 安裝命令裝 pinned SHA 版本）

- [ ] **Step 5: commit**

```bash
git add README.md CHANGELOG.md
git commit -F - <<'EOF'
docs(device-handoff): README 補 device release/attach 用法、CHANGELOG 記錄實作

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Task 14: 實機測試（real-machine, PRE-PR HARD GATE）

> ⚠️ 需 user 確認硬體就緒、測試件可安全反覆燒錄並給 go。未通過不得上 PR。非自動化步驟。

- [ ] **Step 1: 接好 FTDI + CC2674，daemon 啟動並讓 serialwrap attach 該 FTDI（COM0）**
- [ ] **Step 2: `serialwrap device release --selector COM0 --reason "flash CC2674"`**，確認 `lsof /dev/ttyUSB1` 已無 serialwrapd。
- [ ] **Step 3: 外部燒錄** `ocp-mcu-upgrade -d /dev/ttyUSB1 -b 115200 -t 8 -e -s -i fw.bin`，期望 `Return error code : 0x0`。
- [ ] **Step 4: `serialwrap device attach --selector COM0`**，確認回到 ATTACHED/READY、console/command 恢復。
- [ ] **Step 5: 記錄結果**（evidence）作為 PR 佐證。

---

## Self-Review（撰寫後自查，已執行）

- **Spec coverage**：device-handoff spec 六條 requirement 全部對應 Task 2–11；session-selftest delta 對應 Task 9。✓
- **Placeholder scan**：無 TBD/TODO；每個 code step 皆含完整程式碼。✓
- **Type consistency**：`release_device(selector, *, source, reason)` / `attach_device(selector, *, force)` / `_probe_external_holder(real_path, *, _proc_root)` / `_detach_session_locked(..., drop_consoles=False)` 在 RPC（Task 10）、CLI（Task 11）、測試各處簽名一致；`_released_by_ids`、`released_by/at/reason` 欄位命名一致。✓
- **兩處外部簽名已確認**：`SerialwrapService.__init__(profiles, *, templates=None, max_sessions=16)`（`service.py:117`）→ `SerialwrapService(profiles)` 可用；CLI parser 函式為 `build_parser()`（`cli.py:244`）。Task 10/11 引用正確，無需現場微調。
