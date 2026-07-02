"""#120 live guard 判定純函式——每一個 adversarial review F1/F2 失敗模式一個 case。"""
from __future__ import annotations

import json
import types

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


def test_state_non_json_post_fails_even_in_warn():
    """post 非 JSON（截斷/損毀）＝結構性破壞，warn 模式仍 FAIL。"""
    v, _ = liveguard.classify_state(_snap(PRE), _snap(b'{"aliases": {'), mode="warn")
    assert v == "FAIL"


def test_state_structural_key_non_dict_fails_even_in_warn():
    """結構鍵值非 dict（如 released 變 list）＝結構損毀 FAIL，不得 AttributeError。"""
    post = _state(aliases={"dut": {"session_id": "prpl-template:COM0"}},
                  released=["p:COM9"])
    v, _ = liveguard.classify_state(_snap(PRE), _snap(post), mode="warn")
    assert v == "FAIL"


def test_state_preexisting_marker_not_blamed_in_warn():
    """pre 已含污染特徵（baseline 髒）：post 沿用同特徵＋良性新增 → 不因 marker FAIL、warn 模式 WARN。"""
    pre = _state(bindings={"prpl-template:COM0": "/tmp/sw-old/by-id/u"},
                 released={"p:COM9": {"by_id": "x"}})
    post = _state(aliases={"sta": {"session_id": "prpl-template:COM1"}},
                  bindings={"prpl-template:COM0": "/tmp/sw-old/by-id/u"},
                  released={"p:COM9": {"by_id": "x"}})
    v, _ = liveguard.classify_state(_snap(pre), _snap(post), mode="warn")
    assert v == "WARN"


def test_state_binding_value_rewrite_fails_even_in_warn():
    """既有 binding 值被改寫＝結構級（#121 F2：把 live 綁定指去別處比新增更危險），warn 仍 FAIL。"""
    pre = _state(bindings={"prpl-template:COM0": "/dev/serial/by-id/usb-a"},
                 released={"p:COM9": {"by_id": "x"}})
    post = _state(bindings={"prpl-template:COM0": "/dev/serial/by-id/usb-b"},
                  released={"p:COM9": {"by_id": "x"}})
    v, _ = liveguard.classify_state(_snap(pre), _snap(post), mode="warn")
    assert v == "FAIL"


def test_state_alias_value_change_warns_in_warn_mode():
    """alias 值變更維持非結構級（live daemon 合法 churn）→ warn 模式 WARN（#121 F2 邊界）。"""
    pre = _state(aliases={"dut": {"session_id": "prpl-template:COM0"}},
                 released={"p:COM9": {"by_id": "x"}})
    post = _state(aliases={"dut": {"session_id": "prpl-template:COM1"}},
                  released={"p:COM9": {"by_id": "x"}})
    v, _ = liveguard.classify_state(_snap(pre), _snap(post), mode="warn")
    assert v == "WARN"


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


def test_wal_created_passes():
    """刻意盲點釘住：pre 不存在→post 建立＝首次 append（live daemon 常態），不 FAIL。"""
    v, _ = liveguard.classify_wal(
        liveguard.FileSnap(exists=False), liveguard.FileSnap(exists=True, size=10))
    assert v == "PASS"


def test_wal_shrink_warns_in_warn_mode():
    """warn 逃生閥涵蓋 Guard 2：size 縮小（rotation 誤報情境）降 WARN。"""
    v, _ = liveguard.classify_wal(
        liveguard.FileSnap(exists=True, size=200), liveguard.FileSnap(exists=True, size=100),
        mode="warn")
    assert v == "WARN"


def test_wal_deleted_fails_even_in_warn():
    """檔案消失＝結構級（wal reset/清除特徵），warn 閥不管、仍 FAIL。"""
    v, _ = liveguard.classify_wal(
        liveguard.FileSnap(exists=True, size=200), liveguard.FileSnap(exists=False),
        mode="warn")
    assert v == "FAIL"


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
                sessions={"prpl-template:COM0": ("2026-07-02T00:00:00+00:00", 1, "READY")})
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
    post = _dsnap(sessions={"prpl-template:COM0": ("2026-07-02T00:05:00+00:00", 1, "READY")})
    v, _ = liveguard.classify_daemon(_dsnap(), post)
    assert v == "FAIL"


def test_daemon_bridge_generation_change_fails():
    post = _dsnap(sessions={"prpl-template:COM0": ("2026-07-02T00:00:00+00:00", 2, "READY")})
    v, _ = liveguard.classify_daemon(_dsnap(), post)
    assert v == "FAIL"


def test_daemon_state_change_fails():
    """純 detach 型 misroute（不 TX、不 bump gen、state.json 未變）由 state 欄位抓住。"""
    post = _dsnap(sessions={"prpl-template:COM0": ("2026-07-02T00:00:00+00:00", 1, "DETACHED")})
    v, _ = liveguard.classify_daemon(_dsnap(), post)
    assert v == "FAIL"


def test_daemon_untouched_passes():
    v, _ = liveguard.classify_daemon(_dsnap(), _dsnap())
    assert v == "PASS"


def test_daemon_session_disappeared_fails():
    """session 於 post 消失：post.sessions.get 回 (None, None, None) → 經 gen None mismatch FAIL。"""
    pre = _dsnap(sessions={"prpl-template:COM0": (None, 1, "READY")})
    post = _dsnap(sessions={})
    v, _ = liveguard.classify_daemon(pre, post)
    assert v == "FAIL"


