# #120 測試污染 live state.json 雙向量根修 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在有 production daemon 的機器上跑完整 pytest suite，live state.json／WAL／config.yaml／daemon 零接觸；復發時 gate 立刻紅；unittest runner 下 state 維度同樣安全。

**Architecture:** 兩個 production 根修（`SessionManager` state_path 注入＋CLI `--socket` sentinel）＋測試側三層防線（`tests/conftest.py` top-level env 隔離、autouse STATE_PATH patch、`tests/liveguard.py` 四維 live guard）＋subprocess／per-file 併修。詳見 `docs/superpowers/specs/2026-07-02-issue-120-test-state-isolation-design.md`（spec v2）與 `openspec/changes/test-state-isolation-120/`。

**Tech Stack:** Python 3.10+、pytest（conftest hook）、unittest、systemd（guard 4 唯讀查詢）、JSON-RPC unix socket（`sw_core.client.rpc_call`）。

**執行環境注意：**
- 工作區：`/home/paul_chen/prj_pri/serialwrap/.worktrees/120-test-state-isolation`（分支 `feature/120-test-state-isolation`）。開工前 `git branch --show-current` 確認。
- **Task 5 完成前，跑測試一律帶外層 env 隔離**（conftest 防線還沒上，直接跑會污染本機 live state）：
  ```bash
  ISO=$(mktemp -d) && env SERIALWRAP_STATE_DIR="$ISO/state" SERIALWRAP_WAL_DIR="$ISO/wal" \
    SERIALWRAP_CONFIG_DIR="$ISO/config" SERIALWRAP_LOG_DIR="$ISO/blog" \
    SERIALWRAP_EVENTS_DIR="$ISO/ev" SERIALWRAP_EVENTS_RUNTIME_DIR="$ISO/ev-rt" \
    SERIALWRAP_BY_ID_DIR="$ISO/by-id" SERIALWRAP_BY_PATH_DIR="$ISO/by-path" \
    python3 -m pytest -q <target>
  ```
  下文以 `[ISO-ENV]` 代稱這組前綴。Task 5 之後（conftest 已上）直接 `python3 -m pytest` 即可。
- **修好前不得跑 `tests/test_human_agent_coexist.py` 與 `tests/test_multiagent_e2e.py`**（會經向量 2 把 RPC 打到 live daemon）——直到 Task 3＋Task 6 完成。
- 已知 pre-existing flaky（非本 change 造成，見 CLAUDE.md 與 memory）：e2e `agent TX count mismatch`、coexist `t8_full_run_simulation`、`test_t1_wal_reset_preserves_console`；PTY-heavy 6 檔在並行 suite 時競態。
- Commit trailer 一律：
  ```
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  ```

## File Structure

| 檔案 | 動作 | 職責 |
|---|---|---|
| `sw_core/wal.py` | Modify（:16-17） | `WalWriter.wal_dir` None-sentinel，建構時解析 |
| `sw_core/session_manager.py` | Modify（:340-357、:409-435、:479-520） | `state_path` 注入；state I/O 全走 `self._state_path` |
| `sw_core/service.py` | Modify（:216-240） | `state_path` 透傳 |
| `sw_core/cli.py` | Modify（:274-312、:505、:175-224） | `--socket` sentinel；`_resolve_endpoint`／`_run_daemon_start` |
| `tests/test_state_path_injection.py` | Create | Task 1/2 的 RED 測試 |
| `tests/test_resolve_endpoint_sentinel.py` | Create | Task 3 的 RED 測試 |
| `tests/liveguard.py` | Create | live 路徑公式＋快照＋純函式判定（無 sw_core 依賴的頂層 import） |
| `tests/test_liveguard.py` | Create | 每一失敗模式一個 case |
| `tests/conftest.py` | Create | 三層防線（env 隔離／autouse patch／sessionfinish gate） |
| `tests/state_iso.py` | Create | per-file 隔離共用 helper（unittest＋pytest 兩用） |
| `tests/test_setup_materialize.py` | Modify（前 2 測試） | 補 `delenv` |
| `tests/test_human_agent_coexist.py` | Modify（setUp/tearDown） | CONFIG_DIR＋addCleanup |
| `tests/test_multiagent_e2e.py` | Modify（env 區塊） | CONFIG_DIR＋WAL_DIR |
| 8 檔未隔離測試 | Modify | per-file 隔離（Task 7 附表） |
| `CLAUDE.md` | Modify（測試政策） | pytest 為準的註記 |
| `changelog.d/120-test-state-isolation.md` | Create | R-09 fragment |

---

### Task 1: `WalWriter` None-sentinel

**Files:**
- Create: `tests/test_state_path_injection.py`
- Modify: `sw_core/wal.py:16-17`

- [ ] **Step 1: 寫 RED 測試**

```python
# tests/test_state_path_injection.py
"""#120：state/WAL 路徑注入與 def-time 凍結消除的單元測試。"""
from __future__ import annotations

from unittest.mock import MagicMock


def test_walwriter_default_resolves_at_construction(tmp_path, monkeypatch):
    """WalWriter() 的 default wal_dir 須於建構時讀模組層 WAL_DIR，而非 def-time 凍結值。"""
    import sw_core.wal as wal_mod

    monkeypatch.setattr(wal_mod, "WAL_DIR", str(tmp_path / "patched-wal"))
    w = wal_mod.WalWriter()
    assert w.wal_path == str(tmp_path / "patched-wal" / "raw.wal.ndjson")
    assert (tmp_path / "patched-wal").is_dir()
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `[ISO-ENV] python3 -m pytest tests/test_state_path_injection.py::test_walwriter_default_resolves_at_construction -v`
Expected: FAIL——`w.wal_path` 指向 import-time 凍結的舊 `WAL_DIR`（def-time default 無視 setattr patch）。

- [ ] **Step 3: 最小實作**

`sw_core/wal.py:16-17` 改為：

```python
    def __init__(self, wal_dir: str | None = None, rotate_bytes: int = DEFAULT_WAL_ROTATE_BYTES) -> None:
        # None-sentinel：於建構時解析模組層 WAL_DIR（#120）——def-time default 會在類別定義時
        # 凍結 import 當下的值，使 conftest env 隔離與 setattr(wal, "WAL_DIR", ...) 全部失效。
        self._wal_dir = wal_dir or WAL_DIR
```

- [ ] **Step 4: 跑測試確認 GREEN＋WAL 回歸**

Run: `[ISO-ENV] python3 -m pytest tests/test_state_path_injection.py tests/test_wal.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add tests/test_state_path_injection.py sw_core/wal.py
git commit -m "fix(wal): WalWriter wal_dir 改 None-sentinel 建構時解析，消除 def-time 凍結（#120）"
```

---

### Task 2: `SessionManager` state_path 注入＋`SerialwrapService` 透傳

**Files:**
- Modify: `tests/test_state_path_injection.py`（追加）
- Modify: `sw_core/session_manager.py:340-357`（簽章）、`:409-435`（`_load_state`）、`:479-520`（`_save_state`）
- Modify: `sw_core/service.py:216-240`

- [ ] **Step 1: 追加 RED 測試**

在 `tests/test_state_path_injection.py` 追加：

```python
def _mk_manager(**kw):
    from sw_core.session_manager import SessionManager

    return SessionManager(
        [],
        MagicMock(),
        on_ready=lambda sid: None,
        on_detached=lambda sid: None,
        **kw,
    )


