# MCU fw upgrade flash-broker 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development（建議）或 superpowers:executing-plans 逐 task 實作。步驟用 checkbox（`- [ ]`）追蹤。

**Goal:** 在 serialwrap daemon 持續 maintain tty 下，提供 byte-transparent `/dev/ttyMCU` 端點 + 非破壞性 sync-probe 自動認 MCU 線，讓外部 flasher（`ocp-mcu-upgrade`）原生走通 MCU 韌體升級。

**Architecture:** 三個新單元 + 兩處擴充。`sw_core/mcu_patterns.py`（per-family sync/ack registry，非破壞不變式）；`sw_core/flash_endpoint.py`（`SerialwrapService` 層級的常駐 PTY `/dev/ttyMCU`、開啟分流、sync-probe 偵測器）；`UARTBridge` 加 raw/flash 旗標（TX 跳過行處理）+ baud 鏡射；`SessionManager` 加 `FLASHING` 狀態與進出（沿用 release/attach 骨架）；`service.py`/`cli.py` 加 `mcu.*`。daemon 全程是 real device 唯一 reader（無 two-reader race），RAW WAL 全程留證。

**Tech Stack:** Python 3.12、stdlib（`os`/`pty`/`termios`/`select`/`threading`）、pytest、unittest。對應 OpenSpec change `mcu-fw-upgrade-flash-broker`、設計 `docs/superpowers/specs/2026-06-17-mcu-fw-upgrade-flash-broker-design.md`。

---

## 共用約定

- 既有 flaky（不計入新失敗）：`test_multiagent_e2e.py::...test_five_agents_three_rounds_no_conflict`、`t8_full_run_simulation`、`test_t1_wal_reset_preserves_console`。
- 每個 task 完成後跑該 task 的測試；group 結束跑 `python3 -m pytest -q tests/`。
- commit 用 Conventional Commits（繁中 subject）+ trailer `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。
- 分支：`feature/55-mcu-fw-upgrade-flash-broker`（已建立，勿在 main 實作）。

---

## Task 1: MCU pattern registry

**Files:**
- Create: `sw_core/mcu_patterns.py`
- Test: `tests/test_mcu_patterns.py`

- [ ] **Step 1: 寫失敗測試（registry 預設含 TI、非破壞 guard、渲染）**

```python
# tests/test_mcu_patterns.py
import pytest
from sw_core.mcu_patterns import McuPattern, McuPatternRegistry


def test_default_registry_has_ti_cc2674():
    reg = McuPatternRegistry.default()
    p = reg.get("ti-cc26xx")
    assert p.probe == b"\x55\x55"
    assert p.expect == b"\x00\xcc"
    assert p.baud == 115200
    assert p.non_destructive is True


def test_loader_rejects_non_reviewed_destructive_probe():
    bad = {"family": "evil", "probe": "aa55", "expect": "00cc",
           "baud": 115200, "timeout_ms": 500, "non_destructive": False}
    with pytest.raises(ValueError, match="non_destructive"):
        McuPattern.from_dict(bad)


def test_render_support_list_lists_families():
    reg = McuPatternRegistry.default()
    text = reg.render_support_list(candidates=[])
    assert "ti-cc26xx" in text
    assert "55 55" in text.lower() or "0x55" in text.lower()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_mcu_patterns.py -q`
Expected: FAIL（`ModuleNotFoundError: sw_core.mcu_patterns`）

- [ ] **Step 3: 實作 `sw_core/mcu_patterns.py`**

```python
from __future__ import annotations
import dataclasses