def test_daemon_tx_advance_warns_in_warn_mode():
    """warn 逃生閥涵蓋 Guard 4 session 級：tx 前進（開發者測試期間對真板操作）降 WARN。"""
    post = _dsnap(sessions={"prpl-template:COM0": ("2026-07-02T00:05:00+00:00", 1, "READY")})
    v, _ = liveguard.classify_daemon(_dsnap(), post, mode="warn")
    assert v == "WARN"


def test_daemon_state_change_warns_in_warn_mode():
    """warn 下 state 變更（detach/attach 同屬「對真板操作」類）降 WARN。"""
    post = _dsnap(sessions={"prpl-template:COM0": ("2026-07-02T00:00:00+00:00", 1, "DETACHED")})
    v, _ = liveguard.classify_daemon(_dsnap(), post, mode="warn")
    assert v == "WARN"


def test_daemon_pid_change_fails_even_in_warn():
    """MainPID 變更＝結構級（daemon 被 restart），warn 閥不管、仍 FAIL。"""
    v, _ = liveguard.classify_daemon(_dsnap(), _dsnap(main_pid=5678), mode="warn")
    assert v == "FAIL"


def test_daemon_ondemand_untouched_passes():
    """on-demand（無 systemd unit、純 RPC 可達）：active=False/pid=None 是常態，不得誤判 FAIL（#121 F1）。"""
    pre = _dsnap(active=False, main_pid=None)
    post = _dsnap(active=False, main_pid=None)
    v, _ = liveguard.classify_daemon(pre, post)
    assert v == "PASS"


def test_daemon_ondemand_tx_advance_fails():
    """on-demand 情境 session 級判定不可失守：tx 前進仍被抓 FAIL（#121 F1）。"""
    pre = _dsnap(active=False, main_pid=None)
    post = _dsnap(active=False, main_pid=None,
                  sessions={"prpl-template:COM0": ("2026-07-02T00:05:00+00:00", 1, "READY")})
    v, _ = liveguard.classify_daemon(pre, post)
    assert v == "FAIL"


# ---- snap_daemon I/O 薄層 ----

def _fake_systemctl(system_out=None, user_out=None):
    """依 argv 是否含 --user 分派輸出的 subprocess.run 假件；None＝該 scope 不可用（raise）。"""
    def run(argv, **kw):
        out = user_out if "--user" in argv else system_out
        if out is None:
            raise OSError("scope 不可用")
        return types.SimpleNamespace(stdout=out)
    return run


_RPC_OK = {"ok": True, "sessions": [
    {"session_id": "prpl-template:COM0", "last_tx_at": None,
     "bridge_generation": 1, "state": "READY"}]}


def test_snap_daemon_rpc_error_returns_unreachable(monkeypatch):
    """rpc_call 在 socket error/timeout 不丟例外、回 {"ok": False}——必須判 unreachable，
    不得回 reachable+空 sessions（post 瞬時 timeout 會產生假 FAIL、pre 失敗會沉默停擺偵測）。"""
    import sw_core.client

    monkeypatch.setattr(
        liveguard.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(stdout="ActiveState=active\nMainPID=1234\n"))
    monkeypatch.setattr(
        sw_core.client, "rpc_call",
        lambda *a, **k: {"ok": False, "error_code": "TIMEOUT"})
    assert liveguard.snap_daemon().reachable is False


def test_snap_daemon_system_scope(monkeypatch):
    """system scope 命中：active=True、MainPID 取 system unit 的值（#121 F1）。"""
    import sw_core.client

    monkeypatch.setattr(
        liveguard.subprocess, "run",
        _fake_systemctl(system_out="ActiveState=active\nMainPID=4321\n",
                        user_out="ActiveState=inactive\nMainPID=0\n"))
    monkeypatch.setattr(sw_core.client, "rpc_call", lambda *a, **k: dict(_RPC_OK))
    snap = liveguard.snap_daemon()
    assert snap.reachable is True
    assert snap.active is True
    assert snap.main_pid == 4321


def test_snap_daemon_user_scope_fallback(monkeypatch):
    """system scope 未命中 → fallback 到 systemd-user scope（#121 F1）。"""
    import sw_core.client

    monkeypatch.setattr(
        liveguard.subprocess, "run",
        _fake_systemctl(system_out="ActiveState=inactive\nMainPID=0\n",
                        user_out="ActiveState=active\nMainPID=7777\n"))
    monkeypatch.setattr(sw_core.client, "rpc_call", lambda *a, **k: dict(_RPC_OK))
    snap = liveguard.snap_daemon()
    assert snap.reachable is True
    assert snap.active is True
    assert snap.main_pid == 7777


def test_snap_daemon_ondemand_rpc_only(monkeypatch):
    """systemd 兩 scope 皆不可用、RPC 可達 → reachable=True/active=False/pid=None（on-demand，#121 F1）。"""
    import sw_core.client

    monkeypatch.setattr(liveguard.subprocess, "run", _fake_systemctl())  # 兩 scope 都 raise
    monkeypatch.setattr(sw_core.client, "rpc_call", lambda *a, **k: dict(_RPC_OK))
    snap = liveguard.snap_daemon()
    assert snap.reachable is True
    assert snap.active is False
    assert snap.main_pid is None
    assert "prpl-template:COM0" in snap.sessions
