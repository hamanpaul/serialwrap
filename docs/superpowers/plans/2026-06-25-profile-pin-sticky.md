# Profile pin/sticky 持久化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓動態偵測 session 的 profile 可持久化（手動 `pin` + 自動 `sticky`），根治 daemon 重啟後的 profile 漂移。

**Architecture:** 在 `state.json` 新增 `profile_pins`/`profile_detected` 兩個 device_key→profile map；`_attach_by_id_dynamic` 改四層優先序（pin > sticky > detect > fallback）；sticky 只在 `detected` 來源 session 達 READY 後寫入（杜絕 TOCTOU 與 false positive）；新增 `SessionRuntime.profile_source` 欄位兼作顯示／sticky 判斷／explicit 判斷。

**Tech Stack:** Python 3.10+、unittest、既有 `sw_core/{session_manager,service,cli,constants}.py`。對應 issue #95、OpenSpec change `profile-pin-sticky`、設計 `docs/superpowers/specs/2026-06-25-profile-pin-sticky-design.md` v2。

---

## File Structure

- Modify `sw_core/session_manager.py`：`SessionRuntime.profile_source` 欄位 + `to_public_dict`；`__init__` 初始化兩 map + YAML session 標 `yaml-target`；`_load_state`/`_save_state` 納入兩 map；`_template_by_name()`；`_attach_by_id_dynamic` 優先序重構 + READY-gated sticky 寫入；`pin_session()`/`unpin_session()`。
- Modify `sw_core/service.py`：`session.pin`/`session.unpin` RPC 分支。
- Modify `sw_core/cli.py`：`session pin`/`unpin` subparser + dispatch。
- Create `tests/test_profile_pin_sticky.py`：全部單元/整合測試。
- Modify `README.md`、`docs/serialwrap-spec.md`、`CHANGELOG.md`。

測試隔離沿用 `tests/test_session_bind.py`：`setUp` 以 `tempfile` + monkeypatch `sm_mod.STATE_PATH`。

---

## Task 1: profile_source 欄位 + 兩 map 持久化 + yaml-target provenance

**Files:**
- Modify: `sw_core/session_manager.py`（`SessionRuntime` 欄位 ~:224、`to_public_dict` ~:282、`__init__` :357/:368、`_load_state` :437、`_save_state` :451）
- Test: `tests/test_profile_pin_sticky.py`

- [ ] **Step 1: 寫失敗測試（持久化 + 向後相容 + yaml-target）**

建立 `tests/test_profile_pin_sticky.py`：

```python
import json
import tempfile
import unittest
from pathlib import Path

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager
import sw_core.session_manager as sm_mod
from sw_core.wal import WalWriter


def _profile(name="prpl-template", com="COM0", alias="prpl+1",
             by_id="/dev/serial/by-id/usb-FTDI_A-if00-port0", platform="prpl"):
    return SessionProfile(profile_name=name, com=com, act_no=1, alias=alias,
                          device_by_id=by_id, platform=platform, uart=UartProfile())


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(lambda: setattr(sm_mod, "STATE_PATH", self._old))

    def _mgr(self, profiles):
        return SessionManager(profiles, WalWriter(wal_dir=self._tmp.name),
                              on_ready=lambda _sid: None, on_detached=lambda _sid: None)


class TestPersistence(_Base):
    def test_backward_compat_load_without_new_keys(self):
        Path(sm_mod.STATE_PATH).write_text(
            json.dumps({"aliases": {}, "bindings": {}, "released": {}}), encoding="utf-8")
        mgr = self._mgr([_profile()])
        self.assertEqual(mgr._profile_pins, {})
        self.assertEqual(mgr._profile_detected, {})

    def test_pins_persist_across_restart(self):
        mgr = self._mgr([_profile()])
        mgr._profile_pins["/dev/serial/by-id/x"] = "prpl-template"
        mgr._save_state()
        mgr2 = self._mgr([_profile()])
        self.assertEqual(mgr2._profile_pins, {"/dev/serial/by-id/x": "prpl-template"})

    def test_init_save_does_not_wipe_new_keys(self):
        Path(sm_mod.STATE_PATH).write_text(json.dumps({
            "aliases": {}, "bindings": {}, "released": {},
            "profile_pins": {"/dev/serial/by-id/x": "prpl-template"},
            "profile_detected": {"/dev/serial/by-id/y": "op3-template"},
        }), encoding="utf-8")
        self._mgr([_profile()])  # __init__ 尾段會 _save_state()
        on_disk = json.loads(Path(sm_mod.STATE_PATH).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["profile_pins"], {"/dev/serial/by-id/x": "prpl-template"})
        self.assertEqual(on_disk["profile_detected"], {"/dev/serial/by-id/y": "op3-template"})

    def test_yaml_target_session_profile_source(self):
        mgr = self._mgr([_profile()])
        sess = mgr._sessions["prpl-template:COM0"]
        self.assertEqual(sess.profile_source, "yaml-target")
        self.assertEqual(sess.to_public_dict()["profile_source"], "yaml-target")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_profile_pin_sticky.py::TestPersistence -v`