def test_injected_state_path_wins(tmp_path, monkeypatch):
    """注入 state_path 後，建構期的 _save_state 寫注入路徑、不碰模組層 STATE_PATH。"""
    import sw_core.session_manager as sm

    module_level = tmp_path / "module" / "state.json"
    monkeypatch.setattr(sm, "STATE_PATH", str(module_level))
    injected = tmp_path / "injected" / "state.json"
    _mk_manager(state_path=str(injected))
    assert injected.exists()
    assert not module_level.exists()


def test_default_falls_back_to_module_global(tmp_path, monkeypatch):
    """未注入時 fallback 讀模組層全域（於建構時）——既有 19 檔 setattr 隔離手法必須持續有效。"""
    import sw_core.session_manager as sm

    module_level = tmp_path / "module" / "state.json"
    monkeypatch.setattr(sm, "STATE_PATH", str(module_level))
    _mk_manager()
    assert module_level.exists()


def test_corrupt_backup_follows_injected_path(tmp_path, monkeypatch):
    """JSON 損毀備份（.corrupt）須跟隨注入路徑。"""
    import sw_core.session_manager as sm

    monkeypatch.setattr(sm, "STATE_PATH", str(tmp_path / "module" / "state.json"))
    injected = tmp_path / "injected" / "state.json"
    injected.parent.mkdir(parents=True)
    injected.write_text("{not-json", encoding="utf-8")
    _mk_manager(state_path=str(injected))
    assert (tmp_path / "injected" / "state.json.corrupt").exists()


def test_service_passthrough(tmp_path, monkeypatch):
    """SerialwrapService(state_path=...) 透傳到內部 SessionManager。"""
    import sw_core.service as svc_mod
    import sw_core.session_manager as sm
    import sw_core.wal as wal_mod

    monkeypatch.setattr(sm, "STATE_PATH", str(tmp_path / "module-state.json"))
    monkeypatch.setattr(wal_mod, "WAL_DIR", str(tmp_path / "wal"))
    monkeypatch.setattr(svc_mod, "EVENTS_DIR", str(tmp_path / "ev"))
    monkeypatch.setattr(svc_mod, "EVENTS_RUNTIME_DIR", str(tmp_path / "ev-rt"))
    monkeypatch.setattr(svc_mod, "EVENTS_LOG_PATH", str(tmp_path / "ev-rt" / "events.ndjson"))
    injected = tmp_path / "svc" / "state.json"
    svc_mod.SerialwrapService([], state_path=str(injected))
    assert injected.exists()
    assert not (tmp_path / "module-state.json").exists()
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `[ISO-ENV] python3 -m pytest tests/test_state_path_injection.py -v`
Expected: 新增 4 個測試 FAIL——`SessionManager.__init__() got an unexpected keyword argument 'state_path'`（Task 1 的測試維持 PASS）。

- [ ] **Step 3: 實作 `session_manager.py`**

簽章（`:340-350`）加 keyword＋建構時解析：

```python
    def __init__(
        self,
        profiles: list[SessionProfile],
        wal: WalWriter,
        *,
        templates: list[ProfileTemplate] | None = None,
        max_sessions: int = 16,
        on_ready: Callable[[str], None],
        on_detached: Callable[[str], None],
        on_console_line: Callable[[str, str, str], None] | None = None,
        state_path: str | None = None,
    ) -> None:
        # state.json 路徑注入（#120）：daemon 走 default（模組層 STATE_PATH），測試注入 tmp。
        # fallback 於「建構時」讀模組層全域（非 def-time default），使既有
        # setattr(session_manager, "STATE_PATH", ...) 的測試隔離手法持續有效。
        self._state_path = state_path or STATE_PATH
```

`_load_state`／`_save_state` 內所有 `STATE_PATH` 改 `self._state_path`（共 8 處）：
- `:410` `os.path.exists(self._state_path)`
- `:413` `open(self._state_path, "rb")`
- `:423` f-string 錯誤訊息內
- `:433` `backup = f"{self._state_path}.corrupt"`
- `:435` `os.replace(self._state_path, backup)`
- `:480` `os.makedirs(os.path.dirname(self._state_path), exist_ok=True)`
- `:506` `state_dir = os.path.dirname(self._state_path)`
- `:515` `os.replace(tmp_path, self._state_path)`

確認改完無殘留：`grep -n '[^_]STATE_PATH' sw_core/session_manager.py` 應只剩 `:32` 的 import 與 `__init__` 的 fallback。

- [ ] **Step 4: 實作 `service.py` 透傳**

`:216-224` 簽章加參數、`:232` 傳入：

```python
    def __init__(
        self,
        profiles: list[SessionProfile],
        *,
        templates: list[ProfileTemplate] | None = None,
        max_sessions: int = 16,
        by_id_dir: str = DEVICE_BY_ID_DIR,
        by_path_dir: str = DEVICE_BY_PATH_DIR,
        state_path: str | None = None,
    ) -> None:
```

```python
        self._sessions = SessionManager(
            profiles,
            self._wal,
            templates=templates,
            max_sessions=max_sessions,
            on_ready=self._on_ready,
            on_detached=self._on_detached,
            on_console_line=self._on_console_line,
            state_path=state_path,
        )
```

- [ ] **Step 5: 跑測試確認 GREEN＋回歸**

Run: `[ISO-ENV] python3 -m pytest tests/test_state_path_injection.py tests/test_state_persistence_atomic.py tests/test_bounded_memory.py tests/test_session_bind.py tests/test_windows_claim.py -v`
Expected: 全 PASS（fallback 相容性由後三檔實證）。

- [ ] **Step 6: Commit**

```bash
git add tests/test_state_path_injection.py sw_core/session_manager.py sw_core/service.py
git commit -m "feat(session): SessionManager/SerialwrapService 注入 state_path，根修 in-process 測試寫 live state（#120）"
```

---

### Task 3: CLI `--socket` sentinel

**Files:**
- Create: `tests/test_resolve_endpoint_sentinel.py`
- Modify: `sw_core/cli.py:505`（argparse default）、`:274-312`（`_resolve_endpoint`）、`:175-224`（`_run_daemon_start`）

- [ ] **Step 1: 寫 RED 測試**