def _hex_to_bytes(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", "").replace("0x", ""))


@dataclasses.dataclass(frozen=True)
class McuPattern:
    family: str
    probe: bytes        # 非破壞性 sync 位元組
    expect: bytes       # 期望的 ACK 位元組
    baud: int
    timeout_ms: int
    non_destructive: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "McuPattern":
        non_destructive = bool(d.get("non_destructive", False))
        if not non_destructive:
            raise ValueError(
                f"pattern {d.get('family')!r} 未通過 non_destructive 審核，拒絕載入"
            )
        return cls(
            family=str(d["family"]),
            probe=_hex_to_bytes(str(d["probe"])),
            expect=_hex_to_bytes(str(d["expect"])),
            baud=int(d.get("baud", 115200)),
            timeout_ms=int(d.get("timeout_ms", 500)),
            non_destructive=True,
        )


_DEFAULTS = [
    {"family": "ti-cc26xx", "probe": "5555", "expect": "00cc",
     "baud": 115200, "timeout_ms": 500, "non_destructive": True},
]


class McuPatternRegistry:
    def __init__(self, patterns: list[McuPattern]) -> None:
        self._by_family = {p.family: p for p in patterns}

    @classmethod
    def default(cls) -> "McuPatternRegistry":
        return cls([McuPattern.from_dict(d) for d in _DEFAULTS])

    @classmethod
    def load(cls, rows: list[dict] | None) -> "McuPatternRegistry":
        patterns = [McuPattern.from_dict(d) for d in _DEFAULTS]
        for d in rows or []:
            patterns.append(McuPattern.from_dict(d))  # 非破壞 guard 在 from_dict
        return cls(patterns)

    def get(self, family: str) -> McuPattern:
        return self._by_family[family]

    def all(self) -> list[McuPattern]:
        return list(self._by_family.values())

    def render_support_list(self, *, candidates: list[dict]) -> str:
        lines = ["serialwrap MCU flash — 支援家族："]
        for p in self.all():
            lines.append(
                f"  - {p.family}: probe={p.probe.hex(' ')} expect={p.expect.hex(' ')} "
                f"baud={p.baud}"
            )
        lines.append("")
        lines.append("目前候選（已排除 command_capable console）：")
        if not candidates:
            lines.append("  （無）")
        for c in candidates:
            lines.append(f"  - {c.get('com')} {c.get('by_id')} -> {c.get('real_path')}")
        return "\n".join(lines) + "\n"
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_mcu_patterns.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add sw_core/mcu_patterns.py tests/test_mcu_patterns.py
git commit -m "feat(mcu): 新增 MCU pattern registry（預設 TI CC26xx，非破壞不變式）（#55)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 2: sync-probe 偵測器

**Files:**
- Create: `sw_core/flash_endpoint.py`（本 task 先放偵測器；端點 PTY 於 Task 4）
- Test: `tests/test_flash_probe.py`

偵測器以一個「probe 介面」對候選送 bytes 並讀回，方便單測注入假 transport。

- [ ] **Step 1: 寫失敗測試（排除 console / 命中 / ambiguous / 無命中）**

```python
# tests/test_flash_probe.py
from sw_core.mcu_patterns import McuPatternRegistry
from sw_core.flash_endpoint import detect_mcu_line


class FakeTransport:
    """以 by_id -> 回應 bytes 模擬各候選對 probe 的反應。"""
    def __init__(self, replies):
        self._replies = replies  # {by_id: bytes|None}
        self.written = {}

    def probe(self, by_id, probe_bytes, expect, timeout_ms):
        self.written.setdefault(by_id, []).append(probe_bytes)
        reply = self._replies.get(by_id)
        return reply is not None and reply.startswith(expect)


def _cand(com, by_id, command_capable):
    return {"com": com, "by_id": by_id, "real_path": "/dev/x",
            "command_capable": command_capable}


def test_excludes_command_capable_console():
    reg = McuPatternRegistry.default()
    cands = [_cand("COM1", "console-id", True), _cand("COM0", "mcu-id", False)]
    t = FakeTransport({"mcu-id": b"\x00\xcc"})
    res = detect_mcu_line(cands, reg, t)
    assert res.status == "matched"
    assert res.by_id == "mcu-id"
    assert "console-id" not in t.written  # console 從未被 probe


def test_ambiguous_when_multiple_ack():
    reg = McuPatternRegistry.default()
    cands = [_cand("COM0", "a", False), _cand("COM2", "b", False)]
    t = FakeTransport({"a": b"\x00\xcc", "b": b"\x00\xcc"})
    res = detect_mcu_line(cands, reg, t)
    assert res.status == "ambiguous"
    assert set(res.hits) == {"a", "b"}


def test_no_match_returns_none_status():
    reg = McuPatternRegistry.default()
    cands = [_cand("COM0", "a", False)]
    t = FakeTransport({"a": None})
    res = detect_mcu_line(cands, reg, t)
    assert res.status == "none"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_flash_probe.py -q`
Expected: FAIL（`ImportError: cannot import name 'detect_mcu_line'`）

- [ ] **Step 3: 實作偵測器（`sw_core/flash_endpoint.py` 起頭）**

```python
from __future__ import annotations
import dataclasses
from typing import Protocol
from .mcu_patterns import McuPatternRegistry


class ProbeTransport(Protocol):
    def probe(self, by_id: str, probe_bytes: bytes, expect: bytes, timeout_ms: int) -> bool: ...


@dataclasses.dataclass
class DetectResult:
    status: str               # "matched" | "ambiguous" | "none"
    by_id: str | None = None
    family: str | None = None
    hits: list[str] = dataclasses.field(default_factory=list)


def detect_mcu_line(candidates: list[dict], registry: McuPatternRegistry,
                    transport: ProbeTransport) -> DetectResult:
    """排除 command_capable console；逐候選逐 pattern 送非破壞 probe。
    命中 0 → none；命中 1 → matched；命中 >1 → ambiguous（不自動挑）。"""
    eligible = [c for c in candidates if not c.get("command_capable")]
    hits: list[tuple[str, str]] = []  # (by_id, family)
    for c in eligible:
        by_id = c["by_id"]
        for p in registry.all():
            if transport.probe(by_id, p.probe, p.expect, p.timeout_ms):
                hits.append((by_id, p.family))
                break
    if not hits:
        return DetectResult(status="none")
    if len(hits) > 1:
        return DetectResult(status="ambiguous", hits=[h[0] for h in hits])
    by_id, family = hits[0]
    return DetectResult(status="matched", by_id=by_id, family=family)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_flash_probe.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add sw_core/flash_endpoint.py tests/test_flash_probe.py
git commit -m "feat(mcu): sync-probe 偵測器（排除 console / ambiguous 不自動挑）（#55)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 3: UARTBridge raw/flash 模式（TX 不行處理 + baud 鏡射）

**Files:**
- Modify: `sw_core/uart_io.py`（`UARTBridge`：加 `flash_mode` 旗標、flash TX 路徑、`mirror_termios`）
- Test: `tests/test_uart_flash_bridge.py`

`UARTBridge` 既有 `send_bytes()`（`uart_io.py:478`）已原樣寫 real device 並記 WAL；flash 模式核心是「endpoint 收到的 bytes 直接 `send_bytes`，跳過 `_consume_console_input` 行處理」。

- [ ] **Step 1: 寫失敗測試（flash TX 原樣 + 既有行處理不受影響）**

```python
# tests/test_uart_flash_bridge.py
from sw_core.uart_io import UARTBridge
from sw_core.config import UartProfile


class _RecordingWal:
    def __init__(self): self.records = []
    def append(self, **kw): self.records.append(kw)


def _bridge():
    prof = UartProfile(baud=115200, data_bits=8, parity="N", stop_bits=1,
                       flow_control="none", xonxoff=False)
    return UARTBridge("COM0", "/dev/null", prof, _RecordingWal())


def test_flash_tx_is_byte_exact(monkeypatch):
    b = _bridge()
    sent = bytearray()
    monkeypatch.setattr(b, "send_bytes",
                        lambda payload, **kw: sent.extend(payload))
    payload = bytes([0x08, 0x0A, 0x0D, 0x7F, 0x55, 0x00])
    b.flash_tx(payload)                       # 新 API：flash 模式 TX
    assert bytes(sent) == payload             # 無退格/斷行/行組合汙染
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_uart_flash_bridge.py -q`
Expected: FAIL（`AttributeError: 'UARTBridge' object has no attribute 'flash_tx'`）

- [ ] **Step 3: 實作 `flash_tx` 與 baud 鏡射**

於 `UARTBridge` 加入（緊接 `send_bytes` 之後，約 `uart_io.py:486`）：

```python
    def flash_tx(self, payload: bytes) -> None:
        """flash 模式：endpoint→device 原樣送出，跳過行處理（_consume_console_input）。"""
        self.send_bytes(payload, source="flash", cmd_id=None)

    def mirror_termios_from(self, slave_fd: int) -> None:
        """把 endpoint PTY slave 的 baud 鏡射到 real device；失敗則保持 profile baud。"""
        import termios
        with self._state_lock:
            serial_fd = self._serial_fd
        if serial_fd is None:
            return
        try:
            attrs = termios.tcgetattr(slave_fd)
            ispeed, ospeed = attrs[4], attrs[5]
            dst = termios.tcgetattr(serial_fd)
            dst[4], dst[5] = ispeed, ospeed
            termios.tcsetattr(serial_fd, termios.TCSANOW, dst)
        except OSError:
            pass  # fallback：維持 _configure_serial 設定的 registry/profile baud
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_uart_flash_bridge.py -q`
Expected: PASS（1 passed）

- [ ] **Step 5: Commit**

```bash
git add sw_core/uart_io.py tests/test_uart_flash_bridge.py
git commit -m "feat(uart): UARTBridge flash_tx 原樣送出 + baud termios 鏡射（#55)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 4: `/dev/ttyMCU` 端點與開啟分流

**Files:**
- Modify: `sw_core/flash_endpoint.py`（加 `FlashEndpoint` class：常駐 PTY + symlink + 分流 + 重入擋）
- Modify: `sw_core/constants.py`（加 `TTYMCU_PATH = os.path.join(RUN_DIR, "dev", "ttyMCU")`）
- Test: `tests/test_flash_endpoint.py`

開啟分流規則：master 端在 grace window 內**收到 client 寫入**（flasher sync）→ flash 路徑；
若只被讀取、無寫入 → 寫支援清單文字 + EOF（`cat` 路徑）。

- [ ] **Step 1: 寫失敗測試（建 PTY+symlink、只讀回清單、重入擋）**

```python
# tests/test_flash_endpoint.py
import os, tempfile
from sw_core.mcu_patterns import McuPatternRegistry
from sw_core.flash_endpoint import FlashEndpoint


def test_creates_pty_and_symlink():
    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "ttyMCU")
        ep = FlashEndpoint(link_path=link, registry=McuPatternRegistry.default(),
                           list_candidates=lambda: [])
        ep.start()
        try:
            assert os.path.islink(link)
            assert os.path.exists(os.path.realpath(link))
        finally:
            ep.stop()


def test_readonly_open_returns_support_list():
    with tempfile.TemporaryDirectory() as d:
        link = os.path.join(d, "ttyMCU")
        ep = FlashEndpoint(link_path=link, registry=McuPatternRegistry.default(),
                           list_candidates=lambda: [])
        ep.start()
        try:
            with open(link, "rb") as f:
                data = f.read()
            assert b"ti-cc26xx" in data
        finally:
            ep.stop()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_flash_endpoint.py -q`
Expected: FAIL（`ImportError: cannot import name 'FlashEndpoint'`）

- [ ] **Step 3: 實作 `FlashEndpoint`**

於 `sw_core/flash_endpoint.py` 加入（grace window 區分 read-only vs flasher）：

```python
import os
import pty
import select
import threading
import time


class FlashEndpoint:
    GRACE_S = 0.3

    def __init__(self, *, link_path: str, registry, list_candidates,
                 on_flash_open=None):
        self._link_path = link_path
        self._registry = registry
        self._list_candidates = list_candidates   # () -> list[dict]
        self._on_flash_open = on_flash_open        # (master_fd, slave_fd) -> None
        self._master_fd: int | None = None
        self._slave_fd: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._flashing = threading.Lock()

    def start(self) -> None:
        self._master_fd, self._slave_fd = pty.openpty()
        slave_name = os.ttyname(self._slave_fd)
        os.makedirs(os.path.dirname(self._link_path), exist_ok=True)
        try:
            os.remove(self._link_path)
        except FileNotFoundError:
            pass
        os.symlink(slave_name, self._link_path)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="serialwrap-ttyMCU")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        for fd in (self._master_fd, self._slave_fd):
            if fd is not None:
                try: os.close(fd)
                except OSError: pass
        try: os.remove(self._link_path)
        except OSError: pass

    def _loop(self) -> None:
        # 等待 client 寫入（flasher）或純讀（cat）。master 可讀 = client 寫入。
        while not self._stop.is_set():
            rlist, _, _ = select.select([self._master_fd], [], [], self.GRACE_S)
            if self._stop.is_set():
                return
            if self._master_fd in rlist:
                # flasher：交給 flash 路徑（偵測 + bridge）
                if self._flashing.acquire(blocking=False):
                    try:
                        if self._on_flash_open:
                            self._on_flash_open(self._master_fd, self._slave_fd)
                    finally:
                        self._flashing.release()
                continue
            # 無寫入：視為只讀查詢，寫支援清單（best-effort），不阻塞
            text = self._registry.render_support_list(
                candidates=self._list_candidates())
            try:
                os.write(self._master_fd, text.encode())
            except OSError:
                pass
            time.sleep(self.GRACE_S)
```

> 註：`test_readonly_open_returns_support_list` 的 `open().read()` 會在 reader 端收到清單；
> 真實 flasher 因為會先 `write` sync，落入 flash 路徑。重入由 `_flashing` 非阻塞鎖擋
> （已佔用時再開回 `FLASH_IN_PROGRESS`，於 Task 7 的 `mcu status` 反映）。

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_flash_endpoint.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 加 `TTYMCU_PATH` 常數並 commit**

於 `sw_core/constants.py` `SOCKET_PATH` 之後加：
```python
TTYMCU_PATH = _env_path("SERIALWRAP_TTYMCU_PATH", os.path.join(RUN_DIR, "dev", "ttyMCU"))
```
```bash
git add sw_core/flash_endpoint.py sw_core/constants.py tests/test_flash_endpoint.py
git commit -m "feat(mcu): /dev/ttyMCU 常駐端點 + 開啟分流（read-only→清單 / write→flash）（#55)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 5: FLASHING 狀態、仲裁與自動恢復

**Files:**
- Modify: `sw_core/session_manager.py`（`SessionRuntime` 加 `FLASHING` 進出；`cmd submit` guard；恢復沿用 `_probe_external_holder`+`_spawn_attach`）
- Test: `tests/test_flashing_state.py`

沿用 #54 release/attach 骨架：`enter_flashing(selector)` 比照 `release_device`（detach but 標 FLASHING、不自動 re-attach），`exit_flashing(selector)` 比照 `attach_device`（holder 確認 + `_spawn_attach`）。

- [ ] **Step 1: 寫失敗測試（進 FLASHING / cmd 擋 / 結束恢復）**

```python
# tests/test_flashing_state.py
from tests.helpers import make_manager_with_fake_session  # 既有測試 helper 模式


def test_enter_flashing_sets_state():
    mgr, sel = make_manager_with_fake_session(state="READY")
    res = mgr.enter_flashing(sel, source="flash")
    assert res["ok"] is True
    assert mgr.get_session(sel).state == "FLASHING"


def test_cmd_submit_rejected_while_flashing():
    mgr, sel = make_manager_with_fake_session(state="READY")
    mgr.enter_flashing(sel, source="flash")
    res = mgr.execute_command(mgr.get_session(sel).session_id, "ls",
                              "agent", "cid", timeout_s=1, mode="line")
    assert res.get("error_code") == "FLASHING_BUSY"


def test_exit_flashing_restores_via_attach():
    mgr, sel = make_manager_with_fake_session(state="READY")
    mgr.enter_flashing(sel, source="flash")
    res = mgr.exit_flashing(sel)
    assert res["ok"] is True
    assert mgr.get_session(sel).state in ("ATTACHING", "READY", "ATTACHED")
```

> 若 repo 無 `tests/helpers`，比照 `tests/test_device_release.py`（#54）建構 manager 的既有方式；
> 實作前先 `grep -rn "def .*make.*manager\|SessionManager(" tests/` 對齊既有 fixture。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_flashing_state.py -q`
Expected: FAIL（`AttributeError: ... 'enter_flashing'`）

- [ ] **Step 3: 實作 FLASHING 進出 + cmd guard**

於 `session_manager.py`：
1. `execute_command`（送命令入口）最前面加：
```python
        if session.state == "FLASHING":
            return {"ok": False, "error_code": "FLASHING_BUSY", "selector": session.profile.com}
```
2. 新增方法（比照 `release_device`/`attach_device`，`session_manager.py:587`/`:656`）：
```python
    def enter_flashing(self, selector: str, *, source: str = "flash") -> dict:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session.state == "FLASHING":
                return {"ok": True, "already_flashing": True, "session": session.to_public_dict()}
            self._detach_session_locked(session, reason="FLASHING", drop_consoles=False)
            session.state = "FLASHING"
            session.released_by = source     # 沿用 provenance 欄位
            session.released_at = now_iso()
            session.released_reason = "MCU FW upgrade (flash bridge)"
            public = session.to_public_dict()
        self._save_state()
        return {"ok": True, "session": public}

    def exit_flashing(self, selector: str) -> dict:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session.state != "FLASHING":
                return {"ok": True, "already_attached": True, "session": session.to_public_dict()}
            by_id = session.profile.device_by_id
            session.state = "ATTACHING"
            session.released_by = session.released_at = session.released_reason = None
            public = session.to_public_dict()
        self._save_state()
        self._spawn_attach(by_id)
        return {"ok": True, "session": public}
```
3. flash 期間既有 human console 轉唯讀：`enter_flashing` 用 `drop_consoles=False`，並在 bridge
   flash 模式下不接受 console 注入（Task 4 端點獨佔 TX；既有 console 只 RX 快照）。

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_flashing_state.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add sw_core/session_manager.py tests/test_flashing_state.py
git commit -m "feat(session): FLASHING 狀態 + FLASHING_BUSY + 自動恢復（沿用 release/attach 骨架）（#55)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 6: 誤燒防護（明指 console 需 --force）

**Files:**
- Modify: `sw_core/flash_endpoint.py`（`resolve_flash_target(selector, force)`）
- Test: `tests/test_flash_guard.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_flash_guard.py
from sw_core.flash_endpoint import resolve_flash_target


def _sess(com, command_capable):
    return {"com": com, "by_id": com + "-id", "command_capable": command_capable}


def test_explicit_console_blocked_without_force():
    sessions = [_sess("COM1", True)]
    res = resolve_flash_target("COM1", sessions, force=False)
    assert res["ok"] is False and res["error_code"] == "FLASH_TARGET_IS_CONSOLE"


def test_explicit_console_allowed_with_force():
    sessions = [_sess("COM1", True)]
    res = resolve_flash_target("COM1", sessions, force=True)
    assert res["ok"] is True and res["by_id"] == "COM1-id"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_flash_guard.py -q`
Expected: FAIL（`ImportError: cannot import name 'resolve_flash_target'`）

- [ ] **Step 3: 實作**

```python
def resolve_flash_target(selector: str, sessions: list[dict], *, force: bool) -> dict:
    match = next((s for s in sessions
                  if selector in (s.get("com"), s.get("by_id"))), None)
    if match is None:
        return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
    if match.get("command_capable") and not force:
        return {"ok": False, "error_code": "FLASH_TARGET_IS_CONSOLE",
                "selector": selector,
                "hint": "目標是 command_capable console（可能是 DUT）；確認無誤再加 --force"}
    return {"ok": True, "by_id": match["by_id"], "com": match.get("com")}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_flash_guard.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add sw_core/flash_endpoint.py tests/test_flash_guard.py
git commit -m "feat(mcu): 明指 console 誤燒防護（--force 才覆寫）（#55)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 7: CLI / RPC（mcu patterns / status + device 反查）

**Files:**
- Modify: `sw_core/service.py`（dispatch 加 `mcu.patterns` / `mcu.status`，約 `service.py:296` 區塊）
- Modify: `sw_core/cli.py`（加 `mcu` subcommand，比照 `device` subparser，約 `cli.py:282`/`:528`）
- Test: `tests/test_mcu_cli_rpc.py`

- [ ] **Step 1: 寫失敗測試（service dispatch）**

```python
# tests/test_mcu_cli_rpc.py
from tests.helpers import make_service  # 比照既有 service 測試建構方式


def test_mcu_patterns_lists_families():
    svc = make_service()
    res = svc.dispatch("mcu.patterns", {})
    assert res["ok"] is True
    assert any(p["family"] == "ti-cc26xx" for p in res["patterns"])


def test_mcu_status_reports_candidates_and_flashing():
    svc = make_service()
    res = svc.dispatch("mcu.status", {})
    assert res["ok"] is True
    assert "candidates" in res and "flashing" in res
```

> 實作前 `grep -rn "def dispatch\|make_service\|SerialwrapService(" tests/ sw_core/service.py`
> 對齊既有 dispatch 簽章與測試建構方式。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_mcu_cli_rpc.py -q`
Expected: FAIL（`{"ok": False, "error_code": "UNKNOWN_METHOD"}` 或 KeyError）

- [ ] **Step 3: 實作 dispatch 分支**

於 `service.py` dispatch（`device.attach` 分支之後）加：
```python
        if method == "mcu.patterns":
            return {"ok": True, "patterns": [
                {"family": p.family, "probe": p.probe.hex(" "),
                 "expect": p.expect.hex(" "), "baud": p.baud}
                for p in self._mcu_registry.all()]}
        if method == "mcu.status":
            return {"ok": True,
                    "candidates": self._flash_candidates(),
                    "flashing": self._flash_endpoint.is_flashing()}
```
並於 `SerialwrapService.__init__`（`service.py:126` 區塊）建立 `self._mcu_registry =
McuPatternRegistry.load(...)`、`self._flash_endpoint = FlashEndpoint(...)`，並在 daemon
start/stop 時 `self._flash_endpoint.start()/stop()`。`_flash_candidates()` 由 `session.list`
過濾出 attached、排除 `command_capable`。

- [ ] **Step 4: 加 CLI subcommand**

於 `cli.py`（比照 `p_device` 區塊 `:282`）加 `mcu` subparser 與 `patterns`/`status` 子命令；
dispatch 區（比照 `:528`）加：
```python
        if args.cmd == "mcu":
            if args.mcu_cmd == "patterns":
                return _run_rpc(args, "mcu.patterns", {})
            if args.mcu_cmd == "status":
                return _run_rpc(args, "mcu.status", {})
```

- [ ] **Step 5: 跑測試 + 手動驗證 + Commit**

Run: `python3 -m pytest tests/test_mcu_cli_rpc.py -q`
Expected: PASS（2 passed）
```bash
git add sw_core/service.py sw_core/cli.py tests/test_mcu_cli_rpc.py
git commit -m "feat(mcu): mcu patterns/status RPC+CLI（#55)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 8: 整合測試（假 PTY/loopback 假 MCU）

**Files:**
- Test: `tests/test_flash_integration.py`

- [ ] **Step 1: 寫整合測試（端到端 byte-perfect + 其他 COM 不受影響）**

```python
# tests/test_flash_integration.py
import os, pty, threading


def test_end_to_end_detect_and_byte_perfect_bridge():
    """假 MCU：一條 PTY，收到 0x55 0x55 後回 0x00 0xCC，之後 echo 任意 bytes。
    驗證 ttyMCU 端開出去的 payload 與假 MCU 收到的一致（含控制字元）。"""
    mcu_master, mcu_slave = pty.openpty()

    def fake_mcu():
        buf = b""
        while True:
            try: data = os.read(mcu_master, 4096)
            except OSError: return
            if not data: return
            if data.startswith(b"\x55\x55"):
                os.write(mcu_master, b"\x00\xcc")
            else:
                os.write(mcu_master, data)   # echo（驗證 byte-perfect）
    threading.Thread(target=fake_mcu, daemon=True).start()
    # ... 用 FlashEndpoint + 注入「以 mcu_slave 為 real device」的 bridge，
    #     送含 0x08/0x0A/0x7F 的 payload，斷言 echo 回來完全一致。
    # （細節依 Task 3/4 的注入點；參考 tests/test_uart_flash_bridge.py 的 monkeypatch 模式。）
```

- [ ] **Step 2: 跑測試確認失敗→實作接線→通過**

Run: `python3 -m pytest tests/test_flash_integration.py -q`
先 FAIL（接線未完成），補齊 `FlashEndpoint.on_flash_open` → 偵測 → 對命中 session 的 bridge
做 `flash_tx`/RX pump 後 PASS。

- [ ] **Step 3: 全套回歸**

Run: `python3 -m pytest -q tests/`
Expected: 無新失敗（既有 3 個 flaky 不計）。把輸出貼進 PR/commit 作證據。

- [ ] **Step 4: Commit**

```bash
git add tests/test_flash_integration.py sw_core/flash_endpoint.py
git commit -m "test(mcu): flash 端到端整合（假 MCU loopback，byte-perfect）（#55)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Task 9: 文件、真機 gate、policy

**Files:**
- Modify: `README.md`、`CHANGELOG.md`
- Verify only: 真機 + policy_check

- [ ] **Step 1: README**

在「## MCU 韌體升級：device handoff」之後新增「## MCU 韌體升級：flash 端點（/dev/ttyMCU）」段：
說明 `cat /dev/ttyMCU` 列支援家族、`serialwrap mcu patterns/status`、以 `ocp-mcu-upgrade -d
<…/dev/ttyMCU>` 取代 raw device、以及與 #54 handoff 的區隔（handoff=整個交出去；flash 端點=
daemon 不放手、自動認線、留 RAW WAL）。同步重生 README marker 區段（若有 `serialwrap-help`）。

- [ ] **Step 2: CHANGELOG**

`[Unreleased]` 補實作項（端點 / probe / registry / FLASHING / mcu CLI）。

- [ ] **Step 3: 真機 gate（強制，必做）**

依設計 §8.3：DUT console 跑 GPIO BSL-invoke（unbind `1fbf0300.serial`、GPIO13/14 in、
GPIO31/54 reset）→ host `ocp-mcu-upgrade -d <…/dev/ttyMCU> -b 115200 -t 8 -e -s -i fw.bin`。
驗收：`Return error code : 0x0`；`led-test.sh -v` 版本回讀正確；double-sync 不干擾；
其他 COM 不受影響、daemon 不死、結束自動恢復；RAW WAL 留證。把實機輸出貼進 PR。

- [ ] **Step 4: policy_check + commit**

Run: `python3 -m policy_check --repo .`
Expected: 通過（四份 agent 檔本變更預期不改；若有改需同步）。
```bash
git add README.md CHANGELOG.md
git commit -m "docs(mcu): README flash 端點用法 + CHANGELOG（#55)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Self-Review（撰寫後對照 spec）

- **Spec 覆蓋**：byte-transparent 端點→T4；唯一 reader/RAW WAL→T3/T8；sync-probe 排除 console→T2；
  ambiguous→T2；no-MCU 沉默+re-probe→T2（re-probe 於 T4 `_loop` 週期觸發，整合於 T8 驗）；
  registry+cat 列表→T1/T4；FLASHING/FLASHING_BUSY/其他 COM/自動恢復→T5；baud 鏡射→T3；
  明指 console --force→T6；mcu CLI/RPC→T7；真機 gate→T9。
- **Placeholder**：`<…/dev/ttyMCU>` 為實際路徑佔位（`TTYMCU_PATH`）。T5/T7/T8 的測試 helper
  以「實作前 grep 對齊既有 fixture」標明，非 TODO。
- **型別一致**：`detect_mcu_line`→`DetectResult(status/by_id/family/hits)`、`flash_tx`、
  `mirror_termios_from`、`enter_flashing/exit_flashing`、`resolve_flash_target`、
  `FlashEndpoint(start/stop/is_flashing/on_flash_open)`、`mcu.patterns/mcu.status` 全程一致。
