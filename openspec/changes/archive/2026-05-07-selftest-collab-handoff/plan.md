# self_test Collaborative Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `session.self_test` evaluate full readiness (and probe) even when a human console lease is active, while still exposing an opt-in `strict_human_lock` for callers that need a hard human lock.

**Architecture:** Modify `SessionManager.self_test` to (a) compute lease context once and inject `interactive_owner` / `human_attached` into every return, (b) drop the human-lease short-circuit by default but keep it gated behind `strict_human_lock=True`, (c) wrap the `ready_probe` block with `bridge.suspend_interactive()` / `resume_interactive()` whenever a human lease is held, mirroring the pattern already used in `_execute_command` / `file_push` / `file_pull`. Pass the flag through `service.py` RPC and `cli.py` arg parsing. Update tests, docs, and the openspec change tracker.

**Tech Stack:** Python 3.11/3.12, stdlib `unittest`, existing `sw_core` modules. No new deps.

---

## File Structure

- Modify: `sw_core/session_manager.py` — `self_test` body + signature (lines ~1651–1768).
- Modify: `sw_core/service.py` — `session.self_test` RPC handler (lines ~289–294).
- Modify: `sw_core/cli.py` — `session self-test` subparser (lines ~285–287) and dispatch (line ~465).
- Modify: `tests/test_session_bind.py` — rename existing case + add 4 new cases (around line ~247).
- Modify: `docs/serialwrap-spec.md` — §9.1 self_test section (lines ~376–402).
- Modify (conditional): `sw_mcp/` — only if `session_self_test` tool description references the old contract.
- Update: `openspec/changes/selftest-collab-handoff/tasks.md` — tick off as we go.

No new files; everything lives in existing modules.

---

## Task 1: Baseline verification

**Files:**
- Verify: `tests/test_session_bind.py` (no edit yet)

- [ ] **Step 1: Confirm we are on the feature branch**

```bash
git rev-parse --abbrev-ref HEAD
```
Expected: `fix/selftest-collab-42`

- [ ] **Step 2: Run the existing test suite to capture green baseline**

```bash
cd /home/paul_chen/prj_pri/serialwrap
python -m pytest tests/test_session_bind.py -v
```
Expected: all pass, including `test_self_test_reports_human_interactive_active`. Save the count to compare later.

---

## Task 2: Add lease-context helper and inject into all existing returns (no behavior change yet)

**Files:**
- Modify: `sw_core/session_manager.py:1651-1768`

- [ ] **Step 1: Write failing test that asserts `interactive_owner` / `human_attached` appear in the OK return**

Add to `tests/test_session_bind.py` at the end of the same `TestCase` class as `test_self_test_reports_human_interactive_active`:

```python
def test_self_test_ok_result_carries_lease_context_when_no_lease(self) -> None:
    from sw_core.device_watcher import DeviceInfo
    import unittest.mock as mock

    profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
    mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)
    session = mgr.get_session("COM0")
    assert session is not None

    bridge = mock.MagicMock()
    bridge.snapshot.return_value = {
        "running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9",
    }
    bridge.rx_snapshot_len.return_value = 0
    bridge.wait_for_regex_from.return_value = True
    session.bridge = bridge
    session.state = "READY"
    session.attached_real_path = "/dev/ttyUSB0"
    with mgr._lock:
        mgr._devices = {"/dev/serial/by-id/orig": DeviceInfo(by_id="/dev/serial/by-id/orig", real_path="/dev/ttyUSB0")}

    resp = mgr.self_test("COM0")

    self.assertEqual(resp["classification"], "OK")
    self.assertIn("interactive_owner", resp)
    self.assertIn("human_attached", resp)
    self.assertIsNone(resp["interactive_owner"])
    self.assertFalse(resp["human_attached"])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_session_bind.py::TestSessionBind::test_self_test_ok_result_carries_lease_context_when_no_lease -v
```
Expected: FAIL with `KeyError: 'interactive_owner'` or `AssertionError: 'interactive_owner' not in ...`.

(Note: actual `TestCase` class name may differ — adjust prefix accordingly, e.g. `TestSessionManager`. Check existing class via `grep -n "^class.*TestCase" tests/test_session_bind.py`.)

- [ ] **Step 3: Add helper inside `SessionManager` class (above `self_test`)**

In `sw_core/session_manager.py`, add a method just before `def self_test`:

```python
    @staticmethod
    def _lease_context(lease: "InteractiveLease | None") -> dict[str, Any]:
        if lease is None:
            return {"interactive_owner": None, "human_attached": False}
        owner = lease.owner
        return {
            "interactive_owner": owner,
            "human_attached": owner.startswith("human:"),
        }
```