```python
# tests/test_resolve_endpoint_sentinel.py
"""#120 向量 2：--socket 明確性判準改 sentinel（有傳即明確），杜絕等值誤判路由到 live daemon。"""
from __future__ import annotations

import argparse

import sw_core.cli as cli


def _ns(endpoint=None, socket=None):
    return argparse.Namespace(endpoint=endpoint, socket=socket)


def test_socket_equal_to_default_is_explicit(monkeypatch):
    """傳入恰等於預設 SOCKET_PATH 的 --socket 須被尊重：直接回傳、不讀 config。"""
    def _boom():
        raise AssertionError("不得 fallback 讀 config（--socket 已明確指定）")

    monkeypatch.setattr(cli, "_safe_runtime_config", _boom)
    assert cli._resolve_endpoint(_ns(socket=cli.SOCKET_PATH)) == cli.SOCKET_PATH


def test_no_socket_falls_back_to_config(monkeypatch):
    class _RC:
        def socket_path(self):
            return "/cfg/live.sock"

        def mode(self):
            return "on-demand"

    monkeypatch.setattr(cli, "_safe_runtime_config", lambda: _RC())
    monkeypatch.setattr(cli, "_endpoint_alive", lambda p: True)
    assert cli._resolve_endpoint(_ns()) == "/cfg/live.sock"


def test_no_socket_no_config_uses_default(monkeypatch):
    monkeypatch.setattr(cli, "_safe_runtime_config", lambda: None)
    assert cli._resolve_endpoint(_ns()) == cli.SOCKET_PATH


def test_explicit_endpoint_still_wins(monkeypatch):
    monkeypatch.setattr(cli, "_safe_runtime_config", lambda: None)
    assert cli._resolve_endpoint(_ns(endpoint="tcp://127.0.0.1:1", socket="/x")) == "tcp://127.0.0.1:1"


def test_parser_socket_default_is_none():
    """argparse default 必須是 None sentinel。"""
    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is None:
        import pytest

        pytest.skip("parser builder 名稱不同——改以 main() 的 parser 驗證（實作時對齊實際函式名）")
    args = parser.parse_args(["session", "list"])
    assert args.socket is None
```

> 注意：`test_parser_socket_default_is_none` 的 parser 取得方式依 `cli.py` 實際結構調整——找 `argparse.ArgumentParser(prog="serialwrap"...)` 的建構函式名（`cli.py:494` 附近），若 parser 建構內嵌在 `main()` 就改為 `subprocess` 跑 `serialwrap --help` 型驗證或直接刪除此 case（前四個 case 已覆蓋語意）。

- [ ] **Step 2: 跑測試確認 RED**

Run: `[ISO-ENV] python3 -m pytest tests/test_resolve_endpoint_sentinel.py -v`
Expected: `test_socket_equal_to_default_is_explicit` FAIL（現行 `args.socket != SOCKET_PATH` 等值誤判 → 呼叫 `_safe_runtime_config` → AssertionError）；其餘依現行實作可能 PASS。

- [ ] **Step 3: 實作**

`cli.py:505`：

```python
    p.add_argument("--socket", default=None, help="本機 daemon 的 Unix socket 路徑（未指定時依 config.yaml 與 XDG 執行期目錄解析，可用 SERIALWRAP_RUN_DIR 覆寫）")
```

`_resolve_endpoint`（`:289-298`）：

```python
    if args.socket:
        # 有傳即明確（#120 向量 2）：不得與 import-time 預設值比對——測試以 env 覆寫 RUN_DIR 時
        # 傳入值恰等於預設 SOCKET_PATH，等值比對會誤判為「未指定」而 fallback 到 live config。
        return args.socket
    rc = _safe_runtime_config()
    cfg_sock = None
    if rc is not None:
        try:
            cfg_sock = rc.socket_path()
        except Exception:
            cfg_sock = None
    chosen = cfg_sock or SOCKET_PATH
```

（`:299` 起 dangling fallback 邏輯不動；docstring 的「明確指定的 ``--socket``（非預設）」措辭同步改為「明確傳入的 ``--socket``」。）

`_run_daemon_start` on-demand 路徑（`:175` 前）加一行、後續消費點改用 `sock`：

```python
    sock = args.socket or SOCKET_PATH
```

- `:180-181` `"--socket", sock,`
- `:211` `rpc_call(sock, "health.ping", ...)`
- `:213` `"socket": sock`
- `:216` `rpc_call(sock, "health.status", ...)`

- [ ] **Step 4: 跑測試確認 GREEN＋回歸**

Run: `[ISO-ENV] python3 -m pytest tests/test_resolve_endpoint_sentinel.py tests/test_cli_daemon_start.py tests/test_autospawn_gate.py tests/test_daemon_service_selector.py tests/test_constants_endpoint.py -v`
Expected: 全 PASS。若 daemon start 回歸測試斷言了 `--socket` 預設值行為，依「等價替換」原則修測試斷言（結果 socket 應仍為 `SOCKET_PATH`）。

- [ ] **Step 5: Commit**

```bash
git add tests/test_resolve_endpoint_sentinel.py sw_core/cli.py
git commit -m "fix(cli): --socket 改 None sentinel，有傳即明確；根修測試 RPC 等值誤判路由到 live daemon（#120）"
```

---

### Task 4: `tests/liveguard.py` 純函式（TDD）

**Files:**
- Create: `tests/liveguard.py`
- Create: `tests/test_liveguard.py`

- [ ] **Step 1: 寫 RED 測試**