Expected: FAIL（`AttributeError: 'SessionManager' object has no attribute '_profile_pins'` / `profile_source`）

- [ ] **Step 3: 實作 — SessionRuntime 欄位**

`sw_core/session_manager.py`，在 `released_reason: str | None = None`（~:224）之後加：

```python
    # 動態 profile 來源（#95）：pin / sticky / detected / fallback / yaml-target
    profile_source: str = "detected"
```

- [ ] **Step 4: 實作 — to_public_dict 輸出**

在 `to_public_dict` 的 `"platform": self.profile.platform,`（~:282）之後加：

```python
            "profile_source": self.profile_source,
```

- [ ] **Step 5: 實作 — __init__ 初始化兩 map + yaml-target**

在 `self._templates = ...`（:357）之後、`self._load_state()`（:360）之前加：

```python
        # 動態 profile 持久化（#95）：device_key → profile_name
        self._profile_pins: dict[str, str] = {}
        self._profile_detected: dict[str, str] = {}
```

在 YAML session 建立處（:368）`self._sessions[sid] = SessionRuntime(session_id=sid, profile=profile)` 後加一行：

```python
                self._sessions[sid].profile_source = "yaml-target"
```

- [ ] **Step 6: 實作 — _load_state 解析兩 map**

在 `_load_state` 的 released 解析區塊（~:437，`self._loaded_released = loaded` 之後）加：

```python
        pins = obj.get("profile_pins") if isinstance(obj, dict) else None
        if isinstance(pins, dict):
            self._profile_pins = {str(k).strip(): str(v).strip()
                                  for k, v in pins.items()
                                  if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}
        detected = obj.get("profile_detected") if isinstance(obj, dict) else None
        if isinstance(detected, dict):
            self._profile_detected = {str(k).strip(): str(v).strip()
                                      for k, v in detected.items()
                                      if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}
```

- [ ] **Step 7: 實作 — _save_state payload 納入兩 map**

`_save_state` 的 `json.dumps({...})`（:451）改為：

```python
            {"aliases": self._aliases.dump(), "bindings": dict(self._binding_overrides),
             "released": released, "profile_pins": dict(self._profile_pins),
             "profile_detected": dict(self._profile_detected)},
```

- [ ] **Step 8: 跑測試確認通過**

Run: `python3 -m pytest tests/test_profile_pin_sticky.py::TestPersistence -v`
Expected: PASS（4 passed）

- [ ] **Step 9: Commit**

```bash
git add sw_core/session_manager.py tests/test_profile_pin_sticky.py
git commit -m "feat(session): #95 profile_source 欄位 + profile_pins/detected 持久化

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 四層優先序解析（_attach_by_id_dynamic 重構）

**Files:**
- Modify: `sw_core/session_manager.py`（`_template_by_name` 新增；`_attach_by_id_dynamic` :1599-1640）
- Test: `tests/test_profile_pin_sticky.py`

- [ ] **Step 1: 寫失敗測試（優先序 + 跳過 probe）**

加到 `tests/test_profile_pin_sticky.py`：

```python
from sw_core.config import ProfileTemplate
from sw_core.session_manager import DeviceInfo