- [ ] **Step 4: Refactor `self_test` to compute lease context once and merge into every return**

Replace the body of `self_test` (lines ~1651–1768) so the lease is fetched right after the `recovering` check and the helper output is spread into every dict. Keep the existing short-circuit logic intact for now (we'll change behavior in Task 3). Concretely:

```python
    def self_test(self, selector: str, *, timeout_s: float = 2.0) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            device = self._devices.get(session.profile.device_by_id)
            attached_real_path = session.attached_real_path
            bridge = session.bridge
            if session.recovering:
                return {
                    "ok": True,
                    "classification": "SESSION_RECOVERING",
                    "session": session.to_public_dict(),
                    "recommended_action": "wait",
                    **self._lease_context(self._refresh_interactive_locked(session)),
                }
            lease = self._refresh_interactive_locked(session)
            lease_ctx = self._lease_context(lease)
            if lease is not None and lease.owner.startswith("human:"):
                return {
                    "ok": True,
                    "classification": "HUMAN_INTERACTIVE_ACTIVE",
                    "interactive_id": lease.interactive_id,
                    "session": session.to_public_dict(),
                    "recommended_action": "wait_or_detach_console",
                    **lease_ctx,
                }
            if device is None:
                return {
                    "ok": True,
                    "classification": "DEVICE_MISSING",
                    "session": session.to_public_dict(),
                    "recommended_action": "check_cable_or_bind",
                    **lease_ctx,
                }
            if attached_real_path and attached_real_path != device.real_path:
                return {
                    "ok": True,
                    "classification": "DEVICE_REBOUND_REQUIRED",
                    "session": session.to_public_dict(),
                    "attached_real_path": attached_real_path,
                    "current_real_path": device.real_path,
                    "recommended_action": "reattach",
                    **lease_ctx,
                }
            if bridge is None:
                return {
                    "ok": True,
                    "classification": "BRIDGE_DOWN",
                    "session": session.to_public_dict(),
                    "current_real_path": device.real_path,
                    "recommended_action": "attach",
                    **lease_ctx,
                }
            snapshot = bridge.snapshot()
            if not snapshot.get("running") or not snapshot.get("serial_alive"):
                return {
                    "ok": True,
                    "classification": "BRIDGE_DOWN",
                    "session": session.to_public_dict(),
                    "current_real_path": device.real_path,
                    "recommended_action": "recover",
                    **lease_ctx,
                }
            if not snapshot.get("vtty_alive"):
                return {
                    "ok": True,
                    "classification": "VTTY_STALE",
                    "session": session.to_public_dict(),
                    "attached_vtty": snapshot.get("vtty"),
                    "recommended_action": "console_attach",
                    **lease_ctx,
                }
            if session.state == "ATTACHED":
                if session.profile.platform == "passthrough":
                    classification = "PASSTHROUGH"
                    recommended_action = "console_attach"
                elif session.last_error == "LOGIN_REQUIRED":
                    classification = "LOGIN_REQUIRED"
                    recommended_action = "console_attach"
                elif session.last_error == "REBOOTING":
                    classification = "REBOOTING"
                    recommended_action = "wait_or_console_attach"
                else:
                    classification = "ATTACHED_NOT_READY"
                    recommended_action = "console_attach"
                return {
                    "ok": True,
                    "classification": classification,
                    "session": session.to_public_dict(),
                    "attached_real_path": attached_real_path,
                    "current_real_path": device.real_path,
                    "attached_vtty": snapshot.get("vtty"),
                    "bridge_generation": session.bridge_generation,
                    "recommended_action": recommended_action,
                    **lease_ctx,
                }

            nonce = uuid.uuid4().hex[:8]
            probe = session.profile.ready_probe.replace("${nonce}", nonce)
            offset = bridge.rx_snapshot_len()
            session.last_probe_at = now_iso()
            self._mark_session_tx(session)
            bridge.send_command(probe, source="system:self_test", cmd_id=None)
            if not bridge.wait_for_regex_from(nonce, offset, timeout_s):
                return {
                    "ok": True,
                    "classification": "TARGET_UNRESPONSIVE",
                    "session": session.to_public_dict(),
                    "attached_real_path": attached_real_path,
                    "current_real_path": device.real_path,
                    "probe_ok": False,
                    "recommended_action": "recover",
                    **lease_ctx,
                }
            bridge.wait_for_regex_from(session.profile.prompt_regex, offset, timeout_s)
            return {
                "ok": True,
                "classification": "OK",
                "session": session.to_public_dict(),
                "attached_real_path": attached_real_path,
                "current_real_path": device.real_path,
                "attached_vtty": snapshot.get("vtty"),
                "bridge_generation": session.bridge_generation,
                "probe_ok": True,
                "recommended_action": "none",
                **lease_ctx,
            }
```

- [ ] **Step 5: Run the new test and the original test to verify both pass**

```bash
python -m pytest tests/test_session_bind.py::TestSessionBind::test_self_test_ok_result_carries_lease_context_when_no_lease tests/test_session_bind.py::TestSessionBind::test_self_test_reports_human_interactive_active -v
```
Expected: both PASS. The original test still sees `HUMAN_INTERACTIVE_ACTIVE` because behavior is unchanged; `interactive_owner` is now in there from the helper too (it already was, by coincidence — the test only checks equality so it still passes).

- [ ] **Step 6: Run the full file**

```bash
python -m pytest tests/test_session_bind.py -v
```
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add sw_core/session_manager.py tests/test_session_bind.py
git commit -m "$(cat <<'EOF'
refactor(self_test): inject interactive_owner/human_attached in all returns

No behavior change. Adds _lease_context helper and threads it into every
return path in SessionManager.self_test, so callers get a uniform
interactive_owner / human_attached field across all classifications.

Refs: #42

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Add `strict_human_lock` parameter and remove the default short-circuit

**Files:**
- Modify: `sw_core/session_manager.py` (`self_test` signature + body)
- Modify: `tests/test_session_bind.py`

- [ ] **Step 1: Rewrite the existing test to require `strict_human_lock=True`**

Edit the existing `test_self_test_reports_human_interactive_active` in `tests/test_session_bind.py`:
- Rename to `test_self_test_strict_mode_reports_human_interactive_active`
- Change the call from `mgr.self_test("COM0")` to `mgr.self_test("COM0", strict_human_lock=True)`

```python
    def test_self_test_strict_mode_reports_human_interactive_active(self) -> None:
        # ...same setup as before...
        resp = mgr.self_test("COM0", strict_human_lock=True)

        self.assertTrue(resp["ok"])
        self.assertEqual(resp["classification"], "HUMAN_INTERACTIVE_ACTIVE")
        self.assertEqual(resp["interactive_owner"], "human:cid-2")
        self.assertTrue(resp["human_attached"])
        bridge.send_command.assert_not_called()
```

- [ ] **Step 2: Add new failing test for default mode walking through with human attached**

Add right after the renamed test:

```python
    def test_self_test_default_walks_through_with_human_attached(self) -> None:
        from sw_core.device_watcher import DeviceInfo
        import unittest.mock as mock

        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        session = mgr.get_session("COM0")
        assert session is not None

        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {
            "running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9",
        }
        bridge.rx_snapshot_len.return_value = 0
        bridge.wait_for_regex_from.return_value = True
        session.bridge = bridge
        session.state = "READY"
        session.attached_real_path = "/dev/ttyUSB0"
        lease = InteractiveLease(
            interactive_id="lease-x",
            session_id=session.session_id,
            owner="human:cid-7",
            created_at="now",
            timeout_s=60.0,
        )
        with mgr._lock:
            mgr._devices = {"/dev/serial/by-id/orig": DeviceInfo(by_id="/dev/serial/by-id/orig", real_path="/dev/ttyUSB0")}
            mgr._interactive[lease.interactive_id] = lease
            session.interactive_session_id = lease.interactive_id

        resp = mgr.self_test("COM0")

        self.assertTrue(resp["ok"])
        self.assertEqual(resp["classification"], "OK")
        self.assertTrue(resp["probe_ok"])
        self.assertEqual(resp["recommended_action"], "none")
        self.assertTrue(resp["human_attached"])
        self.assertEqual(resp["interactive_owner"], "human:cid-7")
        bridge.send_command.assert_called_once()
```

- [ ] **Step 3: Run both tests and verify both fail**

```bash
python -m pytest tests/test_session_bind.py::TestSessionBind::test_self_test_strict_mode_reports_human_interactive_active tests/test_session_bind.py::TestSessionBind::test_self_test_default_walks_through_with_human_attached -v
```
Expected:
- `test_self_test_strict_mode_...`: FAIL with `TypeError: self_test() got an unexpected keyword argument 'strict_human_lock'`
- `test_self_test_default_walks_through_with_human_attached`: FAIL because today's behavior still returns `HUMAN_INTERACTIVE_ACTIVE` not `OK`.

- [ ] **Step 4: Modify `self_test` signature and short-circuit gating**

In `sw_core/session_manager.py`, change:

```python
    def self_test(self, selector: str, *, timeout_s: float = 2.0) -> dict[str, Any]:
```
to:
```python
    def self_test(self, selector: str, *, timeout_s: float = 2.0, strict_human_lock: bool = False) -> dict[str, Any]:
```

And change the human-lease branch from:
```python
            if lease is not None and lease.owner.startswith("human:"):
                return {
                    "ok": True,
                    "classification": "HUMAN_INTERACTIVE_ACTIVE",
                    ...
                }
```
to:
```python
            if strict_human_lock and lease is not None and lease.owner.startswith("human:"):
                return {
                    "ok": True,
                    "classification": "HUMAN_INTERACTIVE_ACTIVE",
                    "interactive_id": lease.interactive_id,
                    "session": session.to_public_dict(),
                    "recommended_action": "wait_or_detach_console",
                    **lease_ctx,
                }
```

- [ ] **Step 5: Run both tests and verify both pass**

```bash
python -m pytest tests/test_session_bind.py::TestSessionBind::test_self_test_strict_mode_reports_human_interactive_active tests/test_session_bind.py::TestSessionBind::test_self_test_default_walks_through_with_human_attached -v
```
Expected: both PASS.

- [ ] **Step 6: Run the full file to catch regressions**

```bash
python -m pytest tests/test_session_bind.py -v
```
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add sw_core/session_manager.py tests/test_session_bind.py
git commit -m "$(cat <<'EOF'
feat(self_test): default to full readiness walk; add strict_human_lock opt-in

self_test no longer short-circuits when a human interactive lease exists.
Callers that need the legacy hard-lock behavior can pass
strict_human_lock=True to keep the HUMAN_INTERACTIVE_ACTIVE return.

Refs: #42

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Suspend / resume human interactive around the probe

**Files:**
- Modify: `sw_core/session_manager.py` (probe block at end of `self_test`)
- Modify: `tests/test_session_bind.py`

- [ ] **Step 1: Add failing test that probe path calls suspend then resume in order**

Add to `tests/test_session_bind.py`:

```python
    def test_self_test_default_suspends_human_during_probe(self) -> None:
        from sw_core.device_watcher import DeviceInfo
        import unittest.mock as mock

        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        session = mgr.get_session("COM0")
        assert session is not None

        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {
            "running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9",
        }
        bridge.rx_snapshot_len.return_value = 0
        bridge.wait_for_regex_from.return_value = True
        session.bridge = bridge
        session.state = "READY"
        session.attached_real_path = "/dev/ttyUSB0"

        lease = InteractiveLease(
            interactive_id="lease-z",
            session_id=session.session_id,
            owner="human:cid-9",
            created_at="now",
            timeout_s=60.0,
        )
        with mgr._lock:
            mgr._devices = {"/dev/serial/by-id/orig": DeviceInfo(by_id="/dev/serial/by-id/orig", real_path="/dev/ttyUSB0")}
            mgr._interactive[lease.interactive_id] = lease
            session.interactive_session_id = lease.interactive_id

        order: list[str] = []
        bridge.suspend_interactive.side_effect = lambda: order.append("suspend")
        bridge.resume_interactive.side_effect = lambda: order.append("resume")
        bridge.send_command.side_effect = lambda *a, **kw: order.append("send")

        resp = mgr.self_test("COM0")

        self.assertEqual(resp["classification"], "OK")
        self.assertEqual(order, ["suspend", "send", "resume"])
        bridge.suspend_interactive.assert_called_once_with()
        bridge.resume_interactive.assert_called_once_with()
```

- [ ] **Step 2: Add failing test for resume-runs-on-exception (finally guarantee)**

```python
    def test_self_test_resume_runs_even_if_probe_raises(self) -> None:
        from sw_core.device_watcher import DeviceInfo
        import unittest.mock as mock

        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        session = mgr.get_session("COM0")
        assert session is not None

        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {
            "running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9",
        }
        bridge.rx_snapshot_len.return_value = 0
        bridge.send_command.side_effect = RuntimeError("simulated UART failure")
        session.bridge = bridge
        session.state = "READY"
        session.attached_real_path = "/dev/ttyUSB0"
        lease = InteractiveLease(
            interactive_id="lease-r",
            session_id=session.session_id,
            owner="human:cid-r",
            created_at="now",
            timeout_s=60.0,
        )
        with mgr._lock:
            mgr._devices = {"/dev/serial/by-id/orig": DeviceInfo(by_id="/dev/serial/by-id/orig", real_path="/dev/ttyUSB0")}
            mgr._interactive[lease.interactive_id] = lease
            session.interactive_session_id = lease.interactive_id

        with self.assertRaises(RuntimeError):
            mgr.self_test("COM0")

        bridge.suspend_interactive.assert_called_once_with()
        bridge.resume_interactive.assert_called_once_with()
```

- [ ] **Step 3: Add failing test that agent lease does NOT trigger suspend**

```python
    def test_self_test_no_suspend_when_lease_is_agent(self) -> None:
        from sw_core.device_watcher import DeviceInfo
        import unittest.mock as mock

        profiles = [self._make_profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig")]
        mgr = SessionManager(profiles, WalWriter(wal_dir=self._tmp.name), on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        session = mgr.get_session("COM0")
        assert session is not None

        bridge = mock.MagicMock()
        bridge.snapshot.return_value = {
            "running": True, "serial_alive": True, "vtty_alive": True, "vtty": "/dev/pts/9",
        }
        bridge.rx_snapshot_len.return_value = 0
        bridge.wait_for_regex_from.return_value = True
        session.bridge = bridge
        session.state = "READY"
        session.attached_real_path = "/dev/ttyUSB0"
        lease = InteractiveLease(
            interactive_id="lease-a",
            session_id=session.session_id,
            owner="agent",
            created_at="now",
            timeout_s=60.0,
        )
        with mgr._lock:
            mgr._devices = {"/dev/serial/by-id/orig": DeviceInfo(by_id="/dev/serial/by-id/orig", real_path="/dev/ttyUSB0")}
            mgr._interactive[lease.interactive_id] = lease
            session.interactive_session_id = lease.interactive_id

        resp = mgr.self_test("COM0")

        self.assertEqual(resp["classification"], "OK")
        self.assertFalse(resp["human_attached"])
        bridge.suspend_interactive.assert_not_called()
        bridge.resume_interactive.assert_not_called()
```

- [ ] **Step 4: Run all three tests and verify they fail**

```bash
python -m pytest tests/test_session_bind.py -k "self_test" -v
```
Expected: the three new tests FAIL because suspend/resume isn't called yet (or `RuntimeError` propagates without resume).

- [ ] **Step 5: Wrap the probe block with suspend / try / finally / resume**

In `sw_core/session_manager.py`, replace the probe block (lines that send the probe and wait for nonce/prompt) so it computes a `suspend_human` flag inside the lock, releases the lock, then runs the probe inside try/finally:

The probe block sits **inside** the `with self._lock:` block today. We need to move it outside the lock, mirroring the `_execute_command` pattern. Here's the target shape — replace from `nonce = uuid.uuid4().hex[:8]` (around line 1741) through the final `return {... "classification": "OK" ...}` (around line 1768), and **also un-indent it** so it runs after the lock releases:

```python
            # still inside lock — gather probe inputs
            suspend_human = lease is not None and lease.owner.startswith("human:")
            ready_probe = session.profile.ready_probe
            prompt_regex = session.profile.prompt_regex
            session.last_probe_at = now_iso()
            self._mark_session_tx(session)

        # lock released — run probe outside
        nonce = uuid.uuid4().hex[:8]
        probe = ready_probe.replace("${nonce}", nonce)
        offset = bridge.rx_snapshot_len()

        if suspend_human:
            bridge.suspend_interactive()
        try:
            bridge.send_command(probe, source="system:self_test", cmd_id=None)
            probe_ok = bridge.wait_for_regex_from(nonce, offset, timeout_s)
            if not probe_ok:
                return {
                    "ok": True,
                    "classification": "TARGET_UNRESPONSIVE",
                    "session": session.to_public_dict(),
                    "attached_real_path": attached_real_path,
                    "current_real_path": device.real_path,
                    "probe_ok": False,
                    "recommended_action": "recover",
                    **lease_ctx,
                }
            bridge.wait_for_regex_from(prompt_regex, offset, timeout_s)
            return {
                "ok": True,
                "classification": "OK",
                "session": session.to_public_dict(),
                "attached_real_path": attached_real_path,
                "current_real_path": device.real_path,
                "attached_vtty": snapshot.get("vtty"),
                "bridge_generation": session.bridge_generation,
                "probe_ok": True,
                "recommended_action": "none",
                **lease_ctx,
            }
        finally:
            if suspend_human:
                bridge.resume_interactive()
```

Notes:
- `session.to_public_dict()` is called outside the lock here, matching `_execute_command_inner` style; the existing code already calls `to_public_dict()` outside the lock in command paths so this is consistent.
- `snapshot` was captured inside the lock and is safe to read after.
- All earlier-return branches (DEVICE_MISSING / VTTY_STALE / etc.) remain inside the lock — they don't write to UART so they don't need suspend.

- [ ] **Step 6: Run all self_test-related tests and verify they pass**

```bash
python -m pytest tests/test_session_bind.py -k "self_test" -v
```
Expected: all PASS, including the three new suspend/resume tests, the strict-mode test, the default-walk test, and the lease-context test from Task 2.

- [ ] **Step 7: Run the whole file**

```bash
python -m pytest tests/test_session_bind.py -v
```
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add sw_core/session_manager.py tests/test_session_bind.py
git commit -m "$(cat <<'EOF'
feat(self_test): suspend human interactive during ready_probe

Mirror the suspend/resume pattern used in command.submit / file.push /
file.pull so the self_test probe doesn't collide with human typing on
the console. Probe runs outside the SessionManager lock; resume is in
finally so it always runs even if the probe raises.

Refs: #42

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: RPC handler reads `strict_human_lock`

**Files:**
- Modify: `sw_core/service.py:289-294`

- [ ] **Step 1: Update RPC handler**

Change:
```python
        if method == "session.self_test":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            timeout_s = float(params.get("timeout_s") or 2.0)
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.self_test(selector, timeout_s=timeout_s)
```
to:
```python
        if method == "session.self_test":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            timeout_s = float(params.get("timeout_s") or 2.0)
            strict_human_lock = bool(params.get("strict_human_lock") or False)
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.self_test(selector, timeout_s=timeout_s, strict_human_lock=strict_human_lock)
```

- [ ] **Step 2: Verify with quick smoke test**

```bash
python -c "
from sw_core.service import SerialwrapService
import inspect
src = inspect.getsource(SerialwrapService.rpc)
assert 'strict_human_lock' in src, 'strict_human_lock not wired'
print('OK')
"
```
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add sw_core/service.py
git commit -m "$(cat <<'EOF'
feat(self_test): plumb strict_human_lock through session.self_test RPC

Reads strict_human_lock from RPC params and forwards to SessionManager.

Refs: #42

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: CLI `--strict-human-lock` flag

**Files:**
- Modify: `sw_core/cli.py:285-287` (subparser) and line ~465 (dispatch)

- [ ] **Step 1: Add flag to subparser**

In `sw_core/cli.py`, change:
```python
    p_sst = sess_sub.add_parser("self-test")
    p_sst.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sst.add_argument("--probe-timeout", dest="probe_timeout_s", type=float, default=2.0)
```
to:
```python
    p_sst = sess_sub.add_parser("self-test")
    p_sst.add_argument("--selector", required=True, help="session_id | COMx | alias")
    p_sst.add_argument("--probe-timeout", dest="probe_timeout_s", type=float, default=2.0)
    p_sst.add_argument(
        "--strict-human-lock",
        dest="strict_human_lock",
        action="store_true",
        help="legacy mode: return HUMAN_INTERACTIVE_ACTIVE when a human console lease is active "
             "(default: walk full readiness check and suspend/resume around probe)",
    )
```

- [ ] **Step 2: Update dispatch**

Change line ~465:
```python
        if args.session_cmd == "self-test":
            return _run_rpc(args, "session.self_test", {"selector": args.selector, "timeout_s": args.probe_timeout_s})
```
to:
```python
        if args.session_cmd == "self-test":
            return _run_rpc(args, "session.self_test", {
                "selector": args.selector,
                "timeout_s": args.probe_timeout_s,
                "strict_human_lock": getattr(args, "strict_human_lock", False),
            })
```

- [ ] **Step 3: Smoke test the CLI**

```bash
python -c "
import sys
sys.argv = ['serialwrap', 'session', 'self-test', '--help']
from sw_core.cli import main
try: main()
except SystemExit: pass
" 2>&1 | grep -E "strict-human-lock|--probe-timeout"
```
Expected: both options listed in help output.

- [ ] **Step 4: Commit**

```bash
git add sw_core/cli.py
git commit -m "$(cat <<'EOF'
feat(self_test): add --strict-human-lock CLI flag

CLI flag opts into the legacy HUMAN_INTERACTIVE_ACTIVE short-circuit.

Refs: #42

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: MCP tool description sync (conditional)

**Files:**
- Inspect: `sw_mcp/`

- [ ] **Step 1: Look for self_test references in MCP tool descriptions**

```bash
grep -rn "self_test\|self-test\|HUMAN_INTERACTIVE_ACTIVE\|wait_or_detach_console" sw_mcp/ 2>&1 | grep -v __pycache__
```
- If no matches: skip rest of this task and tick the boxes.
- If matches: continue.

- [ ] **Step 2: For each match, update the description / tool schema**

For each file with a hit:
- If a tool description lists classifications: remove `HUMAN_INTERACTIVE_ACTIVE` from the default-mode list, or annotate it as strict-only.
- If a tool schema declares params: add `strict_human_lock: bool = False`.
- If a tool description mentions `interactive_owner` / `human_attached`: leave as is or note new fields.

(Edits are mechanical; no test coverage required at MCP layer for this change.)

- [ ] **Step 3: Commit (only if files were touched)**

```bash
git add sw_mcp/
git commit -m "$(cat <<'EOF'
docs(mcp): align session_self_test tool description with strict_human_lock

Refs: #42

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Update `docs/serialwrap-spec.md` §9.1

**Files:**
- Modify: `docs/serialwrap-spec.md:376-402`

- [ ] **Step 1: Update §9.1 in place**

Replace the §9.1 block (lines ~376–402) with the following:

```markdown
## 9. self_test 與 recover

### 9.1 `session.self_test`

輸入：

- `selector`（必填）：session_id | COMx | alias
- `timeout_s`（預設 `2.0`）：probe wait 秒數
- `strict_human_lock`（預設 `false`）：當為 `true` 且 session 有 human interactive lease 時，立即回 `HUMAN_INTERACTIVE_ACTIVE` 不走後續探測（保留舊行為）

輸出分類（預設模式）：

- `OK`
- `SESSION_RECOVERING`
- `DEVICE_MISSING`
- `DEVICE_REBOUND_REQUIRED`
- `BRIDGE_DOWN`
- `VTTY_STALE`
- `TARGET_UNRESPONSIVE`
- `LOGIN_REQUIRED`
- `ATTACHED_NOT_READY`
- `REBOOTING`
- `PASSTHROUGH`

僅 `strict_human_lock=true` 才會出現：

- `HUMAN_INTERACTIVE_ACTIVE`（recommended_action：`wait_or_detach_console`）

每個 result 額外帶：

- `interactive_owner: string | null` — 若 lease 存在則為 owner 字串（例：`"human:abcd1234"`、`"agent"`），無 lease 為 `null`
- `human_attached: boolean` — owner 是否以 `"human:"` 開頭

判斷順序：

1. session 是否存在
2. 是否處於 recovering
3. （strict_human_lock 模式才檢查）lease 是否為 human
4. by-id 是否仍存在
5. `attached_real_path` 是否與目前 `real_path` 一致
6. bridge / vtty 是否存活
7. session 是否為 ATTACHED 子分類
8. 執行安全 probe

安全 probe 目前使用 profile 的 `ready_probe`。

#### 9.1.1 Collaborative monitoring

預設模式下，self_test 與 `command.submit` / `file.push` / `file.pull` 採同一 collaborative pattern：

- session 有 human attach console 時，readiness 檢查仍走完整流程。
- 走到 probe 階段時，若 lease owner 以 `"human:"` 開頭，self_test 在 `bridge.send_command(probe, ...)` 前後分別呼叫 `bridge.suspend_interactive()` / `resume_interactive()`，期間 human 即時輸入會累積到 deferred buffer，probe 結束後 flush 出去。
- agent lease 或無 lease 時，不觸發 suspend/resume。
- 早期 return 路徑（`DEVICE_MISSING` / `BRIDGE_DOWN` / `VTTY_STALE` / `ATTACHED_*`）不會寫 UART，也不觸發 suspend/resume。

呼叫者要強制鎖定 human session（例如 firmware flash 進行中）時，傳 `strict_human_lock=true` 取得早期 return。
```

- [ ] **Step 2: Sanity-check rendering**

```bash
grep -nA 3 "9.1 \`session.self_test\`" docs/serialwrap-spec.md | head -40
```
Expected: shows the new heading and first few inputs.

- [ ] **Step 3: Commit**

```bash
git add docs/serialwrap-spec.md
git commit -m "$(cat <<'EOF'
docs(self_test): document collaborative monitoring + strict_human_lock

Updates spec §9.1 to describe the default full-walk behavior, the
strict_human_lock opt-in, the new interactive_owner / human_attached
output fields, and the suspend/resume contract during ready_probe.

Refs: #42

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Tick off `tasks.md` and run full suite

**Files:**
- Modify: `openspec/changes/selftest-collab-handoff/tasks.md`

- [ ] **Step 1: Run the entire test suite**

```bash
python -m pytest tests/ -q
```
Expected: all PASS (no regressions outside our touched area).

- [ ] **Step 2: Mark completed checkboxes in tasks.md**

Edit `openspec/changes/selftest-collab-handoff/tasks.md`: change `- [ ]` to `- [x]` for groups 2, 3, 4, 5 (and group 1 except 1.2 already done implicitly). Leave group 6 (functional verification) and 7 (PR) unchecked until after Task 10/11.

- [ ] **Step 3: Commit the tasks.md update**

```bash
git add openspec/changes/selftest-collab-handoff/tasks.md
git commit -m "$(cat <<'EOF'
chore(openspec): tick implemented tasks for selftest-collab-handoff

Refs: #42

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Functional verification with a live daemon

**Files:**
- (none — manual verification)

- [ ] **Step 1: Make sure the dev daemon is running our branch's code**

```bash
serialwrap daemon status
```
If the daemon's `pid` was started before Task 9's commits, restart:
```bash
serialwrap daemon stop
serialwrap daemon start
```
Expected: `serialwrap daemon status` shows the new PID.

- [ ] **Step 2: Bind a session and attach a human console**

```bash
serialwrap session bind --selector COM0 --device-by-id $(ls /dev/serial/by-id/* | head -1)
serialwrap session attach --selector COM0
serialwrap session console-attach --selector COM0 --label manual-test
```
Expected: console attach succeeds and prints a `client_id`.

- [ ] **Step 3: Run self_test in default mode**

```bash
serialwrap session self-test --selector COM0 --probe-timeout 5
```
Expected JSON includes `"classification": "OK"`, `"human_attached": true`, `"interactive_owner": "human:..."`. Should NOT include `HUMAN_INTERACTIVE_ACTIVE`.

- [ ] **Step 4: Run self_test in strict mode**

```bash
serialwrap session self-test --selector COM0 --probe-timeout 5 --strict-human-lock
```
Expected JSON includes `"classification": "HUMAN_INTERACTIVE_ACTIVE"` and `"recommended_action": "wait_or_detach_console"`.

- [ ] **Step 5: Detach console and re-run**

```bash
serialwrap session console-detach --selector COM0 --client-id <id-from-step-2>
serialwrap session self-test --selector COM0 --probe-timeout 5
```
Expected: `"classification": "OK"`, `"human_attached": false`, `"interactive_owner": null`.

If any of steps 3–5 don't match expectations, do NOT proceed to Task 11. Instead, re-open the relevant earlier task and fix.

---

## Task 11: Push and open PR

**Files:**
- (none in repo)

- [ ] **Step 1: Push the branch**

```bash
git push -u origin fix/selftest-collab-42
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "fix(self_test): allow agent handoff while human monitors console (#42)" --body "$(cat <<'EOF'
## Summary
- Removes the default short-circuit that returned `HUMAN_INTERACTIVE_ACTIVE` whenever a human console lease was active.
- Adds `strict_human_lock` opt-in (RPC param + `--strict-human-lock` CLI flag) to keep the legacy hard-lock semantics for callers that need them.
- Adds `interactive_owner` and `human_attached` to every `session.self_test` result so callers always know whether a human is attached.
- Wraps `ready_probe` with `bridge.suspend_interactive()` / `resume_interactive()` whenever a human lease is held, matching the existing pattern in `command.submit` / `file.push` / `file.pull`.
- OpenSpec change: `openspec/changes/selftest-collab-handoff/`. Spec doc §9.1 updated.

Fixes #42.

## Test plan
- [x] `python -m pytest tests/test_session_bind.py -v`
- [x] `python -m pytest tests/ -q` (no regressions)
- [x] Manual: `session console-attach` + `session self-test` (default → OK; `--strict-human-lock` → HUMAN_INTERACTIVE_ACTIVE)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Expected: PR URL printed.

- [ ] **Step 3: Tick PR-related checkboxes in `openspec/changes/selftest-collab-handoff/tasks.md` (group 7) and commit**

```bash
git add openspec/changes/selftest-collab-handoff/tasks.md
git commit -m "chore(openspec): mark PR opened for selftest-collab-handoff

Refs: #42

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
git push
```

---

## Self-Review Notes

- **Spec coverage**: every Requirement in `specs/session-selftest/spec.md` maps to a task — Requirement 1 (full readiness walk default) → Task 3; Requirement 2 (lease context fields in every return) → Task 2 + Task 4 (probe paths); Requirement 3 (suspend/resume during probe) → Task 4; Requirement 4 (strict_human_lock opt-in) → Task 3 (impl) + Task 5 (RPC) + Task 6 (CLI).
- **Placeholder scan**: no TBD / TODO / "fill in later" in any step; all code blocks are concrete.
- **Type consistency**: `_lease_context` returns `dict[str, Any]` shaped `{"interactive_owner": str | None, "human_attached": bool}`; spread (`**lease_ctx`) used uniformly; `strict_human_lock` keyword stays consistent across SessionManager / RPC / CLI.
- **Class name caveat**: tests assume `TestSessionBind` test class; if the actual class differs, Step 2 of Task 2 already notes how to grep for it.
- **Lock-order caveat**: Task 4 explicitly notes the probe block must run outside `self._lock` and mirrors `_execute_command`'s pattern.