```python
# tests/test_liveguard.py
"""#120 live guard 判定純函式——每一個 adversarial review F1/F2 失敗模式一個 case。"""
from __future__ import annotations

import json

import liveguard


def _snap(content: bytes | None):
    if content is None:
        return liveguard.FileSnap(exists=False)
    return liveguard.FileSnap(exists=True, content=content, size=len(content))


def _state(aliases=None, bindings=None, released=None):
    return json.dumps(
        {"aliases": aliases or {}, "bindings": bindings or {},
         "released": released or {}, "profile_pins": {}, "profile_detected": {}},
        sort_keys=True, separators=(",", ":"),
    ).encode()


PRE = _state(aliases={"dut": {"session_id": "prpl-template:COM0"}},
             released={"p:COM9": {"by_id": "x"}})


# ---- Guard 1: state ----

def test_state_created_from_absent_fails():
    v, _ = liveguard.classify_state(_snap(None), _snap(_state()), mode="strict")
    assert v == "FAIL"


def test_state_byte_identical_passes():
    v, _ = liveguard.classify_state(_snap(PRE), _snap(PRE), mode="strict")
    assert v == "PASS"


def test_state_clean_overwrite_fails_strict():
    """乾淨覆寫（無污染特徵、released 消失）在 strict 必 FAIL。"""
    v, _ = liveguard.classify_state(_snap(PRE), _snap(_state()), mode="strict")
    assert v == "FAIL"


def test_state_released_cleared_fails_even_in_warn():
    """released entry 消失＝結構性破壞，warn 模式仍 FAIL。"""
    v, _ = liveguard.classify_state(_snap(PRE), _snap(_state(aliases={"dut": {"session_id": "prpl-template:COM0"}})), mode="warn")
    assert v == "FAIL"


def test_state_pollution_marker_fails_even_in_warn():
    post = _state(aliases={"dut": {"session_id": "prpl-template:COM0"}},
                  bindings={"prpl-template:COM0": "/tmp/sw-coexist-x/by-id/fake-uart0"},
                  released={"p:COM9": {"by_id": "x"}})
    v, _ = liveguard.classify_state(_snap(PRE), _snap(post), mode="warn")
    assert v == "FAIL"


def test_state_benign_addition_warns_in_warn_mode():
    """warn 模式下，非結構性、無特徵的變更（如 live daemon 合法新增 alias）→ WARN。"""
    post = _state(aliases={"dut": {"session_id": "prpl-template:COM0"},
                           "sta": {"session_id": "prpl-template:COM1"}},
                  released={"p:COM9": {"by_id": "x"}})
    v, _ = liveguard.classify_state(_snap(PRE), _snap(post), mode="warn")
    assert v == "WARN"


def test_state_deleted_fails():
    v, _ = liveguard.classify_state(_snap(PRE), _snap(None), mode="warn")
    assert v == "FAIL"


# ---- Guard 2: WAL ----

def test_wal_append_passes():
    v, _ = liveguard.classify_wal(
        liveguard.FileSnap(exists=True, size=100), liveguard.FileSnap(exists=True, size=200))
    assert v == "PASS"


def test_wal_shrink_fails():
    v, _ = liveguard.classify_wal(
        liveguard.FileSnap(exists=True, size=200), liveguard.FileSnap(exists=True, size=100))
    assert v == "FAIL"


def test_wal_deleted_fails():
    v, _ = liveguard.classify_wal(
        liveguard.FileSnap(exists=True, size=200), liveguard.FileSnap(exists=False))
    assert v == "FAIL"


def test_wal_absent_both_passes():
    v, _ = liveguard.classify_wal(liveguard.FileSnap(exists=False), liveguard.FileSnap(exists=False))
    assert v == "PASS"


def test_shell_wal_any_change_fails():
    """外層 shell SERIALWRAP_WAL_DIR 維度：任何變更（size 或存在性）→ FAIL。"""
    v, _ = liveguard.classify_shell_wal(
        liveguard.FileSnap(exists=True, size=100), liveguard.FileSnap(exists=True, size=101))
    assert v == "FAIL"


# ---- Guard 3: config ----

def test_config_change_fails():
    v, _ = liveguard.classify_config(_snap(b"a: 1\n"), _snap(b"a: 2\n"))
    assert v == "FAIL"


def test_config_identical_passes():
    v, _ = liveguard.classify_config(_snap(b"a: 1\n"), _snap(b"a: 1\n"))
    assert v == "PASS"


# ---- Guard 4: daemon ----

def _dsnap(**kw):
    base = dict(reachable=True, active=True, main_pid=1234,
                sessions={"prpl-template:COM0": ("2026-07-02T00:00:00+00:00", 1)})
    base.update(kw)
    return liveguard.DaemonSnap(**base)


def test_daemon_unreachable_pre_skips():
    v, _ = liveguard.classify_daemon(_dsnap(reachable=False), _dsnap())
    assert v == "SKIP"


def test_daemon_pid_change_fails():
    v, _ = liveguard.classify_daemon(_dsnap(), _dsnap(main_pid=5678))
    assert v == "FAIL"


def test_daemon_inactive_post_fails():
    v, _ = liveguard.classify_daemon(_dsnap(), _dsnap(active=False))
    assert v == "FAIL"


def test_daemon_tx_advance_fails():
    post = _dsnap(sessions={"prpl-template:COM0": ("2026-07-02T00:05:00+00:00", 1)})
    v, _ = liveguard.classify_daemon(_dsnap(), post)
    assert v == "FAIL"


def test_daemon_bridge_generation_change_fails():
    post = _dsnap(sessions={"prpl-template:COM0": ("2026-07-02T00:00:00+00:00", 2)})
    v, _ = liveguard.classify_daemon(_dsnap(), post)
    assert v == "FAIL"


def test_daemon_untouched_passes():
    v, _ = liveguard.classify_daemon(_dsnap(), _dsnap())
    assert v == "PASS"
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `[ISO-ENV] python3 -m pytest tests/test_liveguard.py -v`
Expected: 全 FAIL——`ModuleNotFoundError: No module named 'liveguard'`（乾淨 RED；pytest 預設 importmode 會把 `tests/` prepend 到 sys.path，`import liveguard` 於實作後可解析）。

- [ ] **Step 3: 實作 `tests/liveguard.py`**

```python
# tests/liveguard.py
"""#120 live guard：live 路徑公式、快照、純函式判定。

設計約束：
- 頂層 import 不得碰 sw_core（conftest 在覆寫 env 前 import 本模組；提前 import sw_core
  會把 constants 凍結在 live 路徑）。需要 RPC 時於函式內 lazy import。
- live 路徑一律 XDG 公式並「刻意忽略 SERIALWRAP_*」隔離變數——daemon（systemd）env
  沒有它們；勿沿用 setup_cmd._state_path_for（systemd-system 回 /var/lib 與實況不符）。
- 判定函式為純函式（吃快照、回 (verdict, reason)），失敗模式由 tests/test_liveguard.py 逐一釘住。
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess

POLLUTION_MARKERS = ("/tmp/sw-", "test-tpl", '"test:', "fake-uart")
STRUCTURAL_KEYS = ("aliases", "bindings", "released")
VERDICT_FAIL = "FAIL"
VERDICT_WARN = "WARN"
VERDICT_PASS = "PASS"
VERDICT_SKIP = "SKIP"


@dataclasses.dataclass(frozen=True)
class FileSnap:
    exists: bool
    content: bytes = b""
    size: int = 0


@dataclasses.dataclass(frozen=True)
class DaemonSnap:
    reachable: bool
    active: bool = False
    main_pid: int | None = None
    # session_id → (last_tx_at, bridge_generation)
    sessions: dict[str, tuple[str | None, int | None]] = dataclasses.field(default_factory=dict)


# ---- live 路徑公式 ----

def live_state_path() -> str:
    home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(home, "serialwrap", "state.json")


def live_wal_path() -> str:
    home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(home, "serialwrap", "wal", "raw.wal.ndjson")


def live_config_path() -> str:
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(home, "serialwrap", "config.yaml")


# ---- 快照 ----

def snap_file(path: str) -> FileSnap:
    try:
        with open(path, "rb") as fp:
            content = fp.read()
    except OSError:
        return FileSnap(exists=False)
    return FileSnap(exists=True, content=content, size=len(content))


def snap_daemon() -> DaemonSnap:
    """唯讀查 systemd unit 與 live daemon session 快照；任何一步失敗 → unreachable（SKIP）。"""
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "ActiveState,MainPID", "serialwrap"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        props = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
        active = props.get("ActiveState") == "active"
        main_pid = int(props.get("MainPID") or 0) or None
    except Exception:
        return DaemonSnap(reachable=False)
    if not active or not main_pid:
        return DaemonSnap(reachable=False)
    try:
        from sw_core.client import rpc_call  # lazy：此時 env 已隔離，僅用 client、endpoint 顯式傳入

        endpoint = _live_socket_path()
        resp = rpc_call(endpoint, "session.list", {}, timeout_s=3.0)
        sessions: dict[str, tuple[str | None, int | None]] = {}
        for s in resp.get("sessions") or []:
            sessions[s.get("session_id", "?")] = (s.get("last_tx_at"), s.get("bridge_generation"))
    except Exception:
        return DaemonSnap(reachable=False)
    return DaemonSnap(reachable=True, active=active, main_pid=main_pid, sessions=sessions)


def _live_socket_path() -> str:
    """live CLI 同款解析：config.yaml socket_path → systemd-system canonical。唯讀。"""
    try:
        import yaml

        with open(live_config_path(), "r", encoding="utf-8") as fp:
            cfg = yaml.safe_load(fp) or {}
        sock = cfg.get("socket_path")
        if isinstance(sock, str) and sock.strip():
            return sock.strip()
    except Exception:
        pass
    return "/run/serialwrap/serialwrapd.sock"


# ---- 純函式判定 ----

def _structural_damage(pre: FileSnap, post: FileSnap) -> str | None:
    """回傳結構性破壞原因；無則 None。pre 必須 exists。"""
    if not post.exists:
        return "live state.json 被刪除"
    try:
        pre_obj = json.loads(pre.content)
        post_obj = json.loads(post.content)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return "live state.json 內容非 JSON（截斷/損毀）"
    for key in STRUCTURAL_KEYS:
        pre_entries = set((pre_obj.get(key) or {}).keys()) if isinstance(pre_obj, dict) else set()
        post_entries = set((post_obj.get(key) or {}).keys()) if isinstance(post_obj, dict) else set()
        missing = pre_entries - post_entries
        if missing:
            return f"live state.json 的 {key} 少了 {sorted(missing)}"
    text = post.content.decode("utf-8", errors="replace")
    for marker in POLLUTION_MARKERS:
        if marker in text:
            return f"live state.json 含污染特徵 {marker!r}"
    return None


def classify_state(pre: FileSnap, post: FileSnap, mode: str) -> tuple[str, str]:
    if not pre.exists and not post.exists:
        return VERDICT_PASS, "live state.json 不存在（前後皆然）"
    if not pre.exists and post.exists:
        return VERDICT_FAIL, "live state.json 於測試期間被建立（隔離失效）"
    if pre.exists and not post.exists:
        return VERDICT_FAIL, "live state.json 於測試期間被刪除"
    if pre.content == post.content:
        return VERDICT_PASS, "live state.json byte-identical"
    damage = _structural_damage(pre, post)
    if damage:
        return VERDICT_FAIL, damage
    if mode == "warn":
        return VERDICT_WARN, "live state.json 有非結構性變更（warn 模式放行，請人工確認）"
    return VERDICT_FAIL, "live state.json 內容變更（strict 模式；確為 live daemon 合法活動時可 SERIALWRAP_LIVE_GATE=warn）"


def classify_wal(pre: FileSnap, post: FileSnap) -> tuple[str, str]:
    if pre.exists and not post.exists:
        return VERDICT_FAIL, "live WAL 於測試期間消失（wal reset/清除特徵）"
    if pre.exists and post.exists and post.size < pre.size:
        return VERDICT_FAIL, f"live WAL 縮小（{pre.size}→{post.size}；wal reset 特徵，罕見誤報源：64MB rotation）"
    return VERDICT_PASS, "live WAL 未縮小（append 為 live daemon 常態）"


def classify_shell_wal(pre: FileSnap, post: FileSnap) -> tuple[str, str]:
    if pre.exists != post.exists or pre.size != post.size:
        return VERDICT_FAIL, "外層 shell SERIALWRAP_WAL_DIR 的 WAL 有變更（env 繼承類回歸）"
    return VERDICT_PASS, "shell WAL 維度未變"


def classify_config(pre: FileSnap, post: FileSnap) -> tuple[str, str]:
    if pre.exists != post.exists or pre.content != post.content:
        return VERDICT_FAIL, "live config.yaml 於測試期間被變更（CLI 應唯讀）"
    return VERDICT_PASS, "live config.yaml 未變"


def classify_daemon(pre: DaemonSnap, post: DaemonSnap) -> tuple[str, str]:
    if not pre.reachable:
        return VERDICT_SKIP, "live daemon 不存在或不可達（CI/無 daemon 機器）"
    if not post.reachable or not post.active:
        return VERDICT_FAIL, "live daemon 於測試期間停止或不可達（測試動到 live service）"
    if post.main_pid != pre.main_pid:
        return VERDICT_FAIL, f"live daemon MainPID 變更（{pre.main_pid}→{post.main_pid}；被 restart）"
    for sid, (pre_tx, pre_gen) in pre.sessions.items():
        post_tx, post_gen = post.sessions.get(sid, (None, None))
        if post_tx != pre_tx:
            return VERDICT_FAIL, f"live session {sid} 的 last_tx_at 前進（{pre_tx}→{post_tx}；有東西對真板 TX）"
        if post_gen != pre_gen:
            return VERDICT_FAIL, f"live session {sid} 的 bridge_generation 變更（{pre_gen}→{post_gen}；被 rebind/reattach）"
    return VERDICT_PASS, "live daemon 未被觸碰"
```

- [ ] **Step 4: 跑測試確認 GREEN**

Run: `[ISO-ENV] python3 -m pytest tests/test_liveguard.py -v`
Expected: 全 PASS（26 cases；plan 原碼 20 個＋review 修正追加 6 個）。

- [ ] **Step 5: Commit**

```bash
git add tests/liveguard.py tests/test_liveguard.py
git commit -m "test(liveguard): #120 live guard 四維判定純函式＋逐失敗模式測試"
```

---

### Task 5: `tests/conftest.py` 三層防線＋shadow 併修

**Files:**
- Create: `tests/conftest.py`
- Modify: `tests/test_setup_materialize.py`（前 2 測試）

- [ ] **Step 1: 寫 `tests/conftest.py`**

```python
# tests/conftest.py
"""#120 三層防線：live 資源快照 → 強制 env 隔離 → per-test STATE_PATH patch → session-finish gate。