class TestPriority(_Base):
    def _mgr_with_templates(self):
        prpl = ProfileTemplate(profile_name="prpl-template", platform="prpl",
                               prompt_regex="root@prplOS", login_regex="", password_regex="",
                               ready_probe="echo __R__", uart=UartProfile())
        others = ProfileTemplate(profile_name="others-template", platform="passthrough",
                                 prompt_regex=".*", login_regex="", password_regex="",
                                 ready_probe="", uart=UartProfile())
        mgr = SessionManager([], WalWriter(wal_dir=self._tmp.name),
                             templates=[prpl, others],
                             on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        return mgr

    def test_pin_skips_probe(self):
        mgr = self._mgr_with_templates()
        key = "/dev/serial/by-id/usb-X"
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB9")
        mgr._profile_pins[key] = "prpl-template"
        called = {"n": 0}
        import sw_core.session_manager as m
        orig = m.detect_template
        m.detect_template = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or None
        self.addCleanup(lambda: setattr(m, "detect_template", orig))
        # 用 by-path 不可達 real_path 讓 bridge.start 後 probe 失敗即可，重點是 source 與未呼叫 detect
        try:
            mgr._attach_by_id_dynamic(key)
        except Exception:
            pass
        sess = next((s for s in mgr._sessions.values() if s.profile.device_by_id == key), None)
        self.assertIsNotNone(sess)
        self.assertEqual(sess.profile_source, "pin")
        self.assertEqual(called["n"], 0)

    def test_sticky_skips_probe(self):
        mgr = self._mgr_with_templates()
        key = "/dev/serial/by-id/usb-Y"
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB8")
        mgr._profile_detected[key] = "prpl-template"
        import sw_core.session_manager as m
        called = {"n": 0}
        orig = m.detect_template
        m.detect_template = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or None
        self.addCleanup(lambda: setattr(m, "detect_template", orig))
        try:
            mgr._attach_by_id_dynamic(key)
        except Exception:
            pass
        sess = next((s for s in mgr._sessions.values() if s.profile.device_by_id == key), None)
        self.assertEqual(sess.profile_source, "sticky")
        self.assertEqual(called["n"], 0)

    def test_unknown_pin_falls_through(self):
        mgr = self._mgr_with_templates()
        self.assertIsNone(mgr._template_by_name("no-such"))
```

> 註：`_attach_by_id_dynamic` 會開真實 PTY/serial bridge；測試用不可達 `real_path` 讓 attach 後段失敗，但 **profile_source 與 detect 呼叫計數在開 bridge 前已決定**，故斷言有效。若 bridge 例外冒出，以 `try/except` 包覆。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_profile_pin_sticky.py::TestPriority -v`
Expected: FAIL（`_template_by_name` 不存在 / profile_source 非 pin）

- [ ] **Step 3: 實作 — _template_by_name**

在 `_default_passthrough_template`（~:1583）附近加：

```python
    def _template_by_name(self, name: str) -> ProfileTemplate | None:
        for t in self._templates:
            if t.profile_name == name:
                return t
        return None
```

- [ ] **Step 4: 實作 — _attach_by_id_dynamic 優先序重構**

把 `_attach_by_id_dynamic` 開頭「開 probe bridge → detect」區塊（:1609-1634，從 `# 先用預設 UART 參數開 bridge 做 probe` 到 `tpl = detected or passthrough` / `if tpl is None: return`）替換為：

```python
        # 四層優先序（#95）：pin > sticky > detect > fallback。pin/sticky 命中跳過 probe。
        tpl = None
        source = None
        pin_name = self._profile_pins.get(by_id)
        if pin_name:
            tpl = self._template_by_name(pin_name)
            if tpl is not None:
                source = "pin"
        if tpl is None:
            sticky_name = self._profile_detected.get(by_id)
            if sticky_name:
                tpl = self._template_by_name(sticky_name)
                if tpl is not None:
                    source = "sticky"
        if tpl is None:
            default_uart = UartProfile()
            probe_bridge = UARTBridge("PROBE", real_path, default_uart, self._wal)
            detected: ProfileTemplate | None = None
            try:
                probe_bridge.start()
                detected = detect_template(probe_bridge, self._templates)
            except Exception:
                pass
            finally:
                try:
                    probe_bridge.stop()
                except Exception:
                    pass
            if detected is not None:
                tpl, source = detected, "detected"
        if tpl is None:
            tpl, source = self._default_passthrough_template(), "fallback"
        if tpl is None:
            return
```

並在建立 session 後設 source。找 `session = self._session_from_template(tpl, by_id)`（:1640）後加：

```python
            session.profile_source = source
```

> 註：`from .config import UartProfile` 已在函式頂（:1601）。原本無條件開 probe 的程式碼整段被上面取代——確認刪除舊的 `probe_bridge`/`detected`/`passthrough = self._default_passthrough_template()` 重複定義。

- [ ] **Step 5: 跑測試確認通過**

Run: `python3 -m pytest tests/test_profile_pin_sticky.py::TestPriority -v`
Expected: PASS（3 passed）

- [ ] **Step 6: Commit**

```bash
git add sw_core/session_manager.py tests/test_profile_pin_sticky.py
git commit -m "feat(session): #95 _attach_by_id_dynamic 四層優先序（pin>sticky>detect>fallback）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: READY-gated sticky 寫入（含 TOCTOU 防護）

**Files:**
- Modify: `sw_core/session_manager.py`（dynamic attach READY 區塊 :1717-1725）
- Test: `tests/test_profile_pin_sticky.py`

- [ ] **Step 1: 寫失敗測試（達 READY 才寫 / 未達不寫 / real_path 不一致不寫）**

加到 `tests/test_profile_pin_sticky.py`。以白箱方式直接驗證寫入條件函式，避免依賴真實 bridge：

```python
class TestStickyWrite(_Base):
    def _mgr_with_templates(self):
        prpl = ProfileTemplate(profile_name="prpl-template", platform="prpl",
                               prompt_regex="root@prplOS", login_regex="", password_regex="",
                               ready_probe="echo __R__", uart=UartProfile())
        return SessionManager([], WalWriter(wal_dir=self._tmp.name), templates=[prpl],
                              on_ready=lambda _sid: None, on_detached=lambda _sid: None)

    def test_maybe_persist_sticky_writes_when_ready_detected(self):
        mgr = self._mgr_with_templates()
        key = "/dev/serial/by-id/usb-Z"
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB1")
        mgr._maybe_persist_sticky(by_id=key, profile_name="prpl-template",
                                  source="detected", real_path="/dev/ttyUSB1")
        self.assertEqual(mgr._profile_detected.get(key), "prpl-template")

    def test_no_write_when_source_not_detected(self):
        mgr = self._mgr_with_templates()
        key = "/dev/serial/by-id/usb-Z"
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB1")
        mgr._maybe_persist_sticky(by_id=key, profile_name="prpl-template",
                                  source="fallback", real_path="/dev/ttyUSB1")
        self.assertNotIn(key, mgr._profile_detected)

    def test_no_write_when_real_path_changed(self):
        mgr = self._mgr_with_templates()
        key = "/dev/serial/by-id/usb-Z"
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB2")  # 已換
        mgr._maybe_persist_sticky(by_id=key, profile_name="prpl-template",
                                  source="detected", real_path="/dev/ttyUSB1")  # attach 當時
        self.assertNotIn(key, mgr._profile_detected)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_profile_pin_sticky.py::TestStickyWrite -v`
Expected: FAIL（`_maybe_persist_sticky` 不存在）

- [ ] **Step 3: 實作 — _maybe_persist_sticky helper**

在 `_template_by_name` 附近加（須在持有 `self._lock` 的情境呼叫）：

```python
    def _maybe_persist_sticky(self, *, by_id: str, profile_name: str,
                              source: str, real_path: str) -> None:
        """達 READY 的正向偵測才寫 sticky（#95）。TOCTOU：real_path 須與 attach 當時一致。
        須在 self._lock 內呼叫。"""
        if source != "detected":
            return
        cur = self._devices.get(by_id)
        if cur is None or cur.real_path != real_path:
            return
        if self._profile_detected.get(by_id) == profile_name:
            return
        self._profile_detected[by_id] = profile_name
        self._save_state()
```

- [ ] **Step 4: 實作 — 在 dynamic attach READY 區塊呼叫**

`_attach_by_id_dynamic` 的 READY 區塊（:1717-1725，`if ok:` 內、`notify_ready = True` 之前）加：

```python
                    self._maybe_persist_sticky(by_id=by_id, profile_name=profile.profile_name,
                                               source=session.profile_source, real_path=real_path)
```

> 註：此處已在 `with self._lock:`（:1695）內，符合 `_maybe_persist_sticky` 的 lock 前提。`real_path` 為 attach 當時值（:1651），`by_id` 為函式參數。

- [ ] **Step 5: 跑測試確認通過**

Run: `python3 -m pytest tests/test_profile_pin_sticky.py::TestStickyWrite -v`
Expected: PASS（3 passed）

- [ ] **Step 6: Commit**

```bash
git add sw_core/session_manager.py tests/test_profile_pin_sticky.py
git commit -m "feat(session): #95 READY-gated sticky 寫入 + TOCTOU real_path 防護

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: pin_session / unpin_session（含錯誤碼）

**Files:**
- Modify: `sw_core/session_manager.py`（新增 `pin_session`/`unpin_session`）
- Test: `tests/test_profile_pin_sticky.py`

- [ ] **Step 1: 寫失敗測試**

```python
class TestPinUnpin(_Base):
    def _mgr(self):
        prpl = ProfileTemplate(profile_name="prpl-template", platform="prpl",
                               prompt_regex="root@prplOS", login_regex="", password_regex="",
                               ready_probe="echo __R__", uart=UartProfile())
        mgr = SessionManager([_profile()], WalWriter(wal_dir=self._tmp.name),
                             templates=[prpl],
                             on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        return mgr

    def test_pin_valid_profile(self):
        mgr = self._mgr()
        resp = mgr.pin_session("COM0", "prpl-template")
        self.assertTrue(resp["ok"])
        self.assertEqual(mgr._profile_pins["/dev/serial/by-id/usb-FTDI_A-if00-port0"], "prpl-template")

    def test_pin_unknown_profile_rejected(self):
        mgr = self._mgr()
        resp = mgr.pin_session("COM0", "no-such-template")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "UNKNOWN_PROFILE")

    def test_pin_explicit_target_rejected(self):
        mgr = self._mgr()  # _profile() 經 __init__ → profile_source=yaml-target
        resp = mgr.pin_session("COM0", "prpl-template")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "PROFILE_IS_EXPLICIT")

    def test_unpin_keeps_sticky(self):
        mgr = self._mgr()
        key = "/dev/serial/by-id/usb-FTDI_A-if00-port0"
        mgr._profile_pins[key] = "prpl-template"
        mgr._profile_detected[key] = "op3-template"
        resp = mgr.unpin_session("COM0")
        self.assertTrue(resp["ok"])
        self.assertNotIn(key, mgr._profile_pins)
        self.assertEqual(mgr._profile_detected.get(key), "op3-template")
```

> 註：`test_pin_valid_profile` 與 `test_pin_explicit_target_rejected` 對同一 YAML session 有矛盾期望——前者其實該用「動態 session（非 yaml-target）」。修正：`test_pin_valid_profile` 改建動態 session（見 Step 3 解析規則：pin 對動態 session 才成功）。實作時把 `test_pin_valid_profile` 的 session 以 `mgr._sessions` 注入一個 `profile_source!="yaml-target"` 的 session，或對「尚無 session、僅 device key」的裝置 pin。採後者：pin 接受 by-id selector 直接寫 map（見 Step 3）。

修正 `test_pin_valid_profile`：

```python
    def test_pin_valid_profile(self):
        mgr = self._mgr()
        key = "/dev/serial/by-id/usb-NEW-if00-port0"
        from sw_core.session_manager import DeviceInfo
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB5")
        resp = mgr.pin_session(key, "prpl-template")  # 用 by-id 當 selector
        self.assertTrue(resp["ok"])
        self.assertEqual(mgr._profile_pins[key], "prpl-template")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_profile_pin_sticky.py::TestPinUnpin -v`
Expected: FAIL（`pin_session` 不存在）

- [ ] **Step 3: 實作 — pin_session / unpin_session**

在 `bind_session` 附近加（解析 selector→device_key：優先既有 session 的 `device_by_id`，否則視 selector 為 by-id/by-path 直接當 key）：

```python
    def _resolve_device_key(self, selector: str) -> tuple[str | None, SessionRuntime | None]:
        """回 (device_key, session)。selector 可為 COM/alias/sid（→ 既有 session 的 device_by_id）
        或直接 by-id/by-path（→ 該字串即 device_key，session 可能為 None）。"""
        with self._lock:
            for sid, s in self._sessions.items():
                if selector in (sid, s.profile.com, s.profile.alias):
                    return s.profile.device_by_id, s
            if selector in self._devices:
                return selector, None
            for sid, s in self._sessions.items():
                if s.profile.device_by_id == selector:
                    return selector, s
        return (selector if selector.startswith("/dev/") else None), None

    def pin_session(self, selector: str, profile_name: str) -> dict[str, Any]:
        if self._template_by_name(profile_name) is None:
            return {"ok": False, "error_code": "UNKNOWN_PROFILE"}
        device_key, session = self._resolve_device_key(selector)
        if not device_key:
            return {"ok": False, "error_code": "DEVICE_NOT_FOUND"}
        if session is not None and session.profile_source == "yaml-target":
            return {"ok": False, "error_code": "PROFILE_IS_EXPLICIT"}
        with self._lock:
            self._profile_pins[device_key] = profile_name
            self._save_state()
        return {"ok": True, "device_key": device_key, "profile": profile_name}

    def unpin_session(self, selector: str) -> dict[str, Any]:
        device_key, _ = self._resolve_device_key(selector)
        if not device_key:
            return {"ok": False, "error_code": "DEVICE_NOT_FOUND"}
        with self._lock:
            self._profile_pins.pop(device_key, None)
            self._save_state()
        return {"ok": True, "device_key": device_key}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_profile_pin_sticky.py::TestPinUnpin -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add sw_core/session_manager.py tests/test_profile_pin_sticky.py
git commit -m "feat(session): #95 pin_session/unpin_session + UNKNOWN_PROFILE/PROFILE_IS_EXPLICIT

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: RPC + CLI 接線（session.pin / session.unpin）

**Files:**
- Modify: `sw_core/service.py`（`session.bind` 分支 :612 後加兩分支）
- Modify: `sw_core/cli.py`（subparser :478 後、dispatch）
- Test: `tests/test_profile_pin_sticky.py`

- [ ] **Step 1: 寫失敗測試（RPC 路由）**

```python
class TestRpc(_Base):
    def test_rpc_pin_unpin_routed(self):
        # 透過 SerialwrapService.rpc 驗證路由到 pin_session/unpin_session
        import sw_core.service as svc_mod
        # 以最小 stub 取代 SessionManager 的 pin/unpin 回應
        seen = {}
        class _S:
            def pin_session(self, sel, prof): seen["pin"] = (sel, prof); return {"ok": True}
            def unpin_session(self, sel): seen["unpin"] = sel; return {"ok": True}
        service = svc_mod.SerialwrapService.__new__(svc_mod.SerialwrapService)
        service._sessions = _S()
        r1 = service.rpc("session.pin", {"selector": "COM0", "profile": "prpl-template"})
        r2 = service.rpc("session.unpin", {"selector": "COM0"})
        self.assertTrue(r1["ok"]); self.assertTrue(r2["ok"])
        self.assertEqual(seen["pin"], ("COM0", "prpl-template"))
        self.assertEqual(seen["unpin"], "COM0")
```

> 註：若 `rpc()` 早期存取其他屬性導致 stub 不足，改以整合方式（起真實 service）測；先嘗試此輕量法，失敗則在 Step 3 後調整為真實 service 注入。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_profile_pin_sticky.py::TestRpc -v`
Expected: FAIL（`session.pin` 未路由，回 unknown method）

- [ ] **Step 3: 實作 — service.py RPC 分支**

在 `if method == "session.bind":` 區塊（:612-617）之後加：

```python
        if method == "session.pin":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            profile_name = str(params.get("profile") or params.get("profile_name") or "")
            if not selector or not profile_name:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.pin_session(selector, profile_name)

        if method == "session.unpin":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.unpin_session(selector)
```

- [ ] **Step 4: 實作 — cli.py subparser**

在 `p_sb`（bind）subparser 區塊（:476-478）之後加：

```python
    p_pin = sess_sub.add_parser("pin", help="把 device 釘到指定 profile（最高優先，繞過偵測）")
    p_pin.add_argument("--selector", required=True, help="session_id | COMx | alias | by-id | by-path")
    p_pin.add_argument("--profile", required=True, help="要釘的 profile/template 名")
    p_unpin = sess_sub.add_parser("unpin", help="解除 device 的 profile pin（保留 sticky）")
    p_unpin.add_argument("--selector", required=True, help="session_id | COMx | alias | by-id | by-path")
```

- [ ] **Step 5: 實作 — cli.py dispatch**

找 session 子命令 dispatch（比照 `bind` → `session.bind` 的 `_rpc(...)` 呼叫），加：

```python
    if args.session_cmd == "pin":
        return _emit(client.call("session.pin", {"selector": args.selector, "profile": args.profile}))
    if args.session_cmd == "unpin":
        return _emit(client.call("session.unpin", {"selector": args.selector}))
```

> 註：實際 dispatch 變數名（`args.session_cmd` / `client.call` / `_emit`）以該檔 `bind` 既有寫法為準，照抄其模式替換方法名與參數。

- [ ] **Step 6: 跑測試 + 手動煙霧**

Run: `python3 -m pytest tests/test_profile_pin_sticky.py::TestRpc -v`
Expected: PASS

Run: `python3 -m serialwrap session pin --help`
Expected: 顯示 `--selector` / `--profile` 說明，無錯誤。

- [ ] **Step 7: Commit**

```bash
git add sw_core/service.py sw_core/cli.py tests/test_profile_pin_sticky.py
git commit -m "feat(cli,service): #95 session pin/unpin RPC 與 CLI 接線

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: device_key by-path 穩定性（測試 + 文件）

**Files:**
- Test: `tests/test_profile_pin_sticky.py`
- Modify: `README.md`、`docs/serialwrap-spec.md`

- [ ] **Step 1: 寫測試（by-path 當 device_key）**

```python
class TestDeviceKey(_Base):
    def test_by_path_selector_used_as_key(self):
        prpl = ProfileTemplate(profile_name="prpl-template", platform="prpl",
                               prompt_regex="x", login_regex="", password_regex="",
                               ready_probe="echo __R__", uart=UartProfile())
        mgr = SessionManager([], WalWriter(wal_dir=self._tmp.name), templates=[prpl],
                             on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        bypath = "/dev/serial/by-path/pci-0000:00:14.0-usb-0:1:1.0-port0"
        from sw_core.session_manager import DeviceInfo
        mgr._devices[bypath] = DeviceInfo(by_id=bypath, real_path="/dev/ttyUSB0")
        resp = mgr.pin_session(bypath, "prpl-template")
        self.assertTrue(resp["ok"])
        self.assertEqual(mgr._profile_pins[bypath], "prpl-template")
```

- [ ] **Step 2: 跑測試確認通過**

Run: `python3 -m pytest tests/test_profile_pin_sticky.py::TestDeviceKey -v`
Expected: PASS（`_resolve_device_key` 已支援 `/dev/` 開頭 selector，含 by-path）

- [ ] **Step 3: 文件 — README / spec 補 by-path 規範**

在 `README.md` session 管理章節、`docs/serialwrap-spec.md` 對應段落，補一段：

> `session pin --selector <by-path> --profile <name>` 把裝置釘到指定 profile。**同款晶片（如 CH340）by-id 相同時，務必以 `/dev/serial/by-path/...` 當 selector**，避免 pin/sticky 張冠李戴（與既有 binding 規範一致）。`session list` 的 `profile_source` 顯示 profile 來源（pin/sticky/detected/fallback/yaml-target）。

- [ ] **Step 4: Commit**

```bash
git add tests/test_profile_pin_sticky.py README.md docs/serialwrap-spec.md
git commit -m "docs(session): #95 device_key by-path 規範 + profile_source 說明

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: 整合測試 + CHANGELOG + 全套驗證

**Files:**
- Test: `tests/test_profile_pin_sticky.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: 寫整合測試（偵測→READY→sticky→模擬重啟沿用）**

以 monkeypatch 將 `_attach_by_id_dynamic` 的 probe/ready 結果固定，驗證 sticky 寫入後新 SessionManager 沿用且不再 detect：

```python
class TestIntegration(_Base):
    def test_sticky_written_then_reused_after_restart(self):
        # 直接驗證資料層閉環：detected 達 READY 寫 sticky → 重啟載入 → 命中 sticky 跳過 detect
        prpl = ProfileTemplate(profile_name="prpl-template", platform="prpl",
                               prompt_regex="x", login_regex="", password_regex="",
                               ready_probe="echo __R__", uart=UartProfile())
        mgr = SessionManager([], WalWriter(wal_dir=self._tmp.name), templates=[prpl],
                             on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        key = "/dev/serial/by-id/usb-INT"
        from sw_core.session_manager import DeviceInfo
        mgr._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB0")
        with mgr._lock:
            mgr._maybe_persist_sticky(by_id=key, profile_name="prpl-template",
                                      source="detected", real_path="/dev/ttyUSB0")
        # 重啟
        mgr2 = SessionManager([], WalWriter(wal_dir=self._tmp.name), templates=[prpl],
                              on_ready=lambda _sid: None, on_detached=lambda _sid: None)
        self.assertEqual(mgr2._profile_detected.get(key), "prpl-template")
        import sw_core.session_manager as m
        called = {"n": 0}
        orig = m.detect_template
        m.detect_template = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or None
        self.addCleanup(lambda: setattr(m, "detect_template", orig))
        mgr2._devices[key] = DeviceInfo(by_id=key, real_path="/dev/ttyUSB0")
        try:
            mgr2._attach_by_id_dynamic(key)
        except Exception:
            pass
        sess = next((s for s in mgr2._sessions.values() if s.profile.device_by_id == key), None)
        self.assertEqual(sess.profile_source, "sticky")
        self.assertEqual(called["n"], 0)
```

- [ ] **Step 2: 跑全檔測試**

Run: `python3 -m pytest tests/test_profile_pin_sticky.py -v`
Expected: PASS（全部）

- [ ] **Step 3: 更新 CHANGELOG**

`CHANGELOG.md` 的 `## [Unreleased]` 下加：

```markdown
### Added
- 動態裝置 profile 持久化（#95）：`session pin`/`unpin` 手動釘選 + 偵測達 READY 後自動 sticky，根治 daemon 重啟後 profile 漂移；`session list` 新增 `profile_source` 欄位（pin/sticky/detected/fallback/yaml-target）。
```

- [ ] **Step 4: 全套測試 + policy（複現 CI 的 PR 規則）**

```bash
python3 -m pytest -q tests/
python3 -m policy_check --repo . \
  --pr-title "feat: 動態裝置 profile 持久化（pin + sticky）" \
  --pr-body "Closes #95" \
  --pr-base-ref main --pr-head-ref feature/95-profile-pin-sticky
```
Expected: pytest 無新失敗（既有 flaky 見 CLAUDE.md/記憶，以 CI 為準）；policy_check 通過。

- [ ] **Step 5: Commit**

```bash
git add tests/test_profile_pin_sticky.py CHANGELOG.md
git commit -m "test(session): #95 pin/sticky 整合測試 + CHANGELOG

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 6: 真機驗證（throwaway daemon，不動 prod；屬 pipeline Verify 階段）**

依記憶 [mcu-flash-broker-realhw-validation]/[attach-reprobe-realhw-validation] 起 throwaway daemon（獨立 `SERIALWRAP_RUN_DIR`/`_STATE_DIR`），對 COM0：
- `session pin --selector COM0 --profile prpl-template` → 重啟 throwaway daemon → `session list` 應見 `prpl-template:COM0` READY、`profile_source:pin`。
- 清 pin、安靜時讓其偵測達 READY → 重啟 → `profile_source:sticky`、仍 prpl。
記錄輸出為驗證證據。

---

## 完成準則（對照 pipeline Verify / Archive）

- [ ] 所有 task 的測試 RED→GREEN，`pytest -q tests/` 無新失敗。
- [ ] `policy_check`（帶 `--pr-*`）通過。
- [ ] code review（`requesting-code-review` + 每筆 `receiving-code-review`），無未解 Critical/Important。
- [ ] OpenSpec change `profile-pin-sticky` archive。
- [ ] 真機驗證證據記錄。
- [ ] push/PR 僅在使用者明確要求時。