時序關鍵：本檔 module top-level 於 pytest collection 之前執行、早於任何測試模組 import——
sw_core/constants.py 的路徑常數是 import-time 凍結，fixture 階段才 setenv 一律太遲。
限制（載明於 CLAUDE.md）：python3 -m unittest discover 不載入本檔，該跑法的 state 維度
由 8 檔 per-file 隔離（tests/state_iso.py）補上。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import pytest

import liveguard

# ---- 第 0 步：live 快照（必須在覆寫任何 env 之前）----
_MODE = os.environ.get("SERIALWRAP_LIVE_GATE", "strict").strip().lower()
_SHELL_WAL_DIR = os.environ.get("SERIALWRAP_WAL_DIR")  # 覆寫前的外層 shell 值
_PRE_STATE = liveguard.snap_file(liveguard.live_state_path())
_PRE_WAL = liveguard.snap_file(liveguard.live_wal_path())
_PRE_CONFIG = liveguard.snap_file(liveguard.live_config_path())
_PRE_SHELL_WAL = (
    liveguard.snap_file(os.path.join(_SHELL_WAL_DIR, "raw.wal.ndjson")) if _SHELL_WAL_DIR else None
)
_PRE_DAEMON = liveguard.snap_daemon()

# ---- 第 1 層：強制 env 隔離（硬覆寫——外層 shell 可能 export 指向 live 的值）----
_ISO_ROOT = tempfile.mkdtemp(prefix="sw-pytest-iso-")


def _iso(name: str) -> str:
    path = os.path.join(_ISO_ROOT, name)
    os.makedirs(path, exist_ok=True)
    return path


os.environ["SERIALWRAP_STATE_DIR"] = _iso("state")
os.environ["SERIALWRAP_RUN_DIR"] = _iso("run")
os.environ["SERIALWRAP_WAL_DIR"] = _iso("wal")
os.environ["SERIALWRAP_CONFIG_DIR"] = _iso("config")
os.environ["SERIALWRAP_LOG_DIR"] = _iso("blog")
os.environ["SERIALWRAP_EVENTS_DIR"] = _iso("events.d")
os.environ["SERIALWRAP_EVENTS_RUNTIME_DIR"] = _iso("events-rt")
# 空目錄：防 in-process 動態偵測抓到真板（two-reader）
os.environ["SERIALWRAP_BY_ID_DIR"] = _iso("by-id")
os.environ["SERIALWRAP_BY_PATH_DIR"] = _iso("by-path")

# 這些變數優先序高於上方已覆寫目錄的推導值（如 SERIALWRAP_ENDPOINT 蓋過 RUN_DIR 推導的
# socket、PROFILE_DIR 蓋過 CONFIG_DIR/profiles）——外層 shell 若 export 會穿透隔離。
# pop 使其回歸「由已隔離目錄推導」的 default；hard-override 反而要在此重算一次路徑。
for _k in (
    "SERIALWRAP_ENDPOINT",
    "SERIALWRAP_PROFILE_DIR",
    "SERIALWRAP_TTYMCU_PATH",
    "SERIALWRAP_EVENTS_LOG_PATH",
    "SERIALWRAP_DATA_DIR",
):
    os.environ.pop(_k, None)

# tripwire：第 1 層的前提是 sw_core.constants（import-time 凍結路徑）尚未被載入；
# 若 conftest 頂層 import 鏈提前拉進它，隔離會全然無聲地失效——在此顯式釘住。
assert "sw_core.constants" not in sys.modules, (
    "sw_core.constants 已於 env 隔離前被 import——第 1 層隔離失效（檢查 conftest 頂層 import 鏈）"
)


# ---- 第 2 層：per-test STATE_PATH patch（消除順序耦合）----
@pytest.fixture(autouse=True)
def _isolate_state_path(tmp_path, monkeypatch):
    import sw_core.session_manager as sm

    monkeypatch.setattr(sm, "STATE_PATH", str(tmp_path / "state.json"))
    yield


# ---- 第 3 層：session-finish live guard ----
def pytest_sessionfinish(session, exitstatus):
    results = [
        ("state", liveguard.classify_state(
            _PRE_STATE, liveguard.snap_file(liveguard.live_state_path()), mode=_MODE)),
        ("wal", liveguard.classify_wal(
            _PRE_WAL, liveguard.snap_file(liveguard.live_wal_path()))),
        ("config", liveguard.classify_config(
            _PRE_CONFIG, liveguard.snap_file(liveguard.live_config_path()))),
        ("daemon", liveguard.classify_daemon(_PRE_DAEMON, liveguard.snap_daemon())),
    ]
    if _PRE_SHELL_WAL is not None:
        results.append(("shell-wal", liveguard.classify_shell_wal(
            _PRE_SHELL_WAL, liveguard.snap_file(os.path.join(_SHELL_WAL_DIR, "raw.wal.ndjson")))))

    tr = session.config.pluginmanager.get_plugin("terminalreporter")

    def _emit(line: str, *, red: bool = False, yellow: bool = False) -> None:
        if tr is not None:
            tr.write_line(line, red=red, yellow=yellow)
        else:
            print(line, file=sys.stderr)  # reporter 缺席時 gate FAIL 不得無聲

    failed = False
    for name, (verdict, reason) in results:
        line = f"[live-guard:{name}] {verdict}: {reason}"
        if verdict == liveguard.VERDICT_FAIL:
            failed = True
        if verdict != liveguard.VERDICT_PASS:
            _emit(line, red=(verdict == liveguard.VERDICT_FAIL),
                  yellow=(verdict == liveguard.VERDICT_WARN))
    if failed:
        _emit("[live-guard] 偵測到 live 資源被測試觸碰（#120 回歸）——詳見上方各維度", red=True)
        # 只在原 exitstatus 乾淨時升為 1——不蓋掉 interrupted(2)/internal error(3) 等更高語意
        if session.exitstatus == 0:
            session.exitstatus = 1
    shutil.rmtree(_ISO_ROOT, ignore_errors=True)
```

- [ ] **Step 2: 修 `test_setup_materialize.py` 前 2 測試（conftest env shadow）**

`test_materialize_copies_profiles_and_symlinks_skill`（:10-12）與 `test_materialize_does_not_overwrite_existing_profiles`（:22-24），在 `monkeypatch.setenv` 兩行之後各加：

```python
    monkeypatch.delenv("SERIALWRAP_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SERIALWRAP_DATA_DIR", raising=False)
```

（`setup_cmd._user_dirs` 為 runtime 讀 env 且 `SERIALWRAP_*` 優先於 XDG；conftest 第 1 層設定後會 shadow 掉這兩個只 patch XDG 的測試——同檔 `:60-63` 已有兩維度都管理的先例。）

- [ ] **Step 3: 全 suite 驗證（不再需要 [ISO-ENV]）＋掃同型 shadow**

Run: `python3 -m pytest -q tests/ --ignore=tests/test_human_agent_coexist.py --ignore=tests/test_multiagent_e2e.py --ignore=tests/test_multiagent_stress.py --ignore=tests/test_flash_pump.py --ignore=tests/test_flash_service_wiring.py --ignore=tests/test_agent_defer_tx.py 2>&1 | tail -15`
Expected: 與 baseline 等值（825+ passed）＋新增測試全綠、guard 4 維度輸出 PASS/SKIP、**無新失敗**。若出現新失敗：逐一判別是否「runtime-lazy／reload 讀 XDG 但未管理 `SERIALWRAP_*`」同型 shadow（比照 Step 2 補 `delenv`）或 conftest 設計問題（修 conftest）。

- [ ] **Step 4: 驗證 gate 快照確實在隔離前取得**

Run: `python3 -m pytest -q tests/test_liveguard.py tests/test_state_path_injection.py -p no:cacheprovider 2>&1 | tail -5 && echo "live state mtime: $(stat -c %y ~/.local/state/serialwrap/state.json 2>/dev/null || echo ABSENT)"`
Expected: PASS＋live state.json 的 mtime 未因跑測試而更新。

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/test_setup_materialize.py
git commit -m "test(conftest): #120 三層防線——強制 env 隔離＋autouse STATE_PATH patch＋live guard gate"
```

---

### Task 6: coexist／e2e subprocess 併修

**Files:**
- Modify: `tests/test_human_agent_coexist.py`（setUp `:113-177`、tearDown `:176-194`）
- Modify: `tests/test_multiagent_e2e.py`（env 區塊 `:174-178`）

- [ ] **Step 1: coexist——env 補 CONFIG_DIR＋資源建立即 addCleanup**

setUp 重排（建立一項、註冊一項；LIFO 自然得到 tmux→daemon→fake→tempdir 的正確清理順序），tearDown 整段刪除：

```python
    def setUp(self) -> None:
        self._td = tempfile.mkdtemp(prefix="sw-coexist-")
        self.addCleanup(shutil.rmtree, self._td, ignore_errors=True)
        self._root = pathlib.Path(self._td)
        self._by_id_dir = self._root / "by-id"
        self._profile_dir = self._root / "profiles"
        self._by_id_dir.mkdir(parents=True)
        self._profile_dir.mkdir(parents=True)

        self._fake = FakeTarget()
        self._fake.start()
        self.addCleanup(self._fake.stop)

        self._link_path = self._by_id_dir / "fake-uart0"
        os.symlink(self._fake.slave_path, self._link_path)

        profile = f"""...（原樣不動）..."""
        (self._profile_dir / "test.yaml").write_text(profile, encoding="utf-8")

        self._env = os.environ.copy()
        self._env["SERIALWRAP_STATE_DIR"] = str(self._root / "state")
        self._env["SERIALWRAP_RUN_DIR"] = str(self._root / "run")
        self._env["SERIALWRAP_BY_ID_DIR"] = str(self._by_id_dir)
        self._env["SERIALWRAP_BY_PATH_DIR"] = str(self._root / "by-path")
        self._env["SERIALWRAP_WAL_DIR"] = str(self._root / "wal")
        # #120：隔離 config 維度——否則 CLI 子行程讀 live config.yaml 誤路由到 live daemon（縱深防禦）
        self._env["SERIALWRAP_CONFIG_DIR"] = str(self._root / "config")

        self._socket = str(self._root / "run" / "serialwrapd.sock")
        self._lock = str(self._root / "run" / "serialwrapd.lock")

        self._daemon = subprocess.Popen([...原樣...])
        self.addCleanup(self._stop_daemon)

        # tmux session 名先定、清理先掛——_wait_ready 失敗時 unittest 跳過 tearDown，
        # addCleanup 仍會執行（#120：8 個殭屍 daemon 的成因就是舊 tearDown 被跳過）
        self._tmux_session = f"sw_test_{os.getpid()}"
        self.addCleanup(self._kill_tmux)

        self._wait_ready()

    def _stop_daemon(self) -> None:
        try:
            self._sw("daemon", "stop")
        except Exception:
            pass
        if self._daemon.poll() is None:
            self._daemon.terminate()
            try:
                self._daemon.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._daemon.kill()

    def _kill_tmux(self) -> None:
        subprocess.run(
            ["tmux", "kill-session", "-t", self._tmux_session],
            capture_output=True, timeout=5,
        )
```

（`shutil` 若未 import 則補；profile 字串與 Popen 參數原樣保留。）

- [ ] **Step 2: e2e——env 補兩行**

`tests/test_multiagent_e2e.py:178` 後（`SERIALWRAP_BY_PATH_DIR` 之後）加：

```python
            env["SERIALWRAP_WAL_DIR"] = str(root / "wal")  # #120：勿繼承外層 shell（曾真寫 live ~/b-log）
            env["SERIALWRAP_CONFIG_DIR"] = str(root / "config")  # #120：隔離 config 維度
```

> **Review 追加（quality review 發現的 plan 缺口，實作時併修）**：
> 1. **e2e tempdir 生命週期改 mkdtemp+addCleanup（清理順序倒置）**——原本整個測試包在
>    `with tempfile.TemporaryDirectory()` 內，但 addCleanup（`_cleanup_daemon`／`fake.stop`）在測試
>    return **之後**才跑 → tempdir 先被刪、daemon 還活著：(a) graceful `daemon stop` 永遠打已刪除的
>    socket（dead code，e2e daemon 一律吃 SIGTERM）；(b) daemon 關閉期間寫入 vs rmtree 競態會殘留
>    目錄（`/tmp/serialwrap-e2e-muhjfo6n` 為 6/30 實證殘骸）。修法比照 coexist：
>    `td = tempfile.mkdtemp(prefix="serialwrap-e2e-")` ＋ 緊接 `self.addCleanup(shutil.rmtree, td,
>    ignore_errors=True)`，整段 de-indent——LIFO 保證 rmtree 最後跑（daemon stop 之後）。
> 2. **兩檔 env 各補 `SERIALWRAP_EVENTS_DIR`**——unittest runner 下（無 conftest 防線）throwaway
>    daemon 的 `ensure_runtime_dirs` 會 mkdir 到 live `~/.serialwrap/events.d`，且 live 有 event
>    rules 時會被測試 daemon 載入執行（handler 是 `subprocess.Popen`）。

- [ ] **Step 3: 跑這兩檔驗證（Task 3 已修向量 2，現在可以安全跑了）**

Run: `python3 -m pytest -q tests/test_human_agent_coexist.py tests/test_multiagent_e2e.py 2>&1 | tail -8 && pgrep -af 'sw-coexis[t]' || echo NO_LEAKED_DAEMON`
Expected: 通過（t1/t8/TX-mismatch 為已知 pre-existing flaky，若失敗重跑一次判別）；**無殭屍 daemon 殘留**；conftest guard 輸出全 PASS/SKIP——live daemon 未被觸碰（向量 2 根修的實證）。

- [ ] **Step 4: Commit**

```bash
git add tests/test_human_agent_coexist.py tests/test_multiagent_e2e.py
git commit -m "test(subprocess): coexist/e2e 隔離 config 維度＋coexist addCleanup 根絕 daemon 洩漏（#120）"
```

---

### Task 7: 8 檔 per-file 隔離（unittest runner 防線）

**Files:**
- Create: `tests/state_iso.py`
- Modify: 下表 8 檔

- [ ] **Step 1: 寫共用 helper**

```python
# tests/state_iso.py
"""#120 per-file state 隔離 helper（unittest＋pytest 兩用）。

與 tests/conftest.py 第 2 層刻意冗餘：conftest 防 pytest 下的未來漏網；
本 helper 讓 python3 -m unittest（不載入 conftest）與單檔直跑也安全。
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile


@contextlib.contextmanager
def isolated_state():
    import sw_core.session_manager as sm
    import sw_core.wal as wal_mod

    td = tempfile.mkdtemp(prefix="sw-state-iso-")
    orig_state, orig_wal = sm.STATE_PATH, wal_mod.WAL_DIR
    sm.STATE_PATH = os.path.join(td, "state.json")
    wal_mod.WAL_DIR = os.path.join(td, "wal")
    try:
        yield td
    finally:
        sm.STATE_PATH = orig_state
        wal_mod.WAL_DIR = orig_wal
        shutil.rmtree(td, ignore_errors=True)


def isolate_testcase(tc) -> str:
    """unittest.TestCase 的 setUp 內呼叫：patch STATE_PATH/WAL_DIR，tearDown 階段自動還原。"""
    cm = isolated_state()
    td = cm.__enter__()
    tc.addCleanup(cm.__exit__, None, None, None)
    return td
```

- [ ] **Step 2: 逐檔補隔離**

| 檔案 | 型態 | 動作 |
|---|---|---|
| `tests/test_session_capture.py` | unittest（10 類：TestLogStartStop/TestLogStartErrorPaths/TestLogStopFields/TestRxEdgeCases/TestMultiSessionCapture/TestEnvVarLogDir/TestLogFilename/TestPublicDictCapture/TestSelectorVariants/TestStopCaptureLocked） | 各類 setUp 首行加 `state_iso.isolate_testcase(self)`；無 setUp 的類補上 |
| `tests/test_issue24_heartbeat.py` | unittest（3 類，:113/:149/:195） | 同上 |
| `tests/test_session_activity.py` | unittest（:175 `_make_manager`） | 該類 setUp 加 helper |
| `tests/test_command_guard.py` | unittest（TestCommandGuard，:21） | 加 `def setUp(self): state_iso.isolate_testcase(self)` |
| `tests/test_service_human_console.py` | unittest（TestServiceHumanConsole，6 建構點） | 同上 |
| `tests/test_daemon_service_selector.py` | 依 :211/:225 所在類補（已 patch CONFIG_DIR、缺 STATE_PATH） | setUp 加 helper |
| `tests/test_mcu_cli_rpc.py` | pytest 函式式（:5/:12 在函式內） | 檔頭加 module autouse fixture（下方模板） |
| `tests/test_flash_service_wiring.py` | 依實際型態（:19-:92） | unittest 類→setUp helper；pytest 函式→autouse fixture |

unittest 檔各檔頭部：

```python
import state_iso
```

pytest 函式式檔案的模板：

```python
import pytest

import state_iso


@pytest.fixture(autouse=True)
def _iso_state():
    with state_iso.isolated_state():
        yield
```

實作時逐檔先看建構點實際型態再套模板（audit 行號為 2026-07-02 快照，以現檔為準）。

- [ ] **Step 3: unittest runner 實證（無 conftest 防線下不觸 live）**

Run:
```bash
SENTINEL=$(mktemp -d) && env XDG_STATE_HOME="$SENTINEL" python3 -m unittest tests.test_issue24_heartbeat tests.test_command_guard tests.test_service_human_console -v 2>&1 | tail -3 && { test ! -e "$SENTINEL/serialwrap/state.json" && echo "UNITTEST_ISOLATED_OK" || echo "STILL_POLLUTING"; }
```
Expected: 測試 OK＋`UNITTEST_ISOLATED_OK`。

- [ ] **Step 4: pytest 全量回歸**

Run: `python3 -m pytest -q tests/ --ignore=tests/test_human_agent_coexist.py --ignore=tests/test_multiagent_e2e.py --ignore=tests/test_multiagent_stress.py --ignore=tests/test_flash_pump.py --ignore=tests/test_agent_defer_tx.py 2>&1 | tail -5`
Expected: 無新失敗（flash_service_wiring 此輪包含——它已被 Task 7 改動，需驗證）。

- [ ] **Step 5: Commit**

```bash
git add tests/state_iso.py tests/test_session_capture.py tests/test_issue24_heartbeat.py tests/test_session_activity.py tests/test_command_guard.py tests/test_service_human_console.py tests/test_daemon_service_selector.py tests/test_mcu_cli_rpc.py tests/test_flash_service_wiring.py
git commit -m "test(iso): 8 檔未隔離測試補 per-file STATE_PATH/WAL_DIR 隔離——unittest runner 防線（#120）"
```

---

### Task 8: 文件＋changelog fragment

**Files:**
- Modify: `CLAUDE.md`（測試政策段）
- Create: `changelog.d/120-test-state-isolation.md`
- 檢查: `README.md`（若提及測試跑法）

- [ ] **Step 1: CLAUDE.md 測試政策註記**

「測試政策」段的 unittest 命令後加註：

```markdown
- 亦可：
  ```bash
  python3 -m unittest discover -s tests -v
  ```
  （注意：unittest 不載入 `tests/conftest.py` 的強制 env 隔離與 live guard 防線，僅 state.json 維度有 per-file 隔離；**有 production daemon 的機器一律以 pytest 為準**，#120。）
```

- [ ] **Step 2: changelog fragment**

```markdown
---
type: fix
issue: 120
scope: tests
---
修復測試污染 live state.json 的兩個向量：`SessionManager`/`SerialwrapService` 注入 `state_path`（in-process 測試不再寫 live）；CLI `--socket` 改 None sentinel（有傳即明確，杜絕等值誤判把測試 RPC 路由到 live daemon）。新增 `tests/conftest.py` 三層防線（強制 env 隔離／autouse STATE_PATH patch／live guard gate：state/WAL/config/daemon 四維快照，`SERIALWRAP_LIVE_GATE=warn` 逃生閥）、8 檔 per-file 隔離（unittest runner 防線）、coexist/e2e 隔離 config 維度＋coexist `addCleanup` 根絕 daemon 洩漏。
```

- [ ] **Step 3: README 檢查**

Run: `grep -n "unittest\|pytest" README.md | head`
若 README 有測試跑法段落，同步加同款註記；沒有則跳過（R-18 為 WARN 不擋，但 tests 行為變更值得同步）。

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md changelog.d/120-test-state-isolation.md README.md
git commit -m "docs: #120 測試政策註記 pytest 為準＋changelog fragment"
```

---

### Task 9: 驗收

- [ ] **Step 1: 完整 suite（本機、有 production daemon、無外層 env 前綴）**

Run: `python3 -m pytest -q tests/ 2>&1 | tail -12`
Expected: 無新失敗（pre-existing flaky 除外，失敗者重跑判別）；guard 輸出四維 PASS（daemon 維在本機為 PASS 非 SKIP）。

- [ ] **Step 2: live 資源實證**

Run:
```bash
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path.home() / ".local/state/serialwrap/state.json"
d = json.loads(p.read_text())
bad_alias = [a for a in d["aliases"] if a in ("a0","a1","mybox","t0","test")]
bad_bind = [k for k,v in d["bindings"].items() if "/tmp/sw-" in v]
print("POLLUTED" if (bad_alias or bad_bind) else "LIVE_STATE_CLEAN", bad_alias, bad_bind)
EOF
serialwrap session list | python3 -c "import json,sys; d=json.load(sys.stdin); print([ (s['com'], s['state']) for s in d['sessions'] ])"
```
Expected: `LIVE_STATE_CLEAN`＋兩板仍 READY。

- [ ] **Step 3: policy check（含 PR 參數複現 CI）**

Run:
```bash
python3 -m policy_check --repo . \
  --pr-title "fix(tests): #120 測試污染 live state.json 雙向量根修" \
  --pr-body "Closes #120" \
  --pr-base-ref main --pr-head-ref feature/120-test-state-isolation
```
Expected: PASS（R-09 由 fragment 滿足；R-12 branch=feature/*；R-17 closing keyword；R-18 若 WARN 屬預期——CLAUDE.md/README 已同步）。

- [ ] **Step 4: openspec tasks 勾稽**

`openspec/changes/test-state-isolation-120/tasks.md` 全部勾 `[x]`（對應本 plan Task 1-9），commit：

```bash
git add openspec/changes/test-state-isolation-120/tasks.md
git commit -m "docs(openspec): #120 tasks 完成勾稽"
```
